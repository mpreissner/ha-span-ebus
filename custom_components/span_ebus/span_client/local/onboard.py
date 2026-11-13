"""The steps of adding a panel over the local API, without Home Assistant.

The config flow calls these in order: `probe_panel` (CA and status),
`register_client` (passphrase), `wait_until_ready` (broker login and a first
Homie description). Each raises the `rest` exceptions, or `NotReadyInTime`,
and `form_error` turns any of them into a form error key.

Nothing here logs or raises with a secret or a serial in the message.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import uuid
from collections.abc import Callable, Mapping
from typing import Any

import aiohttp

from . import homie, model
from .mqtt import LocalMqtt
from .rest import (
    AuthFailed,
    CannotConnect,
    OutcomeUnknown,
    PanelNotReady,
    PanelStatus,
    RateLimited,
    Registration,
    RegistrationRejected,
    SpanLocalApi,
    SpanLocalError,
)

log = logging.getLogger(__name__)

# The first firmware with the v2 API and the eBus broker.
MIN_FIRMWARE = 202639
READY_TIMEOUT = 15.0
# How long to gather the broker's retained topics when reading the circuits.
CIRCUIT_READ_SECONDS = 5.0
CLIENT_PREFIX = "ha-span-ebus-"


class NotReadyInTime(SpanLocalError):
    """The broker accepted the login but no panel reported ready in time."""


def firmware_supported(firmware: str) -> bool:
    """Whether a firmware string such as `spanos2/r202639/04` has the v2 API.

    A string without a release number is let through: a panel that answers
    `/api/v2/status` at all has the API, and a new naming scheme should not
    lock anyone out.
    """
    match = re.search(r"r(\d{6,})", firmware)
    return match is None or int(match.group(1)) >= MIN_FIRMWARE


def new_client_name() -> str:
    """A name for this registration, unique per attempt.

    A fresh one each time, so a retry never depends on how the panel treats
    a name it has seen before.
    """
    return f"{CLIENT_PREFIX}{uuid.uuid4().hex[:8]}"


async def probe_panel(
    session: aiohttp.ClientSession, host: str, *, port: int = 443
) -> tuple[PanelStatus, str]:
    """Fetch and pin the panel CA, then read its status through it.

    Returns the status and the CA. A panel without the v2 API raises
    NotFound (the CA endpoint 404s); anything else that isn't the API
    raises UnexpectedResponse.
    """
    ca_pem = await SpanLocalApi(session, host, port=port).fetch_ca()
    status = await SpanLocalApi(session, host, ca_pem=ca_pem, port=port).status()
    return status, ca_pem


async def host_matches_ca(
    session: aiohttp.ClientSession, host: str, ca_pem: str, serial: str, *, port: int = 443
) -> bool:
    """Whether `host` is the panel with this serial, proven by the pinned CA."""
    try:
        status = await SpanLocalApi(session, host, ca_pem=ca_pem, port=port).status()
    except SpanLocalError:
        return False
    return status.serial == serial


async def register_client(
    session: aiohttp.ClientSession,
    host: str,
    ca_pem: str,
    name: str,
    passphrase: str,
    *,
    port: int = 443,
    unconfirmed: list[str] | None = None,
) -> tuple[Registration, str]:
    """Register `name` with the passphrase; the registration and its name.

    Retried once when the panel drops the connection: it closes idle
    keep-alive connections, and a POST on a dead one fails before it is
    sent. Each attempt gets its own `SpanLocalApi`, and so its own TLS
    context and connection, and the retry a fresh name, since the first
    POST may have landed after all.

    Every name whose outcome is unknown (a lost connection, a timeout or a
    504) is appended to `unconfirmed`, so the caller can remove it with the
    token a later registration brings.
    """
    for attempt in (1, 2):
        api = SpanLocalApi(session, host, ca_pem=ca_pem, port=port)
        try:
            registration = await api.register(
                name, passphrase=passphrase, description="Home Assistant"
            )
        except (CannotConnect, OutcomeUnknown) as exc:
            if unconfirmed is not None:
                unconfirmed.append(name)
            if attempt == 2 or isinstance(exc, OutcomeUnknown):
                raise
            log.debug("register lost its connection; retrying once")
            name = new_client_name()
        else:
            return registration, name
    raise AssertionError("unreachable")


async def discard_client(
    session: aiohttp.ClientSession,
    host: str,
    ca_pem: str,
    access_token: str,
    name: str,
    *,
    port: int = 443,
) -> None:
    """Best effort: remove a registration that will not be used.

    Never raises: it runs on entry removal, which must finish regardless.
    """
    try:
        api = SpanLocalApi(session, host, ca_pem=ca_pem, access_token=access_token, port=port)
        await api.delete_client(name)
    except Exception as exc:  # noqa: BLE001 — cleanup is best effort
        log.debug("could not remove an unused client registration: %s", type(exc).__name__)


MqttFactory = Callable[..., Any]


async def wait_until_ready(
    registration: Registration,
    host: str,
    ca_pem: str,
    client_id: str,
    *,
    timeout: float = READY_TIMEOUT,
    mqtt_factory: MqttFactory = LocalMqtt,
) -> str:
    """Log in to the broker and wait for a described panel in state ready.

    Returns the root device id. Raises AuthFailed or CannotConnect from the
    login, and NotReadyInTime when nothing is ready within `timeout`. The
    connection is always closed again.
    """
    tree = homie.DeviceTree()
    ready = asyncio.Event()

    def ready_root() -> str | None:
        for root in tree.roots():
            if tree.effective_state(root) is homie.DeviceState.READY:
                return root
        return None

    def on_message(topic: str, payload: bytes, _retained: bool) -> None:
        tree.apply(topic, payload)
        if ready_root() is not None:
            ready.set()

    broker = mqtt_factory(
        host=host,
        port=registration.mqtts_port,
        username=registration.broker_username,
        password=registration.broker_password,
        ca_pem=ca_pem,
        client_id=client_id,
        on_message=on_message,
    )
    try:
        await broker.connect_once()
        try:
            await asyncio.wait_for(ready.wait(), timeout)
        except TimeoutError:
            raise NotReadyInTime("no panel reported ready") from None
    finally:
        with contextlib.suppress(Exception):
            await broker.stop()
    root = ready_root()
    if root is None:  # cleared between the event and here
        raise NotReadyInTime("no panel reported ready")
    return root


def panel_root(tree: homie.DeviceTree, serial: str) -> str:
    """The panel's root device: its serial, or the only root if that differs."""
    if serial in tree.states or serial in tree.descriptions:
        return serial
    roots = tree.roots()
    return roots[0] if len(roots) == 1 else serial


