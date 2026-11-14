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
    cloud_traits.parse_trait_snapshot   -> circuit labels, breakers, panel spaces
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

One command is wired: a circuit's `relay`. Writes go out on a single RPC,
`SendMessages`, as a trait command addressed to the breaker's switch trait — see
`cloud_commands` and docs/specs/circuit-control.md. Relay *state* is not in the
telemetry stream at all; it lives in the subscribe snapshot, so the snapshot is
re-read on a timer and again shortly after a command.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import cloud_ably, cloud_auth, cloud_commands, cloud_grpc, cloud_history
from .cloud_pb import Message, ProtoError, field_message, field_string, field_varint, parse
from .cloud_telemetry import CircuitSample, Frame, decode_frame
from .cloud_traits import (
    FEEDER_FEEDTHROUGH,
    FEEDER_MAIN_BREAKER,
    CircuitInfo,
    parse_feeder_kinds,
    parse_trait_snapshot,
)
from .models import DataType, EnergySample, NodeKind, PanelSchema, PropertySpec, Reading
from .overhead import overhead_w

# Backend callbacks (previously defined on the daemon's backend Protocol).
ReadingCallback = Callable[[Reading], None]
SchemaCallback = Callable[[PanelSchema], None]
# Called, at most once per backend, when the stored credentials are rejected and
# only the user can fix it. The argument is the reason, for the host to show.
AuthFailedCallback = Callable[[str], None]

log = logging.getLogger(__name__)

# How long after attaching the SSE we fire SubscribeAndGetTraits. The iOS client
# registers only once its stream is up, and we mirror that order rather than
# assume the backend tolerates the reverse.
SUBSCRIBE_DELAY_SECONDS = 2.0

# How long to hold the schema back waiting for the subscribe snapshot's circuit
# labels. Entity names are fixed at creation, so a couple of seconds is worth
# spending to name circuits "Cooktop" rather than "Circuit 30". The snapshot
# normally lands before the first frame — SPAN publishes nothing until the
# subscribe is accepted — so this only covers the race.
SCHEMA_LABEL_WAIT_SECONDS = 5.0

# The (vendor_id, trait_id) pairs to subscribe, exactly as the SPAN mobile client
# asks for them (recovered from an iOS capture). The publisher sends only traits
# a subscriber requested, so this set — not just the resource — decides what
# arrives. Vendor 1 is SPAN's trait namespace, 5 the Weave-derived common one;
# the trait ids are opaque and we carry them verbatim.
SUBSCRIBE_TRAITS = (
    (5, 1),
    (1, 36),
    (1, 17),
    (1, 15),
    (1, 16),
    (1, 2),
    (1, 33),
    (1, 47),
    (1, 28),
    (1, 1),
    (1, 13),
    (1, 26),
    (1, 21),
    (1, 12),
    (1, 14),
    (1, 11),
    (1, 31),
    (1, 32),
    (1, 35),
    (1, 18),
    (1, 38),
    (1, 41),
    (1, 19),
    (1, 39),
    (1, 5),
    (1, 6),
    (1, 4),
    (1, 40),
    (1, 20),
    (5, 3),
    (1, 42),
)

# Properties per sample kind, as (property_id, unit, CircuitSample attribute
# holding the channel, Channel attribute holding the value). Units follow SPAN's
# native quantities after the decoder's ÷1000.
#
# The split matters because Home Assistant creates one entity per property: a
# property the wire never carries becomes a permanently unavailable sensor. What
# each kind actually reports was established against live frames (see
# cloud_telemetry's module docstring).
_PROPS_BY_KIND: dict[str, tuple[tuple[str, str, str, str], ...]] = {
    # 120 V branches meter current and power; their voltage slot is never sent.
    "two_wire": (
        ("power", "W", "combined", "power_w"),
        ("current", "A", "combined", "current_a"),
    ),
    # 240 V circuits also meter each leg, but a leg voltage there is just another
    # reading of the panel's own busbars, so the panel node carries those.
    "three_wire": (
        ("power", "W", "combined", "power_w"),
        ("current", "A", "combined", "current_a"),
    ),
    # The panel is the one node with trustworthy voltage and frequency. Its
    # `combined` voltage is skipped deliberately: it drifts frame-wide against
    # the leg voltages, which are steady.
    #
    # `current` is the *combined* channel's, which the wire reports as the larger
    # of the two legs rather than their sum — busbar loading, the figure to
    # compare against the main breaker. The per-leg currents are what shows
    # whether the panel is balanced, so both are published.
    "panel": (
        ("power", "W", "combined", "power_w"),
        ("current", "A", "combined", "current_a"),
        ("current_l1", "A", "line_an", "current_a"),
        ("current_l2", "A", "line_bn", "current_a"),
        ("voltage_l1", "V", "line_an", "voltage_v"),
        ("voltage_l2", "V", "line_bn", "voltage_v"),
        ("frequency", "Hz", "combined", "freq_hz"),
    ),
}

# Site directional flows carry watts, except these which are volts / hertz.
_SITE_VOLT_FLOWS = {"voltage_l1", "voltage_l2"}
_SITE_HZ_FLOWS = {"frequency"}

# The one settable property. Its vocabulary is the ebus/Homie path's: CLOSED is
# energized, and `discovery.component_for` already turns a settable `relay` into
# an HA switch with those payloads, so nothing downstream needs to learn a new
# word. UNKNOWN is a state the panel reports, never a command.
RELAY_PROPERTY = "relay"
RELAY_STATES = ("OPEN", "CLOSED", "UNKNOWN")

# The property a node's energy is the energy *of* — see `energy_samples`.
POWER_PROPERTY = "power"

# Relay state is absent from telemetry frames, so it is only as fresh as the last
# trait snapshot. Re-read it on this cadence to pick up changes made in the SPAN
# app or by the panel's own load management.
SWITCH_REFRESH_SECONDS = 60.0

# How long the channel may go without a telemetry frame before we tear the
# stream down and start over. This counts *any* decodable frame, not just the
# ones carrying readings, and that distinction is what keeps the budget honest: a
# panel on cellular backup publishes readings only once a minute (CLOUD-FLOW.md
# §6), but its lean interval frames still arrive every few seconds — longest gap
# observed in that degraded state was ~40s, comfortably inside this window. So
# this is not for a slow panel but for the failure where the socket stays
# healthy and Ably keeps sending keepalives while SPAN has simply stopped
# publishing — a lapsed SubscribeAndGetTraits registration, typically. Nothing
# about that state resolves on its own, and without a watchdog the entities sit
# on their last values indefinitely, which reads as a panel at constant load
# rather than as an outage.
FRAME_SILENCE_SECONDS = 90.0

# A reattach that carried no telemetry doubles the wait, up to this ceiling. The
# drop we actually see in the field is Ably resetting a long-lived SSE socket,
# which the very next attach fixes — so the base wait stays short and only a run
# of dead attaches backs off. The ceiling still cuts a cloud-side outage from
# twelve full re-bootstraps a minute to one, but it has to stay *under* the
# coordinator's STALE_AFTER_SECONDS: a ceiling longer than that means the panel
# can be back while the entities are still unavailable, waiting on a timer.
RECONNECT_BACKOFF_MAX_SECONDS = 60.0

# Consecutive dead attaches before the reconnect is worth an ERROR. Below this it
# is an ordinary drop and logs at INFO: an outage should read as one loud line in
# the Home Assistant log, not as several hundred identical ones.
LOUD_AFTER_ATTEMPTS = 3

# And again this soon after we send a command, so a relay the panel refused
# (ALWAYS_ON_CIRCUIT, MINIMUM_RECONNECT_TIME) converges back to the truth
# instead of sitting on our optimistic guess for a full refresh interval.
POST_COMMAND_REFRESH_SECONDS = 3.0

# Accepted command spellings. HA sends CLOSED/OPEN via MQTT discovery, but the
# integration's switch platform and hand-written automations both reach for
# booleans, and rejecting those would be a confusing failure.
_RELAY_CLOSE_WORDS = {"closed", "close", "true", "on", "1"}
_RELAY_OPEN_WORDS = {"open", "false", "off", "0"}


@dataclass
class AblyDirective:
    """What the AblyToken RPC told us: how to obtain a token, and which channel."""

    token_request: dict | None  # a signed Ably TokenRequest to exchange
    token: str | None  # or a ready-to-use token, if the RPC returned one
    channel: str | None


# The node the site's aggregate flows are published under. Not a circuit and not
# a resource of the panel's: one node carrying the whole site's directional
# power, and — via GetHistoryAggregation — the whole site's energy.
SITE_NODE = "site"


# --- pure mapping -------------------------------------------------------------


def _circuit_node(instance_id: int) -> str:
    return f"circuit-{instance_id}"


def _fmt(value: float | None) -> str | None:
    return None if value is None else f"{value:.3f}"


