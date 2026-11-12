"""Capture what a panel publishes on its local API, redacted, for test fixtures.

Needs firmware r202639 or later and the panel passphrase from the SPAN app.

    uv run python tools/probe_local.py <panel-ip> --out <directory>

It fetches and pins the panel CA, registers a temporary client with the
passphrase (prompted for, never echoed or written anywhere), reads every
retained topic from the panel's broker for a few seconds, then deletes the
temporary client again. It sends no commands.

Written to `--out`:

- `capture.json`: every topic and payload, with device ids replaced by
  placeholders such as `panel` and `circuit-07`.
- `schema.json`: `GET /api/v2/homie/schema`, with the firmware kept.
- `status.json`: `GET /api/v2/status`, serial replaced.

Serials, the hostname, broker credentials and the values of identifying
properties (serial numbers, MAC and IP addresses, network names) are replaced.
Circuit names are kept. Read the files before sharing them anyway.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import re
import sys
from pathlib import Path
from typing import Any

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "span_ebus"))

from span_client.local import homie, rest, tls
from span_client.local.mqtt import LocalMqtt

CLIENT_NAME = "span-ebus-probe"
# Matched against the hyphen-separated words of a property name, so `city`
# catches `info/city` but not `nameplate-capacity`.
IDENTIFYING = frozenset(
    {
        "serial", "mac", "ip", "ipv4", "ipv6", "hostname", "address", "ssid", "bssid",
        "latitude", "longitude", "locality", "region", "postal", "zip", "street", "city",
        "country",
    }
)  # fmt: skip
# Properties whose value is one or more device ids, comma-separated.
DEVICE_REFERENCE = re.compile(r"(-device-ids?|grid-forming-entity)$")


def is_identifying(key: str, *, root: bool = False) -> bool:
    """Whether a property's value identifies the site, e.g. `info/latitude`.

    On the panel itself `info/name` is the user's name for the site.
    """
    if root and key == "info/name":
        return True
    return not IDENTIFYING.isdisjoint(key.split("/")[-1].lower().split("-"))


def referenced_ids(messages: dict[str, bytes]) -> set[str]:
    """Device ids named in values, e.g. a subpanel's `fed-by-device-id`."""
    found: set[str] = set()
    for topic, payload in messages.items():
        parsed = homie.parse_topic(topic)
        if parsed is None or parsed.key is None or not DEVICE_REFERENCE.search(parsed.key):
            continue
        text = payload.decode("utf-8", errors="replace").strip("\x00")
        found |= {part.strip() for part in text.split(",") if part.strip()}
    return found


def survivors(text: str, private: set[str]) -> int:
    """How many private values still appear anywhere in `text`, ignoring case."""
    lowered = text.lower()
    return sum(1 for value in private if value.lower() in lowered)


async def capture(
    host: str, seconds: float, passphrase: str
) -> tuple[dict[str, Any], dict[str, bytes], set[str]]:
    secrets: set[str] = set()
    messages: dict[str, bytes] = {}
    # A fresh connection per request: the panel closes idle ones, and a
    # reused dead one fails the POST with ServerDisconnectedError.
    connector = aiohttp.TCPConnector(force_close=True)
    async with aiohttp.ClientSession(connector=connector) as session:
        ca = await rest.SpanLocalApi(session, host).fetch_ca()
        print(f"Panel CA fingerprint: {tls.ca_fingerprint(ca)}")
        api = rest.SpanLocalApi(session, host, ca_pem=ca)
        status = await api.status()
        schema = await api.homie_schema()
        print(f"Firmware {status.firmware}, data model {schema.data_model_version}")
        registration = await api.register(CLIENT_NAME, passphrase=passphrase)
        secrets |= {status.serial, registration.serial, registration.hostname}
        secrets |= {registration.broker_username, registration.broker_password or ""}
        secrets |= {registration.access_token, registration.hop_passphrase or ""}
        broker: LocalMqtt | None = None
        try:
            broker = LocalMqtt(
                host=host,
                port=registration.mqtts_port,
                username=registration.broker_username,
                password=registration.broker_password,
                ca_pem=ca,
                client_id=CLIENT_NAME,
                on_message=lambda topic, payload, _retained: messages.__setitem__(topic, payload),
            )
            await broker.connect_once()
            print(f"Connected to the broker; listening for {seconds:.0f} s")
            await asyncio.sleep(seconds)
        finally:
            if broker is not None:
                await broker.stop()
            await api.delete_client(CLIENT_NAME)
            print("Temporary client removed")
        documents = {
            "status": {
                "serialNumber": status.serial,
                "firmwareVersion": status.firmware,
                "hardwareVersion": status.hardware,
                "proximityProven": status.proximity_proven,
            },
            "schema": {
                "firmwareVersion": schema.firmware,
                "homieDomain": schema.homie_domain,
                "homieVersion": schema.homie_version,
                "dataModelVersion": schema.data_model_version,
                "deviceClasses": schema.device_classes,
                "deviceClassesSchemaHash": schema.schema_hash,
            },
        }
    # Very short values would mangle unrelated text; nothing secret is that short.
    return documents, messages, {s for s in secrets if s and len(s) >= 4}


