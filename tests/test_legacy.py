"""The legacy repair's list: old cloud entities a local entry no longer feeds."""

from __future__ import annotations

import json

from span_client.identity import RegistryEntryView, transport_uid
from span_client.legacy import (
    DUPLICATE,
    SHOWN,
    LegacyEntities,
    cloud_suffix,
    legacy_entities,
    still_listed,
)
from span_client.local import model
from span_client.standin import FALLBACK, STANDIN_SUFFIXES, UNMATCHED

S = "nt-0000-test"
K = "XC-AAAA-0000"


def view(entity_id: str, unique_id: str) -> RegistryEntryView:
    return RegistryEntryView(entity_id, unique_id)


def local_views() -> list[RegistryEntryView]:
    """One uid of every local shape, none of which is ever listed."""
    return [
        view("sensor.transport", transport_uid(S)),
        view("sensor.voltage", model.panel_uid(S, "meter/voltage-a")),
        view("sensor.overhead", model.panel_uid(S, "derived/overhead")),
        view("sensor.lugs", model.lugs_uid(S, "lugs-up", "meter/active-power")),
        view("sensor.circuit", model.circuit_uid(S, "c0ffee", "meter/active-power")),
        view("switch.relay", model.circuit_uid(S, "c0ffee", "switch/relay")),
        view("sensor.soc", model.subdevice_uid(S, "bess", "b1", "soc/soc")),
    ]


# --- the uid shape ---------------------------------------------------------------


def test_cloud_suffix_reads_every_cloud_shape():
    for suffix in (
        "transport",
        "panel/power",
        "panel/overhead_energy",
        "site/grid_to_home_energy",
        "circuit-12/power_energy",
        "circuit-3/relay",
        "feed-41/power",
    ):
        assert cloud_suffix(f"{K}_{suffix}") == suffix
        assert cloud_suffix(f"{K}_{suffix}", K) == suffix


def test_cloud_suffix_ignores_local_uids():
    for v in local_views()[1:]:
        assert cloud_suffix(v.unique_id) is None, v.unique_id


def test_cloud_suffix_with_a_stem_takes_only_that_stem():
    assert cloud_suffix(f"{K}_site/grid", S) is None
    assert cloud_suffix(f"{S}_site/grid", S) == "site/grid"
    assert cloud_suffix(f"{S}_lugs/x/meter/active-power", S) is None


def test_cloud_suffix_with_an_underscored_stem():
    assert cloud_suffix("a_b_site/voltage_l1") == "site/voltage_l1"
    assert cloud_suffix("a_b_site/voltage_l1", "a_b") == "site/voltage_l1"


# --- an entry that was never a cloud entry ------------------------------------------


def test_a_plain_local_entry_lists_nothing():
    assert len(legacy_entities(local_views(), local_serial=S, cloud_stem=None)) == 0


def test_cloud_entities_on_a_local_entry_are_duplicates():
    # Before the link step, a local entry's cloud fallback added its own entities.
    old = [
        view("sensor.c3_power", f"{K}_circuit-3/power"),
        view("switch.c3_relay", f"{K}_circuit-3/relay"),
        view("sensor.site_grid", f"{K}_site/grid"),
        view("sensor.cloud_transport", f"{K}_transport"),
        view("sensor.cloud_overhead", f"{K}_panel/overhead"),
    ]
    got = legacy_entities(local_views() + old, local_serial=S, cloud_stem=None)
    assert got.duplicate == tuple(sorted(v.entity_id for v in old))
    assert got.unmatched == got.fallback_only == ()


def test_the_cloud_transport_on_the_local_serial_is_the_local_one():
    got = legacy_entities(
        [view("sensor.transport", f"{S}_transport"), view("sensor.g", f"{S}_site/grid")],
        local_serial=S,
        cloud_stem=None,
    )
    assert got.duplicate == ("sensor.g",)


# --- a cloud entry linked to its panel ----------------------------------------------


def linked_views() -> list[RegistryEntryView]:
    return [
        *local_views(),
        # stand-ins: fed locally
        *(view(f"sensor.standin_{i}", f"{K}_{s}") for i, s in enumerate(sorted(STANDIN_SUFFIXES))),
        # fallback-only: no local source
        view("sensor.grid_to_home", f"{K}_site/grid_to_home"),
        view("sensor.grid_to_home_energy", f"{K}_site/grid_to_home_energy"),
        view("sensor.site_grid_energy", f"{K}_site/grid_energy"),  # multi-panel site
        view("sensor.feed_current", f"{K}_feed-41/current"),
        # unmatched circuit
        view("sensor.c9_power", f"{K}_circuit-9/power"),
        view("switch.c9_relay", f"{K}_circuit-9/relay"),
    ]


