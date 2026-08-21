"""Tests for the cloud backend's pure mapping functions."""

import base64
import json
import threading
import time
from dataclasses import replace

import pytest
from span_client import backend as cloud
from span_client import cloud_pb as pb
from span_client.cloud_commands import SwitchTarget
from span_client.cloud_telemetry import Channel, CircuitSample, Frame
from span_client.cloud_traits import CircuitInfo
from span_client.models import DataType, NodeKind


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
                    combined=Channel(current_ma=3010, power_mw=341980),
                ),
                # A three_wire sample the trait snapshot does not list: the feed.
                CircuitSample(
                    instance_id=2,
                    kind="three_wire",
                    quality_pct=100,
                    combined=Channel(current_ma=10021, power_mw=2003000),
                ),
                CircuitSample(
                    instance_id=1,
                    kind="panel",
                    quality_pct=100,
                    combined=Channel(current_ma=10189, power_mw=2033284, freq_mhz=60038),
                    line_an=Channel(current_ma=9133, voltage_mv=119964),
                    line_bn=Channel(current_ma=10189, voltage_mv=119856),
                ),
            ]
        },
        site_flows={"grid": 2033.284, "voltage_l1": 120.1, "frequency": 60.01},
    )


def _circuits() -> dict[int, CircuitInfo]:
    """A trait snapshot mapping, with placeholder labels — 2 is absent (the feed)."""
    return {
        54: CircuitInfo(instance_id=54, label="Branch A", spaces=(33,), breaker_amps=20),
    }


def _switch(instance_id: int) -> SwitchTarget:
    return SwitchTarget(
        resource_id="a1b2c3d4e5f60718", instance_id=instance_id, metadata=(1, None, 31, 1)
    )


def _switchable_circuits(relay_closed: bool | None = True) -> dict[int, CircuitInfo]:
    """`_circuits()` where circuit 54 also resolved a switch trait.

    The feed (2) deliberately keeps no entry, so a snapshot that never mentioned
    an instance cannot end up with a relay control.
    """
    circuits = _circuits()
    circuits[54] = replace(circuits[54], relay_closed=relay_closed, switch=_switch(54))
    return circuits


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


def test_schema_advertises_only_properties_the_wire_feeds():
    # Every advertised property must have a reading, or HA gets a dead entity.
    frame = _frame()
    schema = cloud.schema_from_frame("<cloud-serial>", frame)
    keys = {r.key for r in cloud.readings_from_frame(frame)}
    assert set(schema.properties) <= keys

    # Circuits get power and current only; voltage and frequency are the panel's.
    circuit_props = {
        spec.property_id for spec in schema.properties.values() if spec.node_id == "circuit-54"
    }
    assert circuit_props == {"power", "current"}
    panel_props = {
        spec.property_id for spec in schema.properties.values() if spec.node_id == "panel"
    }
    assert panel_props == {
        "power",
        "current",
        "current_l1",
        "current_l2",
        "voltage_l1",
        "voltage_l2",
        "frequency",
    }


def test_schema_names_nodes_from_the_trait_snapshot():
    schema = cloud.schema_from_frame("<cloud-serial>", _frame(), _circuits())
    names = {spec.node_id: spec.node_name for spec in schema.properties.values()}
    assert names["circuit-54"] == "Branch A"
    assert names["panel"] == "Panel"
    assert names["site"] == "Site"
    # An instance missing from a non-empty snapshot is the main feed, not a circuit.
    assert names["feed-2"] == "Main feed"
    assert schema.properties["feed-2/power"].node_kind is NodeKind.LUGS
    # The node *id* stays keyed on the instance id, so renaming a circuit in the
    # SPAN app does not orphan its entity history.
    assert "circuit-54/power" in schema.properties


def test_duplicate_labels_are_qualified_by_panel_space():
    frame = _frame()
    frame.resources["res1"].append(
        CircuitSample(
            instance_id=55,
            kind="two_wire",
            combined=Channel(current_ma=100, power_mw=6000),
        )
    )
    circuits = _circuits()
    circuits[55] = CircuitInfo(instance_id=55, label="Branch A", spaces=(35,))

    schema = cloud.schema_from_frame("<cloud-serial>", frame, circuits)
    names = {spec.node_id: spec.node_name for spec in schema.properties.values()}
    assert names["circuit-54"] == "Branch A (33)"
    assert names["circuit-55"] == "Branch A (35)"


