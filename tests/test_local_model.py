"""The local model against the redacted r202639 capture (MAIN 40, 29 circuits)."""

import re

import pytest
from span_client.local import homie, model
from span_client.local.model import Scope

from tests.test_local_capture import PANEL, capture_tree

NO_CONTROLS = {"circuit-17", "circuit-20", "circuit-25", "circuit-26", "circuit-29"}
CLOUD_UID = re.compile(r"^[^_]+_[a-z]+(-\d+)?/[a-z_]+(_energy)?$")


@pytest.fixture(scope="module")
def snapshot() -> model.Snapshot:
    return model.build_snapshot(capture_tree(), PANEL)


def existing(snapshot: model.Snapshot) -> list[tuple[model.Row, str]]:
    return [
        (row, device_id)
        for row in model.ROWS
        for device_id in model.row_devices(snapshot, row)
        if model.row_exists(snapshot, row, device_id)
    ]


def test_device_counts(snapshot):
    assert snapshot.panel is not None and snapshot.panel.device_id == PANEL
    assert len(snapshot.lugs) == 2
    assert len(snapshot.circuits) == 29
    assert all(u.key is not None for u in snapshot.unmapped)  # no whole class unmapped
    assert not {u.key for u in snapshot.unmapped} & model.PII_KEYS
    assert not snapshot.restored


def test_upstream_lugs(snapshot):
    assert snapshot.upstream.device_id == "lugs-01"
    assert snapshot.upstream.active_power == 1264.6
    assert snapshot.downstream.active_power is None


def test_power_flows(snapshot):
    flows = snapshot.panel.flows
    assert flows.published == {"grid", "site"}
    assert flows.pv is None and flows.battery is None
    assert flows.grid == 1537.9  # published negative; GRID_FLOW_SIGN makes import positive


def test_sign_and_energy_on_an_active_circuit(snapshot):
    circuit = next(c for c in snapshot.circuits.values() if c.raw_power == -433.7)
    assert circuit.power == 433.7
    assert circuit.consumed_wh == circuit.exported_wh
    assert circuit.produced_wh == circuit.imported_wh
    assert circuit.net_wh == pytest.approx(circuit.consumed_wh - circuit.produced_wh)


def test_circuits_without_controls(snapshot):
    for device_id, circuit in snapshot.circuits.items():
        if device_id in NO_CONTROLS:
            assert circuit.has_switch is False, device_id
            assert circuit.has_priority_select is False, device_id
            assert circuit.always_on is True, device_id
            assert circuit.sheddable is False, device_id
        else:
            assert circuit.has_switch is True, device_id
            assert circuit.has_priority_select is True, device_id


def test_never_priority_circuits(snapshot):
    for device_id in ("circuit-27", "circuit-28"):
        circuit = snapshot.circuits[device_id]
        assert circuit.priority == "NEVER"
        assert circuit.has_priority_select
        assert circuit.sheddable is False


def test_unknown_priority_circuits(snapshot):
    for n in ("03", "04", "10", "11", "19"):
        assert snapshot.circuits[f"circuit-{n}"].sheddable is None


def test_spaces(snapshot):
    assert snapshot.circuits["circuit-01"].spaces == (9, 11)
    spaces = [s for c in snapshot.circuits.values() for s in c.spaces or ()]
    assert all(9 <= s <= 48 for s in spaces)
    assert len(spaces) == len(set(spaces))


def test_priority_options(snapshot):
    for circuit in snapshot.circuits.values():
        assert circuit.priority_options == ("OFF_GRID", "SOC_THRESHOLD", "NEVER")


def test_unique_ids_are_unique_and_cannot_meet_the_cloud_ones(snapshot):
    uids = [model.row_uid(row, "KEY", device_id) for row, device_id in existing(snapshot)]
    assert len(uids) == len(set(uids))
    shapes = ("KEY_distribution-enclosure/", "KEY_lugs/", "KEY_circuit/")
    assert all(uid.startswith(shapes) for uid in uids)
    assert not any(CLOUD_UID.match(uid) for uid in uids)
    assert not any(uid.endswith("_energy") for uid in uids)


def test_entities_the_fixture_triggers(snapshot):
    keys = {row.key for row, _ in existing(snapshot)}
    absent = {
        "main_relay",
        "pv_power",
        "battery_power",
        "feedthrough_power",
        "feedthrough_imported_energy",
        "feedthrough_exported_energy",
        "upstream_protection_rating",
    }
    assert not keys & absent
    assert {"service_rating", "main_breaker_rating", "grid_power", "site_power"} <= keys
    assert snapshot.panel.main_breaker_rating is not None


def test_every_row_reads_a_property_the_fixture_describes(snapshot):
    """Grounding: the property keys rows need exist somewhere in the capture."""
    described = set(snapshot.panel.described)
    for facts in (*snapshot.lugs.values(), *snapshot.circuits.values()):
        described |= facts.described
    for row in model.ROWS:
        assert set(row.needs) <= described, row.key


def test_clearing_a_value_gives_none_not_zero():
    tree = capture_tree()
    circuit = next(iter(model.build_snapshot(tree, PANEL).circuits))
    tree.apply(homie.value_topic(circuit, "meter/active-power"), b"")
    assert model.build_snapshot(tree, PANEL).circuits[circuit].power is None


def test_circuit_label_and_order(snapshot):
    circuits = list(snapshot.circuits.values())
    firsts = [c.spaces[0] for c in circuits if c.spaces]
    assert firsts == sorted(firsts)
    assert all(model.circuit_label(c) for c in circuits)


def test_store_round_trip_matches_the_live_snapshot():
    tree = capture_tree()
    payload = model.store_payload(tree)
    assert not set(payload["values"].get(PANEL, {})) & model.PII_KEYS
    assert all("$state" not in values for values in payload["values"].values())
    restored = homie.DeviceTree()
    model.load_payload(payload, restored)
    live, back = model.build_snapshot(tree, PANEL), model.build_snapshot(restored, PANEL)
    assert set(back.circuits) == set(live.circuits)
    for device_id, circuit in live.circuits.items():
        assert back.circuits[device_id].name == circuit.name
        assert back.circuits[device_id].power == circuit.power
    assert back.panel.flows == live.panel.flows
    assert back.upstream.imported_wh == live.upstream.imported_wh


def test_scopes_cover_every_row():
    assert {row.scope for row in model.ROWS} == set(Scope)
