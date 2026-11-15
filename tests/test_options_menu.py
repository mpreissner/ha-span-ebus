"""The local panel's options menu: Connection, Topology and Controls.

Home Assistant isn't importable here, so the flow's choices are tested through
the pure helpers it calls, and its strings and step ids by reading the files.
"""

import ast
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from span_client import backfill, cloud_auth, feeds
from span_client.cloud_traits import CircuitInfo
from span_client.link import (
    LOCAL_FIRST,
    LOCAL_ONLY,
    PanelLink,
    connection_action,
    connection_default,
    legacy_local_only,
    link_to_options,
)
from span_client.local.commands import ControlsForm, controls_hint_keys

COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "span_ebus"

# Invented ids only.
THIS = "nt-0000-aaaa1"
UP = "nt-0000-bbbb2"
OTHER = "nt-0000-cccc3"
FAR = "nt-0000-dddd4"
EMAIL = "someone@example.invalid"
HOST = "192.0.2.10"


def _strings() -> dict:
    return json.loads((COMPONENT / "strings.json").read_text())


def _linked(cloud_key: str, **options) -> dict:
    return link_to_options(options, PanelLink(cloud_key=cloud_key, local_serial=THIS))


# --- Topology: reading what older versions stored ----------------------------------------


def test_no_answer_is_own_supply():
    assert feeds.topology_of({}) == {"supply": feeds.OWN_SUPPLY}
    assert not feeds.local_fed({})


@pytest.mark.parametrize(("fed", "supply"), [(True, feeds.LEGACY_FED), (False, feeds.OWN_SUPPLY)])
def test_a_legacy_tick_derives(fed, supply):
    options = {feeds.FED_BY_PANEL: fed}
    assert feeds.topology_of(options) == {"supply": supply}
    assert feeds.local_fed(options) is fed


@pytest.mark.parametrize("feed_type", [feeds.LUGS, feeds.BRANCH])
def test_a_cloud_answer_gives_its_type_without_the_upstream(feed_type):
    options = _linked(
        "cloud-down",
        panels=["cloud-up", "cloud-down"],
        feeds={"cloud-down": {"type": feed_type, "upstream": "cloud-up"}},
    )
    assert feeds.topology_of(options) == {"supply": feed_type}
    assert feeds.local_fed(options)


def test_a_cloud_behind_der_answer_derives():
    options = _linked(
        "cloud-down", panels=["cloud-down"], feeds={"cloud-down": {"type": feeds.BEHIND_DER}}
    )
    assert feeds.topology_of(options) == {"supply": feeds.BEHIND_DER}


def test_the_tick_wins_over_a_cloud_answer():
    options = _linked(
        "cloud-down",
        panels=["cloud-up", "cloud-down"],
        feeds={"cloud-down": {"type": feeds.LUGS, "upstream": "cloud-up"}},
        fed_by_panel=False,
    )
    assert feeds.topology_of(options) == {"supply": feeds.OWN_SUPPLY}


def test_a_saved_answer_wins_over_everything():
    options = _linked(
        "cloud-down",
        panels=["cloud-up", "cloud-down"],
        feeds={"cloud-down": {"type": feeds.LUGS, "upstream": "cloud-up"}},
        fed_by_panel=True,
        topology={"supply": feeds.OWN_SUPPLY},
    )
    assert feeds.topology_of(options) == {"supply": feeds.OWN_SUPPLY}
    assert not feeds.local_fed(options)


@pytest.mark.parametrize(
    "bad",
    [
        "branch",
        None,
        [],
        {},
        {"supply": "utility"},
        {"supply": 3},
    ],
)
def test_an_invalid_saved_answer_falls_back(bad):
    assert feeds.topology_of({feeds.TOPOLOGY: bad, feeds.FED_BY_PANEL: True}) == {
        "supply": feeds.LEGACY_FED
    }
    assert feeds.topology_of({feeds.TOPOLOGY: bad}) == {"supply": feeds.OWN_SUPPLY}


