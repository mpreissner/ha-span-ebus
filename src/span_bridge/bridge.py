"""The bridge: panel backend → Home Assistant broker.

Responsibilities:

- Hold the current `PanelSchema` and republish discovery configs when it changes.
- Forward every `Reading` to the HA broker as a retained state message.
- Forward HA command topics back to the backend for settable properties.
- Maintain an availability topic via MQTT last-will, so entities go `unavailable`
  if this process dies rather than silently freezing at their last value.
"""

from __future__ import annotations

import logging
import threading

import paho.mqtt.client as mqtt

from . import discovery
from .backends import Backend
from .config import Settings
from .models import PanelSchema, Reading

log = logging.getLogger(__name__)


class Bridge:
    def __init__(self, settings: Settings, backend: Backend, serial: str) -> None:
        self._settings = settings
        self._backend = backend
        self._serial = serial

        self._schema: PanelSchema | None = None
        self._last_values: dict[str, str] = {}
        # Command topic → property key, rebuilt whenever the schema changes.
        self._command_routes: dict[str, str] = {}
        self._lock = threading.Lock()

        self._client = self._build_client()

    # --- setup -------------------------------------------------------------

    def _build_client(self) -> mqtt.Client:
        cfg = self._settings.broker
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg.client_id)
        if cfg.username:
            client.username_pw_set(cfg.username, cfg.password or "")

        client.will_set(
            discovery.availability_topic(self._serial),
            payload="offline",
            qos=1,
            retain=True,
        )
        client.on_connect = self._on_ha_connect
        client.on_message = self._on_ha_message
        return client

    def run(self) -> None:
        """Connect to both ends and block until interrupted."""
        cfg = self._settings.broker
        log.info("connecting to Home Assistant broker at %s:%d", cfg.host, cfg.port)
        self._client.connect(cfg.host, cfg.port, keepalive=60)
        self._client.loop_start()

        self._backend.start(self._handle_schema, self._handle_reading)
        log.info("bridge running; backend=%s panel=%s", self._backend.name, self._serial)

        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            log.info("shutting down")
        finally:
            self.stop()

    def stop(self) -> None:
        self._backend.stop()
        self._client.publish(
            discovery.availability_topic(self._serial), "offline", qos=1, retain=True
        )
        self._client.loop_stop()
        self._client.disconnect()

    # --- HA broker callbacks -----------------------------------------------

    def _on_ha_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            log.error("HA broker refused connection: %s", reason_code)
            return
        client.publish(
            discovery.availability_topic(self._serial), "online", qos=1, retain=True
        )
        # Resubscribe after a reconnect, since subscriptions do not survive.
        with self._lock:
            for topic in self._command_routes:
                client.subscribe(topic, qos=1)
        log.info("connected to HA broker")

    def _on_ha_message(self, client, userdata, msg):
        with self._lock:
            key = self._command_routes.get(msg.topic)
        if not key:
            return

        value = msg.payload.decode("utf-8", errors="replace")
        try:
            self._backend.send_command(key, value)
        except Exception:
            log.exception("failed to send command %s=%s", key, value)

    # --- backend callbacks --------------------------------------------------

    def _handle_schema(self, schema: PanelSchema) -> None:
        """Publish discovery for a new or changed schema."""
        with self._lock:
            # Circuit names arrive as property values, so fold in anything we
            # have already observed before naming entities.
            schema = schema.resolve_node_names(self._last_values)
            self._schema = schema

        self._publish_discovery(schema)

    def _publish_discovery(self, schema: PanelSchema) -> None:
        prefix = self._settings.broker.discovery_prefix
        configs = discovery.build_all(prefix, schema)

        routes: dict[str, str] = {}
        for spec in schema.properties.values():
            if spec.settable:
                routes[discovery.command_topic(schema.serial, spec)] = spec.key

        for topic, payload in configs:
            import json

            self._client.publish(topic, json.dumps(payload), qos=1, retain=True)

        with self._lock:
            new_topics = set(routes) - set(self._command_routes)
            self._command_routes = routes
        for topic in new_topics:
            self._client.subscribe(topic, qos=1)

        log.info(
            "published %d discovery configs (%d settable) for %s",
            len(configs),
            len(routes),
            schema.serial,
        )

    def _handle_reading(self, reading: Reading) -> None:
        """Republish one value to the HA broker."""
        with self._lock:
            unchanged = self._last_values.get(reading.key) == reading.value
            self._last_values[reading.key] = reading.value
            schema = self._schema

        if unchanged and self._settings.publish_on_change_only:
            return

        # A circuit's label arriving means entity names can improve.
        if schema and reading.key.endswith("/name") and not unchanged:
            self._handle_schema(schema)
            with self._lock:
                schema = self._schema

        if not schema:
            # Values can precede $description; they are retained on the panel
            # broker, so the next schema publish will pick them up.
            return

        spec = schema.properties.get(reading.key)
        if not spec:
            log.debug("reading for unknown property %s", reading.key)
            return

        self._client.publish(
            discovery.state_topic(schema.serial, spec),
            reading.value,
            qos=0,
            retain=True,
        )
