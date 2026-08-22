"""Decode SPAN's Ably realtime telemetry frames into circuit power readings.

Each realtime frame is a base64-protobuf message pushed on the Ably channel
`c:<userId>:<deviceUUID>`. There is no public `.proto`; the structure below was
recovered from the Android app's bytecode (see docs/CLOUD-PROTO.md) and verified
against captured live frames.

Frame layout (positional — the outer wrapper is Ably push framing, the inner
tree is `io.span` metering traits):

    #1  header
    #2  site block { 1:{1: siteId}  2:{1: site metric instance id} }
    #16 push       { 3: { 1: kind  2:{1: epoch_millis}  3: <body> } }

    body { 2: site metric  { 2: SiteInstantPower }
           3: resource block { 1:{1: resourceId}  2*: InstancePowerStatsSample } }

`InstancePowerStatsSample`:
    2  trait_instance_id (the per-circuit identifier)
    4  metrics_received_percent (quality)
    oneof summaries { 11 two_wire | 12 three_wire | 14 panel_power }

Every measurement leaf is an *aggregate stats* message — `1 min, 2 max, 3 avg`,
of which only `avg` is ever populated — so each reading sits at wire field #3 of
the innermost wrapper. Which of the two stats types it is decides how that
varint is encoded: `UnsignedAggregateStats` (uint32) for current, voltage and
frequency, `SignedAggregateStats` (**sint32, zig-zag**) for anything that is a
power. The *quantity and unit* come from the slot position, not the leaf type:

    SingleChannelMeterPower: 1 current(mA) 2 voltage(mV) 3 power(mW) 4 freq(mHz)
    DoubleChannelMeterPower: 1 line_an 2 line_bn 3 combined (each a Single…)

`panel_power` (tag 14) is *not* a SingleChannelMeterPower despite sharing the
oneof — see `_decode_panel`.

What live frames add to the static recovery:

  * Which slots a circuit actually populates depends on its wiring. A two-wire
    (120 V branch) sample carries current and power but never voltage: the slot
    is absent from the wire, not zero, on every sample observed. Only the panel
    block reports voltage.
  * Power really is zig-zag encoded, and reading it as a plain varint doubles
    every positive reading. The proof is on the wire: the panel feeder reported
    119.964 V / 9.133 A on line_an and 119.856 V / 10.189 A on line_bn — 2316 VA
    of apparent power all told — against a "4066.568 W" combined reading, which
    would be a power factor of 1.76. Zig-zag decoded it is 2033.284 W, i.e. PF
    0.88, and every branch circuit lands under PF 1 the same way.
  * A *present but empty* slot means zero, not missing — proto3 omits the zero
    scalar yet still emits the message holding it. Treating the two alike makes
    a circuit that switches off freeze at its last non-zero reading, so
    `_leaf_value` distinguishes them.
  * The `combined` slot's *current* is the larger of the two legs, not their
    sum — verified on every frame, for the panel feeder and for three-wire
    circuits alike (whichever leg is higher is the one that appears). That is
    the right convention for a series path through a two-pole breaker, and it
    makes the figure busbar loading rather than a total. Per-leg current is
    populated separately and is what shows whether a panel is balanced; per-leg
    *power* is never populated.
  * The `combined` slot's *voltage* is unreliable: it equals line_an + line_bn
    on most frames but reads a frame-wide 0.88x or 0.99x of that on others, for
    every circuit at once regardless of load. The per-leg voltages are stable in
    every frame, so consumers should prefer those and ignore combined voltage.

Static recovery is exact for field numbers and units; the one thing it cannot
pin is which physical circuit a given `trait_instance_id` maps to — that mapping
comes from the trait snapshot (see `cloud_traits`), resolved by the backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .cloud_pb import Message, parse, zigzag_decode

# SiteInstantPower / SiteEnergy directional field numbering (docs/CLOUD-PROTO.md).
SITE_FLOWS: dict[int, str] = {
    1: "grid",
    2: "home",
    3: "solar",
    4: "battery",
    5: "generator",
    6: "balance",
    7: "residual",
    11: "grid_to_home",
    12: "grid_to_battery",
    21: "solar_to_home",
    22: "solar_to_battery",
    23: "solar_to_grid",
    31: "battery_to_home",
    32: "battery_to_grid",
    51: "voltage_l1",
    52: "voltage_l2",
    53: "frequency",
}

# The site flows that are not powers: 51/52 voltage (mV), 53 frequency (mHz).
_UNSIGNED_SITE_FLOWS = frozenset({51, 52, 53})

# oneof `summaries` tag -> kind, on InstancePowerStatsSample.
_KIND_BY_TAG = {11: "two_wire", 12: "three_wire", 14: "panel"}


@dataclass
class Channel:
    """One channel's instantaneous readings, in SPAN's native milli-units."""

    current_ma: int | None = None
    voltage_mv: int | None = None
    power_mw: int | None = None
    freq_mhz: int | None = None

    @property
    def power_w(self) -> float | None:
        return None if self.power_mw is None else self.power_mw / 1000.0

    @property
    def current_a(self) -> float | None:
        return None if self.current_ma is None else self.current_ma / 1000.0

    @property
    def voltage_v(self) -> float | None:
        return None if self.voltage_mv is None else self.voltage_mv / 1000.0

    @property
    def freq_hz(self) -> float | None:
        return None if self.freq_mhz is None else self.freq_mhz / 1000.0


@dataclass
class CircuitSample:
    """One InstancePowerStatsSample: a circuit's power at a point in time."""

    instance_id: int
    kind: str  # "two_wire" | "three_wire" | "panel"
    quality_pct: int | None = None
    # two_wire populates `combined`; three_wire populates all three legs.
    combined: Channel | None = None
    line_an: Channel | None = None
    line_bn: Channel | None = None

    @property
    def power_w(self) -> float | None:
        """Best single power figure for the circuit (the combined channel)."""
        return self.combined.power_w if self.combined else None