def test_a_saved_answer_is_cleaned():
    assert feeds.saved_topology({"supply": feeds.OWN_SUPPLY, "upstream": UP}) == {
        "supply": feeds.OWN_SUPPLY
    }
    assert feeds.saved_topology({"supply": feeds.LUGS, "upstream": UP, "spaces": [1]}) == {
        "supply": feeds.LUGS,
        "upstream": UP,
    }
    assert feeds.saved_topology({"supply": feeds.BRANCH, "spaces": [3, 1]}) == {
        "supply": feeds.BRANCH
    }
    assert feeds.saved_topology({"supply": feeds.BRANCH, "upstream": UP, "spaces": [3, 1]}) == {
        "supply": feeds.BRANCH,
        "upstream": UP,
        "spaces": [1, 3],
    }
    assert feeds.saved_topology({"supply": feeds.BRANCH, "upstream": UP, "spaces": [1, True]}) == {
        "supply": feeds.BRANCH,
        "upstream": UP,
    }


def test_a_branch_without_readable_circuits_keeps_its_upstream():
    saved = feeds.with_topology({}, {"supply": feeds.BRANCH, "upstream": UP})
    assert saved[feeds.TOPOLOGY] == {"supply": feeds.BRANCH, "upstream": UP}
    assert feeds.topology_of(saved) == {"supply": feeds.BRANCH, "upstream": UP}


@pytest.mark.parametrize(
    ("supply", "fed"),
    [
        (feeds.OWN_SUPPLY, False),
        (feeds.BEHIND_DER, True),
        (feeds.LUGS, True),
        (feeds.BRANCH, True),
    ],
)
def test_saving_writes_the_mirror(supply, fed):
    saved = feeds.with_topology({feeds.FED_BY_PANEL: not fed}, {"supply": supply})
    assert saved[feeds.TOPOLOGY] == {"supply": supply}
    assert saved[feeds.FED_BY_PANEL] is fed
    assert feeds.local_fed(saved) is fed


def test_saving_one_panel_leaves_the_others_alone():
    entries = {
        THIS: {"x": 1},
        UP: {feeds.TOPOLOGY: {"supply": feeds.OWN_SUPPLY}, feeds.FED_BY_PANEL: False},
    }
    before = json.loads(json.dumps(entries))
    new = feeds.with_topology(entries[THIS], {"supply": feeds.LUGS, "upstream": UP})
    assert entries == before
    assert new is not entries[THIS]


def test_keeping_the_answer_before_the_cloud_goes():
    options = _linked(
        "cloud-down",
        panels=["cloud-up", "cloud-down"],
        feeds={"cloud-down": {"type": feeds.BRANCH, "upstream": "cloud-up", "circuit": "c1"}},
    )
    kept = feeds.keep_topology(options)
    assert kept[feeds.TOPOLOGY] == {"supply": feeds.BRANCH}
    assert kept[feeds.FED_BY_PANEL] is True
    saved = {feeds.TOPOLOGY: {"supply": feeds.LUGS, "upstream": UP}, feeds.FED_BY_PANEL: False}
    assert feeds.keep_topology(saved) == {
        feeds.TOPOLOGY: {"supply": feeds.LUGS, "upstream": UP},
        feeds.FED_BY_PANEL: True,
    }


def test_the_topology_option_is_not_a_cloud_option():
    from const import CLOUD_OPTION_KEYS, CONF_TOPOLOGY

    assert CONF_TOPOLOGY == feeds.TOPOLOGY
    assert feeds.TOPOLOGY not in CLOUD_OPTION_KEYS


# --- Connection ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("has_tokens", "chosen", "action"),
    [
        (True, LOCAL_FIRST, "noop"),
        (True, LOCAL_ONLY, "remove"),
        (False, LOCAL_FIRST, "add"),
        (False, LOCAL_ONLY, "noop"),
    ],
)
def test_connection_routing(has_tokens, chosen, action):
    mode = LOCAL_FIRST if has_tokens else LOCAL_ONLY
    assert connection_action(has_tokens, mode, chosen) == action


@pytest.mark.parametrize("mode", [None, LOCAL_FIRST, "bogus"])
def test_an_account_with_no_explicit_local_only_is_already_local_first(mode):
    assert connection_action(True, mode, LOCAL_FIRST) == "noop"


