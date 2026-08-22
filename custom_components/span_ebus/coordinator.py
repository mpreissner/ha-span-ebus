"""Coordinator: runs the vendored cloud backend and pushes frames into HA.

The backend streams telemetry on its own daemon thread and invokes our
callbacks from there. Home Assistant is single-threaded asyncio, so every
callback is marshaled onto the event loop with `call_soon_threadsafe`. Readings
arrive at ~1-2 frames/sec with ~90 readings per frame, so we buffer them and
coalesce into a single `async_set_updated_data` per loop iteration.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .energy import EnergyLedger
from .span_client.backend import CloudBackend
from .span_client.models import EnergySample, PanelSchema, PropertySpec, Reading

_LOGGER = logging.getLogger(__name__)

# If the channel is silent this long after start, say so. Entities only exist
# once a frame describes them, so a quiet stream otherwise looks like a broken
# integration with an empty log.
SCHEMA_GRACE_SECONDS = 60

# A push integration has no natural notion of "stale": an entity keeps its last
# value until something says otherwise, so a dead stream looks exactly like a
# panel at constant load — and the recorder writes that flat line into history as
# though it were measured. Past this much silence the entities stop claiming
# their value is current and go unavailable, which is the honest reading and
# leaves a visible gap instead of a plausible lie.
#
# Comfortably longer than the backend's own FRAME_SILENCE_SECONDS watchdog plus a
# reconnect, so an ordinary reattach does not blink every entity in the panel.
STALE_AFTER_SECONDS = 180.0
STALE_CHECK_INTERVAL = timedelta(seconds=30)

# Energy is not pushed — it is read back from SPAN's own meters on a timer (see
# `span_client.cloud_history`). A minute is fast enough that the Energy
# dashboard moves while you watch it, and one poll costs ~18 kB against the
# megabytes the realtime stream carries in the same minute.
ENERGY_POLL_INTERVAL = timedelta(seconds=60)

# The ledger is the entities' memory, so it has to survive a restart; it is also
# rewritten every poll, so it is not written straight through to disk.
ENERGY_STORE_VERSION = 1
ENERGY_SAVE_DELAY_SECONDS = 30


def energy_store(hass: HomeAssistant, entry_id: str) -> Store:
    """The per-entry store holding the energy totals."""
    return Store(hass, ENERGY_STORE_VERSION, f"{DOMAIN}.{entry_id}.energy")


@dataclass
class _Platform:
    """One platform's claim on the schema: which specs it wants, and what it has.

    Each platform keeps its own `seen` set because they partition the schema —
    the sensor platform must not build an entity for the relay property, and the
    switch platform must not build one for power.
    """

    adder: Callable[[list[PropertySpec]], None]
    wants: Callable[[PropertySpec], bool]
    seen: set[str] = field(default_factory=set)


class SpanCloudCoordinator(DataUpdateCoordinator[dict[str, Reading]]):
    """Owns the streaming backend and the latest reading per property key."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        token_path: Path,
        device_uuid: str,
        user_id: str | None,
        serial: str | None,
    ) -> None:
        # No update_interval: this is push, not poll.
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=None, config_entry=entry)
        self.entry = entry
        self.schema: PanelSchema | None = None

        self._backend = CloudBackend(
            token_path,
            device_uuid,
            user_id=user_id,
            serial=serial,
            on_auth_failed=self._on_auth_failed,
        )
        self._buffer: dict[str, Reading] = {}
        self._buffer_lock = threading.Lock()
        self._flush_scheduled = False
        self._platforms: list[_Platform] = []
        self._cancel_grace: Callable[[], None] | None = None
        self._cancel_stale_check: Callable[[], None] | None = None
        # Monotonic time of the last batch of readings, and the liveness we last
        # told the entities about, so a transition is logged and pushed once
        # rather than on every check.
        self._last_frame: float | None = None
        self._reported_live = True

        # Measured energy: a running total per property key, its store, and the
        # entities watching it. Energy has its own listener list because it moves
        # once a minute while readings move twice a second — waking every power
        # sensor in the panel to announce a kilowatt-hour would be pure noise.
        self._ledger = EnergyLedger()
        self._energy_store = energy_store(hass, entry.entry_id)
        self._energy_listeners: list[Callable[[], None]] = []
        self._cancel_energy_poll: Callable[[], None] | None = None
        self._energy_failures = 0

    # --- lifecycle ---------------------------------------------------------

    async def async_start(self) -> None:
        """Start the backend thread. Non-blocking; frames arrive shortly after."""
        self.data = {}
        self._ledger.load(await self._energy_store.async_load())
        self._backend.start(self._on_schema, self._on_reading)
        self._cancel_grace = async_call_later(self.hass, SCHEMA_GRACE_SECONDS, self._warn_if_silent)
        self._cancel_stale_check = async_track_time_interval(
            self.hass, self._check_staleness, STALE_CHECK_INTERVAL
        )
        # The first poll only marks where the meters stand; it is the second one
        # that adds anything, which is why nothing here polls immediately.
        self._cancel_energy_poll = async_track_time_interval(
            self.hass, self._poll_energy, ENERGY_POLL_INTERVAL
        )

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        if self._cancel_grace is not None:
            self._cancel_grace()
            self._cancel_grace = None
        if self._cancel_stale_check is not None:
            self._cancel_stale_check()
            self._cancel_stale_check = None
        if self._cancel_energy_poll is not None:
            self._cancel_energy_poll()
            self._cancel_energy_poll = None
        # Force the delayed save out: unloading an entry is not a Home Assistant
        # stop, so nothing else will flush it, and a lost write means the totals
        # restart from an old mark and re-count what came after it.
        await self._energy_store.async_save(self._ledger.as_dict())
        await self.hass.async_add_executor_job(self._backend.stop)

    # --- authentication ----------------------------------------------------

    def _on_auth_failed(self, reason: str) -> None:
        """Backend thread: the stored credentials are dead. Ask for new ones."""
        self.hass.loop.call_soon_threadsafe(self._start_reauth, reason)

    @callback
    def _start_reauth(self, reason: str) -> None:
        """Raise Home Assistant's "Reconfigure" prompt for this entry.

        The backend keeps retrying in the background, so without this a revoked
        refresh token is an integration that quietly never works again: the log
        fills with auth errors and the UI shows nothing amiss. `async_start_reauth`
        is a no-op when a flow is already open for the entry, so the latch in the
        backend and this together mean exactly one prompt.
        """
        _LOGGER.debug("starting reauth flow for %s: %s", self.entry.title, reason)
        self.entry.async_start_reauth(self.hass)

    # --- liveness ----------------------------------------------------------

    @property
    def stream_is_live(self) -> bool:
        """False once the stream has been silent long enough to distrust the data.

        Entities consult this in `available`, so a stalled stream shows as
        unavailable rather than as fresh-looking numbers that stopped moving.
        """
        if self._last_frame is None:
            return False
        return (time.monotonic() - self._last_frame) < STALE_AFTER_SECONDS

    @callback
    def _check_staleness(self, _now) -> None:
        """Push a liveness change out to the entities.

        Only a transition does anything: `available` is a pull, but nothing polls
        it, so entities keep advertising the old answer until something writes
        state. Readings arriving take the other edge (see `_flush`).
        """
        if self.stream_is_live or not self._reported_live:
            return
        self._reported_live = False
        _LOGGER.warning(
            "No SPAN telemetry for %.0fs; marking entities unavailable rather than "
            "leaving them on their last values. The backend reattaches on its own — "
            "look for a cloud stream error above if this persists.",
            STALE_AFTER_SECONDS,
        )
        self.async_update_listeners()

    @callback
    def _warn_if_silent(self, _now) -> None:
        self._cancel_grace = None
        if self.schema is not None:
            return
        _LOGGER.warning(
            "No SPAN telemetry after %ds, so no entities have been created. "
            "Entities are built from the first frame SPAN publishes, and SPAN only "
            "publishes once our SubscribeAndGetTraits registration is accepted — "
            "look for a subscribe error above. Reloading the integration retries "
            "the whole handshake.",
            SCHEMA_GRACE_SECONDS,
        )

    # --- entity registration ----------------------------------------------

    @callback
    def register_entity_adder(
        self,
        adder: Callable[[list[PropertySpec]], None],
        wants: Callable[[PropertySpec], bool],
    ) -> None:
        """Let a platform create entities for the specs it claims, as they arrive.

        Platforms register before the first frame, so `wants` is also replayed
        against a schema that has already landed (a reload, or a second platform
        setting up late).
        """
        platform = _Platform(adder=adder, wants=wants)
        self._platforms.append(platform)
        if self.schema is not None:
            self._emit_to(platform, self.schema)

    # --- measured energy ---------------------------------------------------

    def energy_total(self, key: str) -> float | None:
        """The running kWh for a power property, or None if never metered."""
        return self._ledger.total(key)

    @callback
    def async_add_energy_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to energy updates. Returns the unsubscribe callable."""
        self._energy_listeners.append(listener)

        @callback
        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._energy_listeners.remove(listener)

        return unsubscribe

    @callback
    def adopt_energy_total(self, key: str, total_kwh: float) -> None:
        """Carry a pre-measurement total forward, if the ledger has none.

        Versions before 0.1.13 integrated energy from the power stream, and that
        total lives in the entity's restored state rather than in the ledger.
        Adopting it means the upgrade is invisible on the dashboard; ignoring it
        would restart every meter at zero, and replacing it with SPAN's lifetime
        figure would book the difference as one hour's consumption.
        """
        self._ledger.adopt(key, total_kwh)

    async def _poll_energy(self, _now) -> None:
        """Read the meters and advance the totals.

        A failure here is a cloud hiccup, not a reason to disturb the entities:
        they keep their last totals, which are still true — a cumulative figure
        that has stopped advancing is not a lie the way a stale power reading is
        — and the next poll picks the window back up from the same cursor.
        """
        try:
            samples = await self.hass.async_add_executor_job(
                self._fetch_energy, self._ledger.covered_through_ms
            )
        except Exception as err:  # noqa: BLE001 — one bad poll must not stop the timer
            self._energy_failures += 1
            # Once loudly, then quietly: a cloud outage should not fill the log
            # with a line a minute.
            log = _LOGGER.warning if self._energy_failures == 1 else _LOGGER.debug
            log("could not read SPAN energy (attempt %d): %s", self._energy_failures, err)
            return

        if self._energy_failures:
            _LOGGER.info("SPAN energy readings resumed after %d failures", self._energy_failures)
            self._energy_failures = 0
        if not samples or not self._ledger.apply(samples):
            return

        self._energy_store.async_delay_save(self._ledger.as_dict, ENERGY_SAVE_DELAY_SECONDS)
        for listener in list(self._energy_listeners):
            listener()

    def _fetch_energy(self, covered_through_ms: int) -> list[EnergySample]:
        """Executor thread: the blocking half of `_poll_energy`."""
        return self._backend.fetch_energy(
            covered_through_ms, time_zone=self.hass.config.time_zone or "UTC"
        )

    # --- commands ----------------------------------------------------------

    async def async_send_command(self, key: str, value: str) -> None:
        """Write one settable property, e.g. `circuit-42/relay` -> `OPEN`.

        The backend call is blocking gRPC, so it runs in an executor. It also
        emits the new state through the normal reading callback, but that arrives
        on the backend thread a moment later; the local update below means the
        switch stops showing its old position as soon as the call returns.
        """
        await self.hass.async_add_executor_job(self._backend.send_command, key, value)
        data = dict(self.data or {})
        data[key] = Reading(key=key, value=value, timestamp=time.time())
        self.async_set_updated_data(data)

    # --- backend callbacks (run on the backend's thread) -------------------

    def _on_schema(self, schema: PanelSchema) -> None:
        self.hass.loop.call_soon_threadsafe(self._apply_schema, schema)

    def _on_reading(self, reading: Reading) -> None:
        with self._buffer_lock:
            self._buffer[reading.key] = reading
            if not self._flush_scheduled:
                self._flush_scheduled = True
                self.hass.loop.call_soon_threadsafe(self._flush)

    # --- loop-thread appliers ---------------------------------------------

    @callback
    def _apply_schema(self, schema: PanelSchema) -> None:
        self.schema = schema
        self._emit_new_specs(schema)

    @callback
    def _emit_new_specs(self, schema: PanelSchema) -> None:
        for platform in self._platforms:
            self._emit_to(platform, schema)

    @callback
    def _emit_to(self, platform: _Platform, schema: PanelSchema) -> None:
        new = [
            spec
            for key, spec in schema.properties.items()
            if key not in platform.seen and platform.wants(spec)
        ]
        if not new:
            return
        platform.seen.update(spec.key for spec in new)
        platform.adder(new)

    @callback
    def _flush(self) -> None:
        with self._buffer_lock:
            batch = self._buffer
            self._buffer = {}
            self._flush_scheduled = False
        self._last_frame = time.monotonic()
        if not self._reported_live:
            self._reported_live = True
            _LOGGER.info("SPAN telemetry resumed; entities are live again")
        data = dict(self.data or {})
        data.update(batch)
        self.async_set_updated_data(data)