@dataclass
class Frame:
    """A fully decoded realtime frame."""

    epoch_millis: int | None = None
    site_id: str | None = None
    # The metric instance the site's aggregate flows are published under. Not
    # needed to read the frame — the flows are right here — but it is the only
    # place it is named, and asking SPAN for the site's *energy* requires it.
    site_instance_id: int | None = None
    # resourceId -> per-circuit samples
    resources: dict[str, list[CircuitSample]] = field(default_factory=dict)
    # directional site flows, in watts (SiteInstantPower), keyed by flow name
    site_flows: dict[str, float] = field(default_factory=dict)


# --- leaf helpers -------------------------------------------------------------


def _leaf_value(msg: Message | None, *, signed: bool = False) -> int | None:
    """Pull the reading out of an aggregate-stats leaf.

    Two nestings occur: a bare `{3: avg}` (the channel slots hold the stats
    message directly) and a `{1: quality, 2: {3: avg}}` (the site directional
    flows wrap it in an `AggregatePowerStats`). Both resolve to the varint at
    field #3 — `avg` — of the innermost message.

    `signed` selects the stats type. A power is a `SignedAggregateStats`, whose
    `avg` is an sint32 and therefore **zig-zag** encoded; current, voltage and
    frequency are `UnsignedAggregateStats` and are plain varints. Reading a
    power as unsigned returns exactly twice the real value when it is positive,
    and a large positive number when the flow is an export.

    An absent wrapper (`msg is None`) means the quantity is not measured. A
    wrapper that is present but carries no scalar means *zero*: proto3 drops the
    zero value and keeps the message around it. Returning None for that case
    would strand a switched-off circuit at its last non-zero reading, so the two
    are kept distinct.
    """
    if msg is None:
        return None
    raw = msg.get_int_opt(3)
    if raw is None:
        inner = msg.get_msg(2)
        if inner is None:
            return 0
        raw = inner.get_int_opt(3) or 0
    return zigzag_decode(raw) if signed else raw


def _decode_single_channel(msg: Message) -> Channel:
    """SingleChannelMeterPower: 1 current, 2 voltage, 3 power, 4 frequency."""
    return Channel(
        current_ma=_leaf_value(msg.get_msg(1)),
        voltage_mv=_leaf_value(msg.get_msg(2)),
        power_mw=_leaf_value(msg.get_msg(3), signed=True),
        freq_mhz=_leaf_value(msg.get_msg(4)),
    )


def _decode_double_channel(
    msg: Message,
) -> tuple[Channel | None, Channel | None, Channel | None]:
    """DoubleChannelMeterPower: 1 line_an, 2 line_bn, 3 combined."""
    an, bn, combined = msg.get_msg(1), msg.get_msg(2), msg.get_msg(3)
    return (
        _decode_single_channel(an) if an is not None else None,
        _decode_single_channel(bn) if bn is not None else None,
        _decode_single_channel(combined) if combined is not None else None,
    )


