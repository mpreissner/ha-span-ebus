"""The link between a local panel and its cloud fallback: matching and storage."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from span_client.link import (
    CONF_LINKS,
    DOWNSTREAM,
    FEEDER_FEEDTHROUGH,
    FEEDER_MAIN_BREAKER,
    UPSTREAM,
    HistoryTarget,
    PanelLink,
    circuits_verified,
    feeds_from_kinds,
    link_from_options,
    link_to_options,
    match_circuits,
    migration_ok,
    options_without_link,
    unlinked_entry,
)

CLOUD_KEY = "nt-0000-test"
SERIAL = "nt-0000-test"


@dataclass
class Local:
    spaces: tuple[int, ...] | None
    poles: int | None = None
    is_remote_ct: bool = False


@dataclass
class Cloud:
    instance_id: int
    spaces: tuple[int, ...]


def test_exact_space_sets_match():
    local = {"c-a": Local((1,), 1), "c-b": Local((3, 5), 2)}
    cloud = [Cloud(10, (1,)), Cloud(11, (5, 3))]
    matched, unmatched_local, unmatched_cloud = match_circuits(local, cloud)
    assert matched == {"c-a": 10, "c-b": 11}
    assert unmatched_local == ()
    assert unmatched_cloud == ()


def test_pole_count_must_agree():
    matched, unmatched_local, _ = match_circuits({"c-a": Local((1,), 2)}, [Cloud(10, (1,))])
    assert matched == {}
    assert unmatched_local == ("c-a",)


def test_unknown_pole_count_matches_on_spaces():
    matched, _, _ = match_circuits({"c-a": Local((1, 3))}, [Cloud(10, (1, 3))])
    assert matched == {"c-a": 10}


def test_remote_ct_never_matches():
    local = {"c-ct": Local((7,), 1, is_remote_ct=True)}
    matched, unmatched_local, unmatched_cloud = match_circuits(local, [Cloud(10, (7,))])
    assert matched == {}
    assert unmatched_local == ("c-ct",)
    assert unmatched_cloud == (10,)


def test_circuit_without_spaces_never_matches():
    matched, unmatched_local, _ = match_circuits({"c-a": Local(None)}, [Cloud(10, ())])
    assert matched == {}
    assert unmatched_local == ("c-a",)


@pytest.mark.parametrize("side", ["local", "cloud"])
def test_duplicate_space_set_matches_nothing(side):
    if side == "local":
        local = {"c-a": Local((1,)), "c-b": Local((1,))}
        cloud = [Cloud(10, (1,))]
    else:
        local = {"c-a": Local((1,))}
        cloud = [Cloud(10, (1,)), Cloud(11, (1,))]
    matched, unmatched_local, unmatched_cloud = match_circuits(local, cloud)
    assert matched == {}
    assert "c-a" in unmatched_local
    assert 10 in unmatched_cloud


def test_main_40_numbering_matches_spaces_9_to_48():
    # A MAIN 40 numbers its spaces 9-48; both sides use the panel's numbering.
    local = {f"c-{n}": Local((n,), 1) for n in range(9, 49)}
    cloud = [Cloud(100 + n, (n,)) for n in range(9, 49)]
    matched, unmatched_local, unmatched_cloud = match_circuits(local, cloud)
    assert len(matched) == 40
    assert matched["c-9"] == 109
    assert matched["c-48"] == 148
    assert unmatched_local == unmatched_cloud == ()


def test_empty_sides():
    assert match_circuits({}, []) == ({}, (), ())
    assert match_circuits({}, [Cloud(10, (1,))]) == ({}, (), (10,))


def test_feeds_from_kinds():
    assert feeds_from_kinds({1: FEEDER_MAIN_BREAKER, 2: FEEDER_FEEDTHROUGH}) == {
        1: UPSTREAM,
        2: DOWNSTREAM,
    }
    # A lone feed of no known kind supplies the panel.
    assert feeds_from_kinds({5: 0}) == {5: UPSTREAM}
    assert feeds_from_kinds({5: 0, 6: 0}) == {}
    assert feeds_from_kinds({}) == {}


def _link() -> PanelLink:
    return PanelLink(
        cloud_key=CLOUD_KEY,
        local_serial=SERIAL,
        circuits={"c-a": 10},
        feeds={1: UPSTREAM, 2: DOWNSTREAM},
        unmatched_local=("c-b",),
        unmatched_cloud=(11,),
        site_history=HistoryTarget("res", 3, "site"),
        site_panel_count=1,
        linked_at="2026-01-01T00:00:00+00:00",
    )


def test_options_round_trip():
    options = link_to_options({"other": 1}, _link())
    assert options["other"] == 1
    assert set(options[CONF_LINKS]) == {CLOUD_KEY}
    # Stored as JSON: feed instances become strings.
    assert set(options[CONF_LINKS][CLOUD_KEY]["feeds"]) == {"1", "2"}
    assert link_from_options(options) == _link()
    assert link_from_options(options).single_panel_site


def test_no_link_or_a_bad_one_reads_as_none():
    assert link_from_options({}) is None
    assert link_from_options({CONF_LINKS: {}}) is None
    assert link_from_options({CONF_LINKS: {CLOUD_KEY: "nope"}}) is None
    assert link_from_options({CONF_LINKS: {CLOUD_KEY: {"circuits": {}}}}) is None
    bad = {CONF_LINKS: {CLOUD_KEY: {"local_serial": SERIAL, "circuits": {"c-a": "x"}}}}
    assert link_from_options(bad) is None


def test_minimal_record_reads_with_defaults():
    link = link_from_options({CONF_LINKS: {CLOUD_KEY: {"local_serial": SERIAL}}})
    assert link == PanelLink(CLOUD_KEY, SERIAL)
    assert not link.single_panel_site


def test_options_without_link():
    options = link_to_options({"other": 1}, _link())
    assert options_without_link(options) == {"other": 1}


@pytest.mark.parametrize(
    ("version", "minor", "ok"),
    [(1, 1, True), (1, 2, True), (1, 3, True), (2, 1, False), (2, 0, False)],
)
def test_migration_ok(version, minor, ok):
    assert migration_ok(version, minor) is ok


def test_unlinked_entry_keeps_the_local_panel_and_drops_the_account():
    from const import (
        CLOUD_DATA_KEYS,
        CLOUD_OPTION_KEYS,
        CONF_DECLINED,
        CONF_DEVICE_UUID,
        CONF_EMAIL,
        CONF_FEEDS,
        CONF_LOCAL_SERIAL,
        CONF_PANELS,
        CONF_PRIMARY_PANEL,
        CONF_SERIAL,
        CONF_TOKENS,
        CONF_TRANSPORT_MODE,
        CONF_USER_ID,
    )

    local = {CONF_LOCAL_SERIAL: SERIAL, "host": "panel.invalid", "ca_pem": "pem"}
    data = {
        **local,
        CONF_TOKENS: {"refresh_token": "r"},
        CONF_EMAIL: "user@example.invalid",
        CONF_USER_ID: "u-1",
        CONF_DEVICE_UUID: "d-1",
        CONF_SERIAL: SERIAL,
        CONF_PRIMARY_PANEL: SERIAL,
    }
    options = link_to_options(
        {
            CONF_TRANSPORT_MODE: "local_first",
            CONF_PANELS: [CLOUD_KEY],
            CONF_DECLINED: [],
            CONF_FEEDS: {},
            "other": 1,
        },
        _link(),
    )
    new_data, new_options = unlinked_entry(
        data, options, data_keys=CLOUD_DATA_KEYS, option_keys=CLOUD_OPTION_KEYS
    )
    assert new_data == local
    assert new_options == {CONF_TRANSPORT_MODE: "local_first", "other": 1}
    # The originals are left alone.
    assert CONF_TOKENS in data
    assert CONF_LINKS in options


@pytest.mark.parametrize(
    ("matched", "total", "ok"),
    [
        (0, 0, False),
        (0, 10, False),
        (4, 10, False),
        (5, 10, True),
        (10, 10, True),
        (2, 3, True),
        (1, 3, False),
    ],
)
def test_circuits_verified_needs_half_the_local_circuits(matched, total, ok):
    assert circuits_verified(matched, total) is ok


def test_a_picked_panel_keeps_its_own_cloud_key_and_the_local_serial():
    # SPAN can list a panel under a serial unlike the one it reports itself;
    # picked by hand, the link is keyed by the cloud's and remembers the local.
    picked = PanelLink(cloud_key="XC-AAAA-0000", local_serial="nt-0000-test")
    options = link_to_options({}, picked)
    assert set(options[CONF_LINKS]) == {"XC-AAAA-0000"}
    read = link_from_options(options)
    assert read is not None
    assert read.cloud_key == "XC-AAAA-0000"
    assert read.local_serial == "nt-0000-test"
