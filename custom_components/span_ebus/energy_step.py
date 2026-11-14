"""Fix the one-time jump in energy history when a cloud entry gets its panel.

Linking a local panel to a cloud entry re-points each register energy entity
(circuit energy, and site grid energy on a single-panel site) from the cloud's
ledger onto a panel register that counts from install. The recorder's sum takes
one false step there. This tracker watches for each entity's first local
reading, waits until the recorder has compiled the 5-minute bucket that holds
the step, and then raises a fixable issue whose repair takes the step back out.

The arithmetic and the record's states live in `span_client.energy_step`; every
recorder call goes through `recorder_api`. Logs carry counts and status names
only: entity ids are statistic ids, and the hour is the user's.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from . import recorder_api
from .const import DOMAIN
from .span_client import energy_step as es

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
ISSUE_KEY = "energy_step"
CHECK_EVERY = timedelta(minutes=5)
VERIFY_POLLS = 30
VERIFY_EVERY_S = 1.0

# Plain reasons are kept as selector options: the issues schema has no room for them.
REASON_SELECTOR = "energy_step_reason"
NOTHING = "nothing"


def store(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(hass, STORE_VERSION, f"{DOMAIN}.{entry_id}.energy_step")


def issue_id(entry_id: str) -> str:
    return f"{ISSUE_KEY}_{entry_id}"


async def async_remove(hass: HomeAssistant, entry_id: str) -> None:
    """Drop the record and its issue (entry removed)."""
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry_id))
    await store(hass, entry_id).async_remove()


class EnergyStepTracker:
    """One entry's energy-step record, from first local reading to the fix."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store = store(hass, entry.entry_id)
        self._record: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self._unsub_state: CALLBACK_TYPE | None = None
        self._unsub_check: CALLBACK_TYPE | None = None
        self._unsub_issue: CALLBACK_TYPE | None = None

    @property
    def active(self) -> bool:
        return self._record is not None

    # --- lifecycle -------------------------------------------------------------------

    async def async_start(self) -> None:
        """Load the record if there is one; subscribe before the entities appear."""
        raw = await self._store.async_load()
        if not isinstance(raw, dict) or not es.entities(raw):
            return
        self._record = raw
        if self._dismissed():
            await self._drop("dismissed")
            return
        for eid in es.with_status(raw, es.QUEUED):
            entity = es.entities(raw)[eid]
            es.reverify(entity, await self._sum_b(eid, entity))
        await self._save()
        self._subscribe()
        await self._check()

    @callback
    def async_stop(self) -> None:
        self._unsubscribe_state()
        self._stop_checks()
        self._unwatch_issue()

    def _subscribe(self) -> None:
        self._unsubscribe_state()
        first = es.with_status(self._record or {}, es.WAITING_FIRST)
        if first:
            self._unsub_state = async_track_state_change_event(self.hass, first, self._on_state)

    def _unsubscribe_state(self) -> None:
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None

    def _start_checks(self) -> None:
        if self._unsub_check is None:
            self._unsub_check = async_track_time_interval(self.hass, self._on_interval, CHECK_EVERY)

    def _stop_checks(self) -> None:
        if self._unsub_check is not None:
            self._unsub_check()
            self._unsub_check = None

    async def _on_interval(self, _now: datetime) -> None:
        await self._check()

    # --- first local reading -------------------------------------------------------

    @callback
    def _on_state(self, event: Event) -> None:
        record = self._record
        state = event.data.get("new_state")
        if record is None or state is None:
            return
        entity = es.entities(record).get(state.entity_id)
        if entity is None or entity.get("status") != es.WAITING_FIRST:
            return
        value = es.to_kwh(state.state, state.attributes.get("unit_of_measurement"))
        if value is None:
            return  # unavailable or unknown in between; the compiler skips them too
        # The restored cloud total keeps the entity waiting (see `es.first_local`).
        if not es.first_local(entity, value, dt_util.as_utc(state.last_updated)):
            return
        if not es.with_status(record, es.WAITING_FIRST):
            self._unsubscribe_state()
        self.hass.async_create_task(self._save(), "span_ebus energy step save")

    # --- gate ----------------------------------------------------------------------------

    async def _check(self) -> None:
        """Move waiting entities on, time them out, and raise or drop the issue."""
        async with self._lock:
            record = self._record
            if record is None:
                return
            if self._dismissed():
                await self._drop("dismissed")
                return
            for eid in es.with_status(record, es.WAITING_STATS):
                await self._gate(eid, es.entities(record)[eid])
            if es.timed_out(record, dt_util.utcnow()):
                if timed := es.time_out(record):
                    _LOGGER.debug("energy step: %d entities timed out", timed)
                self._unsubscribe_state()
            await self._save()
            await self._settle()

    async def _gate(self, eid: str, entity: dict[str, Any]) -> None:
        if not recorder_api.loaded(self.hass):
            es.skip(entity, es.RECORDER_NOT_LOADED)
            return
        if not recorder_api.is_recorded(self.hass, eid):
            es.skip(entity, es.EXCLUDED_FROM_RECORDER)
            return
        if not await recorder_api.has_sum(self.hass, eid):
            es.skip(entity, es.NO_STATISTICS)
            return
        window = es.stats_window(entity)
        if window is None:
            return
        rows = await recorder_api.sums(self.hass, eid, *window, "5minute")
        es.gate(entity, rows)

    async def _settle(self) -> None:
        record = self._record
        if record is None:
            return
        action = es.issue_action(record)
        if action == es.ISSUE_WAIT:
            if es.with_status(record, *es.WAITING):
                self._start_checks()
            else:
                self._stop_checks()
            return
        self._stop_checks()
        if action == es.ISSUE_DROP:
            await self._drop("nothing to fix")
            return
        self._raise()
        if not record.get("issue_raised"):
            record["issue_raised"] = True
            await self._save()

    def _watch_issue(self) -> None:
        """While the issue is up, drop the record as soon as the user ignores it."""
        if self._unsub_issue is None:
            self._unsub_issue = self.hass.bus.async_listen(
                ir.EVENT_REPAIRS_ISSUE_REGISTRY_UPDATED, self._on_issue, self._is_mine
            )

    def _unwatch_issue(self) -> None:
        if self._unsub_issue is not None:
            self._unsub_issue()
            self._unsub_issue = None

    @callback
    def _is_mine(self, data: Mapping[str, Any]) -> bool:
        return (
            data.get("action") == "update"
            and data.get("domain") == DOMAIN
            and data.get("issue_id") == issue_id(self.entry.entry_id)
        )

    @callback
    def _on_issue(self, _event: Event) -> None:
        if self._record is not None and self._dismissed():
            self.hass.async_create_task(self._check(), "span_ebus energy step dismissed")

    def _raise(self) -> None:
        self._watch_issue()
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id(self.entry.entry_id),
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_KEY,
            translation_placeholders={"title": self.entry.title},
            data={"entry_id": self.entry.entry_id},
        )

    def _dismissed(self) -> bool:
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id(self.entry.entry_id))
        return issue is not None and issue.dismissed_version is not None

    async def _drop(self, why: str) -> None:
        """Forget the record: done, nothing to do, or the user chose Ignore."""
        if self._record is not None:
            _LOGGER.debug("energy step: record dropped (%s), %s", why, es.counts(self._record))
        self._record = None
        self.async_stop()
        ir.async_delete_issue(self.hass, DOMAIN, issue_id(self.entry.entry_id))
        await self._store.async_remove()

    async def _save(self) -> None:
        if self._record is not None:
            await self._store.async_save(self._record)

    # --- the fix -------------------------------------------------------------------------

    async def _sum_b(self, eid: str, entity: Mapping[str, Any]) -> float | None:
        start = es.parse_time(entity.get("start"))
        if start is None or not recorder_api.loaded(self.hass):
            return None
        rows = await recorder_api.sums(self.hass, eid, start, start + es.BUCKET, "5minute")
        return es.sum_at(rows, start)

    async def async_apply(self) -> None:
        """Take each ready entity's step out of its history, at most once.

        An adjust is queued only after a read shows the bucket untouched, and
        the entity is saved as queued before the call; a queued one is never
        queued again (the next setup settles it from the database).
        """
        async with self._lock:
            record = self._record
            if record is None:
                return
            pending: list[str] = []
            for eid in es.with_status(record, es.READY):
                entity = es.entities(record)[eid]
                start = es.parse_time(entity.get("start"))
                if start is None:  # a ready entity always has one
                    es.skip(entity, es.CHANGED_SINCE)
                    continue
                match es.read_sum(entity, await self._sum_b(eid, entity)):
                    case es.NOT_APPLIED:
                        entity["status"] = es.QUEUED
                        await self._save()
                        recorder_api.adjust(self.hass, eid, start, -float(entity["step"]))
                        pending.append(eid)
                    case es.ALREADY_APPLIED:
                        entity["status"] = es.APPLIED
                    case _:
                        es.skip(entity, es.CHANGED_SINCE)
            await self._save()
            for _ in range(VERIFY_POLLS):
                if not pending:
                    break
                await asyncio.sleep(VERIFY_EVERY_S)
                for eid in list(pending):
                    entity = es.entities(record)[eid]
                    if es.read_sum(entity, await self._sum_b(eid, entity)) == es.ALREADY_APPLIED:
                        entity["status"] = es.APPLIED
                        pending.remove(eid)
            await self._save()
            _LOGGER.debug("energy step: applied, %s", es.counts(record))

    async def async_finish(self) -> None:
        """The repair flow is done: forget the record, or keep it for queued ones."""
        async with self._lock:
            record = self._record
            if record is None:
                return
            if es.all_terminal(record):
                await self._drop("finished")
                return
            record["reported"] = True
            await self._save()
            ir.async_delete_issue(self.hass, DOMAIN, issue_id(self.entry.entry_id))

    # --- what the repair flow shows ----------------------------------------------------

    async def _reasons(self) -> dict[str, str]:
        prefix = f"component.{DOMAIN}.selector.{REASON_SELECTOR}.options."
        strings = await async_get_translations(
            self.hass, self.hass.config.language, "selector", {DOMAIN}
        )
        return {key[len(prefix) :]: text for key, text in strings.items() if key.startswith(prefix)}

    def _lines(self, statuses: tuple[str, ...], text: Any) -> list[str]:
        record = self._record or {}
        rows = es.entities(record)
        return [f"- {eid}: {text(rows[eid])}" for eid in es.with_status(record, *statuses)]

    async def _skipped(self) -> tuple[int, str]:
        reasons = await self._reasons()
        lines = self._lines(
            (es.SKIPPED,), lambda e: reasons.get(str(e.get("reason")), str(e.get("reason")))
        )
        return len(lines), "\n".join(lines) or reasons.get(NOTHING, NOTHING)

    def _hour(self) -> str:
        record = self._record or {}
        starts = [
            when
            for eid in es.with_status(record, es.READY)
            if (when := es.parse_time(es.entities(record)[eid].get("start")))
        ]
        if not starts:
            return ""
        return dt_util.as_local(min(starts)).strftime("%Y-%m-%d %H:%M")

    async def confirm_placeholders(self) -> dict[str, str]:
        _count, skipped = await self._skipped()
        lines = self._lines((es.READY,), lambda e: f"{float(e['step']):.3f} kWh")
        return {
            "title": self.entry.title,
            "entities": "\n".join(lines) or (await self._reasons()).get(NOTHING, NOTHING),
            "skipped": skipped,
            "hour": self._hour(),
        }

    async def result_placeholders(self) -> dict[str, str]:
        count, skipped = await self._skipped()
        return {
            "title": self.entry.title,
            "applied": str(len(es.with_status(self._record or {}, es.APPLIED))),
            "skipped_count": str(count),
            "skipped": skipped,
        }
