"""Control safety: command records, the confirm wait, the safety rules, the
islanding gate, the EV charger's limit and the options form. Pure helpers only;
the Home Assistant wiring is not importable here. Every id is synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from span_client.local import commands, model
from span_client.local.commands import (
    EVENT_KEYS,
    CommandRecord,
    Control,
    Delivery,
    Outcome,
    Pending,
    Refusal,
    SafetyRules,
)
from span_client.local.homie import DeviceState
from span_client.local.model import ControlOptions, Presence

from tests.test_local_model_synthetic import CIRCUIT, PANEL, _relay_tree, row, snap
from tests.test_local_subdevices import BESS, EVSE, MID, PV, SUBDEVICE_PROPS, site

STORAGE = {"connection/feeds-role": "STORAGE"}
COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "span_ebus"


def _rules(snapshot, **options) -> SafetyRules:
    return SafetyRules.from_options(options, snapshot)


# --- records and events ------------------------------------------------------------------


def test_event_data_carries_exactly_the_event_keys_as_plain_strings():
    record = CommandRecord(
        Control.RELAY, "OPEN", "CLOSED", Outcome.CONFIRMED, delivery=Delivery.ACKED
    )
    data = commands.event_data("switch.kitchen_breaker", record)
    assert set(data) == EVENT_KEYS
    assert data == {
        "entity_id": "switch.kitchen_breaker",
        "control": "relay",
        "requested": "OPEN",
        "previous": "CLOSED",
        "outcome": "confirmed",
        "delivery": "acked",
        "refusal": None,
    }
    assert all(type(v) is str for v in data.values() if v is not None)
    json.dumps(data)  # an event payload must serialise


def test_a_refused_record_has_no_delivery():
    record = CommandRecord(
        Control.RELAY, "OPEN", "CLOSED", Outcome.REFUSED, refusal=Refusal.PROTECTED
    )
    data = commands.event_data("switch.x", record)
    assert data["delivery"] is None
    assert data["refusal"] == "protected"


def test_records_name_no_device():
    """A record's fields are the control, the values and the outcome: there is
    nowhere for a device id to go but the entity id the caller passes."""
    fields = set(CommandRecord.__dataclass_fields__)
    assert fields == {"control", "requested", "previous", "outcome", "delivery", "refusal"}
    data = commands.event_data(
        "switch.x", CommandRecord(Control.ISLANDING, "ON_GRID", None, Outcome.NOT_CONFIRMED)
    )
    assert PANEL not in json.dumps(data)


@pytest.mark.parametrize(
    ("data", "text"),
    [
        ({"requested": "OPEN", "outcome": "refused", "refusal": "protected"},
         "OPEN requested: refused (protected)"),
        ({"requested": "OPEN", "outcome": "refused", "refusal": "storage_or_solar"},
         "OPEN requested: refused (storage or solar)"),
        ({"requested": "16", "outcome": "not_confirmed"}, "16 requested: not confirmed"),
        ({"requested": "LOCKED", "outcome": "confirmed", "refusal": None},
         "LOCKED requested: confirmed"),
        ({"requested": "NONE"}, "NONE requested: unknown"),
    ],
)  # fmt: skip
def test_describe(data, text):
    assert commands.describe(data) == text


def test_as_text():
    assert commands.as_text(None) is None
    assert commands.as_text(16) == "16"
    assert commands.as_text("OPEN") == "OPEN"


# --- the confirm wait -------------------------------------------------------------------

_PENDING = Pending("OPEN", deadline=110.0, previous="CLOSED", delivery=Delivery.ACKED, context=None)


@pytest.mark.parametrize(
    ("presence", "shown", "now", "outcome"),
    [
        (Presence.VALUE, "OPEN", 101.0, Outcome.CONFIRMED),
        (Presence.VALUE, "OPEN", 200.0, Outcome.CONFIRMED),  # echo at the deadline still counts
        (Presence.VALUE, "CLOSED", 101.0, None),  # still waiting
        (Presence.VALUE, "CLOSED", 110.0, Outcome.NOT_CONFIRMED),
        (Presence.VALUE, None, 111.0, Outcome.NOT_CONFIRMED),
        (Presence.UNKNOWN, "CLOSED", 101.0, None),
        (Presence.UNAVAILABLE, "OPEN", 101.0, Outcome.UNAVAILABLE),  # unavailable wins
    ],
)
def test_final_outcome(presence, shown, now, outcome):
    assert commands.final_outcome(_PENDING, presence, shown, now) is outcome


# --- control options ---------------------------------------------------------------------


def test_control_options_ignore_anything_malformed():
    assert ControlOptions.from_options(None) == ControlOptions()
    assert ControlOptions.from_options(
        {
            model.PROTECTED_CIRCUITS: ["c-1", "", 7, None],
            model.RELAY_OVERRIDE: "c-2",  # not a list
            model.ISLANDING_CONTROL: "yes",  # not True
        }
    ) == ControlOptions(protected=frozenset({"c-1"}))
    assert ControlOptions.from_options({model.ISLANDING_CONTROL: True}).islanding


def test_option_keys_match_the_integrations_constants():
    text = (COMPONENT / "const.py").read_text()
    for key in (model.PROTECTED_CIRCUITS, model.RELAY_OVERRIDE, model.ISLANDING_CONTROL):
        assert f'= "{key}"' in text


# --- relays ------------------------------------------------------------------------------


def test_an_unguarded_relay_passes():
    s = snap(_relay_tree())
    assert _rules(s).check(CIRCUIT, model.RELAY_KEY, "OPEN") is None


def test_a_protected_relay_is_refused_both_ways():
    s = snap(_relay_tree())
    rules = _rules(s, **{model.PROTECTED_CIRCUITS: [CIRCUIT]})
    assert rules.check(CIRCUIT, model.RELAY_KEY, "OPEN") is Refusal.PROTECTED
    assert rules.check(CIRCUIT, model.RELAY_KEY, "CLOSED") is Refusal.PROTECTED
    assert rules.check(CIRCUIT, model.RELAY_KEY) is Refusal.PROTECTED  # availability


def test_a_protected_id_the_snapshot_does_not_hold_still_fails_closed():
    s = snap(_relay_tree())
    rules = _rules(s, **{model.PROTECTED_CIRCUITS: ["circuit-gone"]})
    assert rules.check("circuit-gone", model.RELAY_KEY, "OPEN") is Refusal.PROTECTED
    # An unknown, unprotected id has nothing to block it.
    assert _rules(s).check("circuit-gone", model.RELAY_KEY, "OPEN") is None


def test_storage_relays_are_refused_without_the_override():
    s = snap(_relay_tree(**STORAGE))
    assert _rules(s).check(CIRCUIT, model.RELAY_KEY, "OPEN") is Refusal.STORAGE_OR_SOLAR
    overridden = _rules(s, **{model.RELAY_OVERRIDE: [CIRCUIT]})
    assert overridden.check(CIRCUIT, model.RELAY_KEY, "OPEN") is None


def test_protection_beats_the_override():
    s = snap(_relay_tree(**STORAGE))
    rules = _rules(s, **{model.PROTECTED_CIRCUITS: [CIRCUIT], model.RELAY_OVERRIDE: [CIRCUIT]})
    assert rules.check(CIRCUIT, model.RELAY_KEY, "OPEN") is Refusal.PROTECTED


def test_relay_blocked_and_row_exists_honour_the_override():
    s = snap(_relay_tree(**STORAGE))
    override = ControlOptions(relay_override=frozenset({CIRCUIT}))
    assert model.relay_blocked(s, CIRCUIT)
    assert not model.relay_blocked(s, CIRCUIT, override.relay_override)
    assert not model.row_exists(s, row("breaker"), CIRCUIT)
    assert model.row_exists(s, row("breaker"), CIRCUIT, override)


def test_a_protected_relay_is_still_created():
    """Protection refuses commands; the switch stays so its state is visible."""
    s = snap(_relay_tree())
    options = ControlOptions(protected=frozenset({CIRCUIT}))
    assert model.row_exists(s, row("breaker"), CIRCUIT, options)


@pytest.mark.parametrize(
    "key", ["circuit/priority", model.EVSE_LIMIT_KEY, "switch/lock-state", "anything/else"]
)
def test_other_keys_are_never_guarded(key):
    s = snap(_relay_tree(**STORAGE))
    rules = _rules(s, **{model.PROTECTED_CIRCUITS: [CIRCUIT]})
    assert rules.check(CIRCUIT, key, "X") is None


# --- islanding ---------------------------------------------------------------------------

_NOT_ASSERTED = {"shed/asserted-islanding-state": "NONE"}
_OPTED_IN = {model.ISLANDING_CONTROL: True}


def _island(*, bess_comm="LOST", mid_state=None, asserted="NONE", kinds=None):
    kinds = kinds if kinds is not None else {BESS: "bess", MID: "mid"}
    values = {PANEL: {"shed/asserted-islanding-state": asserted}}
    if BESS in kinds:
        values[BESS] = {"status/communication-state": bess_comm}
    if MID in kinds and mid_state is not None:
        values[MID] = {"grid/islanding-state": mid_state}
    return snap(site(kinds, values=values))


def test_islanding_is_refused_until_opted_in():
    s = _island(mid_state="OFF_GRID")
    assert _rules(s).check(PANEL, model.ISLANDING_KEY, "ON_GRID") is Refusal.NOT_OPTED_IN
    assert _rules(s).check(PANEL, model.ISLANDING_KEY, "NONE") is Refusal.NOT_OPTED_IN
    assert _rules(s, **_OPTED_IN).check(PANEL, model.ISLANDING_KEY, "ON_GRID") is None


def test_on_grid_is_gated_while_the_battery_reports():
    s = _island(bess_comm="OK", mid_state="OFF_GRID")
    assert s.panel.bess_healthy
    assert _rules(s, **_OPTED_IN).check(PANEL, model.ISLANDING_KEY, "ON_GRID") is Refusal.GATED
    assert _rules(s, **_OPTED_IN).check(PANEL, model.ISLANDING_KEY) is Refusal.GATED


def test_on_grid_is_gated_while_the_panel_senses_the_grid():
    s = _island(mid_state="ON_GRID")
    assert s.panel.sensed_islanding == "ON_GRID"
    assert _rules(s, **_OPTED_IN).check(PANEL, model.ISLANDING_KEY, "ON_GRID") is Refusal.GATED


def test_no_mid_reads_as_on_grid_so_the_control_is_gated():
    s = _island(kinds={BESS: "bess"})
    assert s.panel.sensed_islanding == "ON_GRID"
    assert model.islanding_gated(s.panel)


def test_an_unready_mid_reads_unknown_and_does_not_gate():
    tree = site(
        {BESS: "bess", MID: "mid"},
        values={PANEL: _NOT_ASSERTED, BESS: {"status/communication-state": "LOST"}},
    )
    tree.apply(f"ebus/5/{MID}/$state", b"init")
    s = snap(tree)
    assert s.panel.sensed_islanding is None
    assert not model.islanding_gated(s.panel, "ON_GRID")


def test_clearing_is_never_gated():
    s = _island(bess_comm="OK", mid_state="ON_GRID", asserted="ON_GRID")
    assert _rules(s, **_OPTED_IN).check(PANEL, model.ISLANDING_KEY, "NONE") is None
    assert _rules(s, **_OPTED_IN).check(PANEL, model.ISLANDING_KEY, "none") is None


def test_an_active_assertion_keeps_the_control_available():
    s = _island(bess_comm="OK", mid_state="ON_GRID", asserted="ON_GRID")
    assert _rules(s, **_OPTED_IN).check(PANEL, model.ISLANDING_KEY) is None
    # but re-asserting while healthy is still gated
    assert _rules(s, **_OPTED_IN).check(PANEL, model.ISLANDING_KEY, "ON_GRID") is Refusal.GATED


@pytest.mark.parametrize("value", ["OFF_GRID", "off_grid", "SOMETHING"])
def test_off_grid_and_unknown_values_are_refused(value):
    s = _island(mid_state="OFF_GRID")
    assert _rules(s, **_OPTED_IN).check(PANEL, model.ISLANDING_KEY, value) is Refusal.GATED


def test_no_panel_facts_gate_everything():
    assert model.islanding_gated(None)
    assert model.islanding_gated(None, "NONE")


@pytest.mark.parametrize(
    ("kinds", "opted_in", "exists"),
    [
        ({BESS: "bess", MID: "mid"}, True, True),
        ({BESS: "bess"}, True, True),
        ({BESS: "bess", MID: "mid"}, False, False),  # not opted in
        ({MID: "mid"}, True, False),  # no battery
        ({PV: "pv"}, True, False),
    ],
)
def test_the_islanding_select_needs_a_battery_and_the_option(kinds, opted_in, exists):
    s = snap(site(kinds))
    options = ControlOptions(islanding=opted_in)
    assert model.row_exists(s, row("asserted_islanding"), PANEL, options) is exists


def test_the_islanding_select_needs_a_settable_property(monkeypatch):
    from tests import test_local_subdevices as subs

    not_settable = {**subs.ROOT_ISLANDING["shed/asserted-islanding-state"], "settable": False}
    monkeypatch.setitem(subs.ROOT_ISLANDING, "shed/asserted-islanding-state", not_settable)
    s = snap(site({BESS: "bess"}))
    assert not s.panel.islanding_settable
    assert not model.row_exists(s, row("asserted_islanding"), PANEL, ControlOptions(islanding=True))


def test_the_islanding_select_shows_the_assertion_upper_case():
    islanding = row("asserted_islanding")
    assert islanding.value(_island(asserted="on_grid").panel) == "ON_GRID"
    assert islanding.value(_island(asserted="OFF_GRID").panel) == "OFF_GRID"


# --- the EV charger ----------------------------------------------------------------------


def _evse(values=None, formats=None, settable=frozenset()) -> model.SubdeviceFacts:
    return model.SubdeviceFacts(
        device_id=EVSE,
        kind="evse",
        state=DeviceState.READY,
        described=frozenset(SUBDEVICE_PROPS["evse"]),
        values=values or {},
        settable=settable,
        vendor=None,
        model=None,
        firmware=None,
        hardware=None,
        formats=formats or {},
    )


_CEILING = "config/max-charge-current"


@pytest.mark.parametrize(
    ("ceiling", "fmt", "bounds"),
    [
        (48.0, None, (6, 48)),  # the J1772 floor when no minimum is declared
        (48.0, "8:40", (8, 40)),  # the declared max below the ceiling wins
        (32.0, "6:80", (6, 32)),  # the ceiling below the declared max wins
        (31.5, None, (6, 31)),  # fractional ceiling floors
        (48.0, "6.5:", (7, 48)),  # fractional minimum rounds up
        (5.0, None, (5, 5)),  # min above max collapses onto max
        (48.0, "x:inf", (6, 48)),  # a malformed format is ignored
        (None, "6:48", None),  # ceiling unknown: no bounds
        ("n/a", None, None),
    ],
)
def test_evse_limit_bounds(ceiling, fmt, bounds):
    values = {} if ceiling is None else {_CEILING: ceiling}
    formats = {} if fmt is None else {model.EVSE_LIMIT_KEY: fmt}
    assert model.evse_limit_bounds(_evse(values, formats)) == bounds


@pytest.mark.parametrize(
    ("facts", "value", "problem"),
    [
        (None, 16, model.EVSE_LIMIT_GATED),
        (_evse(), 16, model.EVSE_LIMIT_GATED),  # ceiling unknown
        (_evse({_CEILING: 48.0}), 16, None),
        (_evse({_CEILING: 48.0}), 6, None),
        (_evse({_CEILING: 48.0}), 48, None),
        (_evse({_CEILING: 48.0}), 48.5, model.EVSE_LIMIT_OUT_OF_RANGE),
        (_evse({_CEILING: 48.0}), 5, model.EVSE_LIMIT_OUT_OF_RANGE),
        (_evse({_CEILING: 32.0}), 40, model.EVSE_LIMIT_OUT_OF_RANGE),
        (_evse({_CEILING: 48.0}), "lots", model.EVSE_LIMIT_OUT_OF_RANGE),
    ],
)
def test_evse_limit_problem(facts, value, problem):
    assert model.evse_limit_problem(facts, value) == problem


@pytest.mark.parametrize(("value", "payload"), [(16.0, 16), (16.9, 16), (6, 6)])
def test_evse_limit_payload_floors_to_whole_amps(value, payload):
    result = model.evse_limit_payload(value)
    assert result == payload
    assert type(result) is int


def _settable_evse(monkeypatch, *keys):
    props = dict(SUBDEVICE_PROPS["evse"])
    for key in keys:
        props[key] = {**props[key], "settable": True}
    monkeypatch.setitem(SUBDEVICE_PROPS, "evse", props)
    return snap(site({EVSE: "evse"}))


def test_evse_controls_exist_only_when_settable(monkeypatch):
    limit, lock = row(f"evse:{model.EVSE_LIMIT_KEY}"), row("evse:switch/lock-state")
    s = snap(site({EVSE: "evse"}))
    assert not model.row_exists(s, limit, EVSE)
    assert not model.row_exists(s, lock, EVSE)
    s = _settable_evse(monkeypatch, model.EVSE_LIMIT_KEY, "switch/lock-state")
    assert model.row_exists(s, limit, EVSE)
    assert model.row_exists(s, lock, EVSE)
    assert limit.value(s.evse[EVSE]) == 40.0
    assert lock.value(s.evse[EVSE]) == "UNLOCKED"


# --- the options form --------------------------------------------------------------------


def test_the_form_offers_switchable_circuits_by_label_never_by_id():
    s = snap(_relay_tree())
    form = commands.controls_form(s, {})
    assert list(form.protected) == [CIRCUIT]
    label = form.protected[CIRCUIT]
    assert CIRCUIT not in label
    assert PANEL not in label
    assert form.override == {}  # nothing feeds storage or solar
    assert not form.show_islanding


def test_the_form_offers_the_override_only_for_storage_or_solar_circuits():
    s = snap(_relay_tree(**STORAGE))
    form = commands.controls_form(s, {})
    assert list(form.override) == [CIRCUIT]
    assert list(form.protected) == [CIRCUIT]


def test_the_form_offers_islanding_only_with_a_battery_and_a_settable_property():
    assert commands.controls_form(snap(site({BESS: "bess"})), {}).show_islanding
    assert not commands.controls_form(snap(site({MID: "mid"})), {}).show_islanding


def test_the_form_defaults_drop_circuits_that_are_gone():
    s = snap(_relay_tree(**STORAGE))
    saved = {
        model.PROTECTED_CIRCUITS: ["circuit-gone", CIRCUIT],
        model.RELAY_OVERRIDE: ["circuit-gone"],
        model.ISLANDING_CONTROL: True,
    }
    form = commands.controls_form(s, saved)
    assert form.protected_default == [CIRCUIT]
    assert form.override_default == []
    assert form.islanding_default


@pytest.mark.parametrize(
    ("values", "label"),
    [
        ({"info/name": "Oven", "info/spaces": "9,11"}, "Oven (9/11)"),
        ({"info/name": "Oven"}, "Oven"),
        ({"info/spaces": "9,11"}, "Circuit 9/11"),
        ({}, "Circuit"),
    ],
)
def test_circuit_choice_label(values, label):
    circuit = snap(_relay_tree(**values)).circuits[CIRCUIT]
    assert commands.circuit_choice_label(circuit) == label


def test_controls_options_keep_only_what_was_offered():
    s = snap(_relay_tree(**STORAGE))
    form = commands.controls_form(s, {})
    saved = {"unrelated": 1, model.ISLANDING_CONTROL: True}
    result = commands.controls_options(
        form,
        saved,
        {
            model.PROTECTED_CIRCUITS: [CIRCUIT, "circuit-forged"],
            model.RELAY_OVERRIDE: [CIRCUIT],
            model.ISLANDING_CONTROL: True,  # not shown: saved off
        },
    )
    assert result == {
        "unrelated": 1,
        model.PROTECTED_CIRCUITS: [CIRCUIT],
        model.RELAY_OVERRIDE: [CIRCUIT],
        model.ISLANDING_CONTROL: False,
    }


def test_controls_options_with_nothing_ticked():
    form = commands.controls_form(snap(site({BESS: "bess"})), {})
    assert form.show_islanding
    result = commands.controls_options(form, {}, {model.ISLANDING_CONTROL: True})
    assert result == {
        model.PROTECTED_CIRCUITS: [],
        model.RELAY_OVERRIDE: [],
        model.ISLANDING_CONTROL: True,
    }


# --- translations ------------------------------------------------------------------------


def _strings() -> dict:
    return json.loads((COMPONENT / "strings.json").read_text())


@pytest.mark.parametrize("refusal", list(Refusal))
def test_every_refusal_has_a_translated_message(refusal):
    assert f"control_{refusal}" in _strings()["exceptions"]


def test_the_new_strings_exist():
    strings = _strings()
    assert "evse_limit_out_of_range" in strings["exceptions"]
    message = strings["exceptions"]["evse_limit_out_of_range"]["message"]
    assert "{min}" in message and "{max}" in message
    steps = strings["options"]["step"]
    assert set(steps["controls"]["data"]) == {
        model.PROTECTED_CIRCUITS,
        model.RELAY_OVERRIDE,
        model.ISLANDING_CONTROL,
    }
    assert "controls" in steps["local_panel"]["menu_options"]
    assert "controls_unavailable" in strings["options"]["abort"]
    entity = strings["entity"]
    assert set(entity["select"]["asserted_islanding"]["state"]) == {"none", "on_grid"}
    assert "evse_config_user_max_charge_current" in entity["number"]
    assert "evse_switch_lock_state" in entity["lock"]


def test_islanding_options_match_the_choices():
    strings = _strings()["entity"]["select"]["asserted_islanding"]["state"]
    assert {choice.lower() for choice in model.ISLANDING_CHOICES} == set(strings)
