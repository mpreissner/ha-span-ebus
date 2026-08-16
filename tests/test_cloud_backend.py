"""Tests for the cloud backend's pure mapping functions."""

import json

from span_bridge import cloud_pb as pb
from span_bridge.backends import cloud
from span_bridge.cloud_telemetry import Channel, CircuitSample, Frame
from span_bridge.models import NodeKind


def _frame() -> Frame:
    return Frame(
        epoch_millis=1786904129000,
        site_id="site1",
        resources={
            "res1": [
                CircuitSample(
                    instance_id=54,
                    kind="two_wire",
                    quality_pct=100,
                    combined=Channel(current_ma=3010, power_mw=674394),
                ),
                CircuitSample(
                    instance_id=1,
                    kind="panel",
                    quality_pct=100,
                    combined=Channel(power_mw=14273602),
                ),
            ]
        },
        site_flows={"grid": 14273.4, "voltage_l1": 120.1, "frequency": 60.01},
    )


def test_schema_nodes_and_kinds():
    schema = cloud.schema_from_frame("<cloud-serial>", _frame())
    assert schema.serial == "<cloud-serial>"
    # circuit 54 is a CIRCUIT node; instance 1 (panel) is CORE.
    assert schema.properties["circuit-54/power"].node_kind is NodeKind.CIRCUIT
    assert schema.properties["panel/power"].node_kind is NodeKind.CORE
    assert schema.properties["site/grid"].node_kind is NodeKind.POWER_FLOWS
    # units follow the slot/flow semantics
    assert schema.properties["circuit-54/current"].unit == "A"
    assert schema.properties["site/voltage_l1"].unit == "V"
    assert schema.properties["site/frequency"].unit == "Hz"


def test_readings_keys_values_and_timestamp():
    readings = cloud.readings_from_frame(_frame(), timestamp=123.0)
    by_key = {r.key: r.value for r in readings}
    assert by_key["circuit-54/power"] == "674.394"
    assert by_key["circuit-54/current"] == "3.010"
    assert "circuit-54/voltage" not in by_key  # two_wire had no voltage
    assert by_key["panel/power"] == "14273.602"
    assert by_key["site/grid"] == "14273.400"
    assert all(r.timestamp == 123.0 for r in readings)


def test_parse_ably_token_ready_token():
    raw = pb.field_string(1, "ready.token.value") + pb.field_string(2, "c:u:d")
    directive = cloud.parse_ably_token(raw)
    assert directive.token == "ready.token.value"
    assert directive.token_request is None
    assert directive.channel == "c:u:d"


def test_parse_ably_token_signed_request():
    signed = json.dumps({"keyName": "v8kFxw.VMjbuw", "nonce": "abc", "mac": "xyz"})
    raw = pb.field_string(1, signed)
    directive = cloud.parse_ably_token(raw, fallback_channel="c:u:d")
    assert directive.token is None
    assert directive.token_request["mac"] == "xyz"
    assert directive.channel == "c:u:d"  # from fallback, field 2 absent


def test_handle_frame_defers_schema_until_content(monkeypatch, tmp_path):
    # An empty (energy/interval) frame must not publish an empty schema; the schema
    # should wait for the first content-bearing power frame.
    empty = Frame(epoch_millis=1, site_id="s", resources={}, site_flows={})
    full = _frame()
    seq = iter([empty, full])
    monkeypatch.setattr(cloud, "decode_frame", lambda raw: next(seq))

    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid", serial="XC-1")
    schemas: list = []
    readings: list = []
    backend._on_schema = schemas.append
    backend._on_reading = readings.append

    backend._handle_frame(b"ignored")  # empty frame -> no schema yet
    assert schemas == []
    assert backend._schema_sent is False

    backend._handle_frame(b"ignored")  # full frame -> schema now emitted
    assert len(schemas) == 1
    assert schemas[0].serial == "XC-1"
    assert backend._schema_sent is True


def test_parse_sites_serial_walks_tree():
    # nest a serial a couple of levels deep
    inner = pb.field_string(3, "<cloud-serial>")
    mid = pb.field_message(2, inner)
    raw = pb.field_message(1, mid)
    assert cloud._parse_sites_serial(raw) == "<cloud-serial>"
    assert cloud._parse_sites_serial(b"") is None
