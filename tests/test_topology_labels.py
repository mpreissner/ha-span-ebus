"""Names on a panel another panel feeds, and the circuit that feeds it.

Only names change: every unique id, device class and state class stays as it
was, so registry entries and statistics carry on.
"""

import ast
import json
from dataclasses import replace
from pathlib import Path

import pytest
from span_client import backend as cloud
from span_client import feeds, standin
from span_client.link import PanelLink, cloud_removed_entry, link_to_options
from span_client.local import model
from span_client.models import DataType, NodeKind, PropertySpec

from tests.test_cloud_backend import _circuits, _frame, _two_feed_inputs

COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "span_ebus"

UP, DOWN, OTHER = "panel-up", "panel-down", "panel-other"
PANELS = [UP, DOWN, OTHER]


def _branch(upstream: str, circuit: str) -> dict:
    return {"type": feeds.BRANCH, "upstream": upstream, "circuit": circuit}


def _linked(cloud_key: str, **options) -> dict:
    link = PanelLink(cloud_key=cloud_key, local_serial="local-serial-1")
    return link_to_options(options, link)


# --- is_fed ----------------------------------------------------------------------------


def test_own_supply_is_not_fed():
    assert not feeds.is_fed({}, DOWN, PANELS)
    assert not feeds.is_fed({DOWN: {"type": feeds.OWN_SUPPLY}}, DOWN, PANELS)


def test_lugs_and_branch_are_fed():
    assert feeds.is_fed({DOWN: {"type": feeds.LUGS, "upstream": UP}}, DOWN, PANELS)
    assert feeds.is_fed({DOWN: _branch(UP, "7")}, DOWN, PANELS)


def test_an_upstream_that_has_gone_falls_back_to_own_supply():
    answers = {DOWN: {"type": feeds.LUGS, "upstream": UP}}
    assert not feeds.is_fed(answers, DOWN, [DOWN, OTHER])


def test_a_panel_cannot_feed_itself():
    assert not feeds.is_fed({DOWN: {"type": feeds.LUGS, "upstream": DOWN}}, DOWN, PANELS)


# --- the local fed flag ----------------------------------------------------------------


def test_an_unlinked_entry_is_not_fed_until_it_says_so():
    assert not feeds.local_fed({})
    assert feeds.local_fed({feeds.FED_BY_PANEL: True})


def test_a_linked_entry_takes_the_cloud_answer():
    options = _linked(DOWN, panels=PANELS, feeds={DOWN: {"type": feeds.LUGS, "upstream": UP}})
    assert feeds.cloud_fed(options)
    assert feeds.local_fed(options)
    assert not feeds.local_fed(_linked(UP, panels=PANELS, feeds=options["feeds"]))


def test_the_saved_option_wins_over_the_cloud_answer():
    options = _linked(DOWN, panels=PANELS, feeds={DOWN: {"type": feeds.LUGS, "upstream": UP}})
    assert not feeds.local_fed({**options, feeds.FED_BY_PANEL: False})
    assert feeds.local_fed({**_linked(UP, panels=PANELS), feeds.FED_BY_PANEL: True})


def test_an_unlinked_entry_ignores_cloud_answers():
    answers = {DOWN: {"type": feeds.LUGS, "upstream": UP}}
    assert not feeds.cloud_fed({"panels": PANELS, "feeds": answers})


def _const_names() -> dict[str, object]:
    tree = ast.parse((COMPONENT / "const.py").read_text())
    names: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            value = node.value
            if isinstance(value, ast.Constant):
                names[node.targets[0].id] = value.value
            elif isinstance(value, ast.Call) and value.args and isinstance(value.args[0], ast.Set):
                names[node.targets[0].id] = frozenset(
                    names[e.id] for e in value.args[0].elts if isinstance(e, ast.Name)
                )
    return names


def test_the_option_is_not_a_cloud_option():
    names = _const_names()
    assert names["CONF_FED_BY_PANEL"] == feeds.FED_BY_PANEL
    assert feeds.FED_BY_PANEL not in names["CLOUD_OPTION_KEYS"]


