"""Async MQTT over TLS to the panel's eBus broker, on paho-mqtt 2.x.

paho is driven from the asyncio loop rather than its own thread: the socket is
registered with `loop.add_reader`/`add_writer`, and `loop_misc` runs once a
second for keepalives. This is the same arrangement Home Assistant's own MQTT
integration uses. Only `connect`, which does the TCP and TLS handshakes, runs
in an executor, because it blocks.

A supervisor task keeps the connection up, reconnecting with backoff from 1 s
to 60 s. It gives up only when the broker refuses the credentials, which
surfaces as `AuthFailed` so the integration can start a reauth.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from collections.abc import Callable, Iterable
from typing import Any

import paho.mqtt.client as paho
from paho.mqtt.enums import CallbackAPIVersion, MQTTErrorCode

from . import tls
from .homie import SUBSCRIPTIONS
from .rest import AuthFailed, CannotConnect, OutcomeUnknown

log = logging.getLogger(__name__)

KEEPALIVE = 60
CONNECT_TIMEOUT = 15
PUBLISH_TIMEOUT = 10
BACKOFF_MIN = 1.0
BACKOFF_MAX = 60.0
MAX_PACKETS_PER_READ = 500  # drains the TLS buffer, which select cannot see

# CONNACK codes that mean "these credentials will never work". 4 and 5 are the
# MQTT 3.1.1 values; 134 and 135 are their MQTT 5 equivalents.
_AUTH_REFUSED = frozenset({4, 5, 134, 135})

MessageHandler = Callable[[str, bytes, bool], None]
StateHandler = Callable[[bool], None]


class LocalMqtt:
    """One panel's broker connection.

    `on_message(topic, payload, retained)` and `on_connection(connected)` are
    called on the event loop. A handler that raises is logged and does not
    break the connection.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str | None,
        ca_pem: str,
        client_id: str,
        on_message: MessageHandler,
        on_connection: StateHandler | None = None,
        subscriptions: Iterable[str] = SUBSCRIPTIONS,
    ) -> None:
        self._host = host
        self._port = port
        self._on_message = on_message
        self._on_connection = on_connection
        self._subscriptions = tuple(subscriptions)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._misc: asyncio.Task[None] | None = None
        self._connack: asyncio.Future[None] | None = None
        self._disconnected = asyncio.Event()
        self._pending: dict[int, asyncio.Future[None]] = {}
        self._connected = False
        self._stopping = False
        self.auth_failed = False

        client = paho.Client(
            CallbackAPIVersion.VERSION2, client_id=client_id, protocol=paho.MQTTv311
        )
        client.username_pw_set(username, password)
        client.tls_set_context(tls.pinned_context(ca_pem))
        client.on_connect = self._handle_connect
        client.on_disconnect = self._handle_disconnect
        client.on_message = self._handle_message
        client.on_publish = self._handle_publish
        client.on_socket_open = self._socket_open
        client.on_socket_close = self._socket_close
        client.on_socket_register_write = self._socket_register_write
        client.on_socket_unregister_write = self._socket_unregister_write
        self._client = client

    @property
    def connected(self) -> bool:
        return self._connected

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Start the supervisor. Returns at once; watch `on_connection`."""
        if self._supervisor is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        self._supervisor = self._loop.create_task(self._supervise(), name="span_ebus mqtt")

    async def connect_once(self) -> None:
        """Connect and wait for the broker to accept, without supervising.

        For the config flow's credential check. Raises AuthFailed or
        CannotConnect. Call `stop` afterwards either way.
        """
        self._loop = asyncio.get_running_loop()
        await self._connect()

    async def stop(self) -> None:
        self._stopping = True
        if self._supervisor is not None:
            self._supervisor.cancel()
            await asyncio.gather(self._supervisor, return_exceptions=True)
            self._supervisor = None
        if self._client.is_connected():
            self._client.disconnect()
        await self._stop_misc()
        self._set_connected(False)
        self._fail_pending(CannotConnect("connection closed"))

    async def _supervise(self) -> None:
        backoff = BACKOFF_MIN
        while not self._stopping:
            try:
                await self._connect()
            except AuthFailed:
                log.warning("panel broker refused the credentials; reauthentication needed")
                self.auth_failed = True
                self._set_connected(False)
                return
            except CannotConnect as exc:
                log.debug("broker connect failed: %s", exc)
            else:
                backoff = BACKOFF_MIN
                await self._disconnected.wait()
            await self._stop_misc()
            if self._stopping:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)

    async def _connect(self) -> None:
        loop = self._require_loop()
        self._disconnected.clear()
        self._connack = loop.create_future()
        try:
            await loop.run_in_executor(
                None, self._client.connect, self._host, self._port, KEEPALIVE
            )
        except (OSError, ssl.SSLError) as exc:
            # The message can include the address, so only the class is kept.
            raise CannotConnect(f"broker unreachable: {type(exc).__name__}") from None
        self._misc = loop.create_task(self._misc_loop(), name="span_ebus mqtt misc")
        try:
            await asyncio.wait_for(self._connack, CONNECT_TIMEOUT)
        except TimeoutError:
            self._client.disconnect()
            raise CannotConnect("no CONNACK from broker") from None

    async def _misc_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            if self._client.loop_misc() is MQTTErrorCode.MQTT_ERR_NO_CONN:
                return

    async def _stop_misc(self) -> None:
        if self._misc is not None:
            self._misc.cancel()
            await asyncio.gather(self._misc, return_exceptions=True)
            self._misc = None

    # --- publishing -------------------------------------------------------

    async def publish(
        self, topic: str, payload: str, *, qos: int = 1, timeout: float = PUBLISH_TIMEOUT
    ) -> None:
        """Publish and wait for the broker's acknowledgement.

        Raises CannotConnect when not connected, and OutcomeUnknown when the
        message was sent but not acknowledged in time: the panel may still
        act on it, so the caller must not assume it failed.
        """
        if not self._connected:
            raise CannotConnect("not connected to the panel broker")
        info = self._client.publish(topic, payload, qos=qos, retain=False)
        if info.rc is not MQTTErrorCode.MQTT_ERR_SUCCESS:
            raise CannotConnect(f"publish refused: {info.rc.name}")
        if info.is_published():
            return
        future = self._require_loop().create_future()
        self._pending[info.mid] = future
        try:
            await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise OutcomeUnknown("no acknowledgement from the panel broker") from None
        finally:
            self._pending.pop(info.mid, None)

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    # --- paho callbacks (all on the event loop, except the socket ones) ----

    def _handle_connect(
        self, client: paho.Client, _userdata: Any, _flags: Any, reason: Any, _props: Any
    ) -> None:
        code = int(getattr(reason, "value", reason))
        if code != 0:
            error: Exception = (
                AuthFailed(f"broker refused credentials ({code})")
                if code in _AUTH_REFUSED
                else CannotConnect(f"broker refused connection ({code})")
            )
            self._resolve_connack(error)
            return
        result, _mid = client.subscribe([(topic, 1) for topic in self._subscriptions])
        if result is not MQTTErrorCode.MQTT_ERR_SUCCESS:
            log.debug("subscribe failed: %s", result.name)
        self._set_connected(True)
        self._resolve_connack(None)

    def _handle_disconnect(
        self, _client: paho.Client, _userdata: Any, _flags: Any, reason: Any, _props: Any
    ) -> None:
        log.debug("broker disconnected (%s)", getattr(reason, "value", reason))
        self._set_connected(False)
        self._resolve_connack(CannotConnect("broker closed the connection"))
        self._fail_pending(CannotConnect("connection lost before acknowledgement"))
        self._disconnected.set()

    def _handle_message(self, _client: paho.Client, _userdata: Any, msg: paho.MQTTMessage) -> None:
        try:
            self._on_message(msg.topic, msg.payload, bool(msg.retain))
        except Exception:
            log.exception("error handling a panel message")

    def _handle_publish(
        self, _client: paho.Client, _userdata: Any, mid: int, _reason: Any, _props: Any
    ) -> None:
        future = self._pending.get(mid)
        if future is not None and not future.done():
            future.set_result(None)

    def _resolve_connack(self, error: Exception | None) -> None:
        future = self._connack
        if future is None or future.done():
            return
        if error is None:
            future.set_result(None)
        else:
            future.set_exception(error)

    def _set_connected(self, connected: bool) -> None:
        if connected == self._connected:
            return
        self._connected = connected
        if self._on_connection is not None:
            try:
                self._on_connection(connected)
            except Exception:
                log.exception("error in a connection-state handler")

    # paho calls these from whichever thread touches the socket, which during
    # `connect` is the executor, so each one hops to the loop.

    def _socket_open(self, client: paho.Client, _userdata: Any, sock: Any) -> None:
        self._require_loop().call_soon_threadsafe(
            self._require_loop().add_reader, sock, client.loop_read, MAX_PACKETS_PER_READ
        )

    def _socket_close(self, _client: paho.Client, _userdata: Any, sock: Any) -> None:
        loop = self._require_loop()
        loop.call_soon_threadsafe(loop.remove_reader, sock)
        loop.call_soon_threadsafe(loop.remove_writer, sock)

    def _socket_register_write(self, client: paho.Client, _userdata: Any, sock: Any) -> None:
        loop = self._require_loop()
        loop.call_soon_threadsafe(loop.add_writer, sock, client.loop_write)

    def _socket_unregister_write(self, _client: paho.Client, _userdata: Any, sock: Any) -> None:
        loop = self._require_loop()
        loop.call_soon_threadsafe(loop.remove_writer, sock)

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError("LocalMqtt used before start")
        return self._loop