def placeholders(tree: homie.DeviceTree) -> dict[str, str]:
    """Device id → stable placeholder, by class, in walk order."""
    names: dict[str, str] = {}
    counts: dict[str, int] = {}
    for root in tree.roots():
        for description in tree.walk(root):
            if description.device_id in names:
                continue
            if description.device_id == root:
                names[description.device_id] = "panel"
                continue
            device_class = description.device_class or "device"
            counts[device_class] = counts.get(device_class, 0) + 1
            names[description.device_id] = f"{device_class}-{counts[device_class]:02d}"
    # Anything published but never described still gets a placeholder.
    for device_id in sorted((set(tree.states) | set(tree._raw)) - set(names)):
        counts["unknown"] = counts.get("unknown", 0) + 1
        names[device_id] = f"unknown-{counts['unknown']:02d}"
    return names


def redact(text: str, replacements: dict[str, str]) -> str:
    """Replace every key, ignoring case, longest first in a single pass.

    Longest first, so `<serial>-lugs-up` wins over `<serial>`; one pass, so a
    placeholder is never itself rewritten.
    """
    if not replacements:
        return text
    lookup = {old.lower(): new for old, new in replacements.items()}
    # Whole tokens only, so an id inside a longer id is never half-replaced.
    alternatives = "|".join(re.escape(old) for old in sorted(replacements, key=len, reverse=True))
    pattern = re.compile(rf"(?<![A-Za-z0-9])(?:{alternatives})(?![A-Za-z0-9])", re.IGNORECASE)
    return pattern.sub(lambda match: lookup[match.group(0).lower()], text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("host", help="the panel's IP address or hostname")
    parser.add_argument("--out", type=Path, required=True, help="directory for the files")
    parser.add_argument("--seconds", type=float, default=8.0)
    args = parser.parse_args()

    passphrase = getpass.getpass("Panel passphrase (not echoed): ")
    try:
        documents, messages, secrets = asyncio.run(capture(args.host, args.seconds, passphrase))
    except rest.SpanLocalError as exc:
        print(f"Failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    tree = homie.DeviceTree()
    for topic, payload in messages.items():
        tree.apply(topic, payload)
    ids = placeholders(tree)
    # Ids named in values but not published here: the other panel of a
    # feed-through pair, a battery the panel knows of, and so on.
    for index, device_id in enumerate(sorted(referenced_ids(messages) - set(ids)), start=1):
        if len(device_id) >= 4:
            ids[device_id] = f"external-{index:02d}"

    # Open question: does the Homie panel serial match the REST serial?
    root_ids = [i for i, name in ids.items() if name == "panel"]
    homie_serial = tree.value(root_ids[0], "info/serial-number") if root_ids else None
    rest_serial = documents["status"]["serialNumber"]
    print(f"Homie info/serial-number equals REST serial: {homie_serial == rest_serial}")

    # Values of identifying properties, anywhere in the tree.
    roots = set(tree.roots())
    for device_id in tree.descriptions:
        for key, value in tree.values(device_id).items():
            identifying = is_identifying(key, root=device_id in roots)
            if identifying and isinstance(value, str) and len(value) >= 4:
                secrets.add(value)

    replacements = dict(ids)
    for index, secret in enumerate(sorted(secrets - set(ids), key=len, reverse=True)):
        replacements[secret] = f"<redacted-{index}>"
    replacements[rest_serial] = "nt-0000-test"
    if args.host not in replacements:
        replacements[args.host] = "<panel-host>"

    capture_out = {}
    for topic in sorted(messages):
        payload = messages[topic]
        parsed = homie.parse_topic(topic)
        if (
            parsed is not None
            and parsed.key is not None
            and is_identifying(parsed.key, root=parsed.device_id in roots)
        ):
            # Whatever its type: latitude and longitude are numbers.
            text = "<redacted>" if payload else ""
        else:
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                text = payload.hex()
        capture_out[redact(topic, replacements)] = redact(text, replacements)

    args.out.mkdir(parents=True, exist_ok=True)
    # Each value is redacted exactly once: a second pass would rewrite
    # placeholders that happen to contain a short secret.
    outputs = {
        "capture.json": json.dumps(capture_out, indent=2, sort_keys=True),
        "schema.json": redact(
            json.dumps(documents["schema"], indent=2, sort_keys=True), replacements
        ),
        "status.json": redact(
            json.dumps(documents["status"], indent=2, sort_keys=True), replacements
        ),
    }
    # Whole-token matching could miss an id glued to other text; refuse to
    # write anything rather than leak it.
    private = {old for old, new in replacements.items() if old != new and len(old) >= 4}
    for name, text in outputs.items():
        if count := survivors(text, private):
            print(f"Refusing to write: {count} private value(s) survive in {name}", file=sys.stderr)
            return 1
    for name, text in outputs.items():
        (args.out / name).write_text(text + "\n")

    classes: dict[str, int] = {}
    for description in tree.descriptions.values():
        name = description.device_class or "?"
        classes[name] = classes.get(name, 0) + 1
    print(f"{len(messages)} topics from {len(tree.descriptions)} devices: {classes}")
    print(f"Written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