def test_an_account_behind_local_only_resumes_on_local_first():
    # The stored account stays: only the mode is saved, so it is the fallback again.
    assert connection_action(True, LOCAL_ONLY, LOCAL_FIRST) == "enable"
    assert connection_action(True, LOCAL_ONLY, LOCAL_ONLY) == "remove"


def _config_entries(*entries) -> Mock:
    config_entries = Mock()

    def update(entry, **changes):
        for key, value in changes.items():
            setattr(entry, key, value)

    config_entries.async_update_entry.side_effect = update
    config_entries.async_entries.return_value = list(entries)
    return config_entries


def _entry(entry_id: str, data: dict, options: dict, minor_version: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        entry_id=entry_id, data=data, options=options, minor_version=minor_version
    )


def test_enable_saves_the_mode_on_this_entry_only():
    from const import CONF_LOCAL_SERIAL, CONF_TOKENS
    from span_client.link import save_options

    data = {CONF_LOCAL_SERIAL: THIS, CONF_TOKENS: {"access": "x"}}
    entry = _entry("this", dict(data), {"transport_mode": LOCAL_ONLY, "keep": 1})
    config_entries = _config_entries(entry)
    saved = save_options(config_entries, entry, {**entry.options, "transport_mode": LOCAL_FIRST})
    assert saved == {"transport_mode": LOCAL_FIRST, "keep": 1}
    config_entries.async_update_entry.assert_called_once_with(entry, options=saved)
    config_entries.async_schedule_reload.assert_called_once_with("this")
    assert entry.data == data  # the account stays
    assert not legacy_local_only(entry.data, entry.options)


@pytest.mark.parametrize(
    ("has_tokens", "mode", "default"),
    [
        (True, LOCAL_FIRST, LOCAL_FIRST),
        (True, None, LOCAL_FIRST),
        (True, "bogus", LOCAL_FIRST),
        (True, LOCAL_ONLY, LOCAL_ONLY),
        (False, LOCAL_FIRST, LOCAL_ONLY),
        (False, None, LOCAL_ONLY),
        (False, LOCAL_ONLY, LOCAL_ONLY),
    ],
)
def test_connection_default(has_tokens, mode, default):
    assert connection_default(has_tokens, mode) == default


def test_the_modes_match_the_transport_modes():
    from span_client.local.health import Mode

    assert Mode.LOCAL_FIRST.value == LOCAL_FIRST
    assert Mode.LOCAL_ONLY.value == LOCAL_ONLY


def _legacy(**extra) -> tuple[dict, dict]:
    from const import CONF_EMAIL, CONF_LOCAL_SERIAL, CONF_TOKENS

    data = {CONF_LOCAL_SERIAL: THIS, CONF_TOKENS: {"access": "x"}, CONF_EMAIL: EMAIL, **extra}
    return data, {"transport_mode": LOCAL_ONLY}


def test_legacy_local_only_needs_all_three():
    from const import CONF_LOCAL_SERIAL, CONF_TOKENS

    data, options = _legacy()
    assert legacy_local_only(data, options)
    for mode in (LOCAL_FIRST, None, "Local_only", "bogus"):
        assert not legacy_local_only(data, {"transport_mode": mode})
    assert not legacy_local_only(data, {})
    assert not legacy_local_only({CONF_LOCAL_SERIAL: THIS}, options)
    cloud_account = {k: v for k, v in data.items() if k != CONF_LOCAL_SERIAL}
    assert not legacy_local_only(cloud_account, options)
    assert CONF_TOKENS in cloud_account