def relay_state(info: CircuitInfo) -> str:
    """The circuit's relay as a Homie enum value."""
    if info.relay_closed is None:
        return "UNKNOWN"
    return "CLOSED" if info.relay_closed else "OPEN"


def parse_relay_command(value: str) -> bool | None:
    """Interpret a relay command as "should the relay end up closed?".

    Returns None for anything unrecognized, which the caller drops rather than
    guessing at — the wrong guess opens a breaker.
    """
    word = str(value).strip().lower()
    if word in _RELAY_CLOSE_WORDS:
        return True
    if word in _RELAY_OPEN_WORDS:
        return False
    return None


def _switchable(sample: CircuitSample, circuits: dict[int, CircuitInfo]) -> CircuitInfo | None:
    """The CircuitInfo for `sample` if — and only if — we can address its relay."""
    if sample.kind == "panel":
        return None
    info = circuits.get(sample.instance_id)
    return info if info is not None and info.switch is not None else None


def _node_for(sample: CircuitSample, circuits: dict[int, CircuitInfo]) -> tuple[str, NodeKind]:
    """Stable node id and kind for one telemetry sample.

    Node ids stay keyed on the instance id even once we know a circuit's label,
    so renaming a circuit in the SPAN app does not orphan its entity history.
    The label rides along as the node's display name instead.
    """
    if sample.kind == "panel":
        return "panel", NodeKind.CORE
    if circuits and sample.instance_id not in circuits:
        # We have the panel's circuit list and this instance is not on it, so it
        # is not a branch circuit. On a MAIN 40 the single such sample is the
        # main feed, whose power tracks the panel total to within one sampling
        # window.
        return f"feed-{sample.instance_id}", NodeKind.LUGS
    return _circuit_node(sample.instance_id), NodeKind.CIRCUIT


def _node_names(frame: Frame, circuits: dict[int, CircuitInfo]) -> dict[str, str]:
    """Display name per node id, from the trait snapshot where we have one.

    Labels are the user's own and need not be unique — two 20 A circuits can
    both be "Outlets" — so a repeated label is qualified with the panel spaces
    the circuit occupies, which are unique by construction.
    """
    names: dict[str, str] = {}
    labels: dict[int, str] = {}
    seen: dict[str, int] = {}

    for samples in frame.resources.values():
        for sample in samples:
            info = circuits.get(sample.instance_id)
            if sample.kind == "panel" or info is None:
                continue
            label = info.display_name
            labels[sample.instance_id] = label
            seen[label] = seen.get(label, 0) + 1

    for samples in frame.resources.values():
        for sample in samples:
            node_id, _kind = _node_for(sample, circuits)
            if sample.kind == "panel":
                names[node_id] = "Panel"
            elif node_id.startswith("feed-"):
                names[node_id] = "Main feed"
            elif sample.instance_id in labels:
                label = labels[sample.instance_id]
                info = circuits[sample.instance_id]
                if seen.get(label, 0) > 1 and info.spaces:
                    spaces = ", ".join(str(s) for s in info.spaces)
                    label = f"{label} ({spaces})"
                names[node_id] = label
    return names


def schema_from_frame(
    serial: str,
    frame: Frame,
    circuits: dict[int, CircuitInfo] | None = None,
    feeders: Mapping[int, int] | None = None,
) -> PanelSchema:
    """Synthesize a PanelSchema from one decoded frame.

    Telemetry frames identify circuits only by `trait_instance_id`. Those are
    internal identifiers with no relation to panel spaces — a 40-space MAIN 40
    reports instances up to 56 — so `circuits`, parsed from the subscribe
    snapshot, supplies the user's labels, breaker ratings and real space numbers.
    Without it nodes fall back to instance-id names. `feeders`, from the same
    snapshot, tags each feed with its feeder kind where trait 1/21 gives one.

    Properties are chosen per sample kind so every entity we advertise is one the
    wire actually feeds; the site directional flows get their own node.
    """
    circuits = circuits or {}
    feeders = feeders or {}
    schema = PanelSchema(serial=serial)
    names = _node_names(frame, circuits)

    for samples in frame.resources.values():
        for sample in samples:
            node_id, kind = _node_for(sample, circuits)
            feeder_kind = feeders.get(sample.instance_id) if kind is NodeKind.LUGS else None
            for prop_id, unit, _source, _attr in _PROPS_BY_KIND.get(sample.kind, ()):
                schema.add(
                    PropertySpec(
                        node_id=node_id,
                        node_kind=kind,
                        property_id=prop_id,
                        name=prop_id,
                        datatype=DataType.FLOAT,
                        unit=unit,
                        node_name=names.get(node_id),
                        feeder_kind=feeder_kind,
                    )
                )
            # Only circuits whose switch trait the snapshot resolved get a relay,
            # so an unreadable snapshot leaves a working read-only panel rather
            # than switches that fail the moment anyone touches them.
            if _switchable(sample, circuits) is not None:
                schema.add(
                    PropertySpec(
                        node_id=node_id,
                        node_kind=kind,
                        property_id=RELAY_PROPERTY,
                        name=RELAY_PROPERTY,
                        datatype=DataType.ENUM,
                        settable=True,
                        enum_values=RELAY_STATES,
                        node_name=names.get(node_id),
                    )
                )

    for flow in frame.site_flows:
        unit = "V" if flow in _SITE_VOLT_FLOWS else "Hz" if flow in _SITE_HZ_FLOWS else "W"
        schema.add(
            PropertySpec(
                node_id=SITE_NODE,
                node_kind=NodeKind.POWER_FLOWS,
                property_id=flow,
                name=flow,
                node_name="Site",
                datatype=DataType.FLOAT,
                unit=unit,
            )
        )

    return schema


def readings_from_frame(
    frame: Frame,
    timestamp: float | None = None,
    circuits: dict[int, CircuitInfo] | None = None,
) -> list[Reading]:
    """Flatten a decoded frame into Readings keyed `<node-id>/<property-id>`.

    `circuits` must match what `schema_from_frame` was given, so reading keys
    line up with the advertised properties.
    """
    ts = time.time() if timestamp is None else timestamp
    circuits = circuits or {}
    out: list[Reading] = []

    for samples in frame.resources.values():
        for sample in samples:
            node_id, _kind = _node_for(sample, circuits)
            for prop_id, _unit, source, attr in _PROPS_BY_KIND.get(sample.kind, ()):
                channel = getattr(sample, source, None)
                if channel is None:
                    continue
                val = _fmt(getattr(channel, attr))
                if val is not None:
                    out.append(Reading(key=f"{node_id}/{prop_id}", value=val, timestamp=ts))
            # Relay state comes from the trait snapshot, not the frame; it rides
            # along on frames only so the value keeps getting republished.
            info = _switchable(sample, circuits)
            if info is not None:
                out.append(
                    Reading(
                        key=f"{node_id}/{RELAY_PROPERTY}",
                        value=relay_state(info),
                        timestamp=ts,
                    )
                )

    for flow, value in frame.site_flows.items():
        out.append(Reading(key=f"{SITE_NODE}/{flow}", value=_fmt(value), timestamp=ts))

    return out


def energy_targets(
    frame: Frame, circuits: dict[int, CircuitInfo] | None = None
) -> dict[str, dict[int, str]]:
    """Which metric instances to ask SPAN for energy on: resource -> instance -> node.

    Built from a realtime frame because that is where the identifiers are: the
    metering instance ids are the same ones the frame publishes power under, and
    the site's own instance is named in the frame envelope and nowhere else.

    Only nodes SPAN actually meters are listed. The panel's own metering block
    and the main feed publish power but return nothing from the history RPC — an
    unmetered instance is silently absent from the response rather than an error
    — so they are left out here instead of asked for and quietly dropped.
    """
    circuits = circuits or {}
    targets: dict[str, dict[int, str]] = {}
    for resource_id, samples in frame.resources.items():
        for sample in samples:
            node_id, kind = _node_for(sample, circuits)
            if kind is NodeKind.CIRCUIT:
                targets.setdefault(resource_id, {})[sample.instance_id] = node_id
    if frame.site_id and frame.site_instance_id and frame.site_flows:
        targets.setdefault(frame.site_id, {})[frame.site_instance_id] = SITE_NODE
    return targets


def is_main_feed_power(spec: PropertySpec) -> bool:
    """Whether this is the main feed's power reading, the total overhead is taken from."""
    return spec.node_kind is NodeKind.LUGS and spec.property_id == POWER_PROPERTY