def _decode_panel(
    msg: Message,
) -> tuple[Channel | None, Channel | None, Channel | None]:
    """PanelInstant — the panel's own metering, and not a SingleChannelMeterPower.

    Verified against live frames; the useful content is all under field #1:

        1 { 1: line_an, 2: line_bn, 3: combined, 4: { 3: frequency_mHz } }
        2 { … }          a second such block, all-zero on a panel with no DER
        3 { 2: { 3: power_mW } }   total panel power
        4 { 2: { 3: power_mW } }   a second power-like value, ~1% below #3
        10 { … }         another all-zero block

    Field #1's combined power matches the site `grid` flow exactly, frame for
    frame, so that block — not #3 or #4 — is the authoritative panel meter. The
    earlier decoder read this message as a SingleChannelMeterPower, which put a
    power value (~4 000 000) into the frequency slot and left panel current and
    voltage empty.
    """
    meter = msg.get_msg(1)
    if meter is None:
        return None, None, None
    an, bn, combined = _decode_double_channel(meter)
    if combined is not None:
        combined.freq_mhz = _leaf_value(meter.get_msg(4))
    return an, bn, combined


def _decode_instance(sample: Message) -> CircuitSample | None:
    """Decode one InstancePowerStatsSample."""
    instance_id = sample.get_uint(2)
    if instance_id is None:
        return None
    quality = sample.get_uint(4)

    for tag, kind in _KIND_BY_TAG.items():
        body = sample.get_msg(tag)
        if body is None:
            continue
        cs = CircuitSample(instance_id=instance_id, kind=kind, quality_pct=quality)
        if kind == "two_wire":
            cs.combined = _decode_single_channel(body)
        elif kind == "three_wire":
            cs.line_an, cs.line_bn, cs.combined = _decode_double_channel(body)
        else:  # panel
            cs.line_an, cs.line_bn, cs.combined = _decode_panel(body)
        return cs
    return None


def _decode_site(site_metric: Message) -> dict[str, float]:
    """SiteInstantPower directional flows -> watts, keyed by flow name."""
    power = site_metric.get_msg(2)
    if power is None:
        return {}
    flows: dict[str, float] = {}
    for no, name in SITE_FLOWS.items():
        # 51/52 are millivolts, 53 is millihertz — unsigned. Every other flow is
        # a power in milliwatts, and so a zig-zagged sint32: `grid` in particular
        # goes negative whenever the site exports.
        val = _leaf_value(power.get_msg(no), signed=no not in _UNSIGNED_SITE_FLOWS)
        if val is not None:
            flows[name] = val / 1000.0
    return flows


# --- top-level ----------------------------------------------------------------


def decode_frame(raw: bytes) -> Frame:
    """Decode one realtime telemetry frame. Tolerant of missing sub-trees."""
    frame = Frame()
    root = parse(raw)

    site_block = root.get_msg(2)
    if site_block is not None:
        inner = site_block.get_msg(1)
        if inner is not None:
            frame.site_id = inner.get_str(1)
        instance = site_block.get_msg(2)
        if instance is not None:
            frame.site_instance_id = instance.get_uint(1)

    push = root.get_msg(16)
    if push is None:
        return frame
    env = push.get_msg(3)
    if env is None:
        return frame

    ts = env.get_msg(2)
    if ts is not None:
        frame.epoch_millis = ts.get_uint(1)

    body = env.get_msg(3)
    if body is None:
        return frame

    # body #2: site-level metric(s)
    for site_metric in body.get_msgs(2):
        frame.site_flows.update(_decode_site(site_metric))

    # body #3: resource block(s), each { 1:{1: resourceId}, 2*: sample }
    for block in body.get_msgs(3):
        rid_msg = block.get_msg(1)
        resource_id = rid_msg.get_str(1) if rid_msg else None
        if not resource_id:
            resource_id = ""
        samples: list[CircuitSample] = []
        for sample_msg in block.get_msgs(2):
            cs = _decode_instance(sample_msg)
            if cs is not None:
                samples.append(cs)
        if samples:
            frame.resources.setdefault(resource_id, []).extend(samples)

    return frame