def _setup(entry: SimpleNamespace, token_file: Path) -> tuple[bool, Mock]:
    """Run setup's removal (`feeds.remove_legacy_account`, as `__init__` calls it)."""
    from const import CLOUD_DATA_KEYS, CLOUD_OPTION_KEYS, CONF_CLOUD_STEM

    config_entries = _config_entries(entry)

    async def discard_tokens() -> None:
        cloud_auth.discard_tokens(token_file)

    removed = asyncio.run(
        feeds.remove_legacy_account(
            entry,
            update=config_entries.async_update_entry,
            discard_tokens=discard_tokens,
            data_keys=CLOUD_DATA_KEYS,
            option_keys=CLOUD_OPTION_KEYS,
            cloud_stem_key=CONF_CLOUD_STEM,
        )
    )
    return removed, config_entries


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "tokens.json"
    path.write_text("{}")
    return path


@pytest.mark.parametrize("direction", ["A", "B"])
def test_setup_removes_a_legacy_account(direction, token_file):
    from const import CLOUD_DATA_KEYS, CLOUD_OPTION_KEYS, CONF_CLOUD_STEM
    from span_client.link import LINKED_MINOR_VERSION, UNLINKED_MINOR_VERSION

    extra = {CONF_CLOUD_STEM: "XC-TEST-0000"} if direction == "A" else {}
    data, options = _legacy(**extra)
    options = _linked(
        "cloud-down",
        **options,
        panels=["cloud-up", "cloud-down"],
        feeds={"cloud-down": {"type": feeds.LUGS, "upstream": "cloud-up"}},
    )
    entry = _entry("this", data, options, minor_version=LINKED_MINOR_VERSION)
    removed, config_entries = _setup(entry, token_file)
    assert removed
    config_entries.async_update_entry.assert_called_once()
    assert not CLOUD_DATA_KEYS & set(entry.data)
    assert not CLOUD_OPTION_KEYS & set(entry.options)
    assert entry.options["transport_mode"] == LOCAL_ONLY
    assert entry.options[feeds.TOPOLOGY] == {"supply": feeds.LUGS}
    assert entry.options[feeds.FED_BY_PANEL] is True
    expected = LINKED_MINOR_VERSION if direction == "A" else UNLINKED_MINOR_VERSION
    assert entry.minor_version == expected
    assert not token_file.exists()
    # A second setup has nothing left to remove.
    assert not legacy_local_only(entry.data, entry.options)
    removed, config_entries = _setup(entry, token_file)
    assert not removed
    config_entries.async_update_entry.assert_not_called()


def test_setup_keeps_a_saved_tick(token_file):
    data, options = _legacy()
    entry = _entry("this", data, {**options, feeds.FED_BY_PANEL: True})
    _setup(entry, token_file)
    assert entry.options[feeds.FED_BY_PANEL] is True
    assert feeds.local_fed(entry.options)


def test_setup_deletes_a_token_file_left_without_an_account(token_file):
    from const import CONF_LOCAL_SERIAL

    entry = _entry("this", {CONF_LOCAL_SERIAL: THIS}, {"transport_mode": LOCAL_ONLY})
    removed, config_entries = _setup(entry, token_file)
    assert not removed
    config_entries.async_update_entry.assert_not_called()
    assert not token_file.exists()


@pytest.mark.parametrize("mode", [LOCAL_FIRST, None])
def test_setup_leaves_an_account_in_use(mode, token_file):
    data, _options = _legacy()
    options = {} if mode is None else {"transport_mode": mode}
    entry = _entry("this", dict(data), options)
    removed, config_entries = _setup(entry, token_file)
    assert not removed
    config_entries.async_update_entry.assert_not_called()
    assert entry.data == data
    assert token_file.exists()


def test_setup_calls_the_shared_removal():
    """`__init__` runs `feeds.remove_legacy_account`, the function tested here."""
    tree = ast.parse((COMPONENT / "__init__.py").read_text())
    names = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "remove_legacy_account" in names
    assert "cloud_removed_entry" not in (COMPONENT / "__init__.py").read_text()


