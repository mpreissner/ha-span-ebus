"""Which cloud entity becomes which local one when a cloud entry gets its panel."""

from __future__ import annotations

from dataclasses import replace

import pytest
from span_client.identity import (
    RegistryEntryView,
    RewritePlan,
    collisions,
    rewrite_plan,
    transport_uid,
)
from span_client.link import DOWNSTREAM, UPSTREAM, PanelLink
from span_client.local import model
from span_client.local.model import Scope
from span_client.standin import FALLBACK_ONLY, STANDINS

K = "nt-0000-test"
S = "nt-0001-test"
UP, DOWN = "lugs-u", "lugs-d"


def local_rows(
    circuits=("c-1", "c-2"), *, relay=("c-1",), upstream=True, downstream=True, derived=True
):
    rows: list[tuple[model.Row | model.DerivedRow, str]] = []
    for row in model.ROWS:
        match row.scope:
            case Scope.PANEL:
                rows.append((row, "panel"))
            case Scope.UPSTREAM if upstream:
                rows.append((row, UP))
            case Scope.DOWNSTREAM if downstream:
                rows.append((row, DOWN))
            case Scope.CIRCUIT:
                for c in circuits:
                    if row.key != "breaker" or c in relay:
                        rows.append((row, c))
    if derived:
        rows += [(row, "panel") for row in model.DERIVED_ROWS]
    return rows


def link(**changes) -> PanelLink:
    base = PanelLink(
        cloud_key=K,
        local_serial=S,
        circuits={"c-1": 1, "c-2": 2},
        feeds={7: UPSTREAM, 8: DOWNSTREAM},
        site_panel_count=1,
    )
    return replace(base, **changes)


def entries(*suffixes: str) -> list[RegistryEntryView]:
    return [RegistryEntryView(f"sensor.e{i}", f"{K}_{suffix}") for i, suffix in enumerate(suffixes)]


def by_suffix(plan: RewritePlan, views: list[RegistryEntryView]) -> dict[str, str]:
    """suffix -> category name (or the new uid for a re-point)."""
    suffix = {v.entity_id: v.unique_id[len(K) + 1 :] for v in views}
    out: dict[str, str] = {}
    for eid, _old, new in plan.repoint:
        out[suffix[eid]] = new
    for name in ("standin", "fallback_only", "unmatched"):
        for eid in getattr(plan, name):
            out[suffix[eid]] = name
    return out


ALL_CLOUD = (
    "circuit-1/power",
    "circuit-1/current",
    "circuit-1/relay",
    "circuit-1/power_energy",
    "circuit-2/power",
    "circuit-2/relay",
    "circuit-2/power_energy",
    "circuit-9/power",
    "circuit-9/power_energy",
    "panel/power",
    "panel/current",
    "panel/current_l1",
    "panel/current_l2",
    "panel/voltage_l1",
    "panel/voltage_l2",
    "panel/frequency",
    "panel/overhead",
    "panel/overhead_energy",
    "feed-7/power",
    "feed-7/current",
    "feed-8/power",
    "transport",
    *(s.uid_suffix for s in STANDINS),
    *sorted(FALLBACK_ONLY),
    "site/grid_energy",
)


def test_every_cloud_row_lands_in_exactly_one_category():
    views = [*entries(*ALL_CLOUD), RegistryEntryView("sensor.other", "someone_else")]
    plan = rewrite_plan(views, link(), local_rows())
    groups = [
        [r[0] for r in plan.repoint],
        list(plan.standin),
        list(plan.fallback_only),
        list(plan.unmatched),
    ]
    flat = [eid for g in groups for eid in g]
    assert len(flat) == len(set(flat)) == len(ALL_CLOUD)
    assert "sensor.other" not in flat


