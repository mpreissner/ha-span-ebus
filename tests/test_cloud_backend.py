"""Tests for the cloud backend's pure mapping functions."""

import json
import threading
import time

import pytest

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
    # nest a serial a couple of levels deep. The scanner keys off the "XC-"
    # prefix real cloud serials carry, so the placeholder must keep that shape.
    inner = pb.field_string(3, "XC-0000-00000")
    mid = pb.field_message(2, inner)
    raw = pb.field_message(1, mid)
    assert cloud._parse_sites_serial(raw) == "XC-0000-00000"
    assert cloud._parse_sites_serial(b"") is None


def test_parse_sites_hardware_ids_dedupes_in_order():
    # sites_with_membership[1] -> 3 -> 1[] -> 2 = the 16-hex hardware id.
    def member(value):
        return pb.field_message(1, pb.field_string(2, value))

    site = pb.field_message(3, member("aaaa0000bbbb1111") + member("cccc2222dddd3333"))
    dup = pb.field_message(3, member("aaaa0000bbbb1111"))
    raw = pb.field_message(1, site) + pb.field_message(1, dup)

    assert cloud.parse_sites_hardware_ids(raw) == [
        "aaaa0000bbbb1111",
        "cccc2222dddd3333",
    ]
    assert cloud.parse_sites_hardware_ids(b"") == []
    # An unfamiliar tree yields nothing rather than garbage.
    assert cloud.parse_sites_hardware_ids(pb.field_string(9, "nope")) == []


def test_build_subscribe_request_names_the_channel_as_subscriber():
    raw = cloud.build_subscribe_request("c:user:DEV-UUID", ["hw1", "hw2"])
    msg = pb.parse(raw)

    # Field 1 is the subscriber, and it is the *channel name* — the crux of the
    # whole flow, since that is what makes a self-generated UUID work.
    assert pb.parse(msg.get_bytes(1)).get_str(1) == "c:user:DEV-UUID"

    entries = msg.get_msgs(2)
    assert [pb.parse(e.get_bytes(1)).get_str(1) for e in entries] == ["hw1", "hw2"]
    for entry in entries:
        pairs = [(t.get_uint(1, 0), t.get_uint(3, 0)) for t in entry.get_msgs(2)]
        assert pairs == list(cloud.SUBSCRIBE_TRAITS)


def test_subscribe_refuses_without_hardware_ids(tmp_path):
    # Better a loud error than a request the server rejects opaquely.
    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid")
    with pytest.raises(cloud.cloud_grpc.GrpcError):
        backend.subscribe("c:u:d")


def test_stream_loop_subscribes_after_attaching(monkeypatch, tmp_path):
    # SPAN publishes nothing until SubscribeAndGetTraits registers the channel,
    # and the mobile client registers only once its stream is up. Same order here.
    monkeypatch.setattr(cloud, "SUBSCRIBE_DELAY_SECONDS", 0.01)
    attached = threading.Event()
    calls: list[tuple[str, bool]] = []

    def fake_stream(token, channel, on_frame, *, stop=None, **kw):
        attached.set()
        while stop is None or not stop():
            time.sleep(0.01)

    monkeypatch.setattr(cloud.cloud_ably, "stream_frames", fake_stream)

    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid")
    monkeypatch.setattr(backend, "bootstrap", lambda: ("tok", "c:u:d"))
    monkeypatch.setattr(
        backend, "subscribe", lambda channel: calls.append((channel, attached.is_set()))
    )

    backend.start(lambda _s: None, lambda _r: None)
    try:
        deadline = time.time() + 5.0
        while not calls and time.time() < deadline:
            time.sleep(0.01)
    finally:
        backend.stop(join_timeout=1.0)

    assert calls == [("c:u:d", True)]


