"""The local transport for a panel added over its local API, and the cloud fallback.

The broker link feeds a Homie device tree and a health tracker; a timer asks
the `TransportSwitch` whether to move to the cloud or back. The rules live in
`span_client.local.health`, free of Home Assistant.

The cloud coordinators are built on the first failover, not at setup, so an
entry whose panel is reachable boots without the internet.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import CONF_HOST
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_BROKER_PASSWORD,
    CONF_BROKER_USERNAME,
    CONF_CA_PEM,
    CONF_CLIENT_NAME,
    CONF_LOCAL_SERIAL,
    CONF_MQTTS_PORT,
    DOMAIN,
    LOCAL_REAUTH,
)
from .span_client import cloud_auth
from .span_client.local import homie
from .span_client.local.health import (
    Decision,
    LocalHealth,
    Mode,
    Transport,
    TransportSwitch,
)
from .span_client.local.mqtt import LocalMqtt

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .coordinator import SpanCloudCoordinator
    from .runtime import SpanRuntime

_LOGGER = logging.getLogger(__name__)

TICK = timedelta(seconds=5)
# How long to wait before trying to build the cloud side again after it failed.
CLOUD_RETRY_AFTER = 60.0

CloudBuilder = Callable[[], Awaitable[list["SpanCloudCoordinator"]]]


def fallback_issue_id(entry_id: str) -> str:
    return f"transport_fallback_{entry_id}"


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
        self._cloud_retry_at = 0.0
        self._cloud_failures = 0
        self._reauth_logged = False
        self._tick_task: asyncio.Task[None] | None = None
        self._stopped = False
        self._cancel_tick: CALLBACK_TYPE | None = None
        self._listeners: list[CALLBACK_TYPE] = []
        self.shown = Transport.OFFLINE

    # --- lifecycle --------------------------------------------------------

    async def async_start(self) -> None:
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
        self._listeners.clear()
        await self._mqtt.stop()
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

    @callback
    def _on_connection(self, connected: bool) -> None:
        self.health.connected(connected, time.monotonic())
        self._refresh_shown()

    def _root(self) -> str:
        """The panel's root device: its serial, or the only root if that differs."""
        if self.serial in self.tree.states or self.serial in self.tree.descriptions:
            return self.serial
        roots = self.tree.roots()
        return roots[0] if len(roots) == 1 else self.serial

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
        for coordinator in self._cloud:
            try:
                await coordinator.async_stop()
            except Exception:  # one panel's failure must not keep the rest streaming
                _LOGGER.exception("could not stop a SPAN cloud stream")

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
        for listener in list(self._listeners):
            listener()
