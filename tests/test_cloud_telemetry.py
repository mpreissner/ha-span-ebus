"""Tests for the realtime telemetry decoder.

The frame here is synthesized with the protobuf writer to mirror the structure
recovered from the app (docs/CLOUD-PROTO.md) — no captured data is committed.
The layout, field numbers and milli-unit conventions match real frames, which
were validated separately against a live capture during development.
"""

from span_bridge import cloud_pb as pb
from span_bridge.cloud_telemetry import decode_frame


def _leaf(value: int) -> bytes:
    """An UnsignedAggregateStats leaf: its `avg` (field #3) as a plain varint."""
    return pb.field_varint(3, value)


def _signed_leaf(value: int) -> bytes:
    """A SignedAggregateStats leaf: `avg` is an sint32, so zig-zag encoded.

    Every power on the wire arrives this way. The encoder is inline rather than
    borrowed from `cloud_pb`, which only ever needs to *read* signed values.
    """
    return pb.field_varint(3, (value << 1) ^ (value >> 63))


def _single(current_ma=None, voltage_mv=None, power_mw=None, freq_mhz=None) -> bytes:
    """SingleChannelMeterPower: 1 current, 2 voltage, 3 power, 4 frequency."""
    out = b""
    if current_ma is not None:
        out += pb.field_message(1, _leaf(current_ma))
    if voltage_mv is not None:
        out += pb.field_message(2, _leaf(voltage_mv))
    if power_mw is not None:
        out += pb.field_message(3, _signed_leaf(power_mw))
    if freq_mhz is not None:
        out += pb.field_message(4, _leaf(freq_mhz))
    return out


def _instance_two_wire(instance_id, power_mw, current_ma, quality=100) -> bytes:
    """InstancePowerStatsSample with a two_wire (#11) summary."""
    body = pb.field_message(1, pb.field_varint(1, 1))  # metric_id
    body += pb.field_varint(2, instance_id)
    body += pb.field_varint(4, quality)
    body += pb.field_message(11, _single(current_ma=current_ma, power_mw=power_mw))
    return body


def _instance_three_wire(instance_id, an, bn, combined, quality=100) -> bytes:
    """InstancePowerStatsSample with a three_wire (#12) summary."""
    dbl = pb.field_message(1, _single(**an))
    dbl += pb.field_message(2, _single(**bn))
    dbl += pb.field_message(3, _single(**combined))
    body = pb.field_varint(2, instance_id)
    body += pb.field_varint(4, quality)
    body += pb.field_message(12, dbl)
    return body


def _instance_off(instance_id, quality=100) -> bytes:
    """A two_wire sample whose current and power slots are present but empty."""
    slots = pb.field_message(1, b"") + pb.field_message(3, b"")
    body = pb.field_varint(2, instance_id) + pb.field_varint(4, quality)
    return body + pb.field_message(11, slots)


def _instance_panel(instance_id, an, bn, combined, freq_mhz, quality=100) -> bytes:
    """InstancePowerStatsSample with a panel_power (#14) summary.

    The panel block is not a SingleChannelMeterPower: its field #1 holds a
    DoubleChannelMeterPower plus frequency at #4, and #3/#4 hold power-only
    blocks the decoder ignores in favour of #1.
    """
    meter = pb.field_message(1, _single(**an))
    meter += pb.field_message(2, _single(**bn))
    meter += pb.field_message(3, _single(**combined))
    meter += pb.field_message(4, _leaf(freq_mhz))
    panel = pb.field_message(1, meter)
    panel += pb.field_message(3, pb.field_message(2, _signed_leaf(2_000_000)))
    body = pb.field_varint(2, instance_id)
    body += pb.field_varint(4, quality)
    body += pb.field_message(14, panel)
    return body


def _site_flow(no, milliwatts):
    """A directional flow: `AggregatePowerStats {1 quality, 2 real_power}`.

    The power is a SignedAggregateStats, hence zig-zagged — `grid` goes negative
    whenever the site exports.
    """
    inner = pb.field_varint(1, 100) + pb.field_message(2, _signed_leaf(milliwatts))
    return pb.field_message(no, inner)


def build_frame() -> bytes:
    # site block: #2 { #1 { #1 siteId } }
    site_block = pb.field_message(1, pb.field_string(1, "site1234"))

    # site metric: { #2: SiteInstantPower{ 1 grid, 2 home, 11 grid_to_home } }
    site_power = _site_flow(1, 2_033_284) + _site_flow(2, 1_980_500) + _site_flow(11, 2_033_284)
    site_metric = pb.field_message(2, site_power)

    # resource block: { #1 {1: resourceId}, #2*: samples }
    # Every sample below stays inside its own apparent power (V × I) — a reading
    # that implies a power factor above 1 is the signature of decoding a power as
    # an unsigned varint, which is exactly what this fixture has to catch.
    samples = (
        pb.field_message(2, _instance_two_wire(54, power_mw=341_980, current_ma=3_010))
        + pb.field_message(
            2,
            # Only `combined` carries power; the legs report current and voltage.
            _instance_three_wire(
                2,
                an=dict(current_ma=31_280, voltage_mv=120_110),
                bn=dict(current_ma=29_023, voltage_mv=119_988),
                combined=dict(current_ma=31_280, voltage_mv=240_099, power_mw=6_872_000),
            ),
        )
        + pb.field_message(
            2,
            _instance_panel(
                1,
                an=dict(current_ma=9_133, voltage_mv=119_964),
                bn=dict(current_ma=10_189, voltage_mv=119_856),
                combined=dict(
                    current_ma=10_189, voltage_mv=239_820, power_mw=2_033_284
                ),
                freq_mhz=60_038,
            ),
        )
        # A circuit that is switched off: proto3 drops the zero scalar but still
        # emits the wrapper, so its slots are present and empty.
        + pb.field_message(2, _instance_off(51))
    )
    resource_block = pb.field_message(1, pb.field_string(1, "res5678")) + samples

    body = pb.field_message(2, site_metric) + pb.field_message(3, resource_block)
    env = (
        pb.field_varint(1, 5)
        + pb.field_message(2, pb.field_varint(1, 1786904129000))
        + pb.field_message(3, body)
    )
    push = pb.field_message(3, env)

    return (
        pb.field_message(1, pb.field_varint(1, 1))  # header
        + pb.field_message(2, site_block)
        + pb.field_message(16, push)
    )