def test_subscribe_failure_does_not_kill_the_stream(monkeypatch, tmp_path):
    # The subscribe runs on its own thread; an error there must be logged and
    # left to the reconnect loop, not raised into nothing.
    monkeypatch.setattr(cloud, "SUBSCRIBE_DELAY_SECONDS", 0.01)
    streaming = threading.Event()

    def fake_stream(token, channel, on_frame, *, stop=None, **kw):
        streaming.set()
        while stop is None or not stop():
            time.sleep(0.01)

    def boom(_channel):
        raise cloud.cloud_grpc.GrpcError(7, "denied", "SubscribeAndGetTraits")

    monkeypatch.setattr(cloud.cloud_ably, "stream_frames", fake_stream)

    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid")
    monkeypatch.setattr(backend, "bootstrap", lambda: ("tok", "c:u:d"))
    monkeypatch.setattr(backend, "subscribe", boom)

    backend.start(lambda _s: None, lambda _r: None)
    try:
        assert streaming.wait(5.0)
        time.sleep(0.1)
        assert backend._thread is not None and backend._thread.is_alive()
    finally:
        backend.stop(join_timeout=1.0)


def test_bootstrap_reads_serial_and_hardware_ids_from_one_call(monkeypatch, tmp_path):
    def member(value):
        return pb.field_message(1, pb.field_string(2, value))

    sites = pb.field_message(
        1, pb.field_string(4, "XC-0000-00000") + pb.field_message(3, member("hw-1"))
    )
    token_request = {"keyName": "k", "nonce": "n", "mac": "m"}
    calls: list[str] = []

    class FakeGrpc:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def get_sites_for_user(self):
            calls.append("sites")
            return sites

        def ably_token(self, request):
            calls.append("ably")
            return pb.field_string(1, json.dumps(token_request))

    monkeypatch.setattr(cloud.cloud_auth, "access_token_from_store", lambda p: "access")
    monkeypatch.setattr(cloud.cloud_grpc, "CloudGrpcClient", FakeGrpc)
    monkeypatch.setattr(
        cloud.cloud_ably,
        "request_token",
        lambda req, **kw: cloud.cloud_ably.AblyTokenDetails(token="realtok"),
    )

    backend = cloud.CloudBackend(tmp_path / "tok.json", "DEV", user_id="user")
    token, channel = backend.bootstrap()

    assert (token, channel) == ("realtok", "c:user:DEV")
    assert backend._serial == "XC-0000-00000"
    assert backend._hardware_ids == ["hw-1"]

    # Topology is resolved once, not on every reconnect.
    backend.bootstrap()
    assert calls == ["sites", "ably", "ably"]


def test_probe_returns_schema_from_first_content_frame(monkeypatch, tmp_path):
    # The config flow uses probe() to prove the channel actually carries data.
    monkeypatch.setattr(cloud, "decode_frame", lambda raw: _frame())

    def fake_stream(token, channel, on_frame, *, stop=None, **kw):
        on_frame(b"frame")
        while stop is None or not stop():
            time.sleep(0.01)

    monkeypatch.setattr(cloud.cloud_ably, "stream_frames", fake_stream)

    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid", serial="XC-1")
    monkeypatch.setattr(backend, "bootstrap", lambda: ("tok", "c:u:d"))
    monkeypatch.setattr(backend, "subscribe", lambda channel: b"")

    schema = backend.probe(timeout=5.0)
    assert schema.serial == "XC-1"
    assert "circuit-54/power" in schema.properties


def test_probe_times_out_on_a_silent_channel(monkeypatch, tmp_path):
    # A channel SPAN does not publish to attaches fine but stays silent; probe
    # must raise rather than report success with no entities.
    def silent_stream(token, channel, on_frame, *, stop=None, **kw):
        while stop is None or not stop():
            time.sleep(0.01)

    monkeypatch.setattr(cloud.cloud_ably, "stream_frames", silent_stream)

    backend = cloud.CloudBackend(tmp_path / "tok.json", "wrong-uuid")
    monkeypatch.setattr(backend, "bootstrap", lambda: ("tok", "c:u:d"))
    monkeypatch.setattr(backend, "subscribe", lambda channel: b"")

    with pytest.raises(TimeoutError):
        backend.probe(timeout=0.2)


def test_probe_propagates_bootstrap_failure(monkeypatch, tmp_path):
    # Auth/channel errors must surface as themselves, not as a timeout — the
    # stream thread's retry loop would otherwise swallow them.
    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid")

    def boom():
        raise cloud.cloud_ably.AblyError("no channel")

    monkeypatch.setattr(backend, "bootstrap", boom)

    with pytest.raises(cloud.cloud_ably.AblyError):
        backend.probe(timeout=0.2)