def is_overhead_feed(spec: PropertySpec, schema: PanelSchema | None) -> bool:
    """Whether `spec` is the feed a panel's overhead pair is built on.

    The main feed, or a panel's only feed whatever its kind. A feed tagged as
    feedthrough lugs is the downstream panel's only when this panel has another
    feed; alone, the tag more likely means the panel itself is fed through lugs
    (a downstream panel), and its feed is still all it takes in.
    """
    if not is_main_feed_power(spec):
        return False
    if spec.feeder_kind != FEEDER_FEEDTHROUGH:
        return True
    others = [] if schema is None else [
        other
        for other in schema.properties.values()
        if is_main_feed_power(other) and other.key != spec.key
    ]  # fmt: skip
    if others:
        return False
    log.debug("A lone feed is tagged as feedthrough lugs; treating it as the main feed")
    return True


def panel_overhead(schema: PanelSchema, readings: Mapping[str, Reading]) -> float | None:
    """Watts entering the panel that no branch circuit or downstream panel accounts for.

    The main feed (`feed-*/power`) is everything through the main lugs; the
    branch meters are everything leaving on a breaker, and a feedthrough feed is
    everything leaving for a downstream panel. What separates them is the
    panel's own draw — its electronics and relays — which is a steady load and
    never negative.

    `panel/power` meters the same intake, but it is sampled a frame ahead of the
    branch meters, so against them it swings by hundreds of watts whenever a load
    changes. The main feed is sampled alongside the circuits: on a live MAIN 40
    the difference held at about 12 W (σ ≈ 4 W) where the panel meter's ranged
    from −420 to +320 W. The result is still floored at zero, since the meters
    are not reconciled and a negative figure could only be their disagreement.

    Which feed is which comes from trait 1/21's feeder kind. A single feed is
    the main feed, with nothing leaving downstream, whatever its kind: a panel
    fed through lugs may well tag its only feed as one (see
    `is_overhead_feed`). Several feeds need every one classified — one main
    breaker and at most one feedthrough — or there is no telling them apart, and
    the result is None.

    None until the feeds and *every* circuit have reported, so a circuit that
    has not landed yet cannot masquerade as overhead of its whole draw.
    """
    feeds = [spec for spec in schema.properties.values() if is_main_feed_power(spec)]
    keys = [
        spec.key
        for spec in schema.properties.values()
        if spec.node_kind is NodeKind.CIRCUIT and spec.property_id == POWER_PROPERTY
    ]
    if not keys:
        return None
    if len(feeds) == 1:
        main, through = feeds[0], None
    else:
        mains = [f for f in feeds if f.feeder_kind == FEEDER_MAIN_BREAKER]
        throughs = [f for f in feeds if f.feeder_kind == FEEDER_FEEDTHROUGH]
        if len(mains) != 1 or len(throughs) > 1 or len(mains) + len(throughs) != len(feeds):
            return None
        main, through = mains[0], (throughs[0] if throughs else None)
    try:
        feed_w = float(readings[main.key].value)
        circuits_w = [float(readings[key].value) for key in keys]
        through_w = float(readings[through.key].value) if through is not None else None
    except (KeyError, TypeError, ValueError):
        return None
    return overhead_w(feed_w, circuits_w, through_w, through is not None)


def energy_samples(
    series: Iterable[cloud_history.Series],
    targets: dict[str, dict[int, str]],
    flows: Iterable[str] = (),
) -> list[EnergySample]:
    """Turn history buckets into per-property energy, keyed like the power readings.

    A circuit reports `import` and `export`; the sample carries the import,
    which is what a consumption sensor means. The site reports eleven
    directional components, mapped back onto the flow names the realtime channel
    uses by `SITE_FLOW_ENERGY`, and narrowed to `flows` — the flows this panel
    actually publishes — so a battery-less site does not accumulate a ledger of
    zeroes.

    A bucket carrying none of the components asked for yields nothing at all: an
    idle circuit is simply absent from the response, and inventing a zero for it
    would be inventing a measurement.
    """
    wanted = {
        flow: components
        for flow, components in cloud_history.SITE_FLOW_ENERGY.items()
        if flow in set(flows)
    }
    out: list[EnergySample] = []
    for one in series:
        node_id = targets.get(one.resource_id, {}).get(one.instance_id)
        if node_id is None:
            continue
        for bucket in one.buckets:
            if node_id == SITE_NODE:
                for flow, components in wanted.items():
                    value = bucket.total(components)
                    if value is not None:
                        out.append(
                            EnergySample(
                                key=f"{SITE_NODE}/{flow}",
                                start_ms=bucket.start_ms,
                                end_ms=bucket.end_ms,
                                kwh=value,
                            )
                        )
                continue
            value = bucket.total((cloud_history.IMPORT,))
            if value is not None:
                out.append(
                    EnergySample(
                        key=f"{node_id}/{POWER_PROPERTY}",
                        start_ms=bucket.start_ms,
                        end_ms=bucket.end_ms,
                        kwh=value,
                    )
                )
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
        if value.startswith(("XC-", "xc-")):
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
        log.debug("could not walk GetSitesForUser for hardware ids: %s", type(exc).__name__)
    return out


# Panelboard model codes seen in GetSitesForUser. 1-02100-04 is documented in
# CLOUD-FLOW.md; 1-04100-01 was observed on an account whose second site is a
# MAIN 16 fed from a MAIN 40. Unknown codes fall back to a generic name.
PANEL_MODELS = {
    "1-02100-04": "MAIN 40",
    "1-04100-01": "MAIN 16",
}


# Member kinds in GetSitesForUser: 4 site, 5 gateway, 6 bess. A panel's circuits
# live on the resource named by its gateway's id.
_KIND_GATEWAY = 5


def same_serial(a: str | None, b: str | None) -> bool:
    """Whether two spellings name the same panel serial.

    The panel reports its serial in lowercase and SPAN's cloud in uppercase.
    """
    if not a or not b:
        return False
    return a.strip().casefold() == b.strip().casefold()


def serial_taken(
    unique_ids: Iterable[str | None],
    local_serials: Iterable[str | None],
    names: Iterable[str | None],
) -> bool:
    """Whether another entry already runs a panel known by any of `names`.

    An entry runs it under its unique id, or (a cloud account entry given its
    panel keeps the account's unique id) under the local serial in its data.
    `names` are the spellings the panel goes by: its local serial and its key.
    """
    wanted = [name for name in names if name]
    return any(same_serial(held, name) for held in (*unique_ids, *local_serials) for name in wanted)


@dataclass(frozen=True)
class PanelInfo:
    """One panelboard from GetSitesForUser."""

    site_id: str
    serial: str | None
    model_code: str | None
    hardware_id: str | None  # the frame and trait resource id, once paired
    stored_key: str | None = None  # the key already recorded for it, which holds

    @property
    def model(self) -> str:
        return PANEL_MODELS.get(self.model_code or "", f"Panel {self.model_code or ''}".strip())

    @property
    def key(self) -> str:
        """What this panel's energy store and stored choices are keyed by."""
        return self.stored_key or self.serial or self.hardware_id or self.site_id

    def answers_to(self, key: str | None) -> bool:
        """Whether a recorded key means this panel.

        A key recorded before the serial was known is the hardware id, and the
        panel is still the same panel once SPAN reports the serial.

        A serial matches whatever its case: the panel's own API spells it in
        lowercase and SPAN's cloud in uppercase. Hardware and site ids are exact.
        """
        if key is None:
            return False
        return key in (self.key, self.hardware_id) or same_serial(key, self.serial)


@dataclass(frozen=True)
class SiteInfo:
    """One site ("house" in the SPAN app) from GetSitesForUser.

    SPAN can put each panel of a multi-panel install on its own site, so the
    account's topology is a list of these, not one panel. Layout, by field:

        1 sites[] {
            1 { 1 site_id  2 label ... }
            3 { 1 member[] { 1 kind  2 hardware_id ... }
                2 panelboard[] { 4 { 3 serial  5 model_code }
                                 8 component[] { 7 { 1 hardware_id } } } }
        }

    Every site has at least one panel. `name` is often a street address, so it
    stays out of logs and device names.
    """

    site_id: str
    hardware_ids: tuple[str, ...]
    panels: tuple[PanelInfo, ...]
    gateways: tuple[str, ...] = ()
    name: str | None = field(default=None, repr=False)

    @property
    def lead(self) -> PanelInfo:
        """The panel a site's single stream is named after."""
        return next((p for p in self.panels if p.serial), self.panels[0])

    @property
    def serial(self) -> str | None:
        return self.lead.serial

    @property
    def model(self) -> str:
        return self.lead.model


def _gateway_ids(group: Message) -> list[str]:
    ids = (m.get_str(2) for m in group.get_msgs(1) if m.get_uint(1) == _KIND_GATEWAY)
    return [value for value in ids if value]


def _component_ids(board: Message) -> set[str]:
    linked = (c.get_msg_opt(7) for c in board.get_msgs(8))
    return {value for link in linked if link is not None and (value := link.get_str(1))}