def test_decode_frame_top_level():
    f = decode_frame(build_frame())
    assert f.site_id == "site1234"
    assert f.epoch_millis == 1786904129000
    assert set(f.resources) == {"res5678"}


def test_site_flows_scaled_to_watts():
    f = decode_frame(build_frame())
    assert round(f.site_flows["grid"], 3) == 2033.284
    assert round(f.site_flows["home"], 1) == 1980.5
    assert round(f.site_flows["grid_to_home"], 3) == 2033.284


def test_two_wire_circuit_power_and_current():
    f = decode_frame(build_frame())
    by_id = {s.instance_id: s for s in f.resources["res5678"]}
    c = by_id[54]
    assert c.kind == "two_wire"
    assert c.quality_pct == 100
    assert round(c.power_w, 3) == 341.980
    assert round(c.combined.current_a, 3) == 3.010
    # A 120 V branch never sends a voltage slot at all — absent, not zero.
    assert c.combined.voltage_mv is None


def test_three_wire_main_legs_and_combined():
    f = decode_frame(build_frame())
    by_id = {s.instance_id: s for s in f.resources["res5678"]}
    main = by_id[2]
    assert main.kind == "three_wire"
    # Only `combined` carries the circuit's power; the legs carry current/voltage.
    assert round(main.power_w, 1) == 6872.0
    assert main.line_an.power_mw is None and main.line_bn.power_mw is None
    assert round(main.combined.voltage_v, 1) == 240.1
    assert round(main.line_an.voltage_v, 2) == 120.11
    assert round(main.line_bn.current_a, 3) == 29.023


def test_panel_block_carries_legs_current_and_frequency():
    # The panel is the only node that reports voltage, and its metering lives
    # under field #1 rather than in a SingleChannelMeterPower.
    f = decode_frame(build_frame())
    panel = {s.instance_id: s for s in f.resources["res5678"]}[1]
    assert panel.kind == "panel"
    assert round(panel.line_an.voltage_v, 3) == 119.964
    assert round(panel.line_bn.voltage_v, 3) == 119.856
    assert round(panel.combined.current_a, 3) == 10.189
    assert round(panel.combined.freq_hz, 3) == 60.038
    # #1's combined power is the authoritative total, matching the grid flow.
    assert round(panel.power_w, 1) == round(f.site_flows["grid"], 1)


def test_present_but_empty_slots_read_as_zero():
    # proto3 omits a zero scalar while still emitting its wrapper. Reading that
    # as "not measured" would freeze a switched-off circuit at its last value.
    f = decode_frame(build_frame())
    off = {s.instance_id: s for s in f.resources["res5678"]}[51]
    assert off.power_w == 0.0
    assert off.combined.current_a == 0.0
    # The voltage slot really is absent here, and must stay distinguishable.
    assert off.combined.voltage_mv is None


def test_power_is_read_as_a_zigzagged_sint():
    # A power leaf is a SignedAggregateStats, so its avg is an sint32. Read as a
    # plain varint, a positive reading comes back exactly doubled: 341.980 W
    # would have been 683.960 W, and every circuit would show a power factor
    # near twice its real one.
    f = decode_frame(build_frame())
    c = {s.instance_id: s for s in f.resources["res5678"]}[54]
    assert c.combined.power_mw == 341_980
    # The varint actually on the wire is twice that — the decoder undoes it.
    assert pb.parse(_signed_leaf(341_980)).get_uint(3) == 683_960
    # Current in the same sample is unsigned and must be left alone.
    assert c.combined.current_ma == 3_010
    assert pb.parse(_leaf(3_010)).get_uint(3) == 3_010


def test_export_reads_as_negative_power():
    # Zig-zag makes an export an odd varint. Decoded unsigned it would surface as
    # a large positive number, so a site sending power back to the grid would
    # read as an enormous import.
    site_power = _site_flow(1, -1_500_250) + _site_flow(23, 2_400_000)
    body = pb.field_message(2, pb.field_message(2, site_power))
    env = pb.field_varint(1, 5) + pb.field_message(3, body)
    raw = pb.field_message(16, pb.field_message(3, env))

    f = decode_frame(raw)
    assert round(f.site_flows["grid"], 3) == -1500.250
    assert round(f.site_flows["solar_to_grid"], 1) == 2400.0


def test_missing_subtrees_are_tolerated():
    # An empty message decodes to an empty-but-valid Frame.
    f = decode_frame(b"")
    assert f.site_id is None
    assert f.resources == {}
    assert f.site_flows == {}
