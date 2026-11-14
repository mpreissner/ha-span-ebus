"""Fill gaps in the local integrated energy rows from SPAN's cloud history.

A local site, solar or battery total counts nothing while it isn't integrating:
Home Assistant was down, or local and cloud were both down. The entities
report each gap here; once the hours have settled this asks the cloud what was
metered then and adds it to the entity's statistics, at most once.

Every decision lives in `span_client.backfill`; every recorder call goes
through `recorder_api`. The cloud is asked with the entry's token file, the same
way its fallback coordinators ask, and never while the fallback holds it. Logs
carry counts, attempt numbers and status names only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_point_in_utc_time
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import recorder_api
from .const import CONF_TOKENS, DOMAIN
from .span_client import backfill as bf
from .span_client import cloud_auth, cloud_grpc, cloud_history
from .span_client.link import HistoryTarget
from .span_client.local.health import Transport

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .energy_step import EnergyStepTracker
    from .transport import SpanTransportManager

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
SAVE_DELAY_S = 10
# After Home Assistant has started, so the recorder has caught up.
STARTUP_DELAY_S = 120
VERIFY_POLLS = 30
VERIFY_EVERY_S = 1.0
# How long to look again when a fetch had to wait for the fallback, or a fetched
# gap couldn't be written yet.
RETRY_SOON = timedelta(minutes=15)
# An unload waits this long for a fetch in flight, so a refreshed token is not
# written after the entry's token file has been discarded.
FETCH_WAIT_S = 60.0

TokenWriter = Callable[[Path, dict], None]


def store(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(hass, STORE_VERSION, f"{DOMAIN}.{entry_id}.backfill")


async def async_remove(hass: HomeAssistant, entry_id: str) -> None:
    """Drop the record (entry removed)."""
    await store(hass, entry_id).async_remove()


def _fetch(
    token_path: Path,
    tokens: dict | None,
    write_tokens: TokenWriter,
    target: HistoryTarget,
    resolution: cloud_history.Resolution,
    start_ms: int,
    end_ms: int,
    time_zone: str,
) -> cloud_history.Series | None:
    """Executor thread: the site's history over the window, or None if absent.

    The token file is written from the entry only when it is missing, as the
    fallback would write it; a refresh is saved to that file and nowhere else.
    """
    if not token_path.exists():
        if tokens is None:
            raise cloud_auth.CloudCredentialsRejected("no cloud credentials")
        write_tokens(token_path, tokens)
    access_token = cloud_auth.access_token_from_store(token_path)
    request = cloud_history.build_request(
        {target.resource: [target.instance]},
        start_ms=start_ms,
        end_ms=end_ms,
        resolution=resolution,
        time_zone=time_zone,
    )
    with cloud_grpc.CloudGrpcClient(access_token) as client:
        raw = client.call(cloud_history.METHOD, request)
    for series in cloud_history.parse_response(raw):
        if series.resource_id == target.resource and series.instance_id == target.instance:
            return series
    return None


class GapBackfill:
    """One local-first entry's gap record, from the gap to its written hours."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        transport: SpanTransportManager,
        energy_step: EnergyStepTracker | None,
        *,
        token_path: Path,
        write_tokens: TokenWriter,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._transport = transport
        self._energy_step = energy_step
        self._token_path = token_path
        self._write_tokens = write_tokens
        self._store = store(hass, entry.entry_id)
        self._record: dict[str, Any] = bf.new_record()
        self._loaded = False
        self._lock = asyncio.Lock()
        self._unsubs: list[CALLBACK_TYPE] = []
        self._cancel_timer: CALLBACK_TYPE | None = None
        self._cancel_startup: CALLBACK_TYPE | None = None
        self._task: asyncio.Task[None] | None = None
        self._fetching: asyncio.Future[Any] | None = None
        self._again = False
        self._started = False  # the startup kick has come
        self._reverified = False
        self._stopped = False
        self._shown = transport.shown

    # --- lifecycle -------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Load the record and wait for the startup kick; gaps can be recorded now."""
        raw = await self._store.async_load()
        if isinstance(raw, dict) and isinstance(raw.get("gaps"), list):
            self._record = raw
        self._loaded = True
        if bf.tidy(self._record, dt_util.utcnow()):
            self._save_later()
        if bf.gaps(self._record):
            _LOGGER.debug("backfill: loaded, %s", bf.counts(self._record))
        self._unsubs.append(self._transport.async_add_listener(self._on_transport))
        self._unsubs.append(async_at_started(self.hass, self._on_started))

    @callback
    def _on_started(self, _hass: HomeAssistant) -> None:
        if not self._stopped:
            self._cancel_startup = async_call_later(
                self.hass, STARTUP_DELAY_S, self._on_startup_delay
            )

    @callback
    def _on_startup_delay(self, _now: datetime) -> None:
        self._cancel_startup = None
        self._started = True
        self.kick("startup")

    async def async_stop(self) -> None:
        """Stop timers and any run; let a fetch in flight finish; save the record."""
        if self._stopped:
            return
        self._stopped = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for cancel in (self._cancel_timer, self._cancel_startup):
            if cancel is not None:
                cancel()
        self._cancel_timer = self._cancel_startup = None
        # Taken first: a cancelled run lets go of it on the way out.
        fetching, self._fetching = self._fetching, None
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if fetching is not None and not fetching.done():
            await asyncio.wait({fetching}, timeout=FETCH_WAIT_S)
        if self._loaded:
            await self._store.async_save(self._record)

    # --- what the entities and the transport report ------------------------------------

    @callback
    def record_gap(
        self, target: str, row: str, start: datetime, end: datetime, *, draw: bool
    ) -> None:
        """An integrated entity missed `[start, end)` (UTC)."""
        if self._stopped or not self._loaded:
            return
        kept = bf.add_gap(
            self._record, row, start, end, now=dt_util.utcnow(), draw=draw, target=target
        )
        if not kept:
            return
        _LOGGER.debug("backfill: gap recorded, %s", bf.counts(self._record))
        self._save_later()
        self.kick("gap")

    @callback
    def _on_transport(self) -> None:
        shown, was = self._transport.shown, self._shown
        self._shown = shown
        if shown is Transport.LOCAL and was is not Transport.LOCAL:
            self.kick("failback" if was is Transport.CLOUD else "reconnect")

    @callback
    def kick(self, reason: str) -> None:
        """Look at the record now: fetch what is due, write what is ready."""
        if self._stopped or not self._started:
            return
        _LOGGER.debug("backfill: kick (%s)", reason)
        if self._task is not None and not self._task.done():
            self._again = True
            return
        self._task = self.hass.async_create_background_task(
            self._drive(), f"{DOMAIN} backfill {self.entry.entry_id}"
        )

    @callback
    def _on_timer(self, _now: datetime) -> None:
        self._cancel_timer = None
        self.kick("timer")

    async def _drive(self) -> None:
        while not self._stopped:
            self._again = False
            try:
                await self._run()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 — retried on the next kick
                _LOGGER.warning("backfill: run failed (%s)", type(err).__name__)
            if not self._again:
                return

    # --- a run -----------------------------------------------------------------------------

    async def _run(self) -> None:
        async with self._lock:
            now = dt_util.utcnow()
            bf.tidy(self._record, now)
            if not self._reverified:
                self._reverified = await self._reverify(now)
            plan = bf.plan(
                self._record, now=now, cloud_shown=self._transport.shown is Transport.CLOUD
            )
            deferred = False
            if plan.fetch:
                if self._transport.cloud_in_use:
                    # One token user at a time; not an attempt.
                    _LOGGER.debug("backfill: %d gap(s) wait for the fallback", len(plan.fetch))
                    deferred = True
                else:
                    await self._fetch_gaps(plan.fetch, now)
            await self._write_ready(now)
            await self._store.async_save(self._record)
            self._schedule(deferred or not self._reverified)

    def _schedule(self, deferred: bool) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        if self._stopped:
            return
        now = dt_util.utcnow()
        plan = bf.plan(self._record, now=now, cloud_shown=self._transport.shown is Transport.CLOUD)
        wake = [plan.wake_at] if plan.wake_at is not None else []
        if deferred or bf.with_status(self._record, bf.READY):
            wake.append(now + RETRY_SOON)
        if wake:
            self._cancel_timer = async_track_point_in_utc_time(
                self.hass, self._on_timer, max(min(wake), now + timedelta(seconds=1))
            )

    # --- fetching ----------------------------------------------------------------------------

    async def _fetch_gaps(self, ids: tuple[str, ...], now: datetime) -> None:
        """One request covering every due gap; each gap takes what it needs from it."""
        target = self._transport.link.site_history if self._transport.link else None
        due = [g for g in bf.with_status(self._record, bf.PENDING) if g.get("id") in ids]
        spans = [bounds for g in due if (bounds := bf.span(g)) is not None]
        if target is None or not spans:
            return
        resolution, start_ms, end_ms = bf.window(spans)
        self._record["last_request"] = bf.iso(now)
        self._fetching = self.hass.async_add_executor_job(
            _fetch,
            self._token_path,
            self.entry.data.get(CONF_TOKENS),
            self._write_tokens,
            target,
            resolution,
            start_ms,
            end_ms,
            self.hass.config.time_zone or "UTC",
        )
        try:
            # Shielded: an unload waits for the thread rather than abandoning it.
            series = await asyncio.shield(self._fetching)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 — a failed attempt, retried later
            for gap in due:
                bf.fail(gap, now, bf.FETCH_FAILED)
            _LOGGER.debug(
                "backfill: fetch failed (%s) for %d gap(s), attempt %s",
                type(err).__name__,
                len(due),
                ",".join(str(g.get("attempts")) for g in due),
            )
            return
        finally:
            self._fetching = None
        buckets = series.buckets if series is not None else ()
        live = {id(g) for g in bf.gaps(self._record)}
        ready = 0
        for gap in due:
            # A gap merged into a newer one while the request was out is gone.
            if id(gap) in live and bf.fetched(gap, buckets, now=now):
                ready += 1
        _LOGGER.debug(
            "backfill: fetched %d bucket(s) for %d gap(s), %d ready, %s",
            len(buckets),
            len(due),
            ready,
            bf.counts(self._record),
        )

    # --- writing -------------------------------------------------------------------------------

    def _entity_id(self, gap: dict[str, Any]) -> str | None:
        target = bf.target_of(gap)
        if not target:
            return None
        return er.async_get(self.hass).async_get_entity_id("sensor", DOMAIN, target)

    async def _writable(self, gap: dict[str, Any], now: datetime) -> str | None:
        """The statistic to write a ready gap to, or None (failed, or later)."""
        if not recorder_api.loaded(self.hass):
            return None  # later; not an attempt
        eid = self._entity_id(gap)
        if (
            eid is None
            or not recorder_api.is_recorded(self.hass, eid)
            or not await recorder_api.has_sum(self.hass, eid)
        ):
            bf.give_up(gap, now, bf.NO_STATISTICS)
            return None
        if self._energy_step is not None and self._energy_step.tracks(eid):
            return None  # its energy-step fix comes first; not an attempt
        return eid

    async def _rows(self, gap: dict[str, Any], eid: str) -> list[tuple[datetime, float]]:
        window = bf.read_window(gap)
        if window is None:
            return []
        return await recorder_api.sums(self.hass, eid, *window, "hour")

    async def _write_ready(self, now: datetime) -> None:
        """Queue ready gaps' hours, once, and prove they took effect.

        At most one gap per statistic per batch; the rest wait for the next
        run. Each gap's rows are read before anything is queued, and the record
        is saved with them as queued before the first adjustment: a queued gap
        is never queued again (the next setup settles it from the database).
        """
        batch: list[tuple[dict[str, Any], str]] = []
        for gap in bf.ready_to_write(self._record):
            eid = await self._writable(gap, now)
            if eid is None:
                continue
            sums = bf.snapshot(gap, await self._rows(gap, eid))
            if sums is None:
                bf.fail(gap, now, bf.NO_STATISTICS)
                continue
            batch.append((gap, eid))
            bf.queue(gap, sums)
        if not batch:
            return
        await self._store.async_save(self._record)
        self._queue_adjustments(batch)
        await self._verify(batch, now)

    @callback
    def _queue_adjustments(self, batch: list[tuple[dict[str, Any], str]]) -> None:
        for gap, eid in batch:
            for hour, kwh in bf.hours_of(gap):
                recorder_api.adjust(self.hass, eid, hour, kwh)

    async def _verify(self, batch: list[tuple[dict[str, Any], str]], now: datetime) -> None:
        pending = list(batch)
        verdicts: dict[int, bf.Verdict] = {}
        for _ in range(VERIFY_POLLS):
            if not pending:
                break
            await asyncio.sleep(VERIFY_EVERY_S)
            for item in list(pending):
                gap, eid = item
                verdict = bf.classify(gap, await self._rows(gap, eid))
                verdicts[id(gap)] = verdict
                if verdict.applied == verdict.total:
                    bf.settle_queued(gap, verdict, now=dt_util.utcnow(), at_setup=False)
                    pending.remove(item)
        for gap, _eid in pending:
            verdict = verdicts.get(id(gap))
            if verdict is not None:
                # Another pattern: someone else changed it. A partial prefix
                # stays queued for the next setup.
                bf.settle_queued(gap, verdict, now=dt_util.utcnow(), at_setup=False)
        _LOGGER.debug("backfill: wrote %d gap(s), %s", len(batch), bf.counts(self._record))

    async def _reverify(self, now: datetime) -> bool:
        """Settle gaps a previous run queued, from what the database shows now.

        Returns whether that's done; False while the recorder isn't loaded
        (later; ready gaps on those statistics wait too). On a reload the
        previous instance's adjustments may still be in the recorder's queue,
        so it's drained first: classifying before they land would hand a
        partial prefix back to ready and count it twice.
        """
        queued = bf.with_status(self._record, bf.QUEUED)
        if not queued:
            return True
        if not recorder_api.loaded(self.hass):
            return False
        await recorder_api.async_block_till_done(self.hass)
        now = dt_util.utcnow()
        for gap in queued:
            eid = self._entity_id(gap)
            if eid is None:
                bf.give_up(gap, now, bf.NO_STATISTICS)
                continue
            bf.settle_queued(
                gap, bf.classify(gap, await self._rows(gap, eid)), now=now, at_setup=True
            )
        _LOGGER.debug("backfill: re-verified %d gap(s), %s", len(queued), bf.counts(self._record))
        return True

    def _save_later(self) -> None:
        self._store.async_delay_save(lambda: self._record, SAVE_DELAY_S)