def test_linked_entry_lists_fallback_only_and_unmatched_never_standins():
    got = legacy_entities(
        linked_views(),
        local_serial=S,
        cloud_stem=K,
        unmatched=["circuit-9/power", "circuit-9/relay"],
    )
    assert got.fallback_only == (
        "sensor.feed_current",
        "sensor.grid_to_home",
        "sensor.grid_to_home_energy",
        "sensor.site_grid_energy",
    )
    assert got.unmatched == ("sensor.c9_power", "switch.c9_relay")
    assert got.duplicate == ()
    assert not any(eid.startswith("sensor.standin_") for eid in got.entity_ids)


def test_linked_entry_with_nothing_left_lists_nothing():
    views = [*local_views()] + [
        view(f"sensor.standin_{i}", f"{K}_{s}") for i, s in enumerate(sorted(STANDIN_SUFFIXES))
    ]
    assert len(legacy_entities(views, local_serial=S, cloud_stem=K)) == 0


def test_linked_entry_where_the_cloud_stem_is_the_local_serial():
    # K == S: the re-pointed transport uid is unchanged and stays the local one.
    views = [
        view("sensor.transport", f"{S}_transport"),
        view("sensor.home", f"{S}_site/home"),
        view("sensor.balance", f"{S}_site/balance"),
    ]
    got = legacy_entities(views, local_serial=S, cloud_stem=S)
    assert got.entity_ids == ("sensor.balance",)
    assert got.fallback_only == ("sensor.balance",)


def test_transport_under_another_stem_came_back_and_is_a_duplicate():
    views = [view("sensor.transport", f"{S}_transport"), view("sensor.old", f"{K}_transport")]
    got = legacy_entities(views, local_serial=S, cloud_stem=K)
    assert got.duplicate == ("sensor.old",)


def test_cloud_entities_on_another_stem_of_a_linked_entry_are_duplicates():
    # A stand-in suffix on a stem the link didn't record is fed by nothing.
    views = [
        view("sensor.home_old", "XC-BBBB-0000_site/home"),
        view("sensor.home", f"{K}_site/home"),
    ]
    got = legacy_entities(views, local_serial=S, cloud_stem=K)
    assert got.duplicate == ("sensor.home_old",)
    assert got.fallback_only == got.unmatched == ()


# --- what the confirm step shows ----------------------------------------------------


def test_counts_and_the_shown_cap():
    got = LegacyEntities(
        duplicate=tuple(f"sensor.d{i:02}" for i in range(15)),
        unmatched=("switch.u",),
        fallback_only=tuple(f"sensor.f{i:02}" for i in range(10)),
    )
    assert len(got) == 26
    assert got.counts() == {DUPLICATE: 15, UNMATCHED: 1, FALLBACK: 10}
    shown, more = got.shown()
    assert len(shown) == SHOWN == 20
    assert more == 6
    assert shown == got.entity_ids[:20]
    assert LegacyEntities(unmatched=("switch.u",)).shown() == (("switch.u",), 0)


def test_shown_names_entity_ids_never_unique_ids():
    got = legacy_entities(linked_views(), local_serial=S, cloud_stem=K)
    shown, _more = got.shown()
    assert all("." in eid and K not in eid for eid in shown)


def test_still_listed_removes_only_what_was_shown_and_still_qualifies():
    now = LegacyEntities(duplicate=("sensor.a",), fallback_only=("sensor.c",))
    assert still_listed(["sensor.a", "sensor.b", "sensor.c"], now) == ("sensor.a", "sensor.c")
    assert still_listed(["sensor.b"], now) == ()


# --- strings -------------------------------------------------------------------------


def test_every_group_has_a_line_and_the_issue_its_placeholders():
    with open("custom_components/span_ebus/strings.json") as f:
        strings = json.load(f)
    options = strings["selector"]["legacy_group"]["options"]
    assert set(options) == {DUPLICATE, UNMATCHED, FALLBACK, "fallback_only_local", "more"}
    assert all("{count}" in text for text in options.values())
    assert "local connection is down" in options[FALLBACK]
    issue = strings["issues"]["legacy_entities"]
    assert "{title}" in issue["title"]
    confirm = issue["fix_flow"]["step"]["confirm"]["description"]
    for placeholder in ("{count}", "{title}", "{groups}", "{entities}"):
        assert placeholder in confirm