def test_without_a_snapshot_every_sample_stays_a_circuit():
    # No labels means no way to tell the feed apart, so nothing is reclassified
    # and names fall back to the ids.
    schema = cloud.schema_from_frame("<cloud-serial>", _frame())
    assert "circuit-2/power" in schema.properties
    assert "feed-2/power" not in schema.properties
    assert schema.properties["circuit-54/power"].node_name is None


def test_readings_keys_values_and_timestamp():
    readings = cloud.readings_from_frame(_frame(), timestamp=123.0)
    by_key = {r.key: r.value for r in readings}
    assert by_key["circuit-54/power"] == "341.980"
    assert by_key["circuit-54/current"] == "3.010"
    assert "circuit-54/voltage" not in by_key  # two_wire had no voltage
    assert by_key["panel/power"] == "2033.284"
    assert by_key["site/grid"] == "2033.284"
    assert all(r.timestamp == 123.0 for r in readings)


def test_panel_readings_cover_legs_and_frequency():
    by_key = {r.key: r.value for r in cloud.readings_from_frame(_frame(), timestamp=1.0)}
    assert by_key["panel/voltage_l1"] == "119.964"
    assert by_key["panel/voltage_l2"] == "119.856"
    assert by_key["panel/frequency"] == "60.038"
    # Per-leg current is what answers "is the panel balanced?"; `current` is the
    # combined channel's, which the wire reports as the larger leg, not the sum.
    assert by_key["panel/current_l1"] == "9.133"
    assert by_key["panel/current_l2"] == "10.189"
    assert by_key["panel/current"] == "10.189"


def test_readings_keys_follow_the_node_mapping():
    frame = _frame()
    by_key = {r.key: r.value for r in cloud.readings_from_frame(frame, 1.0, _circuits())}
    # Same node ids the schema advertised, so readings land on real entities.
    assert by_key["feed-2/power"] == "2003.000"
    assert "circuit-2/power" not in by_key


def test_a_zero_reading_is_published_not_dropped():
    # A switched-off circuit reports 0, and must not be left at its last value.
    frame = Frame(
        resources={
            "res1": [
                CircuitSample(
                    instance_id=51,
                    kind="two_wire",
                    combined=Channel(current_ma=0, power_mw=0),
                )
            ]
        }
    )
    by_key = {r.key: r.value for r in cloud.readings_from_frame(frame, 1.0)}
    assert by_key["circuit-51/power"] == "0.000"
    assert by_key["circuit-51/current"] == "0.000"


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


def test_schema_waits_briefly_for_the_circuit_labels(monkeypatch, tmp_path):
    # Entity names are fixed at creation, so the schema holds back until the
    # subscribe snapshot has named the circuits — but not forever.
    monkeypatch.setattr(cloud, "decode_frame", lambda raw: _frame())
    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid", serial="XC-1")
    schemas: list = []
    backend._on_schema = schemas.append
    backend._label_deadline = time.monotonic() + 60.0

    backend._handle_frame(b"frame")  # snapshot not in yet -> hold
    assert schemas == []

    backend._circuits = _circuits()
    backend._handle_frame(b"frame")
    assert len(schemas) == 1
    names = {s.node_id: s.node_name for s in schemas[0].properties.values()}
    assert names["circuit-54"] == "Branch A"


