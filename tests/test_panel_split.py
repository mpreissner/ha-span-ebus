"""Panels sharing a site: one stream, split so each sees only its own circuits.

The two-panel shapes here are guesses until a real two-panel capture exists; only
the single-panel behavior is measured. Every id and serial is invented.
"""

from __future__ import annotations

import time

import span_client.backend as cloud
from span_client.backend import (
    PanelInfo,
    SiteInfo,
    SiteStream,
    device_name,
    panel_identity,
    site_routes,
    split_frame,
    split_snapshot,
)
from span_client.cloud_pb import field_message, field_string, field_varint
from span_client.cloud_telemetry import Channel, CircuitSample, Frame
from span_client.cloud_traits import parse_trait_snapshot

A = PanelInfo("site-1", "XC-AAAA", "1-04100-01", "gw-a")
B = PanelInfo("site-1", "XC-BBBB", "1-02100-04", "gw-b")
SHARED = SiteInfo(
    site_id="site-1",
    hardware_ids=("site-1", "gw-a", "gw-b"),
    panels=(A, B),
    gateways=("gw-a", "gw-b"),
)


def _sample(instance: int, watts: int = 1) -> CircuitSample:
    return CircuitSample(
        instance_id=instance,
        kind="two_wire",
        quality_pct=100,
        combined=Channel(power_mw=watts * 1000),
    )


def _frame(resources=None, flows=None) -> Frame:
    return Frame(
        epoch_millis=1000,
        site_id="site-1",
        site_instance_id=7,
        resources=resources or {},
        site_flows=flows or {},
    )


def _block(resource_id: str, *payload: bytes) -> bytes:
    return field_message(1, field_message(1, field_string(1, resource_id)) + b"".join(payload))


def _stream(tmp_path, primary=None) -> tuple[SiteStream, dict, dict]:
    routes = site_routes(SHARED, primary)
    stream = SiteStream(tmp_path / "tok.json", "dev-uuid", routes=routes, serial="XC-AAAA")
    schemas: dict[str, list] = {}
    readings: dict[str, list] = {}
    for device in routes.devices:
        backend = stream.panel_backend(device.key, device.key)
        backend._on_schema = schemas.setdefault(device.key, []).append
        backend._on_reading = readings.setdefault(device.key, []).append
    return stream, schemas, readings


def test_one_panel_site_is_not_split():
    alone = SiteInfo("site-1", ("site-1", "gw-a"), (A,), ("gw-a",))
    assert site_routes(alone, None) is None
    many_gateways = SiteInfo("site-1", ("gw-a", "gw-b"), (A,), ("gw-a", "gw-b"))
    assert site_routes(many_gateways, None) is None


def test_boards_on_one_gateway_stay_one_device_and_warn(caplog):
    # Shape C: one block, so nothing collides. The warning must not name the site.
    site = SiteInfo("site-1", ("site-1", "gw-a"), (A, B), ("gw-a",), name="1 Secret St")
    assert site_routes(site, None) is None
    assert "2 panels" in caplog.text
    assert "1 Secret St" not in caplog.text
    assert "gw-a" not in caplog.text


def test_same_instance_id_on_two_panels_stays_apart():
    frame = _frame({"gw-a": [_sample(42, 5)], "gw-b": [_sample(42, 9)]})
    parts = split_frame(frame, site_routes(SHARED, None), set())
    assert parts["XC-AAAA"].resources == {"gw-a": [_sample(42, 5)]}
    assert parts["XC-BBBB"].resources == {"gw-b": [_sample(42, 9)]}


def test_unknown_block_is_dropped_and_logged_once(caplog):
    caplog.set_level("DEBUG")
    unrouted: set = set()
    routes = site_routes(SHARED, None)
    frame = _frame({"gw-unticked": [_sample(1)]})
    assert split_frame(frame, routes, unrouted) == {}
    assert split_frame(frame, routes, unrouted) == {}
    assert caplog.text.count("belongs to no panel here") == 1
    # Block ids are gateway hardware ids: private.
    assert "gw-unticked" not in caplog.text