def _unique_claims(claims: dict[int, str]) -> dict[int, str]:
    """Drop any gateway two boards claim: at most one is right, and nothing says which."""
    counts = Counter(claims.values())
    return {board: gateway for board, gateway in claims.items() if counts[gateway] == 1}


def _pair_boards(groups: list[Message], gateways: list[str]) -> dict[int, str]:
    """Pair boards, by position across the site, with their gateways.

    Response order is never used: a wrong guess swaps two panels' circuits while
    looking correct, so a board with no evidence stays unpaired.
    """
    placed = [(no, board) for no, group in enumerate(groups) for board in group.get_msgs(2)]
    if len(placed) == 1 and len(gateways) == 1:
        return {0: gateways[0]}

    claims: dict[int, str] = {}
    for position, (_, board) in enumerate(placed):
        linked = _component_ids(board) & set(gateways)
        if len(linked) == 1:
            claims[position] = next(iter(linked))
    paired = _unique_claims(claims)

    for no, group in enumerate(groups):
        boards = [position for position, (home, _) in enumerate(placed) if home == no]
        ids = _gateway_ids(group)
        lone = len(boards) == 1 and len(ids) == 1
        if lone and boards[0] not in paired and ids[0] not in paired.values():
            paired[boards[0]] = ids[0]
    return paired


def _parse_site(site: Message) -> SiteInfo | None:
    head = site.get_msg_opt(1)
    groups = site.get_msgs(3)
    hardware_ids: list[str] = []
    gateways: list[str] = []
    for group in groups:
        for member in group.get_msgs(1):
            value = member.get_str(2)
            if value and value not in hardware_ids:
                hardware_ids.append(value)
        for value in _gateway_ids(group):
            if value not in gateways:
                gateways.append(value)
    if not hardware_ids:
        return None
    # A second kind 4 member must not read as a second site, so the id comes only
    # from the head.
    site_id = (head.get_str(1) if head is not None else None) or hardware_ids[0]

    paired = _pair_boards(groups, gateways)
    panels: list[PanelInfo] = []
    for group in groups:
        for board in group.get_msgs(2):
            ident = board.get_msg_opt(4)
            panels.append(
                PanelInfo(
                    site_id=site_id,
                    serial=ident.get_str(3) if ident is not None else None,
                    model_code=ident.get_str(5) if ident is not None else None,
                    hardware_id=paired.get(len(panels)),
                )
            )
    if not panels:
        only = gateways[0] if len(gateways) == 1 else None
        panels.append(PanelInfo(site_id, None, None, only))
    return SiteInfo(
        site_id=site_id,
        hardware_ids=tuple(hardware_ids),
        panels=tuple(panels),
        gateways=tuple(gateways),
        name=head.get_str(2) if head is not None else None,
    )


def parse_sites(raw: bytes) -> list[SiteInfo]:
    """Split a GetSitesForUser response into its sites, in response order.

    Sites without any subscribable hardware id are dropped — there is nothing
    to stream for them. Empty on an unfamiliar shape; never raises.
    """
    if not raw:
        return []
    try:
        return [info for site in parse(raw).get_msgs(1) if (info := _parse_site(site))]
    except Exception as exc:  # noqa: BLE001 — a shape change must not break setup
        log.debug("could not split GetSitesForUser into sites: %s", type(exc).__name__)
        return []


def _lookup_gateway(panel: PanelInfo, lookup: Callable[[bytes], bytes]) -> str | None:
    """The gateway a GetResourcesForHardware answer names for one panel, if it does.

    The response has a group's fields. An answer naming several gateways is the
    whole site, which settles nothing, and a failed call counts as no answer.
    """
    request = field_string(1, panel.serial or "") + field_string(2, panel.model_code or "")
    try:
        result = parse(lookup(request))
        ids = _gateway_ids(result)
        boards = (board.get_msg_opt(4) for board in result.get_msgs(2))
        holds_board = any(i is not None and i.get_str(3) == panel.serial for i in boards)
    except Exception as exc:  # noqa: BLE001 — pairing is best effort
        log.debug("GetResourcesForHardware gave no answer: %s", type(exc).__name__)
        return None
    return ids[0] if holds_board and len(ids) == 1 else None


def discover_sites(access_token: str) -> list[SiteInfo]:
    """Blocking: read the account's sites and pair their panels."""
    with cloud_grpc.CloudGrpcClient(access_token) as grpc:
        return pair_by_lookup(
            parse_sites(grpc.get_sites_for_user()), grpc.get_resources_for_hardware
        )


def pair_by_lookup(sites: list[SiteInfo], lookup: Callable[[bytes], bytes]) -> list[SiteInfo]:
    """Ask the cloud about boards the site response could not pair.

    Only sites with several panels and an unpaired board are looked up, so a
    one-panel site never makes this call.
    """
    return [_pair_site_by_lookup(site, lookup) for site in sites]


def _pair_site_by_lookup(site: SiteInfo, lookup: Callable[[bytes], bytes]) -> SiteInfo:
    if len(site.panels) < 2:
        return site
    taken = {panel.hardware_id for panel in site.panels}
    claims: dict[int, str] = {}
    for position, panel in enumerate(site.panels):
        if panel.hardware_id is None and panel.serial:
            gateway = _lookup_gateway(panel, lookup)
            if gateway in site.gateways and gateway not in taken:
                claims[position] = gateway
    if not claims:
        return site
    paired = _unique_claims(claims)
    panels = tuple(
        replace(panel, hardware_id=paired[position]) if position in paired else panel
        for position, panel in enumerate(site.panels)
    )
    return replace(site, panels=panels)


def site_device_uuid(device_uuid: str, site_id: str) -> str:
    """A stable, distinct client UUID per site, derived from the entry's own.

    The device UUID names the Ably channel, and the channel is what a
    SubscribeAndGetTraits registration publishes to — so two sites sharing one
    UUID would interleave both panels' frames on one channel again.
    """
    return str(uuid.uuid5(uuid.UUID(device_uuid), f"site:{site_id}"))


@dataclass(frozen=True)
class StreamIdentity:
    """What one stream is keyed by: its channel, its entities and its energy."""

    device_uuid: str
    serial: str | None
    store_panel_key: str | None


def primary_panel_for(sites: list[SiteInfo]) -> str | None:
    """The panel that owns the entry's identity: an account's only panel, if so.

    An account that starts with several has no panel the entry's identity could
    mean, so none owns it, whichever sites they are on. Recorded once, because the
    count changes later and the answer must not: see `stream_identity`.
    """
    panels = [panel for site in sites for panel in site.panels]
    return panels[0].key if len(panels) == 1 else None


def stream_identity(
    site: SiteInfo | None,
    *,
    device_uuid: str,
    serial: str | None,
    primary_panel: str | None,
) -> StreamIdentity:
    """Key a stream by panel, except for the panel that owns the entry's identity.

    That panel keeps the entry's own identity for good, so its entities, its
    energy totals and its Ably channel carry on untouched, even after the account
    gains another panel. Any other panel takes its own key, since the entry's
    single saved serial is not true of it: two panels must never share a device.
    """
    if site is None or any(panel.answers_to(primary_panel) for panel in site.panels):
        return StreamIdentity(device_uuid, serial or (site.serial if site else None), None)
    return StreamIdentity(
        site_device_uuid(device_uuid, site.site_id),
        site.lead.key,
        site.lead.key,
    )


@dataclass(frozen=True)
class SiteRoutes:
    """How one shared site's stream divides up: which device each block feeds."""

    site_id: str
    panels: tuple[PanelInfo, ...]
    # Where the site's own block and flows go: the primary panel if the site holds
    # it, since moving them would orphan their history, otherwise a site device.
    flows_owner: PanelInfo
    # Every gateway on the site, ticked or not, so names don't shift with the ticks.
    gateways: tuple[str, ...] = ()

    @property
    def devices(self) -> tuple[PanelInfo, ...]:
        if self.flows_owner in self.panels:
            return self.panels
        return (*self.panels, self.flows_owner)

    @property
    def blocks(self) -> dict[str, str]:
        """Resource id -> the key of the device that takes it."""
        routes = {panel.hardware_id: panel.key for panel in self.panels if panel.hardware_id}
        routes[self.site_id] = self.flows_owner.key
        return routes


def _gateway_panels(site: SiteInfo) -> tuple[PanelInfo, ...]:
    """One panel per gateway: a board that could not be paired leaves it nameless."""
    boards = {panel.hardware_id: panel for panel in site.panels if panel.hardware_id}
    return tuple(
        boards.get(gateway, PanelInfo(site.site_id, None, None, gateway))
        for gateway in site.gateways
    )


def site_choices(site: SiteInfo) -> tuple[PanelInfo, ...]:
    """What a site offers the picker: the devices it will really become."""
    if len(site.panels) < 2 or len(site.gateways) < 2:
        return (site.lead,)
    return _gateway_panels(site)


