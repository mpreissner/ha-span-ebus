"""Command-line entry point: probe / auth / discover / run."""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys

import requests

from . import auth as auth_mod
from .backends import load as load_backend
from .bridge import Bridge
from .config import PanelConfig, Settings

log = logging.getLogger("span_bridge")

PORTS = {
    80: "REST (nginx)",
    443: "REST over TLS",
    8883: "MQTTS — Electrification Bus",
    9001: "MQTT over WebSocket",
    50058: "gRPC (services removed in r202627)",
    50065: "legacy gRPC (removed)",
}


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _port_open(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --- commands ---------------------------------------------------------------


def cmd_probe(args) -> int:
    """Check what the panel currently exposes. Safe to run anytime."""
    host = args.host
    print(f"Probing {host}\n")

    print("Ports:")
    for port, label in sorted(PORTS.items()):
        state = "open" if _port_open(host, port) else "closed"
        marker = "+" if state == "open" else "-"
        print(f"  {marker} {port:<6} {state:<7} {label}")

    print("\nREST endpoints:")
    rest_up = False
    for path in (
        "/api/v2/certificate/ca",
        "/api/v2/auth/register",
        "/api/v2/status",
        "/api/v1/panel",
    ):
        try:
            resp = requests.get(f"http://{host}{path}", timeout=5)
            code = resp.status_code
        except requests.RequestException as exc:
            code = f"ERR ({type(exc).__name__})"
        else:
            if code not in (500, 502, 503):
                rest_up = True
        print(f"  {code}  {path}")

    print()
    if rest_up:
        print("REST API is responding. Run:  span-bridge auth --host " + host)
    else:
        print(
            "REST API is not running (502 = nginx upstream refused).\n"
            "Press the panel door switch 3 times, then re-run this within 15 minutes."
        )
    return 0


def cmd_auth(args) -> int:
    """Register with the panel and cache credentials."""
    panel = PanelConfig(host=args.host, auth_file=args.auth_file)

    try:
        creds = auth_mod.register(
            panel,
            name=args.name,
            hop_passphrase=args.passphrase,
            otp=args.otp,
            dashboard_password=args.dashboard_password,
        )
    except auth_mod.AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    panel = PanelConfig(
        host=args.host, serial=creds.serial, auth_file=args.auth_file
    )
    try:
        cert_path = auth_mod.fetch_ca_certificate(panel, creds.serial)
    except auth_mod.AuthError as exc:
        print(f"error fetching CA certificate: {exc}", file=sys.stderr)
        return 1

    auth_mod.save_credentials(args.auth_file, creds)

    print(f"Registered with panel {creds.serial}")
    print(f"  broker      mqtts://{creds.ebus_host}:{creds.ebus_mqtts_port}")
    print(f"  CA cert     {cert_path}")
    print(f"  credentials {args.auth_file}")
    print("\nNext:  span-bridge discover")
    return 0


def cmd_discover(args) -> int:
    """Connect to the panel and dump its schema without publishing anything."""
    creds = auth_mod.load_credentials(args.auth_file, args.serial)
    if not creds:
        print("error: no cached credentials; run 'span-bridge auth' first", file=sys.stderr)
        return 1

    panel = PanelConfig(host=creds.hostname, serial=creds.serial, auth_file=args.auth_file)
    backend = load_backend("ebus", credentials=creds, ca_cert_path=panel.ca_cert_path)

    import threading

    done = threading.Event()
    captured: dict = {}

    def on_schema(schema):
        captured["schema"] = schema
        done.set()

    backend.start(on_schema, lambda reading: None)
    got = done.wait(timeout=args.timeout)
    backend.stop()

    if not got:
        print(
            f"error: no $description received within {args.timeout}s. "
            "The panel publishes it retained, so this usually means the "
            "subscription or credentials are wrong.",
            file=sys.stderr,
        )
        return 1

    schema = captured["schema"]
    circuits = schema.circuits()
    print(f"Panel {schema.serial}: {len(schema.properties)} properties, {len(circuits)} circuits\n")

    if args.json:
        print(json.dumps({k: v.__dict__ for k, v in schema.properties.items()}, indent=2, default=str))
        return 0

    by_node: dict[str, list] = {}
    for spec in schema.properties.values():
        by_node.setdefault(spec.node_id, []).append(spec)

    for node_id, specs in sorted(by_node.items()):
        kind = specs[0].node_kind.value
        print(f"  {node_id}  [{kind}]")
        for spec in sorted(specs, key=lambda s: s.property_id):
            flag = " (settable)" if spec.settable else ""
            unit = f" {spec.unit}" if spec.unit else ""
            print(f"      {spec.property_id:<32} {spec.datatype.value}{unit}{flag}")
    return 0


def cmd_run(args) -> int:
    """Start the bridge."""
    settings = Settings.from_env()
    _setup_logging(settings.log_level)

    creds = auth_mod.load_credentials(settings.panel.auth_file, settings.panel.serial)
    if not creds:
        print("error: no cached credentials; run 'span-bridge auth' first", file=sys.stderr)
        return 1

    panel = PanelConfig(
        host=settings.panel.host,
        serial=creds.serial,
        auth_file=settings.panel.auth_file,
        ca_cert_dir=settings.panel.ca_cert_dir,
    )
    backend = load_backend("ebus", credentials=creds, ca_cert_path=panel.ca_cert_path)

    Bridge(settings, backend, creds.serial).run()
    return 0


# --- argument parsing --------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    from pathlib import Path

    parser = argparse.ArgumentParser(
        prog="span-bridge",
        description="Bridge a SPAN MAIN 40 panel into Home Assistant over MQTT.",
    )
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    default_auth = Path.home() / ".span-auth.json"

    p = sub.add_parser("probe", help="check what the panel currently exposes")
    p.add_argument("--host", required=True, help="panel IP or hostname")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("auth", help="register with the panel and cache credentials")
    p.add_argument("--host", required=True)
    p.add_argument("--name", default=auth_mod.DEFAULT_CLIENT_NAME)
    p.add_argument("--passphrase", help="hopPassphrase from the SPAN Home app")
    p.add_argument("--otp", help="one-time passcode")
    p.add_argument("--dashboard-password", help="panel dashboard password")
    p.add_argument("--auth-file", type=Path, default=default_auth)
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("discover", help="dump the panel schema without publishing")
    p.add_argument("--serial", help="panel serial (defaults to the cached default)")
    p.add_argument("--auth-file", type=Path, default=default_auth)
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--json", action="store_true", help="emit JSON instead of a tree")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("run", help="start the bridge (configured via environment)")
    p.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.log_level)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
