"""Derived panel rows: overhead and the integrated site energies, on the capture."""

from dataclasses import replace

import pytest
from span_client.local import model
from span_client.local.health import Transport
from span_client.local.homie import DeviceState
from span_client.local.model import Presence, Scope

from tests.test_local_capture import PANEL, capture_tree


@pytest.fixture(scope="module")
def snapshot() -> model.Snapshot:
    return model.build_snapshot(capture_tree(), PANEL, received_at=123.5)


def no_fallback(scope, device_id, key):
    return model.NO_FALLBACK


LOCAL = model.shown_reader(Transport.LOCAL, no_fallback)


def rows() -> dict[str, model.DerivedRow]:
    return {row.key: row for row in model.DERIVED_ROWS}


def _with_lugs(snapshot, direction, **changes):
    lugs = dict(snapshot.lugs)
    for device_id, facts in lugs.items():
        if facts.direction == direction:
            lugs[device_id] = replace(facts, **changes)
    return replace(snapshot, lugs=lugs)


def _with_circuits(snapshot, power, **changes):
    circuits = {
        device_id: replace(facts, power=power, **changes)
        for device_id, facts in snapshot.circuits.items()
    }
    return replace(snapshot, circuits=circuits)


def _with_flows(snapshot, **flows):
    panel = snapshot.panel
    published = frozenset(name for name, value in flows.items() if value is not None)
    new = replace(panel.flows, **flows, published=panel.flows.published | published)
    return replace(snapshot, panel=replace(panel, flows=new))


# --- the rows ---------------------------------------------------------------------


def test_row_keys_sources_and_kinds():
    assert [(r.key, r.source, r.energy) for r in model.DERIVED_ROWS] == [
        ("overhead", "derived/overhead", False),
        ("overhead_energy", "derived/overhead-energy", True),
        ("site_energy", "derived/site-energy", True),
        ("pv_energy", "derived/pv-energy", True),
        ("battery_energy", "derived/battery-energy", True),
    ]


def test_uids_sit_on_the_panel_device():
    assert model.panel_uid("<key>", "derived/overhead") == (
        "<key>_distribution-enclosure/derived/overhead"
    )


def test_flow_energies_exist_only_where_the_flow_is_published(snapshot):
    present = {r.key for r in model.DERIVED_ROWS if r.when(snapshot)}
    # The capture publishes grid and site flows, with no solar or battery.
    assert present == {"overhead", "overhead_energy", "site_energy"}

    both = _with_flows(snapshot, pv=-2500.0, battery=-800.0)
    assert {r.key for r in model.DERIVED_ROWS if r.when(both)} == set(rows())


def test_nothing_exists_before_the_panel_is_described():
    assert not [r for r in model.DERIVED_ROWS if r.when(model.EMPTY)]
    assert all(r.power(model.EMPTY, LOCAL) is None for r in model.DERIVED_ROWS)


def test_pv_and_battery_signs():
    # Production counts for solar, discharge for the battery.
    assert model.PV_TO_CLOUD_SIGN == -1
    assert model.BATTERY_TO_CLOUD_SIGN == -1


def test_flow_powers_apply_their_signs(snapshot):
    flows = _with_flows(snapshot, pv=-2500.0, battery=-800.0)
    r = rows()
    assert r["site_energy"].power(flows, LOCAL) == pytest.approx(1696.3)
    assert r["pv_energy"].power(flows, LOCAL) == pytest.approx(2500.0)
    assert r["battery_energy"].power(flows, LOCAL) == pytest.approx(800.0)
    charging = _with_flows(snapshot, battery=600.0)
    assert r["battery_energy"].power(charging, LOCAL) == pytest.approx(-600.0)


# --- overhead ---------------------------------------------------------------------


def test_the_capture_has_no_feedthrough(snapshot):
    # Its downstream lugs are marked UNUSED and publish no power.
    assert not model.has_feedthrough(snapshot)


def test_downstream_lugs_in_use_are_a_feedthrough(snapshot):
    used = _with_lugs(snapshot, "DOWNSTREAM", feeds_role=None, active_power=500.0)
    assert model.has_feedthrough(used)
    undescribed = _with_lugs(used, "DOWNSTREAM", described=frozenset())
    assert not model.has_feedthrough(undescribed)


def test_local_overhead_is_feed_less_circuits(snapshot):
    flat = _with_lugs(_with_circuits(snapshot, 10.0), "UPSTREAM", active_power=400.0)
    loads = [c for c in flat.circuits.values() if not c.is_remote_ct]
    assert model.local_overhead_w(flat, LOCAL) == pytest.approx(400.0 - 10.0 * len(loads))


def test_local_overhead_subtracts_the_feedthrough(snapshot):
    base = _with_lugs(_with_circuits(snapshot, 10.0), "UPSTREAM", active_power=2000.0)
    fed = _with_lugs(base, "DOWNSTREAM", feeds_role=None, active_power=1500.0)
    n = len(fed.circuits)
    assert model.local_overhead_w(fed, LOCAL) == pytest.approx(2000.0 - 10.0 * n - 1500.0)
    # A feedthrough that has not reported blocks the figure rather than
    # counting the downstream panel's load as overhead.
    silent = _with_lugs(fed, "DOWNSTREAM", feeds_role="LOADS", active_power=None)
    assert model.local_overhead_w(silent, LOCAL) is None


