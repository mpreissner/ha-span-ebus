"""Record how an account's panels report, in a report that is safe to post.

Answers what a one-panel account can't: how two panels appear in SPAN's data, and
whether a panel's readings include the panel fed from it. Run it while switching one
large, steady load on the downstream panel on and off, and press Enter at each step.
Anything that cycles makes the steps hard to compare, so a heater beats an oven.

    git clone --branch dev https://github.com/mpreissner/ha-span-ebus
    cd ha-span-ebus
    python3 -m venv .venv
    .venv/bin/pip install 'httpx[http2]' requests
    .venv/bin/python tools/capture_panels.py

It prompts for the SPAN login and keeps the tokens in memory only. It sends no
commands, and it uses fresh device ids, so it leaves Home Assistant's stream alone.

Every string in the report is replaced with a placeholder, unless it is a model code.
The same string gets the same placeholder throughout, so links between sites still
show. Numbers of 1000 or more in the id dumps are replaced too. The readings are
medians in watts, which identify nobody. Read the report before posting it anyway.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import re
import statistics
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "span_ebus"))

from span_client import backend, cloud_ably, cloud_auth, cloud_grpc
from span_client.cloud_pb import (
    ProtoError,
    field_message,
    field_string,
    iter_fields,
    parse,
)
from span_client.cloud_telemetry import (
    _decode_double_channel,
    _leaf_value,
    decode_frame,
)
from span_client.cloud_traits import CircuitInfo, parse_trait_snapshot

PHASES = ("load off", "load on", "load off again", "load on again", "load off at the end")
DEFAULT_SECONDS = 30
# Long enough for the stream to attach before the subscribe that starts it publishing.
ATTACH_SECONDS = 3
KEEP = re.compile(r"^(Panelboard|\d-\d{5}-\d{2})$")
# Shorter strings can turn up inside placeholders and table headings by chance.
SWEEP_MIN_LENGTH = 8


class Redactor:
    """Swap each private string for a stable placeholder that shows its shape."""

    def __init__(self) -> None:
        self.seen: dict[str, str] = {}

    def text(self, value: str) -> str:
        if not value:
            return "<empty>"
        if KEEP.match(value):
            return value
        if value not in self.seen:
            self.seen[value] = f"<{self._shape(value)}-{len(self.seen) + 1}>"
        return self.seen[value]

    @staticmethod
    def _shape(value: str) -> str:
        if re.fullmatch(r"[0-9a-fA-F]{16}", value):
            return "hex16"
        if re.fullmatch(r"[0-9a-fA-F-]{36}", value):
            return "uuid"
        if "@" in value:
            return "email"
        if value[:3].upper() == "XC-" or re.fullmatch(r"[A-Z0-9-]{8,}", value):
            return "serial"
        return "text"

    def assert_clean(self, report: str) -> None:
        """Refuse a report holding anything that was ever given a placeholder."""
        for value in self.seen:
            if len(value) >= SWEEP_MIN_LENGTH and value in report:
                raise ValueError(f"{self.seen[value]} is in the report unredacted")


def dump(raw: bytes, redact: Redactor, indent: int = 0, depth: int = 0) -> list[str]:
    """Field-number tree of a protobuf message, with every value redacted."""
    pad = "  " * indent
    out: list[str] = []
    for no, wire, value in iter_fields(raw):
        if wire == 0:
            out.append(f"{pad}{no}: {value if value < 1000 else '<bignum>'}")
        elif wire != 2:
            out.append(f"{pad}{no}: <wire{wire}>")
        else:
            out.extend(_dump_len(no, value, redact, indent, depth))
    return out


def _dump_len(no: int, value: bytes, redact: Redactor, indent: int, depth: int) -> list[str]:
    pad = "  " * indent
    try:
        text = value.decode()
    except UnicodeDecodeError:
        text = None
    if text is not None and text.isprintable():
        return [f"{pad}{no}: {redact.text(text)}"]
    if depth < 12:
        try:
            return [f"{pad}{no}:", *dump(value, redact, indent + 1, depth + 1)]
        except (ProtoError, ValueError, IndexError):
            pass
    return [f"{pad}{no}: <bytes:{len(value)}>"]


def panel_bodies(raw: bytes) -> Iterator[tuple[str, int, bytes]]:
    """Each `PanelInstant` in a frame, as resource id, instance id and raw message."""
    for push in parse(raw).values(16):
        for envelope in parse(push).values(3):
            for body in parse(envelope).values(3):
                for block in parse(body).values(3):
                    message = parse(block)
                    ref = message.get_msg_opt(1)
                    resource = (ref.get_str(1) if ref is not None else None) or ""
                    for sample in message.values(2):
                        parsed = parse(sample)
                        for panel in parsed.values(14):
                            yield resource, parsed.get_uint(2) or 0, panel


def panel_readings(raw: bytes) -> dict[str, float]:
    """`PanelInstant`'s four named powers, in watts. A message may lack any of them."""
    message = parse(raw)
    out: dict[str, float] = {}
    for no, name in ((1, "feeder"), (2, "feedthrough")):
        meter = message.get_msg_opt(no)
        combined = _decode_double_channel(meter)[2] if meter is not None else None
        if combined is not None and combined.power_w is not None:
            out[name] = combined.power_w
    for no, name in ((3, "total_branch"), (4, "balance")):
        power = message.get_msg_opt(no)
        if power is not None:
            out[name] = (_leaf_value(power, signed=True) or 0) / 1000.0
    return out