def test_the_mapping_table():
    views = entries(*ALL_CLOUD)
    got = by_suffix(rewrite_plan(views, link(), local_rows()), views)
    assert got["circuit-1/power"] == f"{S}_circuit/c-1/meter/active-power"
    assert got["circuit-1/current"] == f"{S}_circuit/c-1/meter/current"
    assert got["circuit-1/relay"] == f"{S}_circuit/c-1/switch/relay"
    assert got["circuit-1/power_energy"] == f"{S}_circuit/c-1/meter/exported-energy"
    assert got["panel/voltage_l1"] == f"{S}_distribution-enclosure/meter/voltage-a"
    assert got["panel/voltage_l2"] == f"{S}_distribution-enclosure/meter/voltage-b"
    assert got["panel/frequency"] == f"{S}_distribution-enclosure/meter/frequency"
    assert got["panel/current"] == f"{S}_distribution-enclosure/meter/busbar-current"
    assert got["panel/current_l1"] == f"{S}_lugs/{UP}/meter/current-a"
    assert got["panel/current_l2"] == f"{S}_lugs/{UP}/meter/current-b"
    assert got["panel/overhead"] == f"{S}_distribution-enclosure/derived/overhead"
    assert got["panel/overhead_energy"] == f"{S}_distribution-enclosure/derived/overhead-energy"
    assert got["feed-7/power"] == f"{S}_lugs/{UP}/meter/active-power"
    assert got["feed-8/power"] == f"{S}_lugs/{DOWN}/meter/active-power"
    assert got["site/grid_energy"] == f"{S}_lugs/{UP}/meter/imported-energy"
    assert got["transport"] == transport_uid(S) == f"{S}_transport"
    assert got["panel/power"] == "standin"
    assert got["site/home"] == "standin"
    assert got["site/solar_to_grid_energy"] == "fallback_only"
    assert got["feed-7/current"] == "fallback_only"
    assert got["circuit-9/power"] == got["circuit-9/power_energy"] == "unmatched"


def test_every_new_uid_is_one_the_local_panel_creates():
    rows = local_rows()
    local = {
        model.panel_uid(S, r.source) if isinstance(r, model.DerivedRow) else model.row_uid(r, S, d)
        for r, d in rows
    }
    plan = rewrite_plan(entries(*ALL_CLOUD), link(), rows)
    for _eid, _old, new in plan.repoint:
        assert new in local or new == transport_uid(S)


def test_energy_holds_register_energies_only():
    views = entries(*ALL_CLOUD)
    plan = rewrite_plan(views, link(), local_rows())
    suffix = {v.entity_id: v.unique_id[len(K) + 1 :] for v in views}
    assert sorted(suffix[e] for e in plan.energy) == [
        "circuit-1/power_energy",
        "circuit-2/power_energy",
        "site/grid_energy",
    ]
    repointed = {r[0] for r in plan.repoint}
    assert set(plan.energy) <= repointed


def test_grid_energy_on_a_multi_panel_site_is_fallback_only():
    views = entries("site/grid_energy")
    for count in (0, 2, 3):
        plan = rewrite_plan(views, link(site_panel_count=count), local_rows())
        assert plan.fallback_only == ("sensor.e0",)
        assert plan.energy == ()
        assert plan.repoint == ()


def test_unclassified_feed_is_fallback_only():
    views = entries("feed-7/power", "feed-8/power")
    plan = rewrite_plan(views, link(feeds={7: UPSTREAM}), local_rows())
    assert by_suffix(plan, views) == {
        "feed-7/power": f"{S}_lugs/{UP}/meter/active-power",
        "feed-8/power": "fallback_only",
    }


def test_a_lone_feed_is_the_main_feed():
    views = entries("feed-3/power")
    plan = rewrite_plan(views, link(feeds={}), local_rows())
    assert by_suffix(plan, views) == {"feed-3/power": f"{S}_lugs/{UP}/meter/active-power"}


def test_a_feed_onto_missing_lugs_stays_on_the_cloud():
    views = entries("feed-8/power", "panel/current_l1", "site/grid_energy")
    plan = rewrite_plan(views, link(), local_rows(upstream=False, downstream=False))
    assert set(plan.fallback_only) == {"sensor.e0", "sensor.e1", "sensor.e2"}


def test_two_feeds_on_one_lugs_row_keep_the_first():
    views = entries("feed-7/power", "feed-8/power")
    plan = rewrite_plan(views, link(feeds={7: UPSTREAM, 8: UPSTREAM}), local_rows())
    assert [r[0] for r in plan.repoint] == ["sensor.e0"]
    assert plan.fallback_only == ("sensor.e1",)