def test_schema_is_released_once_the_subscribe_answers(monkeypatch, tmp_path):
    # An unfamiliar snapshot yields no labels; waiting out the deadline would
    # only delay the entities, so the subscribe's return releases the schema.
    monkeypatch.setattr(cloud, "SUBSCRIBE_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(cloud.cloud_ably, "stream_frames", lambda *a, **kw: kw["stop"] and None)
    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid", serial="XC-1")
    backend._label_deadline = time.monotonic() + 60.0
    monkeypatch.setattr(backend, "subscribe", lambda channel: b"unfamiliar")

    backend._schedule_subscribe("c:u:d")
    deadline = time.time() + 5.0
    while backend._label_deadline and time.time() < deadline:
        time.sleep(0.01)
    assert backend._label_deadline == 0.0

    monkeypatch.setattr(cloud, "decode_frame", lambda raw: _frame())
    schemas: list = []
    backend._on_schema = schemas.append
    backend._handle_frame(b"frame")
    assert len(schemas) == 1
    assert schemas[0].properties["circuit-54/power"].node_name is None


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

    monkeypatch.setattr(cloud, "SUBSCRIBE_DELAY_SECONDS", 0.01)

    # A live channel pushes a frame a second, so keep pushing: the first frames
    # can arrive before the subscribe snapshot has named the circuits.
    def fake_stream(token, channel, on_frame, *, stop=None, **kw):
        while stop is None or not stop():
            on_frame(b"frame")
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


def test_relay_is_advertised_only_where_a_switch_trait_was_resolved():
    schema = cloud.schema_from_frame("<cloud-serial>", _frame(), _switchable_circuits())

    relay = schema.properties["circuit-54/relay"]
    assert relay.settable is True
    assert relay.datatype is DataType.ENUM
    assert relay.enum_values == cloud.RELAY_STATES
    assert relay.node_name == "Branch A"

    # The feed and the panel are metering nodes, and no snapshot entry addresses
    # them — offering a switch there would mean commanding something else.
    assert "feed-2/relay" not in schema.properties
    assert "panel/relay" not in schema.properties
    # And a snapshot without switch traits leaves every circuit read-only.
    assert (
        "circuit-54/relay"
        not in cloud.schema_from_frame("<cloud-serial>", _frame(), _circuits()).properties
    )


def test_relay_readings_report_the_snapshot_state():
    def state(relay_closed):
        readings = cloud.readings_from_frame(_frame(), 1.0, _switchable_circuits(relay_closed))
        return {r.key: r.value for r in readings}["circuit-54/relay"]

    assert state(True) == "CLOSED"
    assert state(False) == "OPEN"
    # The panel really does report UNKNOWN; it must not be flattened to OPEN,
    # which would show a live breaker as off.
    assert state(None) == "UNKNOWN"


def test_every_advertised_relay_has_a_reading():
    # Same contract as the metering properties: no dead entities.
    frame = _frame()
    circuits = _switchable_circuits()
    schema = cloud.schema_from_frame("<cloud-serial>", frame, circuits)
    keys = {r.key for r in cloud.readings_from_frame(frame, 1.0, circuits)}
    assert set(schema.properties) <= keys


def test_parse_relay_command_reads_the_obvious_words_and_refuses_the_rest():
    assert cloud.parse_relay_command("CLOSED") is True
    assert cloud.parse_relay_command("open") is False
    for word in ("on", "true", "1", " Close "):
        assert cloud.parse_relay_command(word) is True
    for word in ("off", "false", "0"):
        assert cloud.parse_relay_command(word) is False
    # Anything ambiguous is dropped rather than guessed: the wrong guess opens a
    # breaker.
    for junk in ("", "toggle", "maybe", "2", "OPENISH"):
        assert cloud.parse_relay_command(junk) is None


# The caller's SPAN user id, which a command names as its requester.
USER_ID = "00112233445566778899aabbccddeeff"


def _token(username: str | None = USER_ID) -> str:
    """A JWT-shaped access token carrying (or omitting) the `username` claim."""
    claims = {} if username is None else {"username": username}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{body}.signature"


class _FakeGrpc:
    """Stands in for CloudGrpcClient, recording what got posted."""

    def __init__(self, sent: list[bytes], token: str, *, host: str | None = None):
        self._sent = sent
        self.token = token
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send_messages(self, request: bytes) -> bytes:
        self._sent.append(request)
        return b""


def _wire_grpc(monkeypatch, sent: list[bytes], token: str | None = None) -> list[_FakeGrpc]:
    """Point the backend at fake gRPC; returns the clients it opens, in order."""
    clients: list[_FakeGrpc] = []

    def open_client(tok, **kw):
        clients.append(_FakeGrpc(sent, tok, **kw))
        return clients[-1]

    monkeypatch.setattr(
        cloud.cloud_auth,
        "access_token_from_store",
        lambda store: _token() if token is None else token,
    )
    monkeypatch.setattr(cloud.cloud_grpc, "CloudGrpcClient", open_client)
    # The post-command snapshot re-read waits on a thread; keep the test quick.
    monkeypatch.setattr(cloud, "POST_COMMAND_REFRESH_SECONDS", 0.01)
    return clients


def test_send_command_posts_a_switch_request_for_the_addressed_instance(monkeypatch, tmp_path):
    sent: list[bytes] = []
    _wire_grpc(monkeypatch, sent)

    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid")
    backend._circuits = _switchable_circuits()
    readings: list = []
    backend._on_reading = readings.append

    backend.send_command("circuit-54/relay", "OPEN")

    assert len(sent) == 1
    message = pb.parse(sent[0]).get_msgs(1)[0]
    # Right trait, right instance — the only two ways to hit the wrong breaker.
    assert message.get_msg(1).get_uint(3) == 31
    assert message.get_msg(2).get_msg(2).get_uint(1) == 54
    assert message.get_msg(2).get_msg(1).get_str(1) == "a1b2c3d4e5f60718"
    # …and signed as this user, the only requester the server accepts.
    assert message.get_msg(14).get_msg(1).get_msg(2).get_str(1) == USER_ID
    payload = message.get_msg(14).get_msg(2).get_bytes(1)
    assert payload == cloud.cloud_commands.disconnect_payload()

    # The intended state is reported at once, so the UI does not snap back while
    # the relay travels, and the local snapshot agrees with what we sent.
    assert [(r.key, r.value) for r in readings] == [("circuit-54/relay", "OPEN")]
    assert backend._circuits[54].relay_closed is False

    backend.send_command("circuit-54/relay", "on")
    assert (
        pb.parse(sent[1]).get_msgs(1)[0].get_msg(14).get_msg(2).get_bytes(1)
        == cloud.cloud_commands.release_payload()
    )
    assert backend._circuits[54].relay_closed is True


def test_send_command_stays_silent_on_anything_it_cannot_address(monkeypatch, tmp_path):
    sent: list[bytes] = []
    _wire_grpc(monkeypatch, sent)

    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid")
    backend._circuits = _switchable_circuits()
    readings: list = []
    backend._on_reading = readings.append

    backend.send_command("circuit-54/power", "0")  # not a writable property
    backend.send_command("feed-2/relay", "OPEN")  # no snapshot entry
    backend.send_command("panel/relay", "OPEN")  # not a circuit node
    backend.send_command("circuit-99/relay", "OPEN")  # unknown instance
    backend.send_command("circuit-54/relay", "toggle")  # unreadable value

    assert sent == []
    assert readings == []
    assert backend._circuits[54].relay_closed is True


def test_the_requester_follows_the_token_not_the_configured_user(monkeypatch, tmp_path):
    # The server resolves the caller from the same token we send, so the claim in
    # it wins; a configured id that has drifted would only earn PERMISSION_DENIED.
    sent: list[bytes] = []
    _wire_grpc(monkeypatch, sent, token=_token("ffffffffffffffffffffffffffffffff"))

    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid", user_id="stale")
    backend._circuits = _switchable_circuits()

    backend.send_command("circuit-54/relay", "OPEN")

    requester = pb.parse(sent[0]).get_msgs(1)[0].get_msg(14).get_msg(1)
    assert requester.get_msg(2).get_str(1) == "ffffffffffffffffffffffffffffffff"


def test_a_command_is_refused_when_nothing_names_the_requester(monkeypatch, tmp_path):
    # Better to fail loudly than to post a write the server will reject anyway.
    sent: list[bytes] = []
    _wire_grpc(monkeypatch, sent, token=_token(None))

    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid")
    backend._circuits = _switchable_circuits()

    with pytest.raises(RuntimeError, match="requester"):
        backend.send_command("circuit-54/relay", "OPEN")
    assert sent == []


def test_a_configured_user_id_covers_a_token_without_the_claim(monkeypatch, tmp_path):
    sent: list[bytes] = []
    _wire_grpc(monkeypatch, sent, token=_token(None))

    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid", user_id=USER_ID)
    backend._circuits = _switchable_circuits()

    backend.send_command("circuit-54/relay", "OPEN")

    requester = pb.parse(sent[0]).get_msgs(1)[0].get_msg(14).get_msg(1)
    assert requester.get_msg(2).get_str(1) == USER_ID


def test_relay_state_refreshes_on_a_timer_and_announces_changes(monkeypatch, tmp_path):
    # Telemetry never carries switch state, and we do not read the trait channel,
    # so a breaker toggled in the SPAN app only reaches HA via this re-read.
    monkeypatch.setattr(cloud, "SUBSCRIBE_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(cloud, "SWITCH_REFRESH_SECONDS", 0.01)
    states = iter([True, False])
    monkeypatch.setattr(
        cloud,
        "parse_trait_snapshot",
        lambda raw: _switchable_circuits(next(states, False)),
    )

    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid")
    monkeypatch.setattr(backend, "subscribe", lambda channel: b"snapshot")
    readings: list = []
    backend._on_reading = readings.append

    try:
        backend._schedule_subscribe("c:u:d")
        deadline = time.time() + 5.0
        while not readings and time.time() < deadline:
            time.sleep(0.01)
    finally:
        backend.stop(join_timeout=1.0)

    # The first snapshot is what the schema is built from, so it announces
    # nothing; the refresh that follows publishes the state it found.
    assert readings
    assert readings[0].key == "circuit-54/relay"
    assert readings[0].value == "OPEN"


def test_probe_propagates_bootstrap_failure(monkeypatch, tmp_path):
    # Auth/channel errors must surface as themselves, not as a timeout — the
    # stream thread's retry loop would otherwise swallow them.
    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid")

    def boom():
        raise cloud.cloud_ably.AblyError("no channel")

    monkeypatch.setattr(backend, "bootstrap", boom)

    with pytest.raises(cloud.cloud_ably.AblyError):
        backend.probe(timeout=0.2)


def test_the_watchdog_ends_a_stream_that_stopped_carrying_frames(monkeypatch, tmp_path):
    # The failure this exists for: the socket is fine and Ably keeps sending
    # keepalives, but SPAN has stopped publishing. Nothing raises, so without the
    # watchdog the stream thread blocks forever and every entity freezes on its
    # last value.
    monkeypatch.setattr(cloud, "FRAME_SILENCE_SECONDS", 0.05)
    monkeypatch.setattr(cloud, "SUBSCRIBE_DELAY_SECONDS", 0.01)

    attaches: list[str] = []

    def silent_stream(token, channel, on_frame, *, stop=None, **kw):
        attaches.append(channel)
        # Stand in for the keepalive traffic: connected, consulted, no frames.
        while not stop():
            time.sleep(0.005)

    monkeypatch.setattr(cloud.cloud_ably, "stream_frames", silent_stream)

    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid", reconnect_seconds=0.01)
    monkeypatch.setattr(backend, "bootstrap", lambda: ("tok", "c:u:d"))
    monkeypatch.setattr(backend, "subscribe", lambda channel: b"")

    try:
        backend.start(lambda schema: None, lambda reading: None)
        deadline = time.time() + 5.0
        while len(attaches) < 2 and time.time() < deadline:
            time.sleep(0.01)
    finally:
        backend.stop(join_timeout=1.0)

    # It reattached rather than sitting on the dead stream.
    assert len(attaches) >= 2


def test_a_stream_still_delivering_frames_is_left_alone(monkeypatch, tmp_path):
    # The watchdog must not cut a healthy stream: every frame is a reprieve.
    monkeypatch.setattr(cloud, "FRAME_SILENCE_SECONDS", 0.2)
    monkeypatch.setattr(cloud, "SUBSCRIBE_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(cloud, "decode_frame", lambda raw: _frame())

    attaches: list[str] = []

    def live_stream(token, channel, on_frame, *, stop=None, **kw):
        attaches.append(channel)
        while not stop():
            on_frame(b"frame")
            time.sleep(0.01)

    monkeypatch.setattr(cloud.cloud_ably, "stream_frames", live_stream)

    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid", reconnect_seconds=0.01)
    monkeypatch.setattr(backend, "bootstrap", lambda: ("tok", "c:u:d"))
    monkeypatch.setattr(backend, "subscribe", lambda channel: b"")

    try:
        backend.start(lambda schema: None, lambda reading: None)
        time.sleep(1.0)  # several watchdog intervals' worth of healthy stream
    finally:
        backend.stop(join_timeout=1.0)

    assert len(attaches) == 1


def test_a_decodable_frame_is_what_counts_as_liveness(monkeypatch, tmp_path):
    # Bytes off the socket are not evidence SPAN is publishing anything we can
    # use, so garbage must not hold the watchdog off.
    def boom(raw):
        raise ValueError("not a frame")

    monkeypatch.setattr(cloud, "decode_frame", boom)

    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid")
    backend._last_frame = 0.0
    backend._handle_frame(b"garbage")

    assert backend._last_frame == 0.0


def test_a_drop_after_live_telemetry_is_retried_promptly_and_quietly(caplog, tmp_path):
    # The failure this exists for: Ably resets a long-lived SSE socket every so
    # often, and the old loop called each one an ERROR. Twenty-five hours of that
    # is 314 identical ERROR lines for a stream that healed itself every time.
    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid", reconnect_seconds=5.0)
    backend._dead_attaches = 4  # a rough patch, now over

    with caplog.at_level("INFO", logger=cloud.log.name):
        delay = backend._note_attach_ended(delivered=True, reason="cloud stream error (reset)")

    assert 4.0 <= delay <= 6.0
    assert backend._dead_attaches == 0
    assert not [r for r in caplog.records if r.levelname in ("ERROR", "WARNING")]


def test_attaches_that_deliver_nothing_back_off_and_are_loud_exactly_once(caplog, tmp_path):
    # A cloud-side outage must not become a re-auth storm, and must not bury the
    # log either: one ERROR says it is down, the rest is INFO.
    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid", reconnect_seconds=5.0)

    with caplog.at_level("INFO", logger=cloud.log.name):
        delays = [
            backend._note_attach_ended(delivered=False, reason="cloud stream error (refused)")
            for _ in range(12)
        ]

    assert backend._dead_attaches == 12
    # Geometric until the ceiling, then pinned there.
    assert delays[0] <= delays[1] <= delays[2]
    assert delays[-1] >= cloud.RECONNECT_BACKOFF_MAX_SECONDS * 0.8
    assert all(d <= cloud.RECONNECT_BACKOFF_MAX_SECONDS for d in delays)
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1


def test_a_silent_attach_counts_as_dead_however_healthy_the_socket_was(monkeypatch, tmp_path):
    # An attach whose channel never published is a failure even though nothing
    # raised — otherwise a lapsed registration retries at full speed forever.
    monkeypatch.setattr(cloud, "FRAME_SILENCE_SECONDS", 0.05)
    monkeypatch.setattr(cloud, "SUBSCRIBE_DELAY_SECONDS", 0.01)

    def silent_stream(token, channel, on_frame, *, stop=None, **kw):
        while not stop():
            time.sleep(0.005)

    monkeypatch.setattr(cloud.cloud_ably, "stream_frames", silent_stream)

    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid", reconnect_seconds=0.01)
    monkeypatch.setattr(backend, "bootstrap", lambda: ("tok", "c:u:d"))
    monkeypatch.setattr(backend, "subscribe", lambda channel: b"")

    try:
        backend.start(lambda schema: None, lambda reading: None)
        deadline = time.time() + 5.0
        while backend._dead_attaches < 2 and time.time() < deadline:
            time.sleep(0.01)
    finally:
        backend.stop(join_timeout=1.0)

    assert backend._dead_attaches >= 2


def test_an_attach_that_delivered_nothing_re_resolves_the_topology(tmp_path):
    # The failure this exists for: bootstrap caches the hardware ids from the
    # first GetSitesForUser and every later reattach re-subscribes those same
    # ids. Once they go stale, SubscribeAndGetTraits keeps being accepted and the
    # channel keeps being silent — reloading the integration was the only cure.
    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid")
    backend._hardware_ids = ["hw-old"]

    backend._note_attach_ended(delivered=False, reason="no telemetry for 90s")

    assert backend._hardware_ids == []


def test_a_stream_that_carried_telemetry_keeps_its_topology(tmp_path):
    # Re-resolving costs a gRPC round-trip on every reconnect; a stream that was
    # working has no reason to pay it.
    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid")
    backend._hardware_ids = ["hw-1"]

    backend._note_attach_ended(delivered=True, reason="cloud stream error (reset)")

    assert backend._hardware_ids == ["hw-1"]


def test_a_changed_hardware_id_set_is_reported(monkeypatch, caplog, tmp_path):
    # The line that proves the diagnosis next time it happens.
    ids = iter([["hw-old"], ["hw-new"]])

    class FakeGrpc:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_sites_for_user(self):
            return b"sites"

        def ably_token(self, request):
            return b"token"

    monkeypatch.setattr(cloud.cloud_auth, "access_token_from_store", lambda path: "at")
    monkeypatch.setattr(cloud.cloud_grpc, "CloudGrpcClient", lambda *a, **kw: FakeGrpc())
    monkeypatch.setattr(cloud, "parse_sites_hardware_ids", lambda sites: next(ids))
    monkeypatch.setattr(cloud, "_parse_sites_serial", lambda sites: "SN-1")
    monkeypatch.setattr(
        cloud,
        "parse_ably_token",
        lambda raw, fallback_channel=None: cloud.AblyDirective(
            token="tok", token_request=None, channel="c:u:d"
        ),
    )

    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid", user_id="u")
    backend.bootstrap()
    assert backend._hardware_ids == ["hw-old"]

    # A dead attach drops the cache; the next bootstrap sees a different set.
    backend._note_attach_ended(delivered=False, reason="no telemetry for 90s")
    with caplog.at_level("WARNING", logger=cloud.log.name):
        backend.bootstrap()

    assert backend._hardware_ids == ["hw-new"]
    assert "subscribable hardware ids changed" in caplog.text


def test_the_backoff_ceiling_stays_under_the_staleness_threshold():
    # A ceiling longer than the coordinator's STALE_AFTER_SECONDS means the panel
    # can be back while the entities are still unavailable, waiting on our timer.
    # Hard-coded rather than imported: the suite covers span_client, which does
    # not depend on homeassistant. Keep in step with coordinator.py.
    coordinator_stale_after_seconds = 180.0

    assert coordinator_stale_after_seconds > cloud.RECONNECT_BACKOFF_MAX_SECONDS


def test_a_dead_credential_is_announced_once_however_often_it_is_hit(monkeypatch, tmp_path):
    # The backend keeps retrying a rejected credential — a revoked token is
    # indistinguishable from a restored one until you try — so the host must be
    # told exactly once, or every retry raises another login prompt.
    asked: list[str] = []

    def reject(path):
        raise cloud.cloud_auth.CloudCredentialsRejected("refresh token revoked")

    monkeypatch.setattr(cloud.cloud_auth, "access_token_from_store", reject)
    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid", on_auth_failed=asked.append)

    for _ in range(3):
        with pytest.raises(cloud.cloud_auth.CloudCredentialsRejected):
            backend.bootstrap()

    assert asked == ["refresh token revoked"]


def test_a_transient_auth_failure_does_not_ask_the_user_to_log_in(monkeypatch, tmp_path):
    # Cognito being unreachable or unhappy is not something a new password fixes.
    def wobble(path):
        raise cloud.cloud_auth.CloudAuthError("InitiateAuth failed (500): oops")

    monkeypatch.setattr(cloud.cloud_auth, "access_token_from_store", wobble)
    asked: list[str] = []
    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid", on_auth_failed=asked.append)

    with pytest.raises(cloud.cloud_auth.CloudAuthError):
        backend.bootstrap()

    assert asked == []


def test_a_host_callback_that_raises_does_not_kill_the_stream_thread(monkeypatch, tmp_path):
    def reject(path):
        raise cloud.cloud_auth.CloudCredentialsRejected("gone")

    def explode(reason):
        raise RuntimeError("host is having a day")

    monkeypatch.setattr(cloud.cloud_auth, "access_token_from_store", reject)
    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid", on_auth_failed=explode)

    # The credential error propagates; the callback's own failure does not.
    with pytest.raises(cloud.cloud_auth.CloudCredentialsRejected):
        backend.bootstrap()