def test_saving_topology_writes_this_entry_only():
    from const import CONF_LOCAL_SERIAL, CONF_TOPOLOGY

    this = _entry("this", {CONF_LOCAL_SERIAL: THIS}, {"transport_mode": LOCAL_ONLY})
    other_topology = {"supply": feeds.LUGS, "upstream": THIS}
    other = _entry("other", {CONF_LOCAL_SERIAL: UP}, {CONF_TOPOLOGY: dict(other_topology)})
    config_entries = _config_entries(this, other)
    answer = {"supply": feeds.LUGS, "upstream": OTHER}
    saved = feeds.save_topology(config_entries, this, answer)
    config_entries.async_update_entry.assert_called_once_with(this, options=saved)
    config_entries.async_schedule_reload.assert_called_once_with("this")
    assert this.options[CONF_TOPOLOGY] == answer
    assert this.options["transport_mode"] == LOCAL_ONLY
    assert other.options == {CONF_TOPOLOGY: other_topology}


def _log_calls(source: str, function: str | None = None) -> list[ast.Call]:
    tree = ast.parse(source)
    roots = [tree]
    if function is not None:
        roots = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == function
        ]
        assert roots, function
    calls = []
    for root in roots:
        for node in ast.walk(root):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "_LOGGER"
            ):
                calls.append(node)
    return calls


def test_the_setup_removal_logs_no_identity(caplog):
    source = (COMPONENT / "__init__.py").read_text()
    calls = [
        call
        for call in _log_calls(source)
        if isinstance(call.args[0], ast.Constant)
        and "unused SPAN account" in str(call.args[0].value)
    ]
    assert len(calls) == 1
    (call,) = calls
    assert len(call.args) == 1 and not call.keywords
    message = call.args[0].value
    with caplog.at_level(logging.INFO):
        logging.getLogger("custom_components.span_ebus").info(message)
    for value in (THIS, EMAIL, HOST):
        assert value not in caplog.text


@pytest.mark.parametrize("function", ["_gather_upstreams", "_breakers_of"])
def test_a_failed_account_read_logs_no_panel(function):
    calls = _log_calls((COMPONENT / "config_flow.py").read_text(), function)
    assert calls
    for call in calls:
        assert len(call.args) == 1 and isinstance(call.args[0], ast.Constant)
        assert not call.keywords


# --- Connection wording guard -------------------------------------------------------------


def test_the_connection_labels_keep_their_qualifiers():
    labels = _strings()["selector"]["transport_mode"]["options"]
    first = labels[LOCAL_FIRST]
    assert first.startswith("Local, with cloud fallback")
    assert "five minutes" in first
    assert "site, solar and battery" in first
    assert "7 days" in first and "single-panel" in first
    assert "circuit energy is not backfilled" in first
    only = labels[LOCAL_ONLY]
    assert only.startswith("Local only")
    assert "account is removed" in only
    assert "backfill" in only


def test_the_backfill_limits_the_wording_states():
    # Change these and the Connection strings (and README) must be revisited.
    assert backfill.MIN_GAP_SECONDS == 300
    assert set(backfill.ROW_FIELDS) == {"site_energy", "pv_energy", "battery_energy"}


def test_the_remove_confirm_says_what_is_lost():
    step = _strings()["options"]["step"]["cloud_remove"]
    assert step["title"] == "Use local only"
    assert "removes your SPAN account" in step["description"]
    assert "is lost" in step["description"]
    assert step["description"].endswith("{warning}")


# --- Topology upstream choices -----------------------------------------------------------


def test_every_source_is_listed_and_this_panel_is_not():
    candidates = [
        (UP, None),  # an ignored entry
        (OTHER.upper(), "MAIN40"),  # an unloaded entry, other spelling
        (OTHER, None),  # the account's panel, same serial
        (THIS.upper(), "MAIN40"),  # this panel, from the account
        (FAR, None),  # a zeroconf flow
        (None, "MAIN40"),
    ]
    choices = feeds.upstream_choices(THIS, candidates, {})
    assert list(choices) == [UP, OTHER.upper(), FAR]
    assert choices[OTHER.upper()] == "SPAN MAIN40"
    assert choices[UP] == "Panel 1"
    assert choices[FAR] == "Panel 2"


def test_labels_fall_back_and_dedupe():
    candidates = [(UP, "MAIN40"), (OTHER, "MAIN40"), (FAR, None)]
    choices = feeds.upstream_choices(THIS, candidates, {FAR.upper(): "Garage"})
    assert choices == {UP: "SPAN MAIN40", OTHER: "SPAN MAIN40 (2)", FAR: "Garage"}


