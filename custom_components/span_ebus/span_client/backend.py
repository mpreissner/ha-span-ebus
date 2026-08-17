"""Cloud backend — Cognito → gRPC → Ably realtime telemetry.

This is the live-data path for a MAIN 40 whose local API is not yet enabled
(SPAN says ~H2 2026). It is a first-class backend behind the same `Backend`
contract as `ebus`, so the bridge can flip between them without downstream
changes — the hybrid strategy in docs/CLOUD-FLOW.md.

Flow, on `start()` (see the sibling modules for the wire detail):

    cloud_auth.access_token_from_store  -> Bearer access token (auto-refreshed)
    cloud_grpc.get_sites_for_user       -> site / serial / hardware-id topology
    cloud_grpc.ably_token(device_uuid)  -> a signed Ably TokenRequest + channel
    cloud_ably.request_token            -> a usable Ably realtime token
    cloud_ably.stream_frames            -> base64-protobuf telemetry frames
    cloud_grpc.subscribe_and_get_traits -> registers the channel for publishing
    cloud_telemetry.decode_frame        -> circuits + site flows
    -> PanelSchema (synthesized on the first frame) + a stream of Readings

The `device_uuid` is a client-chosen identifier, not something SPAN issues: the
AblyToken RPC echoes whatever we send into the channel name and the token's
capability. What actually makes telemetry flow is `SubscribeAndGetTraits`, which
names our channel as the subscriber for a set of hardware resources and traits.
Skip it and the channel attaches but stays permanently silent.

The network loop runs on a daemon thread; `start()` returns immediately, matching
`EbusBackend`. The pure mapping functions (`schema_from_frame`,
`readings_from_frame`, `parse_ably_token`) are module-level and unit-tested.

Commands are not yet wired: writing a dispatch is a separate RPC surface
(ListDispatches/SetDispatch) that this read path does not cover, so
`send_command` logs and no-ops rather than pretending to act.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from . import cloud_ably, cloud_auth, cloud_grpc
from .cloud_pb import Message, field_message, field_string, field_varint, parse
from .cloud_telemetry import Frame, decode_frame
from .models import DataType, NodeKind, PanelSchema, PropertySpec, Reading

# Backend callbacks (previously defined on the daemon's backend Protocol).
ReadingCallback = Callable[[Reading], None]
SchemaCallback = Callable[[PanelSchema], None]

log = logging.getLogger(__name__)

# How long after attaching the SSE we fire SubscribeAndGetTraits. The iOS client
# registers only once its stream is up, and we mirror that order rather than
# assume the backend tolerates the reverse.
SUBSCRIBE_DELAY_SECONDS = 2.0

# The (vendor_id, trait_id) pairs to subscribe, exactly as the SPAN mobile client
# asks for them (recovered from an iOS capture). The publisher sends only traits
# a subscriber requested, so this set — not just the resource — decides what
# arrives. Vendor 1 is SPAN's trait namespace, 5 the Weave-derived common one;
# the trait ids are opaque and we carry them verbatim.
SUBSCRIBE_TRAITS = (
    (5, 1), (1, 36), (1, 17), (1, 15), (1, 16), (1, 2), (1, 33), (1, 47),
    (1, 28), (1, 1), (1, 13), (1, 26), (1, 21), (1, 12), (1, 14), (1, 11),
    (1, 31), (1, 32), (1, 35), (1, 18), (1, 38), (1, 41), (1, 19), (1, 39),
    (1, 5), (1, 6), (1, 4), (1, 40), (1, 20), (5, 3), (1, 42),
)

# Per-circuit properties we surface, with (datatype, unit) and the Channel attr
# they read. Units follow SPAN's native quantities after the decoder's ÷1000.
_CIRCUIT_PROPS = (
    ("power", DataType.FLOAT, "W", "power_w"),
    ("current", DataType.FLOAT, "A", "current_a"),
    ("voltage", DataType.FLOAT, "V", "voltage_v"),
)

# Site directional flows carry watts, except these which are volts / hertz.
_SITE_VOLT_FLOWS = {"voltage_l1", "voltage_l2"}
_SITE_HZ_FLOWS = {"frequency"}


@dataclass
class AblyDirective:
    """What the AblyToken RPC told us: how to obtain a token, and which channel."""

    token_request: dict | None  # a signed Ably TokenRequest to exchange
    token: str | None  # or a ready-to-use token, if the RPC returned one
    channel: str | None


# --- pure mapping -------------------------------------------------------------


def _circuit_node(instance_id: int) -> str:
    return f"circuit-{instance_id}"


def _fmt(value: float | None) -> str | None:
    return None if value is None else f"{value:.3f}"


def schema_from_frame(serial: str, frame: Frame) -> PanelSchema:
    """Synthesize a PanelSchema from one decoded frame.

    The cloud gives us `trait_instance_id`s, not friendly circuit labels (those
    come from the resource topology and are filled in later via
    `PanelSchema.resolve_node_names` if we learn them). Each circuit becomes a
    node with power/current/voltage properties; the panel total and the site
    directional flows get their own nodes.
    """
    schema = PanelSchema(serial=serial)

    for samples in frame.resources.values():
        for sample in samples:
            if sample.kind == "panel":
                node_id, kind = "panel", NodeKind.CORE
            else:
                node_id, kind = _circuit_node(sample.instance_id), NodeKind.CIRCUIT
            for prop_id, dtype, unit, _attr in _CIRCUIT_PROPS:
                schema.add(
                    PropertySpec(
                        node_id=node_id,
                        node_kind=kind,
                        property_id=prop_id,
                        name=prop_id,
                        datatype=dtype,
                        unit=unit,
                    )
                )

    for flow in frame.site_flows:
        unit = "V" if flow in _SITE_VOLT_FLOWS else "Hz" if flow in _SITE_HZ_FLOWS else "W"
        schema.add(
            PropertySpec(
                node_id="site",
                node_kind=NodeKind.POWER_FLOWS,
                property_id=flow,
                name=flow,
                datatype=DataType.FLOAT,
                unit=unit,
            )
        )

    return schema


def readings_from_frame(frame: Frame, timestamp: float | None = None) -> list[Reading]:
    """Flatten a decoded frame into Readings keyed `<node-id>/<property-id>`."""
    ts = time.time() if timestamp is None else timestamp
    out: list[Reading] = []

    for samples in frame.resources.values():
        for sample in samples:
            node_id = "panel" if sample.kind == "panel" else _circuit_node(sample.instance_id)
            channel = sample.combined
            if channel is None:
                continue
            for prop_id, _dtype, _unit, attr in _CIRCUIT_PROPS:
                val = _fmt(getattr(channel, attr))
                if val is not None:
                    out.append(Reading(key=f"{node_id}/{prop_id}", value=val, timestamp=ts))

    for flow, value in frame.site_flows.items():
        out.append(Reading(key=f"site/{flow}", value=_fmt(value), timestamp=ts))

    return out


def parse_ably_token(raw: bytes, *, fallback_channel: str | None = None) -> AblyDirective:
    """Interpret the AblyToken RPC response.

    The response carries, at field 1, either a signed Ably *TokenRequest* (a JSON
    object with `mac`/`nonce`/`keyName` to exchange) or an already-issued token
    string; field 2, when present, is the channel name. We accept both token
    shapes so a change in the RPC's contract degrades gracefully.
    """
    msg = parse(raw)
    field1 = msg.get_str(1)
    channel = msg.get_str(2) or fallback_channel

    token_request: dict | None = None
    token: str | None = None
    if field1:
        stripped = field1.lstrip()
        if stripped.startswith("{"):
            try:
                obj = json.loads(field1)
                if isinstance(obj, dict) and ("mac" in obj or "nonce" in obj):
                    token_request = obj
                elif isinstance(obj, dict) and "token" in obj:
                    token = obj["token"]
            except (json.JSONDecodeError, ValueError):
                token = field1
        else:
            token = field1

    return AblyDirective(token_request=token_request, token=token, channel=channel)


def _parse_sites_serial(raw: bytes) -> str | None:
    """Best-effort pull of a panel serial from a GetSitesForUser response.

    The message tree is deep and not fully pinned; we walk it defensively and
    return the first plausible serial string, or None if we can't find one.
    """
    if not raw:
        return None
    root = parse(raw)
    # Serials in the captured schema look like "XC-####-#####"; scan strings.
    for value in _walk_strings(root):
        if value.startswith("XC-") or value.startswith("xc-"):
            return value
    return None


def parse_sites_hardware_ids(raw: bytes) -> list[str]:
    """Pull the subscribable hardware ids from a GetSitesForUser response.

    These are the short ids (site and gateway, e.g. `a1b2c3d4e5f60718`) that
    `SubscribeAndGetTraits` keys its per-resource trait request on — *not* the
    resource UUIDs the topology also carries. Layout, by field number:

        1 sites_with_membership[] -> 3 membership[] -> 1 member[] -> 2 id

    Returned in encounter order, de-duplicated; empty if the shape is unfamiliar.
    """
    if not raw:
        return []
    out: list[str] = []
    try:
        for site in parse(raw).get_msgs(1):
            for group in site.get_msgs(3):
                for member in group.get_msgs(1):
                    value = member.get_str(2)
                    if value and value not in out:
                        out.append(value)
    except Exception as exc:  # noqa: BLE001 — a shape change must not break setup
        log.debug("could not walk GetSitesForUser for hardware ids: %s", exc)
    return out


def build_subscribe_request(channel: str, hardware_ids: Iterable[str]) -> bytes:
    """Serialize a SubscribeAndGetTraitsRequest for `channel`.

        SubscribeAndGetTraitsRequest {
          1 subscriber_resource_id: ResourceId { 1 id }
          2 resources[]: ResourceIdWithTraitMetadata {
              1 resource_id: ResourceId { 1 id }
              2 traits[]:    TraitMetadata { 1 vendor_id, 3 trait_id }
            }
        }

    The subscriber is identified by the *Ably channel name* we picked, which is
    why a locally generated device UUID works — this call is the registration.
    """
    traits = b"".join(
        field_message(2, field_varint(1, vendor) + field_varint(3, trait))
        for vendor, trait in SUBSCRIBE_TRAITS
    )
    body = field_message(1, field_string(1, channel))
    for hardware_id in hardware_ids:
        body += field_message(
            2, field_message(1, field_string(1, hardware_id)) + traits
        )
    return body


def _walk_strings(msg: Message, depth: int = 0):
    """Yield every UTF-8-decodable leaf string in a message tree (bounded depth)."""
    if depth > 8:
        return
    for values in msg.fields.values():
        for wire, value in values:
            if not isinstance(value, bytes):
                continue
            try:
                text = value.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is not None and text.isprintable() and text:
                yield text
            try:
                yield from _walk_strings(parse(value), depth + 1)
            except Exception:
                continue


# --- backend ------------------------------------------------------------------


class CloudBackend:
    """Streams live panel telemetry from SPAN's cloud."""

    name = "cloud"

    def __init__(
        self,
        token_store: Path,
        device_uuid: str,
        *,
        user_id: str | None = None,
        serial: str | None = None,
        host: str = cloud_grpc.DEFAULT_HOST,
        key_name: str = cloud_ably.DEFAULT_KEY_NAME,
        reconnect_seconds: float = 5.0,
    ) -> None:
        self._token_store = Path(token_store)
        self._device_uuid = device_uuid
        self._user_id = user_id
        self._serial = serial
        self._host = host
        self._key_name = key_name
        self._reconnect_seconds = reconnect_seconds
        self._hardware_ids: list[str] = []

        self._on_schema: SchemaCallback | None = None
        self._on_reading: ReadingCallback | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._schema_sent = False

    # --- lifecycle ---------------------------------------------------------

    def start(self, on_schema: SchemaCallback, on_reading: ReadingCallback) -> None:
        self._on_schema = on_schema
        self._on_reading = on_reading
        self._stop.clear()
        self._schema_sent = False
        self._thread = threading.Thread(
            target=self._run, name="span-cloud", daemon=True
        )
        self._thread.start()

    def stop(self, join_timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread:
            # The stream thread only notices `_stop` on its next SSE event, so a
            # silent channel can outlast the join. It is a daemon and exits on
            # the next event or transport error; callers that cannot wait (the
            # config-flow probe) pass a short timeout.
            self._thread.join(timeout=join_timeout)
            self._thread = None
            log.info("cloud backend stopped")

    def probe(self, timeout: float = 45.0) -> PanelSchema:
        """Bootstrap, subscribe, and return the first content-bearing schema.

        A one-shot connectivity check for the config flow. Auth and topology
        problems surface as their own exceptions; `TimeoutError` means the
        channel attached and the subscribe was accepted, but no content-bearing
        frame followed. Allow generous time: the first frames after a subscribe
        are often lean energy/interval ones carrying no circuits.
        """
        # Run it once up front so a bad token or missing channel raises here
        # rather than being swallowed by the stream thread's retry loop.
        self.bootstrap()

        captured: list[PanelSchema] = []
        got = threading.Event()

        def on_schema(schema: PanelSchema) -> None:
            captured.append(schema)
            got.set()

        self.start(on_schema, lambda _reading: None)
        try:
            if not got.wait(timeout):
                raise TimeoutError(
                    f"no telemetry arrived on the Ably channel within {timeout:.0f}s"
                )
        finally:
            self.stop(join_timeout=1.0)
        return captured[0]

    def send_command(self, key: str, value: str) -> None:
        # Writing a dispatch is a separate RPC surface not covered by this read
        # path; in the hybrid setup, route commands through the ebus backend.
        log.warning("cloud backend is read-only; ignoring command %s -> %s", key, value)

    # --- network loop ------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                token, channel = self.bootstrap()
                log.info("attaching to Ably channel %s", channel)
                # Registration is per-connection as far as we can tell, so it is
                # re-issued on every (re)attach, from a helper thread because
                # stream_frames blocks for the life of the stream.
                self._schedule_subscribe(channel)
                cloud_ably.stream_frames(
                    token,
                    channel,
                    self._handle_frame,
                    stop=self._stop.is_set,
                )
            except Exception as exc:  # noqa: BLE001 — keep the daemon alive
                if self._stop.is_set():
                    return
                log.error(
                    "cloud stream error (%s); reconnecting in %.0fs",
                    exc,
                    self._reconnect_seconds,
                )
            if self._stop.is_set():
                return
            # Wait, but wake immediately on stop.
            self._stop.wait(self._reconnect_seconds)

    def bootstrap(self) -> tuple[str, str]:
        """Authenticate, resolve topology, and obtain an Ably token + channel."""
        access_token = cloud_auth.access_token_from_store(self._token_store)
        with cloud_grpc.CloudGrpcClient(access_token, host=self._host) as grpc:
            if self._serial is None or not self._hardware_ids:
                sites = grpc.get_sites_for_user()
                if self._serial is None:
                    self._serial = _parse_sites_serial(sites)
                    if self._serial:
                        log.info(
                            "resolved panel serial %s from GetSitesForUser", self._serial
                        )
                if not self._hardware_ids:
                    self._hardware_ids = parse_sites_hardware_ids(sites)
                    log.debug("subscribable hardware ids: %s", self._hardware_ids)

            request = field_string(1, self._device_uuid)
            directive = parse_ably_token(
                grpc.ably_token(request), fallback_channel=self._default_channel()
            )

        channel = directive.channel or self._default_channel()
        if channel is None:
            raise cloud_ably.AblyError(
                "AblyToken gave no channel and no user_id is configured to build one"
            )

        if directive.token:
            return directive.token, channel
        if directive.token_request:
            details = cloud_ably.request_token(
                directive.token_request, key_name=self._key_name
            )
            return details.token, channel
        raise cloud_ably.AblyError("AblyToken response had neither a token nor a TokenRequest")

    def _schedule_subscribe(self, channel: str) -> None:
        """Fire `subscribe` shortly after the caller starts reading the stream."""

        def worker() -> None:
            if self._stop.wait(SUBSCRIBE_DELAY_SECONDS):
                return
            try:
                self.subscribe(channel)
            except Exception as exc:  # noqa: BLE001 — the stream retry owns recovery
                if not self._stop.is_set():
                    log.error(
                        "SubscribeAndGetTraits failed (%s); the channel will stay "
                        "silent until the next reconnect",
                        exc,
                    )

        threading.Thread(
            target=worker, name="span-cloud-subscribe", daemon=True
        ).start()

    def subscribe(self, channel: str) -> bytes:
        """Register `channel` as a telemetry subscriber; return the trait snapshot.

        Without this SPAN publishes nothing, so a failure here is the difference
        between live entities and an empty integration.
        """
        if not self._hardware_ids:
            raise cloud_grpc.GrpcError(
                5,
                "GetSitesForUser returned no subscribable hardware ids",
                "SubscribeAndGetTraits",
            )
        access_token = cloud_auth.access_token_from_store(self._token_store)
        request = build_subscribe_request(channel, self._hardware_ids)
        with cloud_grpc.CloudGrpcClient(access_token, host=self._host) as grpc:
            snapshot = grpc.subscribe_and_get_traits(request)
        log.info(
            "subscribed %d resource(s) for telemetry on %s (%d-byte trait snapshot)",
            len(self._hardware_ids),
            channel,
            len(snapshot),
        )
        return snapshot

    def _default_channel(self) -> str | None:
        if not self._user_id:
            return None
        return f"c:{self._user_id}:{self._device_uuid}"

    def _handle_frame(self, raw: bytes) -> None:
        try:
            frame = decode_frame(raw)
        except Exception as exc:  # noqa: BLE001 — a bad frame must not kill the stream
            log.debug("undecodable telemetry frame (%d bytes): %s", len(raw), exc)
            return

        # The stream interleaves power frames (with circuits) and lean energy/
        # interval frames (empty of resources+flows). Hold the schema until a
        # content-bearing frame so we don't publish an empty topology.
        if (
            not self._schema_sent
            and self._on_schema is not None
            and (frame.resources or frame.site_flows)
        ):
            serial = self._serial or "span-cloud"
            self._on_schema(schema_from_frame(serial, frame))
            self._schema_sent = True

        if self._on_reading is not None:
            ts = frame.epoch_millis / 1000.0 if frame.epoch_millis else None
            for reading in readings_from_frame(frame, ts):
                self._on_reading(reading)