async def read_circuits(
    *,
    host: str,
    port: int,
    username: str,
    password: str | None,
    ca_pem: str,
    client_id: str,
    serial: str,
    seconds: float = CIRCUIT_READ_SECONDS,
    mqtt_factory: MqttFactory = LocalMqtt,
) -> Mapping[str, model.CircuitFacts]:
    """Log in to the broker, gather what it holds, and return the panel's circuits.

    For checking a cloud panel picked by hand before there is an entry, and so
    a transport, to read them from. Raises AuthFailed or CannotConnect from the
    login. The connection is always closed again.
    """
    tree = homie.DeviceTree()
    broker = mqtt_factory(
        host=host,
        port=port,
        username=username,
        password=password,
        ca_pem=ca_pem,
        client_id=client_id,
        on_message=lambda topic, payload, _retained: tree.apply(topic, payload),
    )
    try:
        await broker.connect_once()
        await asyncio.sleep(seconds)
    finally:
        with contextlib.suppress(Exception):
            await broker.stop()
    return model.build_snapshot(tree, panel_root(tree, serial)).circuits


def form_error(exc: BaseException) -> tuple[str, dict[str, str]]:
    """The config-flow error key for a failure, and its placeholders."""
    if isinstance(exc, RateLimited):
        return "rate_limited", {"retry_after": str(exc.retry_after or 60)}
    if isinstance(exc, RegistrationRejected):
        return "invalid_passphrase", {}
    if isinstance(exc, PanelNotReady):
        return "panel_not_ready", {}
    if isinstance(exc, AuthFailed):
        return "broker_auth", {}
    if isinstance(exc, NotReadyInTime):
        return "no_ready_state", {}
    if isinstance(exc, (CannotConnect, OutcomeUnknown)):
        return "panel_unreachable", {}
    return "unknown", {}