def test_panel_numbers_are_stable():
    one = feeds.upstream_choices(THIS, [(FAR, None), (UP, None)], {})
    two = feeds.upstream_choices(THIS, [(UP, None), (FAR, None)], {})
    assert one[UP] == two[UP] == "Panel 1"
    assert one[FAR] == two[FAR] == "Panel 2"


def test_no_label_carries_a_serial_or_host():
    candidates = [(UP, None), (OTHER, "MAIN40"), (FAR, None)]
    choices = feeds.upstream_choices(THIS, candidates, {OTHER: None})
    options, _default = feeds.upstream_options(choices, None)
    for label in options.values():
        for value in (THIS, UP, OTHER, FAR, HOST):
            assert value.casefold() not in label.casefold()


def test_a_stored_upstream_not_found_is_kept_as_default():
    options, default = feeds.upstream_options({UP: "Panel 1"}, FAR)
    assert default == FAR
    assert options[FAR] == feeds.NOT_FOUND_LABEL
    assert list(options)[-1] == feeds.NOT_LISTED


def test_a_stored_upstream_matches_any_spelling():
    options, default = feeds.upstream_options({UP: "Panel 1"}, UP.upper())
    assert default == UP
    assert UP.upper() not in options


def test_upstreams_dedupe_as_same_serial_matches():
    from span_client.backend import same_serial

    candidates = [
        (f" {UP} ", None),
        (UP.upper(), "MAIN40"),
        (f"{THIS.upper()} ", None),
        (" ", None),
    ]
    choices = feeds.upstream_choices(THIS, candidates, {f"{UP.upper()} ": "Garage"})
    assert choices == {UP: "Garage"}
    for a, b in [(f" {UP}", UP.upper()), (UP, OTHER), (THIS, f"{THIS.upper()}\t")]:
        assert same_serial(a, b) is (feeds.serial_key(a) == feeds.serial_key(b))
    _options, default = feeds.upstream_options(choices, f"{UP.upper()} ")
    assert default == UP


def test_a_panel_device_is_found_under_any_spelling_or_its_stem():
    stem = "XC-TEST-0001"
    linked = [
        (UP, "XC-TEST-0002"),
        (f"{THIS.upper()} ", stem),
        (OTHER, None),
        (None, "XC-TEST-0003"),
    ]
    assert feeds.panel_identifiers(THIS, linked) == [THIS, THIS.upper(), stem]
    assert feeds.panel_identifiers(f" {FAR}", []) == [f" {FAR}", FAR, FAR.upper()]


def test_no_stored_upstream_defaults_to_not_listed():
    options, default = feeds.upstream_options({UP: "Panel 1"}, None)
    assert default == feeds.NOT_LISTED
    assert options[feeds.NOT_LISTED] == feeds.NOT_LISTED_LABEL


@pytest.mark.parametrize(
    "info",
    [
        CircuitInfo(7, "Oven", (3, 5)),
        CircuitInfo(8, None, (9,)),
        CircuitInfo(9, "Spare", ()),
        CircuitInfo(10, None, ()),
    ],
)
def test_both_circuit_pickers_label_a_circuit_alike(info):
    assert (
        feeds.cloud_circuit_label(info) == feeds.circuit_choices({1: info})[str(info.instance_id)]
    )


def test_a_cloud_circuit_label_uses_its_display_name():
    assert feeds.cloud_circuit_label(CircuitInfo(7, "Oven", (3, 5))) == "Oven (3, 5)"
    assert feeds.cloud_circuit_label(CircuitInfo(8, None, (9,))) == "Circuit 8 (9)"
    assert feeds.cloud_circuit_label(CircuitInfo(10, None, ())) == "Circuit 10"


def test_breaker_choices_round_trip():
    choices = feeds.breaker_choices([("Sub panel (12/14)", [14, 12]), ("Oven", [3]), ("x", [])])
    assert list(choices) == ["3", "12,14"]
    assert feeds.spaces_from_value("12,14") == [12, 14]
    assert feeds.spaces_value([14, 12]) == "12,14"


