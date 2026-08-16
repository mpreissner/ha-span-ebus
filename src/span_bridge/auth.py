"""SPAN local API authentication.

Flow (per `specs/r202627/openapi.json`):

1. ``GET /api/v2/certificate/ca`` → the panel's self-signed CA, needed to verify
   the MQTT broker's TLS certificate.
2. ``POST /api/v2/auth/register`` → exchange a proof-of-identity for an
   ``accessToken`` (REST) and ``ebusBroker*`` credentials (MQTT).

The register call accepts one of three proofs:

``hopPassphrase``
    Read from the SPAN Home mobile app.
``otp``
    One-time passcode.
``dashboardPassword``
    Panel dashboard password.

During a **proof-of-proximity** window — press the panel door switch 3×, which
opens a 15-minute window — registration succeeds with none of these supplied.
That is the easiest path and the one `span-bridge auth` tries first.

Credentials are cached at ``~/.span-auth.json`` with mode 0600, matching the
layout used by SPAN's own ``scripts/span-auth`` so the two interoperate.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import stat
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from . import tls
from .config import PanelConfig

log = logging.getLogger(__name__)

REGISTER_TIMEOUT = 15
DEFAULT_CLIENT_NAME = "span-bridge"


class AuthError(RuntimeError):
    """Registration or certificate retrieval failed."""


@dataclass(frozen=True)
class Credentials:
    """Everything needed to talk to a panel after registration."""

    serial: str
    hostname: str
    access_token: str
    issued_at_ms: int
    ebus_username: str
    ebus_password: str
    ebus_host: str
    ebus_mqtts_port: int
    hop_passphrase: str | None = None

    @classmethod
    def from_response(cls, body: dict) -> "Credentials":
        try:
            return cls(
                serial=body["serialNumber"],
                hostname=body["hostname"],
                access_token=body["accessToken"],
                issued_at_ms=int(body.get("iatMs", time.time() * 1000)),
                ebus_username=body["ebusBrokerUsername"],
                ebus_password=body["ebusBrokerPassword"],
                ebus_host=body.get("ebusBrokerHost") or body["hostname"],
                ebus_mqtts_port=int(body.get("ebusBrokerMqttsPort", 8883)),
                hop_passphrase=body.get("hopPassphrase"),
            )
        except KeyError as exc:
            raise AuthError(f"register response missing field {exc}") from exc


def fetch_ca_from_broker(panel: PanelConfig, serial: str) -> Path:
    """Recover the panel CA from the broker's TLS handshake, without REST.

    The broker on :8883 presents its whole chain — leaf plus the per-device CA
    that signed it — so the CA can be read straight off the wire. This matters
    because the REST tier and the broker fail independently: on a panel whose
    REST tier is down (502 on every `/api/*` route) the broker can still be
    perfectly healthy, and then this is the only way to obtain the CA.

    Candidates are accepted only if a full *verified* handshake then succeeds
    using them, which proves the certificate actually signed what the broker
    presented. That is a stronger check than matching the subject name, and it
    is what makes reading the chain over an unverified connection safe: a
    forged CA injected by a man in the middle could carry any name it liked,
    but it could not also make the real broker's certificate verify against it.
    """
    host, port = panel.host, panel.mqtt_port
    log.info("recovering CA from the broker TLS handshake at %s:%d", host, port)

    try:
        chain = tls.peer_chain_pem(host, port, timeout=REGISTER_TIMEOUT)
    except (OSError, ssl.SSLError) as exc:
        raise AuthError(f"could not reach the panel broker at {host}:{port}: {exc}") from exc

    if not chain:
        raise AuthError(f"broker at {host}:{port} presented no certificate chain")

    panel.ca_cert_dir.mkdir(parents=True, exist_ok=True)
    path = panel.ca_cert_dir / f"{serial}.crt"

    # The issuer is normally last, so search from the end.
    for pem in reversed(chain):
        candidate = path.with_suffix(".candidate")
        candidate.write_text(pem)
        if tls.verifies_peer(candidate, host, port, timeout=REGISTER_TIMEOUT):
            candidate.replace(path)
            log.info("recovered CA certificate to %s", path)
            return path
        candidate.unlink(missing_ok=True)

    raise AuthError(
        f"broker at {host}:{port} presented {len(chain)} certificate(s), none of "
        "which verify it — the panel may have re-issued its CA mid-connection"
    )


def fetch_ca_certificate(panel: PanelConfig, serial: str) -> Path:
    """Obtain and cache the panel's CA certificate. Returns its path.

    REST is tried first because it is the documented route, with a fallback to
    the broker handshake when the REST tier is down. A 5xx here is not a
    transient error worth retrying — it means nginx has no live upstream — so
    the fallback runs immediately rather than after a delay.
    """
    url = f"{panel.rest_base}/api/v2/certificate/ca"
    log.info("fetching CA certificate from %s", url)

    try:
        resp = requests.get(url, timeout=REGISTER_TIMEOUT)
    except requests.RequestException as exc:
        log.warning("panel REST unreachable (%s); trying the broker handshake", exc)
        return fetch_ca_from_broker(panel, serial)

    if resp.status_code >= 500:
        log.warning(
            "panel REST returned %d — the API tier is down; "
            "recovering the CA from the broker instead",
            resp.status_code,
        )
        return fetch_ca_from_broker(panel, serial)

    resp.raise_for_status()

    if "BEGIN CERTIFICATE" not in resp.text:
        raise AuthError(f"unexpected CA response body: {resp.text[:200]!r}")

    panel.ca_cert_dir.mkdir(parents=True, exist_ok=True)
    path = panel.ca_cert_dir / f"{serial}.crt"
    path.write_text(resp.text)
    log.info("cached CA certificate at %s", path)
    return path


def register(
    panel: PanelConfig,
    *,
    name: str = DEFAULT_CLIENT_NAME,
    description: str = "Home Assistant bridge",
    hop_passphrase: str | None = None,
    otp: str | None = None,
    dashboard_password: str | None = None,
) -> Credentials:
    """Register this client with the panel and obtain credentials."""
    url = f"{panel.rest_base}/api/v2/auth/register"

    body: dict[str, str] = {"name": name, "description": description}
    if hop_passphrase:
        body["hopPassphrase"] = hop_passphrase
    if otp:
        body["otp"] = otp
    if dashboard_password:
        body["dashboardPassword"] = dashboard_password

    proof = "proximity window" if len(body) == 2 else "supplied credential"
    log.info("registering %r with panel via %s", name, proof)

    resp = requests.post(url, json=body, timeout=REGISTER_TIMEOUT)

    if resp.status_code == 502:
        raise AuthError(
            "panel returned 502 — the REST API backend is not running. "
            "Press the panel door switch 3 times and retry within 15 minutes."
        )
    if resp.status_code in (401, 403):
        raise AuthError(
            "panel rejected the registration. Either the proximity window has "
            "expired (press the door switch 3 times again) or the supplied "
            "passphrase is wrong."
        )
    if resp.status_code == 422:
        raise AuthError(f"panel rejected the request body: {resp.text[:300]}")
    resp.raise_for_status()

    creds = Credentials.from_response(resp.json())
    log.info("registered with panel %s (%s)", creds.serial, creds.hostname)
    return creds


# --- credential cache -------------------------------------------------------


def _open_0600(path, flags):
    return os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)


def load_cache(auth_file: Path) -> dict:
    if not auth_file.exists():
        return {"version": 1, "default_panel": None, "panels": {}}

    mode = auth_file.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        log.warning(
            "%s is readable by other users; run 'chmod 600 %s'", auth_file, auth_file
        )

    return json.loads(auth_file.read_text())


def save_credentials(auth_file: Path, creds: Credentials) -> None:
    """Persist credentials, preserving any other panels already cached."""
    data = load_cache(auth_file)

    data.setdefault("panels", {})[creds.serial] = {
        "hostname": creds.hostname,
        "hop_passphrase": creds.hop_passphrase,
        "ebus_broker_username": creds.ebus_username,
        "ebus_broker_password": creds.ebus_password,
        "ebus_broker_host": creds.ebus_host,
        "ebus_broker_mqtts_port": creds.ebus_mqtts_port,
        "access_token": creds.access_token,
        "access_token_issued_at": creds.issued_at_ms,
    }
    if not data.get("default_panel") or len(data["panels"]) == 1:
        data["default_panel"] = creds.serial

    auth_file.parent.mkdir(parents=True, exist_ok=True)
    with open(auth_file, "w", opener=_open_0600) as fh:
        json.dump(data, fh, indent=2)
    log.info("saved credentials for %s to %s", creds.serial, auth_file)


def load_credentials(auth_file: Path, serial: str | None = None) -> Credentials | None:
    """Load cached credentials for a panel, or the default panel."""
    data = load_cache(auth_file)
    panels = data.get("panels") or {}
    if not panels:
        return None

    target = serial or data.get("default_panel")
    if not target and len(panels) == 1:
        target = next(iter(panels))
    if not target or target not in panels:
        return None

    p = panels[target]
    return Credentials(
        serial=target,
        hostname=p["hostname"],
        access_token=p["access_token"],
        issued_at_ms=int(p.get("access_token_issued_at", 0)),
        ebus_username=p.get("ebus_broker_username", ""),
        ebus_password=p.get("ebus_broker_password", ""),
        ebus_host=p.get("ebus_broker_host") or p["hostname"],
        ebus_mqtts_port=int(p.get("ebus_broker_mqtts_port", 8883)),
        hop_passphrase=p.get("hop_passphrase"),
    )