def leaves(raw: bytes, prefix: str = "") -> Iterator[tuple[str, int]]:
    """Every varint in a message by field path, undecoded, in case a reading is misread."""
    for no, wire, value in iter_fields(raw):
        path = f"{prefix}{no}"
        if wire == 0:
            yield path, value
        elif wire == 2:
            try:
                nested = list(leaves(value, f"{path}."))
            except (ProtoError, ValueError, IndexError):
                continue
            yield from nested


def resource_circuits(snapshot: bytes) -> dict[str, dict[int, CircuitInfo]]:
    """Circuits per trait resource, taken one at a time since instance ids can repeat."""
    return {
        resource: parse_trait_snapshot(block)
        for resource, block in _snapshot_blocks(snapshot, wrapped=True)
    }


def _snapshot_blocks(snapshot: bytes, *, wrapped: bool) -> Iterator[tuple[str, bytes]]:
    for block in parse(snapshot).values(1):
        ref = parse(block).get_msg_opt(1)
        resource = (ref.get_str(1) if ref is not None else None) or ""
        yield resource, field_message(1, block) if wrapped else block


class Recorder:
    """Collects each stream's readings under the phase running when they arrive."""

    def __init__(self, redact: Redactor) -> None:
        self.redact = redact
        self.phase: str | None = None
        self.circuits: dict[str, dict[int, CircuitInfo]] = {}
        self.frames: Counter[tuple[str, str]] = Counter()
        # Section, then row, then phase, then samples.
        self.values: dict[str, dict[str, dict[str, list[float]]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(list))
        )
        self.lock = threading.Lock()

    def add(self, site: str, raw: bytes) -> None:
        with self.lock:
            phase = self.phase
            if phase is None:
                return
            self.frames[site, phase] += 1
            try:
                frame = decode_frame(raw)
                panels = list(panel_bodies(raw))
            except (ProtoError, ValueError, IndexError):
                self.frames[site, "undecodable"] += 1
                return
            for name, value in frame.site_flows.items():
                self._put(f"{site} site flows", name, phase, value)
            for resource, samples in frame.resources.items():
                self._block(self.redact.text(resource), resource, samples, phase)
            for resource, instance, body in panels:
                self._panel(self.redact.text(resource), instance, body, phase)

    def _block(self, block: str, resource: str, samples: list, phase: str) -> None:
        circuits = self.circuits.get(resource, {})
        branches = []
        for sample in samples:
            if sample.kind == "panel" or sample.power_w is None:
                continue
            if sample.instance_id in circuits:
                branches.append(sample.power_w)
            else:
                name = f"instance {sample.instance_id} ({sample.kind}), not a circuit"
                self._put(f"block {block}", name, phase, sample.power_w)
        self._put(f"block {block}", "circuits reporting", phase, len(branches))
        self._put(f"block {block}", "sum of circuits", phase, sum(branches))

    def _panel(self, block: str, instance: int, body: bytes, phase: str) -> None:
        for name, value in panel_readings(body).items():
            self._put(f"block {block}", f"panel {name}", phase, value)
        for path, value in leaves(body):
            self._put(f"block {block} raw panel {instance}", path, phase, value)

    def _put(self, section: str, row: str, phase: str, value: float) -> None:
        self.values[section][row][phase].append(value)

    def tables(self, phases: Iterable[str]) -> list[str]:
        phases = list(phases)
        lines: list[str] = []
        for section, rows in sorted(self.values.items()):
            lines += ["", f"-- {section} --", "row | " + " | ".join(phases)]
            for row, by_phase in sorted(rows.items()):
                cells = [_median(by_phase.get(phase, [])) for phase in phases]
                lines.append(f"{row} | " + " | ".join(cells))
        return lines


def _median(values: list[float]) -> str:
    """The median, and its range when it moved, so a cycling load shows as one."""
    if not values:
        return "-"
    low, high = min(values), max(values)
    median = f"{statistics.median(values):.1f}"
    return median if low == high else f"{median} [{low:.1f}..{high:.1f}]"


def account_lines(sites: list[backend.SiteInfo], redact: Redactor) -> list[str]:
    lines = [f"sites: {len(sites)}"]
    for number, site in enumerate(sites, 1):
        if site.name:
            redact.text(site.name)
        lines.append(
            f"site {number} {redact.text(site.site_id)}: {len(site.panels)} panel(s), "
            f"{len(site.gateways)} gateway(s), {len(site.hardware_ids)} hardware id(s), "
            f"{len(backend.site_choices(site))} choice(s)"
        )
        for panel in site.panels:
            lines.append(
                f"  {panel.model}: serial {redact.text(panel.serial or '')}, "
                f"resource {redact.text(panel.hardware_id or '')}"
            )
    return lines