# --- loop check --------------------------------------------------------------------------


def _up(serial: str) -> dict:
    return {"supply": feeds.LUGS, "upstream": serial}


def test_a_chain_through_saved_answers_is_a_loop():
    others = {UP: _up(OTHER), OTHER.upper(): _up(THIS.upper())}
    assert feeds.topology_loops(THIS, _up(UP), others)


def test_an_upstream_without_a_saved_answer_ends_the_chain():
    assert not feeds.topology_loops(THIS, _up(UP), {})
    assert not feeds.topology_loops(THIS, _up(UP), {UP: {"supply": feeds.OWN_SUPPLY}})


def test_a_derived_answer_does_not_count():
    assert not feeds.topology_loops(THIS, _up(UP), {UP: {"supply": feeds.LEGACY_FED}})


def test_own_supply_never_loops():
    assert not feeds.topology_loops(THIS, {"supply": feeds.OWN_SUPPLY}, {UP: _up(THIS)})


def test_cloud_answers_count_only_with_known_serials():
    answers = [(UP, {"type": feeds.BRANCH, "upstream": THIS, "circuit": "c1"})]
    assert feeds.topology_loops(THIS, _up(UP), {}, answers, [THIS, UP])
    assert not feeds.topology_loops(THIS, _up(UP), {}, answers, [THIS])


# --- cloud behind_der --------------------------------------------------------------------

PANELS = ["k-up", "k-down"]


def test_cloud_behind_der():
    answer = {"type": feeds.BEHIND_DER}
    assert feeds.feed_of({"k-down": answer}, "k-down", PANELS) == answer
    assert feeds.is_fed({"k-down": answer}, "k-down", PANELS)
    assert feeds.form_value(answer) == feeds.BEHIND_DER
    assert feeds.from_form(feeds.form_value(answer)) == answer
    assert feeds.from_form(feeds.OWN_SUPPLY) == {"type": feeds.OWN_SUPPLY}
    choices = feeds.feed_choices("k-down", {"k-up": "Up", "k-down": "Down"}, answer)
    assert list(choices)[:2] == [feeds.OWN_SUPPLY, feeds.BEHIND_DER]
    assert list(choices)[2:] == [f"{feeds.LUGS}:k-up", f"{feeds.BRANCH}:k-up"]
    cleaned = feeds.clean_feeds({"k-down": answer, "k-up": {"type": "lugs"}}, PANELS)
    assert cleaned == {"k-up": {"type": feeds.OWN_SUPPLY}, "k-down": answer}
    assert not feeds.has_cycle({"k-down": answer, "k-up": {"type": feeds.OWN_SUPPLY}})


def test_cloud_behind_der_names_a_linked_panel_fed():
    options = _linked("k-down", panels=PANELS, feeds={"k-down": {"type": feeds.BEHIND_DER}})
    assert feeds.cloud_fed(options)


# --- the other panels' answers -----------------------------------------------------------


def test_others_summary_phrases():
    labels = {UP: "Main", OTHER: "Garage", FAR: "Barn", THIS: "Here"}
    answers = [
        (UP, {"supply": feeds.OWN_SUPPLY}),
        (OTHER, {"supply": feeds.LUGS, "upstream": UP}),
        (FAR.upper(), {"supply": feeds.BRANCH, "upstream": THIS}),
        ("nt-0000-eeee5", {"supply": feeds.BEHIND_DER}),  # no label: left out
        (THIS, {"supply": feeds.BEHIND_DER}),  # this panel: left out
    ]
    text = feeds.others_summary(THIS, answers, labels)
    assert text == (
        "\n\nOther panels:\n"
        "- Main: fed from the utility meter\n"
        "- Garage: fed from Main's feed-through lugs\n"
        "- Barn: fed from a breaker on this panel"
    )
    for value in (THIS, UP, OTHER, FAR):
        assert value.casefold() not in text.casefold()


