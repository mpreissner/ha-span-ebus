"""Tests for the realtime telemetry decoder.

The frame here is synthesized with the protobuf writer to mirror the structure
recovered from the app (docs/CLOUD-PROTO.md) — no captured data is committed.
The layout, field numbers and milli-unit conventions match real frames, which
were validated separately against a live capture during development.
"""

from span_bridge import cloud_pb as pb
from span_bridge.cloud_telemetry import decode_frame


def _leaf(value: int) -> bytes:
    """A measurement wrapper carrying its scalar at field #3."""
    return pb.field_varint(3, value)


def _single(current_ma=None, voltage_mv=None, power_mw=None, freq_mhz=None) -> bytes:
    """SingleChannelMeterPower: 1 current, 2 voltage, 3 power, 4 frequency."""
    out = b""
    if current_ma is not None:
        out += pb.field_message(1, _leaf(current_ma))
    if voltage_mv is not None:
        out += pb.field_message(2, _leaf(voltage_mv))
    if power_mw is not None:
        out += pb.field_message(3, _leaf(power_mw))
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
    panel += pb.field_message(3, pb.field_message(2, _leaf(4_000_000)))
    body = pb.field_varint(2, instance_id)
    body += pb.field_varint(4, quality)
    body += pb.field_message(14, panel)
    return body


def _site_flow(no, milli):
    """A directional flow: { <no>: {1: quality, 2: {3: milliwatts}} }."""
    inner = pb.field_varint(1, 100) + pb.field_message(2, _leaf(milli))
    return pb.field_message(no, inner)


def build_frame() -> bytes:
    # site block: #2 { #1 { #1 siteId } }
    site_block = pb.field_message(1, pb.field_string(1, "site1234"))

    # site metric: { #2: SiteInstantPower{ 1 grid, 2 home, 11 grid_to_home } }
    site_power = _site_flow(1, 14_273_400) + _site_flow(2, 14_276_100) + _site_flow(11, 14_273_400)
    site_metric = pb.field_message(2, site_power)

    # resource block: { #1 {1: resourceId}, #2*: samples }
    samples = (
        pb.field_message(2, _instance_two_wire(54, power_mw=674_394, current_ma=3_010))
        + pb.field_message(
            2,
            _instance_three_wire(
                2,
                an=dict(current_ma=31_280, voltage_mv=120_110, power_mw=7_140_000),
                bn=dict(current_ma=29_023, voltage_mv=119_988, power_mw=7_139_000),
                combined=dict(current_ma=31_280, voltage_mv=240_099, power_mw=14_279_434),
            ),
        )
        + pb.field_message(
            2,
            _instance_panel(
                1,
                an=dict(current_ma=9_133, voltage_mv=119_964, power_mw=1_095_000),
                bn=dict(current_ma=10_189, voltage_mv=119_856, power_mw=1_220_000),
                combined=dict(
                    current_ma=10_189, voltage_mv=239_820, power_mw=14_273_400
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
    assert round(f.site_flows["grid"], 1) == 14273.4
    assert round(f.site_flows["home"], 1) == 14276.1
    assert round(f.site_flows["grid_to_home"], 1) == 14273.4


def test_two_wire_circuit_power_and_current():
    f = decode_frame(build_frame())
    by_id = {s.instance_id: s for s in f.resources["res5678"]}
    c = by_id[54]
    assert c.kind == "two_wire"
    assert c.quality_pct == 100
    assert round(c.power_w, 3) == 674.394
    assert round(c.combined.current_a, 3) == 3.010
    # A 120 V branch never sends a voltage slot at all — absent, not zero.
    assert c.combined.voltage_mv is None


def test_three_wire_main_legs_and_combined():
    f = decode_frame(build_frame())
    by_id = {s.instance_id: s for s in f.resources["res5678"]}
    main = by_id[2]
    assert main.kind == "three_wire"
    # combined power is the true total, ≈ the site grid figure
    assert round(main.power_w, 1) == 14279.4
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


def test_missing_subtrees_are_tolerated():
    # An empty message decodes to an empty-but-valid Frame.
    f = decode_frame(b"")
    assert f.site_id is None
    assert f.resources == {}
    assert f.site_flows == {}