def test_unroled_lugs_count_only_once_they_report(snapshot):
    base = _with_lugs(_with_circuits(snapshot, 10.0), "UPSTREAM", active_power=2000.0)
    n = len(base.circuits)
    quiet = _with_lugs(base, "DOWNSTREAM", feeds_role=None, active_power=None)
    # No role and no power yet: assumed to feed nothing, so the overhead is not
    # held at None waiting for a reading that may never come.
    assert not model.has_feedthrough(quiet)
    assert model.local_overhead_w(quiet, LOCAL) == pytest.approx(2000.0 - 10.0 * n)
    reported = _with_lugs(quiet, "DOWNSTREAM", active_power=1500.0)
    assert model.has_feedthrough(reported)
    assert model.local_overhead_w(reported, LOCAL) == pytest.approx(2000.0 - 10.0 * n - 1500.0)


def test_unroled_lugs_are_logged_once(snapshot, caplog):
    model._ASSUMED_FEEDTHROUGH.clear()
    reported = _with_lugs(snapshot, "DOWNSTREAM", feeds_role=None, active_power=5.0)
    with caplog.at_level("DEBUG", logger=model.log.name):
        model.has_feedthrough(reported)
        model.has_feedthrough(reported)
    assert len([r for r in caplog.records if "assuming a feedthrough" in r.message]) == 1


def test_circuits_sharing_a_meter_each_count_once(snapshot):
    # Pins today's behaviour: both circuits on a shared meter are summed as
    # published. Whether the panel duplicates the shared power onto both, which
    # would double count it here, needs a capture of a panel with a shared meter.
    base = _with_lugs(_with_circuits(snapshot, 0.0), "UPSTREAM", active_power=1000.0)
    loads = [d for d, c in base.circuits.items() if not c.is_remote_ct][:2]
    circuits = dict(base.circuits)
    circuits[loads[0]] = replace(circuits[loads[0]], power=300.0, shared_meter_with=(loads[1],))
    circuits[loads[1]] = replace(circuits[loads[1]], power=300.0, shared_meter_with=(loads[0],))
    shared = replace(base, circuits=circuits)
    assert model.local_overhead_w(shared, LOCAL) == pytest.approx(400.0)


def test_a_producing_circuit_counts_as_power_into_the_bus(snapshot):
    base = _with_lugs(_with_circuits(snapshot, 0.0), "UPSTREAM", active_power=100.0)
    first = next(iter(base.circuits))
    circuits = dict(base.circuits)
    circuits[first] = replace(circuits[first], power=300.0, role=model.Role.SOLAR)
    with_pv = replace(base, circuits=circuits)
    assert model.local_overhead_w(with_pv, LOCAL) == pytest.approx(400.0)


def test_a_circuit_without_a_reading_blocks_the_figure(snapshot):
    first = next(iter(snapshot.circuits))
    circuits = dict(snapshot.circuits)
    circuits[first] = replace(circuits[first], power=None)
    assert model.local_overhead_w(replace(snapshot, circuits=circuits), LOCAL) is None


def test_a_device_that_is_down_is_not_read(snapshot):
    lost = _with_lugs(snapshot, "UPSTREAM", state=DeviceState.LOST)
    assert model.local_overhead_w(lost, LOCAL) is None


def test_overhead_is_never_negative(snapshot):
    # On the capture the feed and branch meters were sampled apart.
    assert model.local_overhead_w(snapshot, LOCAL) == 0.0


def test_cloud_reads_the_fallback_and_offline_reads_nothing(snapshot):
    assert (
        model.local_overhead_w(snapshot, model.shown_reader(Transport.CLOUD, no_fallback)) is None
    )
    assert (
        model.local_overhead_w(snapshot, model.shown_reader(Transport.OFFLINE, no_fallback)) is None
    )

    def fallback(scope, device_id, key):
        assert key == "meter/active-power"
        return 1000.0 if scope is Scope.UPSTREAM else 20.0

    cloud = model.shown_reader(Transport.CLOUD, fallback)
    assert model.local_overhead_w(snapshot, cloud) == pytest.approx(
        1000.0 - 20.0 * len(snapshot.circuits)
    )


def test_a_fallback_without_a_value_reads_none(snapshot):
    read = model.shown_reader(Transport.CLOUD, lambda *a: None)
    assert read(Scope.PANEL, PANEL, "power-flows/site", DeviceState.READY, 5.0) is None


# --- presence and timing ----------------------------------------------------------


@pytest.mark.parametrize(
    ("shown", "root", "has_value", "expected"),
    [
        (Transport.LOCAL, DeviceState.READY, True, Presence.VALUE),
        (Transport.LOCAL, DeviceState.ALERT, False, Presence.UNKNOWN),
        (Transport.LOCAL, DeviceState.LOST, True, Presence.UNAVAILABLE),
        (Transport.LOCAL, None, True, Presence.UNAVAILABLE),
        (Transport.CLOUD, DeviceState.LOST, True, Presence.VALUE),
        (Transport.CLOUD, None, False, Presence.UNKNOWN),
        (Transport.OFFLINE, DeviceState.READY, True, Presence.UNAVAILABLE),
    ],
)
def test_derived_presence(shown, root, has_value, expected):
    assert model.derived_presence(shown, root, has_value) is expected


def test_the_receive_time_rides_on_the_snapshot(snapshot):
    assert snapshot.received_at == 123.5
    assert model.build_snapshot(capture_tree(), PANEL).received_at is None


def test_a_local_sample_is_stamped_with_its_receive_time(snapshot):
    assert model.derived_timestamp(snapshot, Transport.LOCAL, 999.0) == 123.5
    restored = replace(snapshot, received_at=None)
    assert model.derived_timestamp(restored, Transport.LOCAL, 999.0) is None
    assert model.derived_timestamp(snapshot, Transport.CLOUD, 999.0) == 999.0
