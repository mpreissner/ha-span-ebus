"""Cloud entities kept on their cloud uid and fed from a local row."""

from __future__ import annotations

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
