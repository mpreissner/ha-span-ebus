"""Which panels run: the stored list, read against what the account holds now.

Shapes with several panels on a site are guesses until a real capture exists.
Every id here is invented.
"""

from __future__ import annotations

from dataclasses import replace

from span_client import backend as cloud

A = cloud.PanelInfo("site-1", "XC-AAAA", "1-04100-01", "gw-a")
B = cloud.PanelInfo("site-1", "XC-BBBB", "1-04100-01", "gw-b")
C = cloud.PanelInfo("site-2", "XC-CCCC", "1-04100-01", "gw-c")

SHARED = cloud.SiteInfo(
    "site-1", ("site-1", "gw-a", "gw-b"), (A, B), ("gw-a", "gw-b"), name="1 Secret St"
)
ALONE = cloud.SiteInfo("site-2", ("site-2", "gw-c"), (C,), ("gw-c",))
# Two gateways whose boards could not be paired.
UNPAIRED = cloud.SiteInfo(
    "site-1",
    ("site-1", "gw-a", "gw-b"),
    (replace(A, hardware_id=None), replace(B, hardware_id=None)),
    ("gw-a", "gw-b"),
)


def test_nothing_recorded_runs_every_panel_and_announces_none():
    plan = cloud.panel_plan([SHARED, ALONE], None)
    assert plan.chosen == ("XC-AAAA", "XC-BBBB", "XC-CCCC")
    assert plan.running == plan.sites
    assert plan.new == ()


def test_recorded_list_decides_what_runs():
    plan = cloud.panel_plan([SHARED, ALONE], ["XC-CCCC"], ["XC-AAAA", "XC-BBBB"])
    assert [site.site_id for site in plan.running] == ["site-2"]
    assert plan.new == ()


def test_panel_the_record_does_not_know_stays_off_and_is_announced():
    plan = cloud.panel_plan([SHARED, ALONE], ["XC-AAAA", "XC-BBBB"])
    assert [site.site_id for site in plan.running] == ["site-1"]
    assert plan.new == ("XC-CCCC",)


def test_new_board_on_a_chosen_site_gets_no_route():
    plan = cloud.panel_plan([SHARED], ["XC-AAAA"])
    assert plan.new == ("XC-BBBB",)
    routes = cloud.site_routes(plan.running[0], None, plan.chosen)
    assert routes.blocks == {"gw-a": "XC-AAAA", "site-1": "site-1"}


def test_declined_panel_stays_quiet():
    assert cloud.panel_plan([SHARED, ALONE], ["XC-AAAA", "XC-BBBB"], ["XC-CCCC"]).new == ()


def test_departed_panel_is_counted_and_ignored():
    plan = cloud.panel_plan([ALONE], ["XC-CCCC", "XC-GONE"])
    assert plan.departed == 1
    assert [site.site_id for site in plan.running] == ["site-2"]


def test_nothing_left_running_when_every_chosen_panel_departed():
    plan = cloud.panel_plan([ALONE], ["XC-GONE"])
    assert plan.running == []
    assert plan.new == ("XC-CCCC",)


def test_panel_recorded_by_hardware_id_keeps_that_key_once_paired():
    plan = cloud.panel_plan([SHARED], ["gw-b", "XC-AAAA"])
    routes = cloud.site_routes(plan.running[0], None, plan.chosen)
    keys = {panel.hardware_id: panel.key for panel in routes.panels}
    assert keys == {"gw-a": "XC-AAAA", "gw-b": "gw-b"}
    named = next(panel for panel in routes.panels if panel.hardware_id == "gw-b")
    assert cloud.device_name(named, routes).endswith("XC-BBBB")
    assert plan.new == ()


def test_panels_are_keyed_the_same_by_the_picker_and_the_router():
    site = cloud.panel_plan([UNPAIRED], None).running[0]
    assert list(cloud.panel_labels([site])) == [
        panel.key for panel in cloud.site_routes(site, None).panels
    ]


def test_unticking_a_panel_keeps_the_other_ones_name():
    routes = cloud.site_routes(UNPAIRED, None, ["gw-b"])
    assert [cloud.device_name(panel, routes) for panel in routes.panels] == ["SPAN panel 2"]


def test_primary_left_out_hands_flows_to_the_site_device():
    routes = cloud.site_routes(SHARED, "XC-AAAA", ["XC-BBBB"])
    assert routes.flows_owner.key == "site-1"
    assert [device.key for device in routes.devices] == ["XC-BBBB", "site-1"]


def test_labels_name_the_site_only_when_it_has_one():
    assert cloud.panel_labels([SHARED, ALONE]) == {
        "XC-AAAA": f"1 Secret St: {A.model} XC-AAAA",
        "XC-BBBB": f"1 Secret St: {B.model} XC-BBBB",
        "XC-CCCC": f"{C.model} XC-CCCC",
    }


def test_labels_offer_unpaired_gateways_by_number():
    assert cloud.panel_labels([UNPAIRED]) == {"gw-a": "panel 1", "gw-b": "panel 2"}


def test_boards_sharing_one_gateway_are_one_choice():
    site = replace(SHARED, gateways=("gw-a",))
    assert list(cloud.panel_labels([site])) == [site.lead.key]


def test_device_keys_include_the_site_device_only_with_a_chosen_panel():
    assert cloud.device_keys([SHARED], ["XC-AAAA"], None) == {"XC-AAAA", "site-1"}
    assert cloud.device_keys([SHARED, ALONE], ["XC-CCCC"], None) == {"XC-CCCC"}