def snapshot_lines(snapshot: bytes, redact: Redactor) -> list[str]:
    """Each resource's traits, and in full any resource that isn't a panel's circuits."""
    lines: list[str] = []
    for (resource, block), (_, wrapped) in zip(
        _snapshot_blocks(snapshot, wrapped=False),
        _snapshot_blocks(snapshot, wrapped=True),
        strict=True,
    ):
        traits: Counter[str] = Counter()
        for entry in parse(block).get_msgs(2):
            key = entry.get_msg_opt(1)
            if key is not None:
                traits[f"{key.get_int_opt(1)}/{key.get_int_opt(3)}"] += 1
        circuits = parse_trait_snapshot(wrapped)
        lines.append(
            f"{redact.text(resource)}: {len(circuits)} circuit(s), traits {sorted(traits.items())}"
        )
        if not circuits:
            lines += dump(block, redact, 1)
    return lines


def _listen(token: str, channel: str, site: str, recorder: Recorder, stop, errors: list) -> None:
    try:
        cloud_ably.stream_frames(
            token, channel, lambda raw: recorder.add(site, raw), stop=stop.is_set
        )
    except Exception as exc:  # noqa: BLE001 — report the type, whose text can quote ids
        errors.append(f"{site}: stream ended, {type(exc).__name__}")


def _attach(grpc, site, number: int, user_id, recorder: Recorder, stop, errors: list) -> bytes:
    """Start a site's stream on a throwaway channel and return its trait snapshot."""
    identity = backend.stream_identity(
        site, device_uuid=str(uuid.uuid4()), serial=None, primary_panel=None
    )
    directive = backend.parse_ably_token(
        grpc.ably_token(field_string(1, identity.device_uuid)),
        fallback_channel=f"c:{user_id}:{identity.device_uuid}",
    )
    channel = directive.channel or f"c:{user_id}:{identity.device_uuid}"
    token = directive.token or cloud_ably.request_token(directive.token_request).token
    threading.Thread(
        target=_listen,
        args=(token, channel, f"site {number}", recorder, stop, errors),
        daemon=True,
    ).start()
    time.sleep(ATTACH_SECONDS)
    snapshot = grpc.subscribe_and_get_traits(
        backend.build_subscribe_request(channel, site.hardware_ids)
    )
    with recorder.lock:
        recorder.circuits.update(resource_circuits(snapshot))
    return snapshot


def capture(email: str, password: str, seconds: int, redact: Redactor) -> str:
    redact.text(email)
    tokens = cloud_auth.authenticate(email, password)
    user_id = cloud_auth.user_id_from_token(tokens.access_token)
    if user_id:
        redact.text(user_id)
    recorder = Recorder(redact)
    stop = threading.Event()
    errors: list[str] = []

    with cloud_grpc.CloudGrpcClient(tokens.access_token) as grpc:
        raw_sites = grpc.get_sites_for_user()
        sites = backend.pair_by_lookup(
            backend.parse_sites(raw_sites), grpc.get_resources_for_hardware
        )
        lines = ["== account ==", *account_lines(sites, redact)]
        lines += ["", "== GetSitesForUser ==", *dump(raw_sites, redact)]
        for number, site in enumerate(sites, 1):
            snapshot = _attach(grpc, site, number, user_id, recorder, stop, errors)
            lines += ["", f"== trait snapshot, site {number} ==", *snapshot_lines(snapshot, redact)]

        for step, phase in enumerate(PHASES, 1):
            input(f"\nStep {step} of {len(PHASES)}: {phase}. Press Enter once it is. ")
            with recorder.lock:
                recorder.phase = phase
            print(f"Recording for {seconds}s...")
            time.sleep(seconds)
            with recorder.lock:
                recorder.phase = None
        stop.set()

    with recorder.lock:
        lines += ["", f"== stream, {seconds}s per phase =="]
        lines += [
            f"{site}, {phase}: {count} frame(s)" for (site, phase), count in recorder.frames.items()
        ]
        lines += errors
        lines += ["", "== readings, median watts [low..high] ==", *recorder.tables(PHASES)]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Record how an account's panels report.")
    parser.add_argument("--seconds", type=int, default=DEFAULT_SECONDS, help="length of each step")
    parser.add_argument("--out", type=Path, default=Path("span-capture.txt"), help="report file")
    args = parser.parse_args()
    # The client libraries log ids at debug, and nothing here should reach a terminal
    # that gets pasted from.
    logging.disable(logging.INFO)

    email = input("SPAN email: ").strip()
    password = getpass.getpass("SPAN password: ")
    redact = Redactor()
    try:
        report = capture(email, password, args.seconds, redact)
        redact.assert_clean(report)
    except KeyboardInterrupt:
        print("\nStopped. No report written.")
        return 1
    except Exception as exc:  # noqa: BLE001 — the type only, whose text can quote ids
        print(f"Failed: {type(exc).__name__}. No report written.")
        return 1
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(report)
    print(f"Done: {args.out}. Read it before posting it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
