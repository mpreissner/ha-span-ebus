"""Cloud entities kept on their cloud uid and fed from a local row."""

from __future__ import annotations

from span_client import standin as st
from span_client.local import model
from span_client.local.model import BATTERY_TO_CLOUD_SIGN, PV_TO_CLOUD_SIGN, Scope
from span_client.standin import (
    FALLBACK_ONLY,
    STANDIN_SUFFIXES,
    STANDINS,
    row_keys,
    standins_for,
    suppressed_rows,
)

from tests.test_local_capture import PANEL, capture_tree

K = "nt-0000-test"


def by_suffix():
    return {s.uid_suffix: s for s in STANDINS}


def test_every_standin_names_an_existing_row():
    rows = row_keys()
    for s in STANDINS:
        assert (s.scope, s.source_row) in rows, s.uid_suffix


def test_standins_and_fallback_only_are_disjoint():
    assert not STANDIN_SUFFIXES & FALLBACK_ONLY
    assert len(STANDIN_SUFFIXES) == len(STANDINS) == 11
    assert len(FALLBACK_ONLY) == 20


def test_fallback_only_table():
    assert "site/solar_to_grid" in FALLBACK_ONLY
    assert "site/battery_to_home_energy" in FALLBACK_ONLY
    assert "site/residual_energy" in FALLBACK_ONLY
    assert "site/grid_energy" not in FALLBACK_ONLY  # decided per link


def test_pv_and_battery_signs():
    table = by_suffix()
    assert table["site/solar"].transform(-800.0) == -800.0 * PV_TO_CLOUD_SIGN
    assert table["site/battery"].transform(500.0) == 500.0 * BATTERY_TO_CLOUD_SIGN
    assert table["site/home"].transform(42.0) == 42.0


def test_raw_grid_import_shows_positive_on_site_grid():
    tree = capture_tree()
    tree.apply(f"ebus/5/{PANEL}/power-flows/grid", b"-1200")
    snapshot = model.build_snapshot(tree, PANEL)
    row = next(r for r in model.ROWS if r.key == "grid_power")
    grid = by_suffix()["site/grid"]
    assert grid.source_row == row.key
    assert grid.transform(row.value(snapshot.panel)) == 1200.0


def test_duplicates_suppressed_except_the_same_value_pairs():
    suppressed = suppressed_rows(STANDINS)
    assert (Scope.PANEL, "site_power") in suppressed
    assert (Scope.PANEL, "site_energy") in suppressed
    assert (Scope.PANEL, "voltage_a") not in suppressed
    assert (Scope.PANEL, "frequency") not in suppressed
    assert (Scope.UPSTREAM, "current_power") not in suppressed


def test_standins_for_only_those_in_the_registry():
    uids = [f"{K}_site/home", f"{K}_site/solar_energy", f"{K}_site/grid_to_home", "x_site/grid"]
    assert [s.uid_suffix for s in standins_for(uids, K)] == ["site/home", "site/solar_energy"]
    assert standins_for([], K) == ()


# --- what a linked entry builds for its kept rows ------------------------------------


def test_classify_kept():
    got = st.classify_kept(
        ["transport", "site/grid", "site/solar_to_grid", "circuit-9/power", "feed-8/power"],
        ["circuit-9/power"],
    )
    assert got == {
        "site/grid": st.STANDIN,
        "site/solar_to_grid": st.FALLBACK,
        "circuit-9/power": st.UNMATCHED,
        "feed-8/power": st.FALLBACK,
    }


def test_a_standin_wins_over_a_stale_unmatched_record():
    assert st.classify_kept(["site/home"], ["site/home"]) == {"site/home": st.STANDIN}


def test_standin_by_suffix():
    assert st.standin_by_suffix("site/home").source_row == "site_power"
    assert st.standin_by_suffix("site/nothing") is None


def test_ledger_key():
    assert st.ledger_key("site/solar_to_grid_energy") == "site/solar_to_grid"
    assert st.ledger_key("feed-7/power") == "feed-7/power"


def test_look_follows_the_device_class_not_the_display_unit():
    assert st.look("power", "site/generator") == ("W", "measurement")
    assert st.look("current", "feed-7/current") == ("A", "measurement")
    assert st.look("energy", "site/generator_energy") == ("kWh", "total_increasing")
    assert st.look(None, "circuit-9/relay") == (None, None)


def test_held_total_adopts_the_ledger_with_nothing_restored():
    held = st.HeldTotal()
    assert held.sample(12.5) is True
    assert held.total == 12.5
    assert held.sample(13.0) is True
    assert held.total == 13.0


def test_held_total_reseeds_at_each_cloud_start():
    held = st.HeldTotal(100.0)
    assert held.sample(40.0) is False  # first reading on the cloud: the mark only
    assert held.sample(41.5) is True
    assert held.total == 101.5
    assert held.sample(None) is False  # off the cloud: held
    assert held.total == 101.5
    assert held.sample(60.0) is False  # back on: the jump is not counted
    assert held.sample(60.25) is True
    assert held.total == 101.75


def test_held_total_never_steps_back():
    held = st.HeldTotal(5.0)
    held.sample(10.0)
    assert held.sample(2.0) is False
    assert held.total == 5.0
    assert held.sample(3.0) is True
    assert held.total == 6.0
