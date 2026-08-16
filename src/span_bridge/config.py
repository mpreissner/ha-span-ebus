"""Environment-driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _path(env: str, default: str) -> Path:
    return Path(os.environ.get(env) or default).expanduser()


@dataclass(frozen=True)
class PanelConfig:
    """How to reach the SPAN panel."""

    host: str
    serial: str | None = None
    rest_port: int = 80
    mqtt_port: int = 8883
    auth_file: Path = Path.home() / ".span-auth.json"
    ca_cert_dir: Path = Path.home() / ".span-ca-certs"

    @property
    def rest_base(self) -> str:
        return f"http://{self.host}:{self.rest_port}"

    @property
    def ca_cert_path(self) -> Path:
        if not self.serial:
            raise ValueError("serial required to locate the CA certificate")
        return self.ca_cert_dir / f"{self.serial}.crt"


@dataclass(frozen=True)
class BrokerConfig:
    """The Home Assistant MQTT broker we republish to."""

    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    discovery_prefix: str = "homeassistant"
    client_id: str = "span-bridge"


@dataclass(frozen=True)
class Settings:
    panel: PanelConfig
    broker: BrokerConfig
    log_level: str = "INFO"
    # Republish every reading, or only on change. Change-only keeps HA's
    # recorder from filling with duplicate rows on a fast-moving stream.
    publish_on_change_only: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        host = os.environ.get("SPAN_HOST")
        if not host:
            raise SystemExit("SPAN_HOST is required (panel IP or hostname)")

        ha_host = os.environ.get("HA_MQTT_HOST")
        if not ha_host:
            raise SystemExit("HA_MQTT_HOST is required (your Home Assistant broker)")

        panel = PanelConfig(
            host=host,
            serial=os.environ.get("SPAN_SERIAL"),
            rest_port=int(os.environ.get("SPAN_REST_PORT", "80")),
            mqtt_port=int(os.environ.get("SPAN_MQTT_PORT", "8883")),
            auth_file=_path("SPAN_AUTH_FILE", "~/.span-auth.json"),
            ca_cert_dir=_path("SPAN_CA_CERT_DIR", "~/.span-ca-certs"),
        )

        broker = BrokerConfig(
            host=ha_host,
            port=int(os.environ.get("HA_MQTT_PORT", "1883")),
            username=os.environ.get("HA_MQTT_USERNAME"),
            password=os.environ.get("HA_MQTT_PASSWORD"),
            discovery_prefix=os.environ.get("HA_DISCOVERY_PREFIX", "homeassistant"),
            client_id=os.environ.get("HA_MQTT_CLIENT_ID", "span-bridge"),
        )

        return cls(
            panel=panel,
            broker=broker,
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            publish_on_change_only=os.environ.get("PUBLISH_ON_CHANGE_ONLY", "1") != "0",
        )
