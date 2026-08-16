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
class CloudConfig:
    """How to reach SPAN's cloud data plane (the hybrid live-data path).

    `device_uuid` is a stable client identifier the AblyToken RPC binds the
    realtime channel to; `user_id` (the Cognito sub) lets us construct the
    channel name `c:<user_id>:<device_uuid>` if the RPC doesn't return it.
    """

    token_store: Path = Path.home() / ".span-cloud.json"
    device_uuid: str | None = None
    user_id: str | None = None
    serial: str | None = None


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
    cloud: CloudConfig = CloudConfig()
    # Which data source feeds the bridge: the panel's local broker ("ebus") or
    # SPAN's cloud ("cloud"). Local is preferred once the MAIN 40 exposes its
    # API; cloud is the interim path (docs/CLOUD-FLOW.md).
    backend: str = "ebus"
    log_level: str = "INFO"
    # Republish every reading, or only on change. Change-only keeps HA's
    # recorder from filling with duplicate rows on a fast-moving stream.
    publish_on_change_only: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        backend = os.environ.get("SPAN_BACKEND", "ebus").lower()

        host = os.environ.get("SPAN_HOST")
        # The cloud backend does not need a panel IP; the local one does.
        if not host and backend != "cloud":
            raise SystemExit("SPAN_HOST is required (panel IP or hostname)")

        ha_host = os.environ.get("HA_MQTT_HOST")
        if not ha_host:
            raise SystemExit("HA_MQTT_HOST is required (your Home Assistant broker)")

        serial = os.environ.get("SPAN_SERIAL")
        panel = PanelConfig(
            host=host or "",
            serial=serial,
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

        cloud = CloudConfig(
            token_store=_path("SPAN_CLOUD_TOKEN_STORE", "~/.span-cloud.json"),
            device_uuid=os.environ.get("SPAN_CLOUD_DEVICE_UUID"),
            user_id=os.environ.get("SPAN_CLOUD_USER_ID"),
            # The cloud serial namespace differs from the local mDNS one.
            serial=os.environ.get("SPAN_CLOUD_SERIAL", serial),
        )

        return cls(
            panel=panel,
            broker=broker,
            cloud=cloud,
            backend=backend,
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            publish_on_change_only=os.environ.get("PUBLISH_ON_CHANGE_ONLY", "1") != "0",
        )