def _is_chosen(panel: PanelInfo, chosen: Iterable[str]) -> bool:
    return any(panel.answers_to(key) for key in chosen)


def site_routes(
    site: SiteInfo, primary_panel: str | None, chosen: Iterable[str] | None = None
) -> SiteRoutes | None:
    """Plan the split of a site with several panels. None means don't split.

    One panel takes every block, as it always has. Several panels behind a single
    gateway share one block, where instance ids are unique, so nothing collides
    and the site stays one device. Otherwise each gateway is one panel: a board
    that could not be paired with it leaves it nameless but still correct. A panel
    left out of `chosen` gets no route, so its blocks are dropped, and a site that
    is split stays split however few of its panels are chosen.
    """
    if len(site.panels) < 2:
        return None
    if len(site.gateways) < 2:
        log.warning(
            "SPAN site has %d panels on %d gateway(s), so its circuits stay on one "
            "device; please share a capture so this layout can be supported",
            len(site.panels),
            len(site.gateways),
        )
        return None
    panels = _gateway_panels(site)
    if chosen is not None:
        panels = tuple(panel for panel in panels if _is_chosen(panel, chosen))
    owner = next((panel for panel in panels if panel.answers_to(primary_panel)), None)
    return SiteRoutes(
        site.site_id,
        panels,
        owner or PanelInfo(site.site_id, None, None, site.site_id),
        site.gateways,
    )


def _panel_name(panel: PanelInfo, gateways: tuple[str, ...]) -> str:
    if panel.serial:
        return f"SPAN {panel.model} {panel.serial}"
    if panel.hardware_id in gateways:
        return f"SPAN panel {gateways.index(panel.hardware_id) + 1}"
    return "SPAN site"


def device_name(panel: PanelInfo, routes: SiteRoutes) -> str:
    """Name a device on a shared site. Never the site's label: that is an address."""
    return _panel_name(panel, routes.gateways)


def panel_labels(sites: list[SiteInfo]) -> dict[str, str]:
    """Panel key -> what the picker calls it. Forms only: a site's name is an address."""
    labels: dict[str, str] = {}
    for site in sites:
        for panel in site_choices(site):
            name = _panel_name(panel, site.gateways).removeprefix("SPAN ")
            label = f"{site.name}: {name}" if site.name else name
            # Forms key their fields by label, so two must never match.
            taken = list(labels.values())
            labels[panel.key] = f"{label} ({taken.count(label) + 1})" if label in taken else label
    return labels


def find_panel(sites: list[SiteInfo], key: str) -> tuple[SiteInfo, PanelInfo] | None:
    """The picker's panel for a key, with the site it is on."""
    for site in sites:
        for panel in site_choices(site):
            if panel.answers_to(key):
                return site, panel
    return None


def sibling_answers(sites: Iterable[SiteInfo], chosen_key: str, serial: str | None) -> bool:
    """Whether a serial belongs to another panel on the account than the chosen one.

    Overlapping space numbers (a MAIN 40 beside an MLO 48) can pass the
    circuit check, so a sibling that answers refuses the link. A serial no
    panel answers to (the cloud lists this one under another) is left to the
    circuit check.
    """
    if not serial:
        return False
    return any(
        panel.answers_to(serial) and not panel.answers_to(chosen_key)
        for site in sites
        for panel in (*site.panels, *site_choices(site))
    )


def panel_circuits(
    access_token: str,
    site: SiteInfo,
    panel: PanelInfo,
    *,
    user_id: str | None,
    device_uuid: str,
    primary_panel: str | None,
) -> dict[int, CircuitInfo]:
    """Blocking: one panel's circuits, read from its site's trait snapshot.

    The snapshot answers the subscribe directly, so this needs no stream. It asks
    for the channel the way a stream does, and a running stream shares it.
    """
    return parse_trait_snapshot(
        _panel_snapshot(
            access_token, site, panel, user_id=user_id, device_uuid=device_uuid,
            primary_panel=primary_panel,
        )
    )  # fmt: skip


def panel_traits(
    access_token: str,
    site: SiteInfo,
    panel: PanelInfo,
    *,
    user_id: str | None,
    device_uuid: str,
    primary_panel: str | None,
) -> tuple[dict[int, CircuitInfo], dict[int, int]]:
    """Blocking: as `panel_circuits`, with the feeder kinds of the same snapshot."""
    snapshot = _panel_snapshot(
        access_token, site, panel, user_id=user_id, device_uuid=device_uuid,
        primary_panel=primary_panel,
    )  # fmt: skip
    return parse_trait_snapshot(snapshot), parse_feeder_kinds(snapshot)


def _panel_snapshot(
    access_token: str,
    site: SiteInfo,
    panel: PanelInfo,
    *,
    user_id: str | None,
    device_uuid: str,
    primary_panel: str | None,
) -> bytes:
    """Blocking: the trait snapshot of one panel, split from its site's if shared."""
    identity = stream_identity(
        site, device_uuid=device_uuid, serial=None, primary_panel=primary_panel
    )
    fallback = f"c:{user_id}:{identity.device_uuid}" if user_id else None
    with cloud_grpc.CloudGrpcClient(access_token) as grpc:
        directive = parse_ably_token(
            grpc.ably_token(field_string(1, identity.device_uuid)), fallback_channel=fallback
        )
        if directive.channel is None:
            raise cloud_grpc.GrpcError(5, "no channel to subscribe on", "AblyToken")
        request = build_subscribe_request(directive.channel, site.hardware_ids)
        snapshot = grpc.subscribe_and_get_traits(request)
    routes = site_routes(site, None)
    if routes is None:
        return snapshot
    return split_snapshot(snapshot, routes, set()).get(panel.key, b"")


def pin_panels(sites: list[SiteInfo], recorded: Iterable[str]) -> list[SiteInfo]:
    """Hold each panel to the key already recorded for it.

    A panel recorded by hardware id keeps that key once its serial is known, or
    pairing it later would make it a new device with new totals.
    """
    keys = tuple(recorded)

    def pin(panel: PanelInfo) -> PanelInfo:
        key = next((key for key in keys if panel.answers_to(key)), None)
        return replace(panel, stored_key=key) if key else panel

    return [replace(site, panels=tuple(pin(panel) for panel in site.panels)) for site in sites]


@dataclass(frozen=True)
class PanelPlan:
    """The account's panels read against the entry's stored answers."""

    sites: list[SiteInfo]
    chosen: tuple[str, ...]  # recorded keys, or every panel if nothing was chosen yet
    new: tuple[str, ...]  # on the account, and neither chosen nor declined
    departed: int  # recorded, but no longer on the account

    @property
    def running(self) -> list[SiteInfo]:
        return [
            site
            for site in self.sites
            if any(_is_chosen(panel, self.chosen) for panel in site_choices(site))
        ]


def panel_plan(
    sites: list[SiteInfo], recorded: Iterable[str] | None, declined: Iterable[str] = ()
) -> PanelPlan:
    """Decide which panels run. No record yet means every panel does.

    A panel the record doesn't know stays off until the user says otherwise: it
    may not belong to this Home Assistant.
    """
    known = [*(recorded or ()), *declined]
    pinned = pin_panels(sites, known)
    panels = [panel for site in pinned for panel in site_choices(site)]
    if recorded is None:
        return PanelPlan(pinned, tuple(panel.key for panel in panels), (), 0)
    new = tuple(panel.key for panel in panels if not _is_chosen(panel, known))
    departed = sum(1 for key in recorded if not any(p.answers_to(key) for p in panels))
    return PanelPlan(pinned, tuple(recorded), new, departed)


def device_keys(
    sites: list[SiteInfo], chosen: Iterable[str], primary_panel: str | None
) -> set[str]:
    """The HA devices these panels make, including any site device they bring."""
    picked = tuple(chosen)
    keys: set[str] = set()
    for site in panel_plan(sites, picked).running:
        routes = site_routes(site, primary_panel, picked)
        keys |= {device.key for device in routes.devices} if routes else {site.lead.key}
    return keys


def panel_identity(
    site: SiteInfo,
    panel: PanelInfo,
    *,
    device_uuid: str,
    serial: str | None,
    primary_panel: str | None,
) -> StreamIdentity:
    """Key one panel of a shared site. They share its channel and nothing else."""
    stream = stream_identity(
        site, device_uuid=device_uuid, serial=serial, primary_panel=primary_panel
    )
    if panel.answers_to(primary_panel):
        return StreamIdentity(stream.device_uuid, serial or panel.key, None)
    return StreamIdentity(stream.device_uuid, panel.key, panel.key)


def _drop_block(block_id: str | None, unrouted: set[str | None]) -> None:
    if block_id not in unrouted:
        unrouted.add(block_id)
        log.debug("dropping a block that belongs to no panel here")