def test_a_relay_with_no_local_switch_gets_no_feed():
    views = entries("circuit-2/relay", "circuit-2/power")
    plan = rewrite_plan(views, link(), local_rows(relay=()))
    assert plan.unmatched == ("sensor.e0",)
    assert [r[0] for r in plan.repoint] == ["sensor.e1"]


def test_overhead_without_its_derived_row_is_fallback_only():
    views = entries("panel/overhead")
    plan = rewrite_plan(views, link(), local_rows(derived=False))
    assert plan.fallback_only == ("sensor.e0",)


def test_same_key_on_both_sides():
    same = link(cloud_key=S)
    views = [
        RegistryEntryView("sensor.t", f"{S}_transport"),
        RegistryEntryView("sensor.p", f"{S}_circuit-1/power"),
    ]
    plan = rewrite_plan(views, same, local_rows())
    assert plan.repoint == (
        ("sensor.p", f"{S}_circuit-1/power", f"{S}_circuit/c-1/meter/active-power"),
        ("sensor.t", f"{S}_transport", f"{S}_transport"),
    )
    assert plan.changes == plan.repoint[:1]
    assert plan.reverse() == (
        ("sensor.p", f"{S}_circuit/c-1/meter/active-power", f"{S}_circuit-1/power"),
    )


def test_cloud_and_local_uids_never_collide():
    # A cloud uid has no `/` before its first `-`-numbered node; a local one
    # always names its device class with a `/` after it.
    plan = rewrite_plan(entries(*ALL_CLOUD), link(), local_rows())
    olds = {old for _e, old, _n in plan.changes}
    news = {new for _e, _o, new in plan.changes}
    assert not olds & news
    assert len(news) == len(plan.changes)


def test_collisions_count_new_uids_already_taken():
    plan = rewrite_plan(entries("circuit-1/power", "transport"), link(), local_rows())
    assert collisions(plan, [f"{K}_circuit-1/power"]) == 0
    assert collisions(plan, [f"{S}_circuit/c-1/meter/active-power", f"{S}_transport"]) == 2


@pytest.mark.parametrize("suffix", sorted(FALLBACK_ONLY))
def test_fallback_only_table(suffix):
    plan = rewrite_plan(entries(suffix), link(), local_rows())
    assert plan.fallback_only == ("sensor.e0",)


# --- rows from a capture, and the confirmation counts ----------------------------


def test_local_rows_from_a_capture_match_what_the_platforms_create():
    from span_client.identity import local_rows as rows_of

    from tests.test_local_capture import PANEL, capture_tree

    tree = capture_tree()
    snap = model.build_snapshot(tree, tree.panel_root(PANEL))
    rows = rows_of(snap)
    expected = {
        (row.key, device_id)
        for row in model.ROWS
        for device_id in model.row_devices(snap, row)
        if model.row_exists(snap, row, device_id)
    }
    assert {(r.key, d) for r, d in rows if isinstance(r, model.Row)} == expected
    derived = [r for r, _d in rows if isinstance(r, model.DerivedRow)]
    assert derived == [r for r in model.DERIVED_ROWS if r.when(snap)]
    assert expected


def test_confirm_counts_add_up():
    from span_client.identity import confirm_counts

    views = entries("circuit-1/power", "circuit-9/power", "transport", "panel/voltage_l1")
    plan = rewrite_plan(views, link(), local_rows())
    counts = confirm_counts(plan)
    assert counts["total"] == len(views)
    assert (
        counts["matched"] + counts["standins"] + counts["fallback_only"] + counts["unmatched"]
        == counts["total"]
    )
    assert counts["unmatched"] == 1


def test_cloud_stem_reads_uids_under_the_coordinator_serial():
    stem = "XC-AAAA"
    views = [
        RegistryEntryView("sensor.a", f"{stem}_circuit-1/power"),
        RegistryEntryView("sensor.b", f"{K}_circuit-1/power"),
    ]
    plan = rewrite_plan(views, link(), local_rows(), cloud_stem=stem)
    assert [r[0] for r in plan.repoint] == ["sensor.a"]
    assert rewrite_plan(views, link(), local_rows()).repoint[0][0] == "sensor.b"
