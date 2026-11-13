"""The local transport for a panel added over its local API, and the cloud fallback.

The broker link feeds a Homie device tree and a health tracker; a timer asks
the `TransportSwitch` whether to move to the cloud or back. The rules live in
`span_client.local.health`, free of Home Assistant.

The cloud coordinators are built on the first failover, not at setup, so an
entry whose panel is reachable boots without the internet.

Messages are coalesced into one `model.Snapshot` per interval, which the local
entities listen to. The tree is kept in a Store so entities, and the energy
registers, are back at startup even while the panel is unreachable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_HOST
from homeassistant.core import CALLBACK_TYPE, HassJob, HassJobType, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    CONF_BROKER_PASSWORD,
    CONF_BROKER_USERNAME,
    CONF_CA_PEM,
    CONF_CLIENT_NAME,
    CONF_LOCAL_SERIAL,
    CONF_MQTTS_PORT,
    CONF_SNAPSHOT_INTERVAL,
    DEFAULT_SNAPSHOT_INTERVAL,
    DOMAIN,
    LOCAL_REAUTH,
)
from .span_client import cloud_auth
from .span_client.backend import SITE_NODE
from .span_client.fallback import IMPORTED_ENERGY, RegisterContinuation, resolve
from .span_client.link import (
    HistoryTarget,
    PanelLink,
    feeds_from_kinds,
    link_from_options,
    link_to_options,
    match_circuits,
)
from .span_client.local import homie, model
from .span_client.local.debounce import Coalescer, clamp_interval
from .span_client.local.health import (
    Decision,
    LocalHealth,
    Mode,
    Transport,
    TransportSwitch,
)
from .span_client.local.mqtt import LocalMqtt
from .span_client.local.rest import CannotConnect, OutcomeUnknown

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .coordinator import SpanCloudCoordinator
    from .runtime import SpanRuntime

_LOGGER = logging.getLogger(__name__)

TICK = timedelta(seconds=5)
# How long to wait before trying to build the cloud side again after it failed.
CLOUD_RETRY_AFTER = 60.0

# The tree Store is written at most this often while values change.
TREE_SAVE_DELAY = 300

CloudBuilder = Callable[[], Awaitable[list["SpanCloudCoordinator"]]]
ModelListener = Callable[[model.Snapshot], None]


def fallback_issue_id(entry_id: str) -> str:
    return f"transport_fallback_{entry_id}"


def local_tree_store(hass: HomeAssistant, entry_id: str) -> Store:
    """The last descriptions and values of a local panel, for a restart without it."""
    return Store(hass, model.STORE_VERSION, f"{DOMAIN}.{entry_id}.local_tree")


class PublishResult(StrEnum):
    ACKED = "acked"  # the broker acknowledged it
    UNACKED = "unacked"  # sent, unacknowledged: it may or may not reach the panel


class SpanTransportManager:
    """One local panel's broker link, its health, and the switch to the cloud."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        runtime: SpanRuntime,
        *,
        mode: Mode,
        build_cloud: CloudBuilder | None,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._runtime = runtime
        # Without cloud credentials there is nothing to fail over to.
        self._build_cloud = build_cloud
        self.serial: str = entry.data[CONF_LOCAL_SERIAL]
        self.health = LocalHealth(time.monotonic())
        self.switch = TransportSwitch(mode if build_cloud is not None else Mode.LOCAL_ONLY)
        self.tree = homie.DeviceTree()
        self._mqtt = LocalMqtt(
            host=entry.data[CONF_HOST],
            port=entry.data[CONF_MQTTS_PORT],
            username=entry.data[CONF_BROKER_USERNAME],
            password=entry.data.get(CONF_BROKER_PASSWORD),
            ca_pem=entry.data[CONF_CA_PEM],
            client_id=entry.data[CONF_CLIENT_NAME],
            on_message=self._on_message,
            on_connection=self._on_connection,
        )
        self._cloud: list[SpanCloudCoordinator] | None = None
        self._cloud_running = False
        # Which cloud circuit and feed stand for which local one. Read once
        # here; refreshed in place, and in the options, on each cloud start.
        self.link: PanelLink | None = link_from_options(entry.options)
        self._cloud_unsubs: list[CALLBACK_TYPE] = []
        self._link_refreshed = False
        # One per upstream lugs device: its imported-energy register carried
        # across a spell on the cloud.
        self._continuations: dict[str, RegisterContinuation] = {}
        self._cloud_retry_at = 0.0
        self._cloud_failures = 0
        self._reauth_logged = False
        self._tick_task: asyncio.Task[None] | None = None
        self._stopped = False
        self._cancel_tick: CALLBACK_TYPE | None = None
        self._listeners: list[CALLBACK_TYPE] = []
        self.shown = Transport.OFFLINE
        # Unique ids are built on this. It stays the serial when a cloud link is
        # added, so linking never renames an entity.
        self.panel_key: str = self.serial
        self.snapshot: model.Snapshot = model.EMPTY
        self._model_listeners: list[ModelListener] = []
        self._live = False  # a message has arrived since start: values are no longer restored
        # Monotonic time of the newest parsed message: the time integrated rows stamp
        # each snapshot's sample with.
        self._received_at: float | None = None
        self._logged_unmapped: set[model.Unmapped] = set()
        self._store = local_tree_store(hass, entry.entry_id)
        self._save_pending = False
        self._coalescer = Coalescer(
            clamp_interval(
                entry.options.get(CONF_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL),
                DEFAULT_SNAPSHOT_INTERVAL,
            ),
            call_soon=self._call_soon,
            # A bare bound method would run in the executor; the flush adds
            # entities and touches registries, so it must stay on the loop.
            call_later=lambda delay, action: async_call_later(
                hass, delay, HassJob(action, job_type=HassJobType.Callback)
            ),
            flush=self._flush,
        )

    # --- lifecycle --------------------------------------------------------

    async def async_start(self) -> None:
        await self._restore_tree()
        await self._mqtt.start()
        self._cancel_tick = async_track_time_interval(self._hass, self._tick, TICK)

    async def async_stop(self) -> None:
        """Stop the broker link and the timer. Coordinators are the entry's to stop.

        A tick in flight is cancelled and awaited, so nothing it started is left
        running behind the unload.
        """
        self._stopped = True
        if self._cancel_tick is not None:
            self._cancel_tick()
            self._cancel_tick = None
        task, self._tick_task = self._tick_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._unlisten_to_cloud()
        self._listeners.clear()
        self._model_listeners.clear()
        self._coalescer.stop()
        await self._mqtt.stop()
        if self._live:
            await self._save_tree()
        ir.async_delete_issue(self._hass, DOMAIN, fallback_issue_id(self._entry.entry_id))

    # --- broker callbacks (on the event loop) -----------------------------

    @callback
    def _on_message(self, topic: str, payload: bytes, _retained: bool) -> None:
        now = time.monotonic()
        parsed = self.tree.apply(topic, payload)
        self.health.message(now)
        # A description can settle which device is the root after its state
        # arrived, so either one re-reads the root's state.
        if parsed is not None and parsed.kind in (
            homie.TopicKind.STATE,
            homie.TopicKind.DESCRIPTION,
        ):
            self.health.state(self.tree.states.get(self._root()), now)
        if parsed is not None:
            self._live = True
            self._received_at = now
            self._coalescer.mark()

    @callback
    def _on_connection(self, connected: bool) -> None:
        self.health.connected(connected, time.monotonic())
        self._refresh_shown()

    def _root(self) -> str:
        return self.tree.panel_root(self.serial)

    # --- the switch -------------------------------------------------------

    @callback
    def _tick(self, _now: object = None) -> None:
        # Starting the cloud can outlast a tick; never run two at once.
        if self._stopped or (self._tick_task is not None and not self._tick_task.done()):
            return
        self._tick_task = self._hass.async_create_background_task(
            self._evaluate(time.monotonic()), f"{DOMAIN} transport {self._entry.entry_id}"
        )

    async def _evaluate(self, now: float) -> None:
        if self._stopped:
            return
        if self._mqtt.auth_failed:
            if not self._reauth_logged:
                self._reauth_logged = True
                _LOGGER.warning("the panel broker refused this entry's credentials")
            # Asked every tick: Home Assistant skips it while a reauth is open,
            # and one that was closed, or blocked by another, is asked again.
            self._entry.async_start_reauth(
                self._hass, data={**self._entry.data, LOCAL_REAUTH: True}
            )

        decision = self.switch.evaluate(self.health, now)
        if decision is Decision.START_CLOUD:
            _LOGGER.warning(
                "local link to the SPAN panel is down; using the cloud until it returns"
            )
        elif decision is Decision.STOP_CLOUD:
            _LOGGER.info("local link to the SPAN panel is back; leaving the cloud")
            await self._stop_cloud()

        if self.switch.active is Transport.CLOUD and not self._cloud_running:
            await self._start_cloud(now)
        if self._stopped:
            return

        issue = fallback_issue_id(self._entry.entry_id)
        # Only while the cloud is carrying the entry: when it is not, the reauth
        # or the Transport sensor already says what is wrong.
        if self.switch.needs_repair(now) and self._cloud_running:
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                issue,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="transport_fallback",
                translation_placeholders={"title": self._entry.title},
            )
        elif self.switch.active is Transport.LOCAL:
            ir.async_delete_issue(self._hass, DOMAIN, issue)
        self._refresh_shown(now)

    async def _start_cloud(self, now: float) -> None:
        if self._build_cloud is None or now < self._cloud_retry_at:
            return
        try:
            if self._cloud is None:
                coordinators = await self._build_cloud()
                if self._stopped:  # unloaded while the account was read
                    return
                for coordinator in coordinators:
                    self._runtime.add_coordinator(coordinator)
                self._cloud = coordinators
            self._listen_to_cloud()
            for coordinator in self._cloud:
                if self._stopped:
                    return
                await coordinator.async_start()
        except cloud_auth.CloudCredentialsRejected:
            _LOGGER.warning("SPAN cloud refused this entry's credentials; the fallback is off")
            self._cloud_retry_at = float("inf")
            if not self._stopped:
                self._entry.async_start_reauth(self._hass)
            return
        except Exception as err:  # noqa: BLE001 — retried on a later tick
            self._cloud_failures += 1
            log = _LOGGER.warning if self._cloud_failures == 1 else _LOGGER.debug
            log(
                "could not start the SPAN cloud fallback (attempt %d): %s",
                self._cloud_failures,
                err,
            )
            self._cloud_retry_at = now + CLOUD_RETRY_AFTER
            return
        self._cloud_failures = 0
        self._cloud_running = True

    async def _stop_cloud(self) -> None:
        if not self._cloud_running or self._cloud is None:
            return
        self._cloud_running = False
        self._unlisten_to_cloud()
        for coordinator in self._cloud:
            try:
                await coordinator.async_stop()
            except Exception:  # one panel's failure must not keep the rest streaming
                _LOGGER.exception("could not stop a SPAN cloud stream")

    async def async_detach_cloud(self) -> None:
        """Let go of the cloud before its credentials are removed.

        Nothing is restarted after this, so no token refresh can write the
        credentials back before the entry reloads.
        """
        self._build_cloud = None
        self._cloud_retry_at = float("inf")
        await self._stop_cloud()

    @callback
    def _listen_to_cloud(self) -> None:
        """Wake the local entities on each cloud update while the cloud is shown.

        Once per start: a failed start that is retried does not stack listeners.
        """
        if self._cloud_unsubs or not self._cloud:
            return
        self._link_refreshed = False
        self._cloud_unsubs = [c.async_add_listener(self._on_cloud_update) for c in self._cloud]

    @callback
    def _unlisten_to_cloud(self) -> None:
        unsubs, self._cloud_unsubs = self._cloud_unsubs, []
        for unsub in unsubs:
            unsub()

    @callback
    def _on_cloud_update(self) -> None:
        if self._stopped:
            return
        if not self._link_refreshed and self.link is not None:
            self._async_refresh_link()
        if self.shown is not Transport.CLOUD:
            return
        # A fresh snapshot for the local entities, which read the fallback,
        # and a nudge for those that read only the transport (derived rows).
        self._coalescer.mark()
        for listener in list(self._listeners):
            listener()

    # --- the link -----------------------------------------------------------

    def _panel_coordinator(self) -> SpanCloudCoordinator | None:
        """The coordinator streaming the linked panel: by key, else the only one."""
        cloud = self._cloud or []
        link = self.link
        if link is not None:
            for coordinator in cloud:
                if coordinator.cloud_key == link.cloud_key:
                    return coordinator
        return cloud[0] if cloud else None

    @callback
    def _async_refresh_link(self) -> None:
        """Re-match circuits and feeds against what the cloud now says.

        Runs once per cloud start, as soon as the panel's schema is in. The
        options are written only when something changed, and nothing reloads:
        the new link is used from here on.
        """
        link = self.link
        panel = self._panel_coordinator()
        if link is None or panel is None or panel.schema is None:
            return
        circuits, feeders = panel.traits()
        if not circuits or not self.snapshot.circuits:
            return  # either side not read yet: try again on the next update
        self._link_refreshed = True
        matched, unmatched_local, unmatched_cloud = match_circuits(
            self.snapshot.circuits, circuits.values()
        )
        history = link.site_history
        for coordinator in self._cloud or ():
            target = coordinator.site_history_target()
            if target is not None:
                history = HistoryTarget(target[0], target[1], SITE_NODE)
                break
        count = len(panel.site.panels) if panel.site is not None else 0
        fresh = replace(
            link,
            circuits=matched,
            feeds=feeds_from_kinds(feeders),
            unmatched_local=unmatched_local,
            unmatched_cloud=unmatched_cloud,
            site_history=history,
            site_panel_count=count or link.site_panel_count,
        )
        if fresh == link:
            return
        self.link = fresh
        # Counts only: the ids are private.
        _LOGGER.info(
            "cloud fallback matched %d of %d local circuits",
            len(matched),
            len(self.snapshot.circuits),
        )
        self._hass.config_entries.async_update_entry(
            self._entry, options=link_to_options(self._entry.options, fresh)
        )

    # --- the Transport sensor ---------------------------------------------

    @callback
    def async_add_listener(self, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    @callback
    def _refresh_shown(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        cloud_live = self._cloud_running and any(c.stream_is_live for c in self._cloud or ())
        shown = self.switch.shown(self.health, now, cloud_live=cloud_live)
        if shown is self.shown:
            return
        self.shown = shown
        if shown is Transport.CLOUD:
            for continuation in self._continuations.values():
                continuation.cloud_started()
        self._coalescer.mark()
        for listener in list(self._listeners):
            listener()

    # --- the model ----------------------------------------------------------

    @callback
    def async_add_model_listener(self, listener: ModelListener) -> CALLBACK_TYPE:
        self._model_listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self._model_listeners:
                self._model_listeners.remove(listener)

        return remove

    def _fallback_ready(self) -> bool:
        return (
            self.link is not None
            and self.switch.mode is Mode.LOCAL_FIRST
            and self._cloud_running
            and bool(self._cloud)
            and self.shown is Transport.CLOUD
        )

    def _cloud_data(self) -> dict[str, Any]:
        """The readings the fallback maps from: every coordinator's site values,
        under the linked panel's own (a shared site's flows may stream apart).
        """
        data: dict[str, Any] = {}
        for coordinator in self._cloud or ():
            data.update(
                (key, reading)
                for key, reading in (coordinator.data or {}).items()
                if key.startswith(f"{SITE_NODE}/")
            )
        panel = self._panel_coordinator()
        if panel is not None:
            data.update(panel.data or {})
        return data

    def _cloud_energy(self, key: str) -> float | None:
        panel = self._panel_coordinator()
        ordered = [panel] if panel is not None else []
        ordered += [c for c in self._cloud or () if c is not panel]
        for coordinator in ordered:
            total = coordinator.energy_total(key)
            if total is not None:
                return total
        return None

    def fallback_value(self, scope: model.Scope, device_id: str, key: str) -> Any:
        """The cloud's reading for a local entity while on the fallback, in the
        local row's frame. NO_FALLBACK unless the cloud is live and shown and the
        row maps; None where it maps but the cloud has no reading yet.
        """
        link = self.link
        if (
            link is None
            or not self._fallback_ready()
            or not any(c.stream_is_live for c in self._cloud or ())
        ):
            return model.NO_FALLBACK
        circuit = self.snapshot.circuits.get(device_id) if scope is model.Scope.CIRCUIT else None
        return resolve(
            link,
            self._cloud_data(),
            scope,
            device_id,
            key,
            producer=circuit is not None and circuit.role in model.PRODUCERS,
            energy=self._cloud_energy,
        )

    def continue_energy(
        self, scope: model.Scope, device_id: str, source: str, local_value: Any
    ) -> Any:
        """The panel's imported-energy register, carried across a spell on the cloud.

        Every other energy row, and this one without a link, on a shared site or
        with the fallback off, shows its local value as it is.
        """
        link = self.link
        if (
            scope is not model.Scope.UPSTREAM
            or source != IMPORTED_ENERGY
            or link is None
            or not link.single_panel_site
            or self.switch.mode is not Mode.LOCAL_FIRST
        ):
            return local_value
        continuation = self._continuations.setdefault(device_id, RegisterContinuation())
        value = float(local_value) if isinstance(local_value, int | float) else None
        counting = self.shown is Transport.CLOUD and self._cloud_running
        shown = continuation.show(
            value,
            local=self.shown is Transport.LOCAL,
            cloud_counting=counting,
            total=self._cloud_energy("site/grid") if counting else None,
        )
        return local_value if shown is None else shown

    def _call_soon(self, action: Callable[[], None]) -> Callable[[], None]:
        handle = self._hass.loop.call_soon(action)
        return handle.cancel

    @callback
    def _flush(self) -> None:
        if self._stopped:
            return
        self._publish_snapshot(
            model.build_snapshot(
                self.tree,
                self._root(),
                restored=not self._live,
                received_at=self._received_at if self._live else None,
            )
        )
        if self._live:
            self._save_tree_later()

    @callback
    def _publish_snapshot(self, snapshot: model.Snapshot) -> None:
        self.snapshot = snapshot
        self._log_unmapped(snapshot.unmapped)
        for listener in list(self._model_listeners):
            listener(snapshot)

    def _log_unmapped(self, unmapped: frozenset[model.Unmapped]) -> None:
        # Class and key only: never a device id or the serial.
        for item in unmapped - self._logged_unmapped:
            self._logged_unmapped.add(item)
            if item.key is None:
                _LOGGER.debug("r202639 model: %s devices are not modelled yet", item.device_class)
            elif item.key == model.LINK_CONFLICT:
                _LOGGER.debug(
                    "r202639 model: link conflict on %s; lowest claimant kept", item.device_class
                )
            else:
                _LOGGER.debug("r202639 model: ignoring %s property %s", item.device_class, item.key)

    # --- the tree Store -----------------------------------------------------

    async def _restore_tree(self) -> None:
        """Replay the last descriptions and values, so entities exist and energy
        holds even if the panel is unreachable at startup. Live values replace
        them as the broker resends its retained topics.
        """
        try:
            data = await self._store.async_load()
            if data is None:
                return
            model.load_payload(data, self.tree)
        except Exception as err:  # noqa: BLE001 — an empty start is always safe
            _LOGGER.debug("ignoring the stored local tree: %s", type(err).__name__)
            self.tree = homie.DeviceTree()
            return
        self._publish_snapshot(model.build_snapshot(self.tree, self._root(), restored=True))

    def _save_tree_later(self) -> None:
        # HA restarts the delay on every call, which at one flush a second would
        # never save; ask once and let the save clear the flag.
        if self._save_pending:
            return
        self._save_pending = True
        self._store.async_delay_save(self._tree_data, TREE_SAVE_DELAY)

    def _tree_data(self) -> dict[str, Any]:
        self._save_pending = False
        return model.store_payload(self.tree, self._root())

    async def _save_tree(self) -> None:
        try:
            await self._store.async_save(self._tree_data())
        except Exception as err:  # noqa: BLE001 — a lost save only costs a restored start
            _LOGGER.debug("could not save the local tree: %s", type(err).__name__)

    # --- commands -----------------------------------------------------------

    async def async_send_command(self, device_id: str, key: str, value: Any) -> PublishResult:
        """Set a property on the panel. Raises with a translated message the user sees;
        no device id or topic goes into one.
        """
        if self.shown is not Transport.LOCAL:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="local_unavailable")
        description = self.tree.descriptions.get(device_id)
        definition = description.properties.get(key) if description else None
        if definition is None or not definition.settable:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="not_settable")
        try:
            payload = homie.encode_value(definition, value)
        except ValueError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="bad_value"
            ) from err
        try:
            await self._mqtt.publish(homie.set_topic(device_id, key), payload, qos=1)
        except CannotConnect as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="command_not_sent"
            ) from err
        except OutcomeUnknown:
            # Nothing is retried: a stale command is worse than a lost one.
            _LOGGER.warning("the SPAN panel did not acknowledge a %s command", key)
            return PublishResult.UNACKED
        return PublishResult.ACKED