def split_frame(frame: Frame, routes: SiteRoutes, unrouted: set[str | None]) -> dict[str, Frame]:
    """Divide a frame between a site's devices, keyed by device.

    A device appears only if the frame held something for it, and a frame may hold
    any mix of blocks or none. `unrouted` remembers blocks already logged as dropped.
    """
    blocks = routes.blocks
    parts: dict[str, dict[str, list[CircuitSample]]] = {}
    for resource_id, samples in frame.resources.items():
        key = blocks.get(resource_id)
        if key is None:
            _drop_block(resource_id, unrouted)
            continue
        parts.setdefault(key, {})[resource_id] = samples

    out = {key: replace(frame, resources=res, site_flows={}) for key, res in parts.items()}
    if frame.site_flows:
        owner = routes.flows_owner.key
        out[owner] = replace(
            out.get(owner, frame), resources=parts.get(owner, {}), site_flows=frame.site_flows
        )
    return out


def split_snapshot(raw: bytes, routes: SiteRoutes, unrouted: set[str | None]) -> dict[str, bytes]:
    """Divide a trait snapshot between a site's devices, keyed by device.

    Each part is a snapshot in its own right, so `parse_trait_snapshot` needs no
    idea that a site can hold two panels. An unparseable snapshot yields nothing,
    which is what `parse_trait_snapshot` makes of one too.
    """
    blocks = routes.blocks
    out: dict[str, bytes] = {}
    try:
        for block in parse(raw).values(1):
            ref = parse(block).get_msg(1)
            resource_id = ref.get_str(1) if ref is not None else None
            key = blocks.get(resource_id) if resource_id else None
            if key is None:
                _drop_block(resource_id, unrouted)
                continue
            out[key] = out.get(key, b"") + field_message(1, block)
    except (ProtoError, ValueError, TypeError) as exc:
        log.debug("unparseable trait snapshot (%d bytes): %s", len(raw), exc)
        return {}
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
        body += field_message(2, field_message(1, field_string(1, hardware_id)) + traits)
    return body