def test_removing_the_cloud_keeps_the_option():
    cloud_keys = _const_names()["CLOUD_OPTION_KEYS"]
    options = _linked(DOWN, panels=PANELS, feeds={}, fed_by_panel=True)
    _data, kept, _minor = cloud_removed_entry(
        {}, options, data_keys=(), option_keys=cloud_keys, cloud_stem_key="cloud_stem"
    )
    assert kept == {feeds.FED_BY_PANEL: True}
    assert feeds.local_fed(kept)


# --- local names -----------------------------------------------------------------------


def test_fed_rows_get_feed_keys():
    by_key = {row.key: row for row in model.ROWS}
    for key, fed_key in model.FED_TRANSLATION_KEYS.items():
        row = by_key[key]
        assert model.translation_key(row, fed=True) == fed_key
        assert model.translation_key(row, fed=False) == key
        assert model.translation_key(row) == key


@pytest.mark.parametrize("row", model.ROWS, ids=lambda row: row.key)
def test_only_the_name_changes_on_a_fed_panel(row):
    if row.key not in model.FED_TRANSLATION_KEYS:
        assert model.translation_key(row, fed=True) == model.translation_key(row)
    # The unique id is built from the row alone, so it is the same either way;
    # the row itself (device class, state class, category) is never touched.
    before = replace(row)
    model.translation_key(row, fed=True)
    assert row == before
    assert model.row_uid(row, "panel-key", "device-1") == model.row_uid(
        before, "panel-key", "device-1"
    )


def test_the_neutral_rows_keep_their_names():
    by_key = {row.key: row for row in model.ROWS}
    for key in ("site_power", "upstream_current_a", "upstream_current_b"):
        if key in by_key:
            assert model.translation_key(by_key[key], fed=True) == key


# --- cloud names -----------------------------------------------------------------------


def _schema():
    return cloud.schema_from_frame("cloud-serial", _frame(), _circuits(), {})


def test_site_grid_is_named_for_the_feed_on_a_fed_panel():
    spec = _schema().properties["site/grid"]
    assert cloud.sensor_name(spec) == "Site grid"
    assert cloud.sensor_name(spec, fed=True) == "Site feed"
    assert cloud.energy_name(spec) == "Site grid energy"
    assert cloud.energy_name(spec, fed=True) == "Site feed energy"


def test_cloud_unique_ids_do_not_depend_on_the_feed():
    spec = _schema().properties["site/grid"]
    assert cloud.sensor_uid("cloud-serial", spec) == "cloud-serial_site/grid"
    assert cloud.energy_uid("cloud-serial", spec) == "cloud-serial_site/grid_energy"


def test_other_cloud_names_are_unchanged_on_a_fed_panel():
    for key, spec in _schema().properties.items():
        if key == "site/grid":
            continue
        assert cloud.sensor_name(spec, fed=True) == cloud.sensor_name(spec)
        assert cloud.energy_name(spec, fed=True) == cloud.energy_name(spec)


def test_names_keep_their_released_form():
    props = _schema().properties
    assert cloud.sensor_name(props["circuit-54/power"]) == "Branch A power"
    assert cloud.energy_name(props["circuit-54/power"]) == "Branch A energy"
    assert cloud.energy_name(props["site/grid"]) == "Site grid energy"


def test_a_spec_without_a_node_name_uses_its_id():
    spec = PropertySpec(
        node_id="site", node_kind=NodeKind.POWER_FLOWS, property_id="grid", name="grid",
        datatype=DataType.FLOAT,
    )  # fmt: skip
    assert cloud.sensor_name(spec, fed=True) == "site feed"


def test_a_feedthrough_is_not_a_second_main_feed():
    schema, _ = _two_feed_inputs({2: 3, 41: 4})
    assert schema.properties["feed-2/power"].node_name == "Main feed"
    assert schema.properties["feed-41/power"].node_name == "Feedthrough"
    assert cloud.sensor_name(schema.properties["feed-41/power"]) == "Feedthrough power"