def test_others_summary_unknown_upstream_and_behind_der():
    labels = {UP: "Main", OTHER: "Garage"}
    answers = [
        (UP, {"supply": feeds.BEHIND_DER}),
        (OTHER, {"supply": feeds.BRANCH, "upstream": FAR}),
    ]
    assert feeds.others_summary(THIS, answers, labels) == (
        "\n\nOther panels:\n"
        "- Main: fed from the utility meter, with solar or a battery before it\n"
        "- Garage: fed from a breaker on another panel"
    )


def test_others_summary_is_empty_without_answers():
    assert feeds.others_summary(THIS, [], {UP: "Main"}) == ""
    derived = [(UP, {"supply": feeds.LEGACY_FED}), (OTHER, "junk")]
    assert feeds.others_summary(THIS, derived, {UP: "Main", OTHER: "Garage"}) == ""


# --- Controls hints ----------------------------------------------------------------------


def _form(override: dict, show_islanding: bool) -> ControlsForm:
    return ControlsForm(
        protected={"d1": "Oven (3)"},
        override=override,
        show_islanding=show_islanding,
        protected_default=[],
        override_default=[],
        islanding_default=False,
    )


@pytest.mark.parametrize(
    ("override", "islanding", "keys"),
    [
        ({}, False, ["controls_no_override", "controls_no_islanding"]),
        ({}, True, ["controls_no_override"]),
        ({"d2": "Battery (5/7)"}, False, ["controls_no_islanding"]),
        ({"d2": "Battery (5/7)"}, True, []),
    ],
)
def test_controls_hints(override, islanding, keys):
    form = _form(override, islanding)
    assert controls_hint_keys(not form.override, form.show_islanding) == keys
    notes = _strings()["selector"]["link_note"]["options"]
    for key in keys:
        assert notes[key]


def test_controls_description_has_the_hint_placeholder():
    assert _strings()["options"]["step"]["controls"]["description"].endswith("{hidden}")


# --- strings -----------------------------------------------------------------------------


def test_strings_and_english_match():
    english = json.loads((COMPONENT / "translations" / "en.json").read_text())
    assert _strings() == english


def _options_flow() -> ast.ClassDef:
    tree = ast.parse((COMPONENT / "config_flow.py").read_text())
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SpanEbusOptionsFlow"
    )


def test_every_options_step_and_menu_option_has_a_string():
    steps = _strings()["options"]["step"]
    shown = set()
    for node in ast.walk(_options_flow()):
        name = getattr(getattr(node, "func", None), "attr", "")
        if not (isinstance(node, ast.Call) and name in ("async_show_form", "async_show_menu")):
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        step_id = kwargs["step_id"]
        if not isinstance(step_id, ast.Constant):
            continue
        shown.add(step_id.value)
        assert step_id.value in steps, step_id.value
        if name == "async_show_menu":
            options = [elt.value for elt in kwargs["menu_options"].elts]
            assert set(options) == set(steps[step_id.value]["menu_options"]), step_id.value
            for option in options:
                assert option in steps, option
    assert {"panel", "transport", "topology", "topology_panel", "topology_circuit"} <= shown


def test_the_panel_menu():
    step = _strings()["options"]["step"]["panel"]
    assert step["title"] == "Panel options"
    assert step["menu_options"] == {
        "transport": "Connection",
        "topology": "Topology",
        "controls": "Controls",
    }


def test_the_old_steps_are_gone():
    strings = _strings()["options"]
    for step in ("local_panel", "linked", "fed_by_panel"):
        assert step not in strings["step"]
    assert "cloud_remove_unsupported" not in strings["abort"]


def test_the_topology_strings():
    strings = _strings()
    steps = strings["options"]["step"]
    assert steps["topology"]["title"] == "Topology"
    assert steps["topology"]["description"].endswith("Saving reloads the panel.{others}")
    assert steps["topology_panel"]["data"]["upstream"] == "Which panel"
    circuit = steps["topology_circuit"]
    assert circuit["data"]["circuit"] == "Which breaker"
    assert "{panel}" in circuit["description"] and "{upstream}" in circuit["description"]
    assert set(strings["selector"]["supply"]["options"]) == set(feeds.SUPPLIES)