def test_unticked_panel_is_absent_from_every_part():
    routes = site_routes(SHARED, None)
    only_a = cloud.SiteRoutes("site-1", (A,), routes.flows_owner)
    parts = split_frame(_frame({"gw-a": [_sample(1)], "gw-b": [_sample(1)]}), only_a, set())
    assert set(parts) == {"XC-AAAA"}


def test_flows_and_site_block_go_to_a_site_device():
    routes = site_routes(SHARED, None)
    assert routes.flows_owner.key == "site-1"
    frame = _frame({"site-1": [_sample(3)], "gw-a": [_sample(1)]}, {"grid": 100.0})
    parts = split_frame(frame, routes, set())
    assert parts["site-1"].site_flows == {"grid": 100.0}
    assert parts["site-1"].resources == {"site-1": [_sample(3)]}
    assert parts["XC-AAAA"].site_flows == {}


def test_flows_stay_on_the_primary_panel_if_the_site_holds_it():
    routes = site_routes(SHARED, "XC-BBBB")
    assert routes.devices == (A, B)
    frame = _frame({"gw-b": [_sample(1)]}, {"grid": 100.0})
    part = split_frame(frame, routes, set())["XC-BBBB"]
    assert part.site_flows == {"grid": 100.0}
    assert part.resources == {"gw-b": [_sample(1)]}


def test_frame_with_no_block_is_harmless():
    assert split_frame(_frame(), site_routes(SHARED, None), set()) == {}


def test_snapshot_blocks_go_to_their_panels():
    raw = _block("gw-a", field_varint(9, 1)) + _block("gw-b") + _block("site-1") + _block("other")
    parts = split_snapshot(raw, site_routes(SHARED, None), set())
    assert parts["XC-AAAA"] == _block("gw-a", field_varint(9, 1))
    assert parts["XC-BBBB"] == _block("gw-b")
    assert parts["site-1"] == _block("site-1")
    assert set(parts) == {"XC-AAAA", "XC-BBBB", "site-1"}
    assert parse_trait_snapshot(parts["XC-AAAA"]) == {}


def test_unparseable_snapshot_splits_to_nothing():
    assert split_snapshot(b"\xff\xff\xff", site_routes(SHARED, None), set()) == {}


def test_unpaired_gateways_become_numbered_panels_keyed_on_hardware_id():
    # Shape B: boards with no pairing. Right circuits, no invented identity.
    site = SiteInfo(
        "site-1",
        ("gw-a", "gw-b"),
        (PanelInfo("site-1", "XC-AAAA", None, None), PanelInfo("site-1", "XC-BBBB", None, None)),
        ("gw-a", "gw-b"),
    )
    routes = site_routes(site, None)
    assert [panel.key for panel in routes.panels] == ["gw-a", "gw-b"]
    assert [device_name(panel, routes) for panel in routes.panels] == [
        "SPAN panel 1",
        "SPAN panel 2",
    ]
    assert device_name(routes.flows_owner, routes) == "SPAN site"


def test_device_names_never_use_the_site_label():
    routes = site_routes(SHARED, None)
    assert device_name(A, routes) == "SPAN MAIN 16 XC-AAAA"


def test_panel_identity_shares_the_channel_only():
    kwargs = {"device_uuid": "dev-uuid", "serial": "XC-AAAA", "primary_panel": "XC-AAAA"}
    primary = panel_identity(SHARED, A, **kwargs)
    other = panel_identity(SHARED, B, **kwargs)
    assert primary == cloud.StreamIdentity("dev-uuid", "XC-AAAA", None)
    assert other == cloud.StreamIdentity("dev-uuid", "XC-BBBB", "XC-BBBB")


def test_stream_gives_each_panel_its_own_schema(monkeypatch, tmp_path):
    stream, schemas, readings = _stream(tmp_path)
    frame = _frame({"gw-a": [_sample(42, 5)], "gw-b": [_sample(42, 9)]})
    monkeypatch.setattr(cloud, "decode_frame", lambda raw: frame)
    stream._handle_frame(b"frame")
    assert schemas["XC-AAAA"][0].serial == "XC-AAAA"
    assert schemas["XC-BBBB"][0].serial == "XC-BBBB"
    power = {key: {r.key: float(r.value) for r in got} for key, got in readings.items()}
    assert power["XC-AAAA"]["circuit-42/power"] == 5.0
    assert power["XC-BBBB"]["circuit-42/power"] == 9.0
    assert schemas["site-1"] == []