def test_feeds_of_no_known_kind_are_named_by_instance():
    schema, _ = _two_feed_inputs({})
    assert schema.properties["feed-2/power"].node_name == "Feed 2"
    assert schema.properties["feed-41/power"].node_name == "Feed 41"


def test_a_lone_feed_is_the_main_feed_whatever_its_tag():
    schema = cloud.schema_from_frame("cloud-serial", _frame(), _circuits(), {2: 4})
    assert schema.properties["feed-2/power"].node_name == "Main feed"


def test_circuit_instance():
    props = _schema().properties
    assert cloud.circuit_instance(props["circuit-54/power"]) == 54
    assert cloud.circuit_instance(props["feed-2/power"]) is None
    assert cloud.circuit_instance(props["site/grid"]) is None


# --- kept cloud entities ---------------------------------------------------------------


def test_kept_grid_entities_follow_the_feed():
    assert standin.kept_name("site/grid", "Site grid", True) == "Site feed"
    assert standin.kept_name("site/grid_energy", "Site grid energy", True) == "Site feed energy"
    assert standin.kept_name("site/grid", "Site grid", False) == "Site grid"


def test_other_kept_names_are_the_registry_names():
    assert standin.kept_name("circuit-54/power", "Branch A power", True) == "Branch A power"
    assert standin.kept_name("site/grid", "Registry name", False) == "Registry name"
    assert standin.kept_name("panel/power", None, True) is None


# --- feeds_panel -----------------------------------------------------------------------


def test_branch_circuits_name_only_the_chosen_circuit():
    answers = {DOWN: _branch(UP, "7"), OTHER: {"type": feeds.LUGS, "upstream": UP}}
    assert feeds.branch_circuits(answers, UP, PANELS) == {"7": DOWN}
    assert feeds.branch_circuits(answers, DOWN, PANELS) == {}


def test_branch_circuits_ignore_a_stale_answer():
    assert feeds.branch_circuits({DOWN: _branch(UP, "7")}, UP, [DOWN]) == {}
    assert feeds.branch_circuits({DOWN: {"type": feeds.BRANCH, "upstream": UP}}, UP, PANELS) == {}


def test_feeds_panel_is_the_device_name_only():
    circuits = feeds.branch_circuits({DOWN: _branch(UP, "7")}, UP, PANELS)
    names = {DOWN: "Garage panel"}
    assert feeds.feeds_panel(circuits, 7, names) == "Garage panel"
    assert feeds.feeds_panel(circuits, "7", names) == "Garage panel"
    assert feeds.feeds_panel(circuits, 8, names) is None


def test_feeds_panel_is_absent_without_an_answer_or_a_name():
    assert feeds.feeds_panel({}, 7, {DOWN: "Garage panel"}) is None
    assert feeds.feeds_panel({"7": DOWN}, 7, {}) is None
    assert feeds.feeds_panel({"7": DOWN}, 7, {DOWN: None}) is None
    assert feeds.feeds_panel({"7": DOWN}, None, {DOWN: "Garage panel"}) is None


def test_feeds_panel_rows_are_the_circuit_power_and_energy_sensors():
    shown = {row.key for row in model.ROWS if model.shows_feeds_panel(row)}
    assert shown == model.FEEDS_PANEL_ROWS
    assert all(
        row.scope is model.Scope.CIRCUIT for row in model.ROWS if model.shows_feeds_panel(row)
    )


# --- strings ---------------------------------------------------------------------------


def _strings() -> dict:
    return json.loads((COMPONENT / "strings.json").read_text())


def test_strings_and_english_match():
    english = json.loads((COMPONENT / "translations" / "en.json").read_text())
    assert _strings() == english


def test_every_fed_key_has_a_name():
    sensors = _strings()["entity"]["sensor"]
    for fed_key in model.FED_TRANSLATION_KEYS.values():
        assert sensors[fed_key]["name"].startswith("Feed ")


def test_the_option_is_offered_on_both_menus():
    steps = _strings()["options"]["step"]
    assert "fed_by_panel" in steps["local_panel"]["menu_options"]
    assert "fed_by_panel" in steps["linked"]["menu_options"]
    assert steps["fed_by_panel"]["data"]["fed_by_panel"]