def _walk_strings(msg: Message, depth: int = 0):
    """Yield every UTF-8-decodable leaf string in a message tree (bounded depth)."""
    if depth > 8:
        return
    for values in msg.fields.values():
        for _wire, value in values:
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
            except ProtoError:
                # Speculative descent: these bytes were simply not a nested
                # message. Nothing to report — the caller wants the strings we
                # did find, not a parse log for every leaf.
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
        on_auth_failed: AuthFailedCallback | None = None,
        site_id: str | None = None,
    ) -> None:
        self._token_store = Path(token_store)
        self._device_uuid = device_uuid
        self._user_id = user_id
        self._serial = serial
        # When set, this backend streams only that site's panel. Each site needs
        # its own backend (and its own device UUID, hence its own Ably channel):
        # circuit instance ids restart per panel, so two panels on one channel
        # collide on ids like 21-27 and overwrite each other's names, readings
        # and relay targets. None keeps the original whole-account behavior.
        self._site_id = site_id
        self._host = host
        self._key_name = key_name
        self._reconnect_seconds = reconnect_seconds
        self._on_auth_failed = on_auth_failed
        # Latched once the credentials have been reported dead, so a retry loop
        # that keeps hitting the same rejection asks the user exactly once.
        self._auth_failure_reported = False
        self._hardware_ids: list[str] = []
        # What the last GetSitesForUser actually returned, kept past a cache drop
        # so a re-resolve can say whether the topology moved under us.
        self._resolved_hardware_ids: list[str] = []
        # instance id -> circuit identity, from the subscribe snapshot.
        self._circuits: dict[int, CircuitInfo] = {}
        # feed instance id -> trait 1/21 feeder kind, from the same snapshot.
        self._feeders: dict[int, int] = {}
        # What `fetch_energy` asks about, learned from the frames: resource ->
        # instance -> node id, and the site flows this panel publishes.
        self._energy_targets: dict[str, dict[int, str]] = {}
        self._energy_flows: tuple[str, ...] = ()

        self._on_schema: SchemaCallback | None = None
        self._on_reading: ReadingCallback | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._schema_sent = False
        self._label_deadline = 0.0
        # Monotonic time of the last decodable frame, and whether the silence
        # watchdog is what ended the current stream (as opposed to stop() or an
        # error) — only used to explain the reconnect in the log.
        self._last_frame = 0.0
        self._watchdog_tripped = False
        # Consecutive attaches that delivered no telemetry; drives the backoff
        # and decides how loudly the reconnect is logged.
        self._dead_attaches = 0
        # Set to wake the snapshot-refresh loop before its timer is up.
        self._refresh_request = threading.Event()
        # Bumped on every (re)attach so a refresh loop left over from a previous
        # connection retires instead of subscribing a channel that is gone.
        self._subscribe_epoch = 0

    # --- lifecycle ---------------------------------------------------------

    def start(self, on_schema: SchemaCallback, on_reading: ReadingCallback) -> None:
        self._on_schema = on_schema
        self._on_reading = on_reading
        self._stop.clear()
        self._schema_sent = False
        self._dead_attaches = 0
        self._label_deadline = time.monotonic() + SCHEMA_LABEL_WAIT_SECONDS
        self._thread = threading.Thread(target=self._run, name="span-cloud", daemon=True)
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

    # --- commands ----------------------------------------------------------

    def send_command(self, key: str, value: str) -> None:
        """Open or close a circuit's relay. `key` is `circuit-<instance>/relay`.

        Raises `GrpcError` if the panel rejects the call outright; the bridge logs
        that and Home Assistant surfaces it as a failed call. A command the *panel*
        declines on policy grounds (an always-on circuit, a minimum reconnect
        time) still returns OK here — it shows up as the state reverting on the
        next snapshot refresh, which is scheduled below.
        """
        node_id, _, prop = key.rpartition("/")
        if prop != RELAY_PROPERTY:
            log.warning("cloud backend has no writable '%s' property; ignoring %s", prop, key)
            return

        info = self._circuit_for_node(node_id)
        if info is None or info.switch is None:
            log.warning("no switch trait known for %s; ignoring relay command", key)
            return

        closed = parse_relay_command(value)
        if closed is None:
            log.warning("unrecognized relay command %r for %s; ignoring", value, key)
            return

        access_token = self._access_token()
        request = cloud_commands.build_switch_request(
            info.switch, closed, requester_id=self._requester_id(access_token)
        )
        with cloud_grpc.CloudGrpcClient(access_token, host=self._host) as grpc:
            grpc.send_messages(request)
        log.info("relay command accepted for %s -> %s", key, "CLOSED" if closed else "OPEN")

        # Report the intended state at once so the UI doesn't snap back while the
        # relay moves, then re-read the snapshot to replace the guess with truth.
        self._circuits[info.instance_id] = replace(info, relay_closed=closed)
        self._emit_relay_reading(self._circuits[info.instance_id])
        self._request_refresh(POST_COMMAND_REFRESH_SECONDS)

    def fetch_energy(
        self, covered_through_ms: int = 0, *, time_zone: str = "UTC"
    ) -> list[EnergySample]:
        """Read SPAN's metered energy for everything this panel meters.

        Blocking — one or two gRPC calls, ~18 kB and under a second for the live
        window on a 40-space panel. `covered_through_ms` is the end of the last
        interval the caller counted; a gap wider than the live window puts an
        hourly backfill pass in front, and the results come back in the order
        they must be applied (see `cloud_history.plan_windows`).

        Returns nothing at all until a frame has named the metric instances,
        which is the same condition that gates the entities existing.
        """
        targets = self._energy_targets
        if not targets:
            return []

        now_ms = int(time.time() * 1000)
        instances = {resource: list(nodes) for resource, nodes in targets.items()}
        samples: list[EnergySample] = []
        with cloud_grpc.CloudGrpcClient(self._access_token(), host=self._host) as grpc:
            for resolution, start_ms, end_ms in cloud_history.plan_windows(
                now_ms=now_ms, covered_through_ms=covered_through_ms
            ):
                request = cloud_history.build_request(
                    instances,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    resolution=resolution,
                    time_zone=time_zone,
                )
                raw = grpc.call(cloud_history.METHOD, request)
                series = cloud_history.parse_response(raw)
                log.debug(
                    "energy: %s over %dm returned %d series (%d bytes)",
                    resolution.name.lower(),
                    (end_ms - start_ms) // 60_000,
                    len(series),
                    len(raw),
                )
                samples.extend(energy_samples(series, targets, self._energy_flows))
        return samples

    def traits(self) -> tuple[dict[int, CircuitInfo], dict[int, int]]:
        """The circuits and feeder kinds of the last trait snapshot.

        Both are replaced whole on the backend's thread, so reading them from
        another thread sees one snapshot's or the next's, never a mix of a dict.
        """
        return self._circuits, self._feeders

    def site_history_target(self) -> tuple[str, int] | None:
        """The resource and instance SPAN keeps the site's history under, once seen."""
        for resource, instances in self._energy_targets.items():
            for instance, node in instances.items():
                if node == SITE_NODE:
                    return resource, instance
        return None

    def _requester_id(self, access_token: str) -> str:
        """Who a write says it is: this user's SPAN id.

        Read from the token rather than from config, because it has to match the
        id the server resolves from that same token; a stale configured value
        would only earn a PERMISSION_DENIED.
        """
        user_id = cloud_auth.user_id_from_token(access_token) or self._user_id
        if not user_id:
            raise RuntimeError(
                "cannot send a command: the access token carries no username claim "
                "to name as the requester"
            )
        return user_id

    def _circuit_for_node(self, node_id: str) -> CircuitInfo | None:
        """Resolve a `circuit-<instance>` node id back to its CircuitInfo."""
        prefix = _circuit_node("")
        if not node_id.startswith(prefix):
            return None
        try:
            instance = int(node_id[len(prefix) :])
        except ValueError:
            return None
        return self._circuits.get(instance)

    def _emit_relay_reading(self, info: CircuitInfo) -> None:
        if self._on_reading is None:
            return
        self._on_reading(
            Reading(
                key=f"{_circuit_node(info.instance_id)}/{RELAY_PROPERTY}",
                value=relay_state(info),
                timestamp=time.time(),
            )
        )

    def _request_refresh(self, delay: float) -> None:
        """Ask the refresh loop to re-read the trait snapshot in `delay` seconds."""

        def worker() -> None:
            if self._stop.wait(delay):
                return
            self._refresh_request.set()

        threading.Thread(target=worker, name="span-cloud-relay-refresh", daemon=True).start()

    # --- network loop ------------------------------------------------------

    def _stream_should_stop(self) -> bool:
        """Stop-check for `stream_frames`, consulted once per line off the wire.

        Ends the stream on shutdown, or when the channel has been silent past
        `FRAME_SILENCE_SECONDS`. Returning True unwinds `stream_frames` normally,
        so `_run` falls through to its reconnect wait and re-bootstraps — a fresh
        Ably token and a fresh SubscribeAndGetTraits, which is what a lapsed
        registration actually needs.
        """
        if self._stop.is_set():
            return True
        if time.monotonic() - self._last_frame < FRAME_SILENCE_SECONDS:
            return False
        self._watchdog_tripped = True
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            attached_at = time.monotonic()
            try:
                token, channel = self.bootstrap()
                log.debug("attaching to the Ably channel")
                # The watchdog measures from the attach, not from the last frame
                # of the previous connection, so the reconnect gets a full budget.
                self._last_frame = attached_at = time.monotonic()
                self._watchdog_tripped = False
                # Registration is per-connection as far as we can tell, so it is
                # re-issued on every (re)attach, from a helper thread because
                # stream_frames blocks for the life of the stream.
                self._schedule_subscribe(channel)
                cloud_ably.stream_frames(
                    token,
                    channel,
                    self._handle_frame,
                    stop=self._stream_should_stop,
                )
                if self._watchdog_tripped:
                    reason = (
                        f"no telemetry for {FRAME_SILENCE_SECONDS:.0f}s on a "
                        "connection that never dropped"
                    )
                else:
                    reason = "cloud stream closed"
            except Exception as exc:  # noqa: BLE001 — keep the daemon alive
                reason = f"cloud stream error ({type(exc).__name__})"
            if self._stop.is_set():
                return
            # Frames arriving after the attach are the only proof the attempt was
            # worth anything: bootstrap can succeed and the channel attach with a
            # registration SPAN never honours.
            delay = self._note_attach_ended(delivered=self._last_frame > attached_at, reason=reason)
            # Wait, but wake immediately on stop.
            self._stop.wait(delay)

    def _note_attach_ended(self, *, delivered: bool, reason: str) -> float:
        """Log how the stream ended and return how long to wait before retrying.

        A stream that carried telemetry clears the failure count however badly it
        ended, so the common case — Ably resetting a long-lived SSE socket — is
        still retried in `reconnect_seconds` and logged as the routine event it
        is. Only a run of attaches that delivered nothing backs off and gets
        loud, and it gets loud exactly once per outage rather than once per try.
        """
        if delivered:
            self._dead_attaches = 0
        else:
            self._dead_attaches += 1
            self._forget_topology()
        delay = self._reconnect_delay()
        if self._dead_attaches == LOUD_AFTER_ATTEMPTS:
            log.error(
                "%s; %d attaches in a row carried no telemetry, retrying in %.0fs",
                reason,
                self._dead_attaches,
                delay,
            )
        else:
            log.info("%s; reattaching in %.0fs", reason, delay)
        return delay

    def _forget_topology(self) -> None:
        """Drop the cached GetSitesForUser answer so the next bootstrap re-reads it.

        `bootstrap` resolves the serial and hardware ids once and then keeps them
        for the life of the backend, so every reattach re-registers the *same*
        resource ids however stale they have become. When they stop being the ids
        SPAN publishes for, `SubscribeAndGetTraits` is still accepted and the
        channel is still silent, and no amount of reattaching fixes it — reloading
        the integration was the only thing that did, because that builds a fresh
        backend with empty caches. An attach that delivered nothing is exactly the
        symptom, so re-resolve on one rather than making the user notice.
        """
        self._hardware_ids = []

    def _reconnect_delay(self) -> float:
        """Seconds to wait before the next attach: geometric in the number of
        consecutive dead attaches, jittered so a cloud-wide outage does not bring
        every panel back in lockstep."""
        steps = min(max(self._dead_attaches - 1, 0), 8)
        delay = min(self._reconnect_seconds * 2.0**steps, RECONNECT_BACKOFF_MAX_SECONDS)
        jitter = random.uniform(0.8, 1.2)  # noqa: S311 — spreading retries, not a secret
        return min(delay * jitter, RECONNECT_BACKOFF_MAX_SECONDS)

    def _access_token(self) -> str:
        """The current access token, reporting a dead credential on the way out.

        Every network path goes through here so that a revoked refresh token is
        announced wherever it is first noticed — the stream loop usually, but a
        relay command just as well — instead of only from whichever one happens
        to run first.
        """
        try:
            return cloud_auth.access_token_from_store(self._token_store)
        except cloud_auth.CloudCredentialsRejected as exc:
            self._report_auth_failure(str(exc))
            raise

    def _report_auth_failure(self, reason: str) -> None:
        """Tell the host once that only the user can fix this.

        Latched: the stream loop keeps retrying afterwards (a revoked token is
        indistinguishable from a revoked-then-restored one, and the retry costs
        nothing at the backoff ceiling), but it must not raise a fresh prompt on
        every attempt.
        """
        if self._auth_failure_reported:
            return
        self._auth_failure_reported = True
        log.error("SPAN cloud credentials rejected (%s); re-authentication needed", reason)
        if self._on_auth_failed is not None:
            try:
                self._on_auth_failed(reason)
            except Exception:  # a bad host callback must not kill the stream thread
                log.exception("on_auth_failed callback raised")

    def bootstrap(self) -> tuple[str, str]:
        """Authenticate, resolve topology, and obtain an Ably token + channel."""
        access_token = self._access_token()
        with cloud_grpc.CloudGrpcClient(access_token, host=self._host) as grpc:
            if self._serial is None or not self._hardware_ids:
                sites = grpc.get_sites_for_user()
                site = None
                if self._site_id is not None:
                    site = next((s for s in parse_sites(sites) if s.site_id == self._site_id), None)
                    if site is None:
                        raise cloud_grpc.GrpcError(
                            5,
                            "the site is no longer on this account",
                            "GetSitesForUser",
                        )
                if self._serial is None:
                    self._serial = (site.serial if site else None) or _parse_sites_serial(sites)
                    if self._serial:
                        log.debug("resolved the panel serial from GetSitesForUser")
                if not self._hardware_ids:
                    self._hardware_ids = (
                        list(site.hardware_ids) if site else parse_sites_hardware_ids(sites)
                    )
                    if (
                        self._resolved_hardware_ids
                        and self._hardware_ids != self._resolved_hardware_ids
                    ):
                        # Worth a WARNING: this is the silent-channel cause, and
                        # a line naming both sets is what proves it in the field.
                        log.warning(
                            "subscribable hardware ids changed (%d -> %d); the "
                            "previous set is why the channel went quiet",
                            len(self._resolved_hardware_ids),
                            len(self._hardware_ids),
                        )
                    self._resolved_hardware_ids = self._hardware_ids
                    log.debug("subscribable hardware ids: %d", len(self._hardware_ids))

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
            details = cloud_ably.request_token(directive.token_request, key_name=self._key_name)
            return details.token, channel
        raise cloud_ably.AblyError("AblyToken response had neither a token nor a TokenRequest")

    def _schedule_subscribe(self, channel: str) -> None:
        """Subscribe shortly after the caller starts reading the stream, then keep
        re-reading the snapshot so relay state stays current."""
        self._subscribe_epoch += 1
        epoch = self._subscribe_epoch

        def worker() -> None:
            if self._stop.wait(SUBSCRIBE_DELAY_SECONDS):
                return
            first = True
            while epoch == self._subscribe_epoch:
                try:
                    self._refresh_circuits(channel, announce=not first)
                except Exception as exc:  # noqa: BLE001 — the stream retry owns recovery
                    if self._stop.is_set():
                        return
                    if first:
                        log.error(
                            "SubscribeAndGetTraits failed (%s); the channel will stay "
                            "silent until the next reconnect",
                            type(exc).__name__,
                        )
                    else:
                        log.warning(
                            "relay state refresh failed (%s); keeping last known",
                            type(exc).__name__,
                        )
                first = False
                if not self._wait_before_refresh(SWITCH_REFRESH_SECONDS):
                    return

        threading.Thread(target=worker, name="span-cloud-subscribe", daemon=True).start()

    def _refresh_circuits(self, channel: str, *, announce: bool = False) -> None:
        """Re-read the trait snapshot, updating circuit identities and relay state.

        `announce` publishes the relay values straight away, which is how a change
        made in the SPAN app reaches Home Assistant: telemetry frames never carry
        switch state, so without this the value would only move when we ourselves
        commanded it.
        """
        snapshot = self.subscribe(channel)
        circuits = parse_trait_snapshot(snapshot)
        if circuits:
            self._circuits = circuits
            self._feeders = parse_feeder_kinds(snapshot)
            if announce:
                for info in circuits.values():
                    if info.switch is not None:
                        self._emit_relay_reading(info)
        # The subscribe answered, so there is nothing left to wait for: release
        # the schema even if the snapshot named no circuits.
        self._label_deadline = 0.0

    def _wait_before_refresh(self, seconds: float) -> bool:
        """Sleep until the next refresh is due; False means we are shutting down.

        Woken early by `_request_refresh`, and polled in short slices so `stop()`
        is not held up by a refresh interval.
        """
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            if self._refresh_request.wait(min(1.0, remaining)):
                self._refresh_request.clear()
                return True
        return False

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
        access_token = self._access_token()
        request = build_subscribe_request(channel, self._hardware_ids)
        with cloud_grpc.CloudGrpcClient(access_token, host=self._host) as grpc:
            snapshot = grpc.subscribe_and_get_traits(request)
        log.info(
            "subscribed %d resource(s) for telemetry (%d-byte trait snapshot)",
            len(self._hardware_ids),
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

        # Liveness is "SPAN is publishing frames we understand", so this is set
        # from a decoded frame rather than from raw bytes off the socket.
        self._last_frame = time.monotonic()

        self._apply_frame(frame)

    def _schema_source(self, frame: Frame) -> Frame:
        """The frame that says what this panel holds."""
        return frame

    def _apply_frame(self, frame: Frame) -> None:
        source = self._schema_source(frame)
        # The stream interleaves power frames (with circuits) and lean energy/
        # interval frames (empty of resources+flows). Hold the schema until a
        # content-bearing frame so we don't publish an empty topology — and,
        # briefly, until the subscribe snapshot has named the circuits.
        if (
            not self._schema_sent
            and self._on_schema is not None
            and (frame.resources or frame.site_flows)
            and (self._circuits or time.monotonic() >= self._label_deadline)
        ):
            serial = self._serial or "span-cloud"
            self._on_schema(schema_from_frame(serial, source, self._circuits, self._feeders))
            self._schema_sent = True

        # Only a content-bearing frame names the metering instances: the lean
        # interval frames carry a different site instance and no circuits at all,
        # so taking the identifiers from one would ask about the wrong things.
        if frame.resources or frame.site_flows:
            targets = energy_targets(source, self._circuits)
            if targets != self._energy_targets:
                self._energy_targets = targets
                log.debug(
                    "energy targets: %d node(s) on %d resource(s)",
                    sum(len(nodes) for nodes in targets.values()),
                    len(targets),
                )
            if frame.site_flows:
                self._energy_flows = tuple(frame.site_flows)

        if self._on_reading is not None:
            ts = frame.epoch_millis / 1000.0 if frame.epoch_millis else None
            for reading in readings_from_frame(frame, ts, self._circuits):
                self._on_reading(reading)


class SitePanelBackend(CloudBackend):
    """One panel of a shared site: all of a backend but the stream.

    Holds what `CloudBackend` holds for its one panel, so commands, energy and the
    schema work unchanged, and is fed its slice of the site's stream by `SiteStream`.
    """

    def __init__(self, stream: SiteStream, token_store: Path, device_uuid: str, **kwargs) -> None:
        super().__init__(token_store, device_uuid, **kwargs)
        self._stream = stream
        # A flows frame and a block frame may arrive apart, so unlike a whole
        # site's frame neither one alone describes this panel.
        self._seen = Frame(epoch_millis=0, site_id=None)

    def start(self, on_schema: SchemaCallback, on_reading: ReadingCallback) -> None:
        self._on_schema = on_schema
        self._on_reading = on_reading
        self._schema_sent = False
        self._label_deadline = time.monotonic() + SCHEMA_LABEL_WAIT_SECONDS
        self._stream.attach()

    def stop(self, join_timeout: float = 10.0) -> None:
        self._stream.detach(join_timeout)

    def _request_refresh(self, delay: float) -> None:
        self._stream._request_refresh(delay)

    def _schema_source(self, frame: Frame) -> Frame:
        return self._seen

    def _apply_frame(self, frame: Frame) -> None:
        seen = replace(
            frame,
            resources={**self._seen.resources, **frame.resources},
            site_flows={**self._seen.site_flows, **frame.site_flows},
        )
        # Something new to describe: schema again, and the coordinator only adds
        # the entities it has not seen.
        if seen.resources.keys() != self._seen.resources.keys() or (
            seen.site_flows.keys() != self._seen.site_flows.keys()
        ):
            self._schema_sent = False
        self._seen = seen
        super()._apply_frame(frame)


class SiteStream(CloudBackend):
    """The one stream of a site with several panels, split between them.

    Starts once every panel has attached, so none misses the first snapshot.
    """

    def __init__(
        self,
        token_store: Path,
        device_uuid: str,
        *,
        routes: SiteRoutes,
        user_id: str | None = None,
        serial: str | None = None,
    ) -> None:
        super().__init__(
            token_store, device_uuid, user_id=user_id, serial=serial, site_id=routes.site_id
        )
        self._routes = routes
        self._panels: dict[str, SitePanelBackend] = {}
        self._attached = 0
        self._unrouted: set[str | None] = set()

    def panel_backend(
        self, key: str, serial: str | None, on_auth_failed: AuthFailedCallback | None = None
    ) -> SitePanelBackend:
        # Any panel's coordinator can raise the prompt; there is one entry to reauth.
        self._on_auth_failed = self._on_auth_failed or on_auth_failed
        panel = SitePanelBackend(
            self,
            self._token_store,
            self._device_uuid,
            user_id=self._user_id,
            serial=serial,
            host=self._host,
            key_name=self._key_name,
            on_auth_failed=on_auth_failed,
        )
        self._panels[key] = panel
        return panel

    @property
    def routes(self) -> SiteRoutes:
        return self._routes

    def attach(self) -> None:
        self._attached += 1
        if self._attached == len(self._panels):
            super().start(lambda _schema: None, lambda _reading: None)

    def detach(self, join_timeout: float = 10.0) -> None:
        self._attached -= 1
        if self._attached == 0:
            super().stop(join_timeout)

    def _refresh_circuits(self, channel: str, *, announce: bool = False) -> None:
        parts = split_snapshot(self.subscribe(channel), self._routes, self._unrouted)
        for key, panel in self._panels.items():
            part = parts.get(key, b"")
            circuits = parse_trait_snapshot(part)
            if circuits:
                panel._circuits = circuits
                panel._feeders = parse_feeder_kinds(part)
                if announce:
                    for info in circuits.values():
                        if info.switch is not None:
                            panel._emit_relay_reading(info)
            panel._label_deadline = 0.0

    def _handle_frame(self, raw: bytes) -> None:
        try:
            frame = decode_frame(raw)
        except Exception as exc:  # noqa: BLE001 — a bad frame must not kill the stream
            log.debug("undecodable telemetry frame (%d bytes): %s", len(raw), exc)
            return

        self._last_frame = time.monotonic()
        for key, part in split_frame(frame, self._routes, self._unrouted).items():
            panel = self._panels.get(key)
            if panel is not None:
                panel._apply_frame(part)
