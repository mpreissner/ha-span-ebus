"""Official SPAN local API backend — MQTT over TLS, Homie 5.

Connects to the panel's own broker on :8883, verifying its certificate against
the per-device CA fetched during registration. Subscribes to the whole `ebus`
tree, parses `$description` into a schema, and emits every property value as a
`Reading`.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import paho.mqtt.client as mqtt

from ..auth import Credentials
from ..homie import parse_description, parse_value_topic
from ..models import Reading
from ..tls import build_ssl_context
from . import ReadingCallback, SchemaCallback

log = logging.getLogger(__name__)


class EbusBackend:
    """Subscribes to the panel's Electrification Bus."""

    name = "ebus"

    def __init__(self, credentials: Credentials, ca_cert_path: Path) -> None:
        self._creds = credentials
        self._ca_cert = Path(ca_cert_path)
        self._on_schema: SchemaCallback | None = None
        self._on_reading: ReadingCallback | None = None
        self._client: mqtt.Client | None = None

    # --- lifecycle ---------------------------------------------------------

    def start(self, on_schema: SchemaCallback, on_reading: ReadingCallback) -> None:
        self._on_schema = on_schema
        self._on_reading = on_reading

        if not self._ca_cert.exists():
            raise FileNotFoundError(
                f"panel CA certificate not found at {self._ca_cert}; "
                "run 'python -m span_bridge.cli auth' first"
            )

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"span-bridge-{self._creds.serial}",
        )
        client.username_pw_set(self._creds.ebus_username, self._creds.ebus_password)

        client.tls_set_context(build_ssl_context(self._ca_cert))

        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect

        host, port = self._creds.ebus_host, self._creds.ebus_mqtts_port
        log.info("connecting to panel broker at mqtts://%s:%d", host, port)
        client.connect(host, port, keepalive=60)
        client.loop_start()
        self._client = client

    def stop(self) -> None:
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None
            log.info("disconnected from panel broker")

    # --- commands ----------------------------------------------------------

    def send_command(self, key: str, value: str) -> None:
        if not self._client:
            raise RuntimeError("backend not started")
        topic = f"ebus/5/{self._creds.serial}/{key}/set"
        log.info("command %s -> %s", topic, value)
        self._client.publish(topic, value, qos=1)

    # --- mqtt callbacks ----------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            log.error("panel broker refused connection: %s", reason_code)
            return
        topic = f"ebus/5/{self._creds.serial}/#"
        log.info("connected; subscribing to %s", topic)
        client.subscribe(topic, qos=1)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        # paho reconnects automatically while the network loop is running.
        if reason_code != 0:
            log.warning("unexpected disconnect from panel broker: %s", reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode("utf-8", errors="replace")
        except Exception:
            log.debug("undecodable payload on %s", msg.topic)
            return

        if msg.topic.endswith("/$description"):
            self._handle_description(payload)
            return

        if msg.topic.endswith("/$state"):
            log.info("panel lifecycle state: %s", payload)
            return

        parsed = parse_value_topic(msg.topic)
        if not parsed or not self._on_reading:
            return

        _, key = parsed
        self._on_reading(Reading(key=key, value=payload, timestamp=time.time()))

    def _handle_description(self, payload: str) -> None:
        try:
            doc = json.loads(payload)
        except json.JSONDecodeError:
            log.error("could not parse $description as JSON")
            return

        schema = parse_description(self._creds.serial, doc)
        if self._on_schema:
            self._on_schema(schema)