def test_panel_on_a_quiet_frame_is_not_held_back_by_the_other(monkeypatch, tmp_path):
    # Panels may publish in separate frames, so each sends its schema on its own.
    stream, schemas, _ = _stream(tmp_path)
    frames = iter([_frame({"gw-a": [_sample(1)]}), _frame({"gw-b": [_sample(1)]})])
    monkeypatch.setattr(cloud, "decode_frame", lambda raw: next(frames))
    stream._handle_frame(b"frame")
    assert len(schemas["XC-AAAA"]) == 1
    assert schemas["XC-BBBB"] == []
    stream._handle_frame(b"frame")
    assert len(schemas["XC-AAAA"]) == 1
    assert len(schemas["XC-BBBB"]) == 1


def test_flows_arriving_apart_from_the_block_still_reach_the_schema(monkeypatch, tmp_path):
    stream, schemas, _ = _stream(tmp_path, primary="XC-AAAA")
    frames = iter([_frame(flows={"grid": 1.0}), _frame({"gw-a": [_sample(1)]})])
    monkeypatch.setattr(cloud, "decode_frame", lambda raw: next(frames))
    stream._handle_frame(b"frame")
    stream._handle_frame(b"frame")
    latest = schemas["XC-AAAA"][-1].properties
    assert "site/grid" in latest
    assert any(key.startswith("circuit-1/") for key in latest)
    targets = stream._panels["XC-AAAA"]._energy_targets
    assert "gw-a" in targets and "site-1" in targets


def test_refresh_releases_every_panel_and_splits_the_snapshot(monkeypatch, tmp_path):
    stream, _, _ = _stream(tmp_path)
    for panel in stream._panels.values():
        panel._label_deadline = time.monotonic() + 60.0
    monkeypatch.setattr(stream, "subscribe", lambda channel: _block("gw-a") + _block("gw-b"))
    seen: list[bytes] = []
    monkeypatch.setattr(cloud, "parse_trait_snapshot", lambda raw: seen.append(raw) or {})
    stream._refresh_circuits("c:u:d")
    assert sorted(seen) == sorted([_block("gw-a"), _block("gw-b"), b""])
    assert all(panel._label_deadline == 0.0 for panel in stream._panels.values())


def test_stream_starts_when_every_panel_attached_and_stops_with_the_last(monkeypatch, tmp_path):
    stream, _, _ = _stream(tmp_path)
    runs: list[bool] = []
    monkeypatch.setattr(stream, "_run", lambda: runs.append(True))
    panels = list(stream._panels.values())
    panels[0].start(lambda s: None, lambda r: None)
    panels[1].start(lambda s: None, lambda r: None)
    assert runs == []
    panels[2].start(lambda s: None, lambda r: None)
    stream._thread.join(2)
    assert runs == [True]
    panels[0].stop()
    assert not stream._stop.is_set()
    panels[1].stop()
    panels[2].stop()
    assert stream._stop.is_set()


def test_a_panels_command_refresh_goes_to_the_site_stream(monkeypatch, tmp_path):
    stream, _, _ = _stream(tmp_path)
    asked: list[float] = []
    monkeypatch.setattr(stream, "_request_refresh", asked.append)
    stream._panels["XC-AAAA"]._request_refresh(1.5)
    assert asked == [1.5]


def test_one_panel_backend_still_takes_every_block(monkeypatch, tmp_path):
    frame = _frame({"gw-a": [_sample(1)], "gw-b": [_sample(2)]})
    monkeypatch.setattr(cloud, "decode_frame", lambda raw: frame)
    backend = cloud.CloudBackend(tmp_path / "tok.json", "dev-uuid", serial="XC-1")
    schemas: list = []
    backend._on_schema = schemas.append
    backend._handle_frame(b"frame")
    assert {"circuit-1/power", "circuit-2/power"} <= set(schemas[0].properties)
