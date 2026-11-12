"""The local model on cases the r202639 capture lacks.

All data here is SYNTHETIC: hand-written descriptions in the r202639 shape with
made-up ids and values, because the captured panel has no solar, remote CT,
battery or PV flows.
"""

import json

import pytest
from span_client.local import homie, model
from span_client.local.debounce import Coalescer, clamp_interval
from span_client.local.health import Transport
from span_client.local.homie import DeviceState
from span_client.local.model import Presence, ValueKind

from tests.test_local_homie import description

PANEL = "nt-0000-test"
LUGS = "lugs-up"
CIRCUIT = "circuit-01"

ENUM_RELAY = {"datatype": "enum", "format": "UNKNOWN,OPEN,CLOSED", "settable": True}


def _props_map(keys: tuple[str, ...], special: dict[str, dict]) -> dict:
    """Description nodes for `capability/property` keys; floats unless given."""
    nodes: dict = {}
    for key in keys:
        capability, prop = key.split("/")
        spec = special.get(key, {"datatype": "float"})
        nodes.setdefault(capability, {"properties": {}})["properties"][prop] = spec
    return nodes


ROOT_KEYS = (
    "info/model",
    "info/name",
    "meter/frequency",
    "breaker/rating",
    "power-flows/grid",
    "power-flows/pv",
)
ROOT_SPECIAL = {
    "info/model": {"datatype": "string"},
    "info/name": {"datatype": "string"},
    "breaker/rating": {"datatype": "integer"},
}
CIRCUIT_KEYS = (
    "info/name",
    "info/spaces",
    "meter/active-power",
    "meter/imported-energy",
    "meter/exported-energy",
    "switch/relay",
    "switch/relay-controllable",
    "load-shed/priority",
    "connection/feeds-role",
)
CIRCUIT_SPECIAL = {
    "info/name": {"datatype": "string"},
    "info/spaces": {"datatype": "string"},
    "switch/relay": ENUM_RELAY,
    "switch/relay-controllable": {"datatype": "boolean"},
    "load-shed/priority": {
        "datatype": "enum",
        "format": "UNKNOWN,OFF_GRID,SOC_THRESHOLD,NEVER",
        "settable": True,
    },
    "connection/feeds-role": {"datatype": "enum", "format": "LOADS,SOLAR,STORAGE,GENERATOR"},
}
LUGS_KEYS = ("info/direction", "meter/active-power")
LUGS_SPECIAL = {"info/direction": {"datatype": "enum", "format": "UPSTREAM,DOWNSTREAM"}}


def tree_with(
    *,
    root_keys: tuple[str, ...] = ROOT_KEYS,
    circuit_keys: tuple[str, ...] = CIRCUIT_KEYS,
    extra_children: dict[str, bytes] | None = None,
    values: dict[str, dict[str, str]] | None = None,
) -> homie.DeviceTree:
    extra_children = extra_children or {}
    children = [LUGS, CIRCUIT, *extra_children]
    tree = homie.DeviceTree()
    tree.apply(
        homie.description_topic(PANEL),
        description(
            "distribution-enclosure", _props_map(root_keys, ROOT_SPECIAL), children=children
        ),
    )
    tree.apply(
        homie.description_topic(LUGS),
        description("lugs", _props_map(LUGS_KEYS, LUGS_SPECIAL), parent=PANEL, root=PANEL),
    )
    tree.apply(
        homie.description_topic(CIRCUIT),
        description("circuit", _props_map(circuit_keys, CIRCUIT_SPECIAL), parent=PANEL, root=PANEL),
    )
    for device_id, payload in extra_children.items():
        tree.apply(homie.description_topic(device_id), payload)
    for device_id in (PANEL, LUGS, CIRCUIT, *extra_children):
        tree.apply(f"ebus/5/{device_id}/$state", b"ready")
    tree.apply(homie.value_topic(LUGS, "info/direction"), b"UPSTREAM")
    for device_id, props in (values or {}).items():
        for key, text in props.items():
            tree.apply(homie.value_topic(device_id, key), text.encode())
    return tree


def snap(tree: homie.DeviceTree) -> model.Snapshot:
    return model.build_snapshot(tree, PANEL)


def row(key: str) -> model.Row:
    return next(r for r in model.ROWS if r.key == key)


def test_solar_circuit_keeps_the_panel_sign_and_nets_produced_minus_consumed():
    tree = tree_with(
        values={
            CIRCUIT: {
                "connection/feeds-role": "SOLAR",
                "meter/active-power": "2500.0",
                "meter/imported-energy": "9000.0",
                "meter/exported-energy": "100.0",
                "info/spaces": "9,11",
            }
        }
    )
    circuit = snap(tree).circuits[CIRCUIT]
    assert circuit.role is model.Role.SOLAR
    assert circuit.power == 2500.0
    assert circuit.net_wh == 8900.0


def test_load_circuit_inverts_power_and_nets_consumed_minus_produced():
    tree = tree_with(
        values={
            CIRCUIT: {
                "meter/active-power": "-120.5",
                "meter/imported-energy": "1.0",
                "meter/exported-energy": "500.0",
            }
        }
    )
    circuit = snap(tree).circuits[CIRCUIT]
    assert circuit.power == 120.5
    assert circuit.net_wh == 499.0


def test_remote_ct_has_no_controls_and_keeps_the_raw_sign():
    keys = tuple(k for k in CIRCUIT_KEYS if not k.startswith(("switch/", "info/spaces")))
    tree = tree_with(circuit_keys=keys, values={CIRCUIT: {"meter/active-power": "-80.0"}})
    snapshot = snap(tree)
    circuit = snapshot.circuits[CIRCUIT]
    assert circuit.is_remote_ct
    assert not circuit.has_switch
    assert circuit.power == -80.0
    assert not model.row_exists(snapshot, row("breaker"), CIRCUIT)
    assert model.circuit_label(circuit) == "Circuit"


def test_root_without_a_breaker():
    keys = tuple(k for k in ROOT_KEYS if not k.startswith("breaker/"))
    snapshot = snap(tree_with(root_keys=keys))
    assert snapshot.panel.has_main_breaker is False
    assert not model.row_exists(snapshot, row("main_breaker_rating"), PANEL)


def test_pv_flow_appears_once_published():
    tree = tree_with(values={PANEL: {"power-flows/grid": "-10.0"}})
    snapshot = snap(tree)
    assert snapshot.panel.flows.published == {"grid"}
    assert snapshot.panel.flows.grid == 10.0
    assert not model.row_exists(snapshot, row("pv_power"), PANEL)
    tree.apply(homie.value_topic(PANEL, "power-flows/pv"), b"3000.0")
    snapshot = snap(tree)
    assert snapshot.panel.flows.published == {"grid", "pv"}
    assert model.row_exists(snapshot, row("pv_power"), PANEL)


def test_a_root_not_ready_takes_its_children_down():
    tree = tree_with(values={CIRCUIT: {"meter/active-power": "-1.0"}})
    tree.apply(f"ebus/5/{PANEL}/$state", b"lost")
    circuit = snap(tree).circuits[CIRCUIT]
    assert circuit.state is DeviceState.LOST
    shown = model.presence(
        ValueKind.MEASUREMENT,
        shown=Transport.LOCAL,
        device_state=circuit.state,
        has_value=circuit.power is not None,
    )
    assert shown is Presence.UNAVAILABLE


def test_unmapped_devices_and_properties():
    bess = description("bess", _props_map(("meter/soc",), {}), parent=PANEL, root=PANEL)
    keys = (*CIRCUIT_KEYS, "meter/mystery")
    snapshot = snap(tree_with(circuit_keys=keys, extra_children={"bess-1": bess}))
    assert model.Unmapped("bess", None) in snapshot.unmapped
    assert model.Unmapped("circuit", "meter/mystery") in snapshot.unmapped
    assert "bess-1" not in snapshot.circuits and "bess-1" not in snapshot.lugs


def test_circuit_label_falls_back_to_spaces():
    tree = tree_with(values={CIRCUIT: {"info/spaces": "9,11"}})
    assert model.circuit_label(snap(tree).circuits[CIRCUIT]) == "Circuit 9/11"


def test_store_excludes_the_root_pii_but_keeps_circuit_names():
    tree = tree_with(
        values={
            PANEL: {"info/name": "Somewhere", "info/model": "MAIN_40"},
            CIRCUIT: {"info/name": "Oven", "meter/active-power": "-5.0"},
        }
    )
    payload = model.store_payload(tree)
    assert "info/name" not in payload["values"][PANEL]
    assert payload["values"][CIRCUIT]["info/name"] == "Oven"
    assert all("$state" not in v for v in payload["values"].values())
    restored = homie.DeviceTree()
    model.load_payload(payload, restored)
    back = model.build_snapshot(restored, PANEL, restored=True)
    assert back.restored
    assert back.circuits[CIRCUIT].name == "Oven"
    assert back.panel.model == "MAIN_40"
    assert back.circuits[CIRCUIT].power == snap(tree).circuits[CIRCUIT].power


def test_store_keeps_only_the_roots_devices_and_strips_child_serials():
    orphan = "circuit-99"
    tree = tree_with(values={CIRCUIT: {"info/name": "Oven", "info/serial-number": "child-serial"}})
    tree.apply(
        homie.description_topic(orphan),
        description("circuit", {"info/name": {"datatype": "string"}}, parent=PANEL, root=PANEL),
    )
    tree.apply(homie.value_topic(orphan, "info/name"), b"Gone")
    payload = model.store_payload(tree, PANEL)
    assert orphan not in payload["descriptions"]
    assert orphan not in payload["values"]
    assert payload["values"][CIRCUIT] == {"info/name": "Oven"}
    # Without a described root nothing is pruned: a save must never empty the Store.
    assert orphan in model.store_payload(tree, "not-described")["descriptions"]


@pytest.mark.parametrize(
    ("live", "restored", "from_store", "shown"),
    [
        (None, 10.0, True, 10.0),  # nothing in the tree: the sensor's own state
        (8.0, 10.0, True, 10.0),  # stale Store after a crash: never step back
        (12.0, 10.0, True, 12.0),
        (8.0, 10.0, False, 8.0),  # live values always win
        (8.0, None, True, 8.0),
        (None, None, True, None),
    ],
)
def test_energy_floor(live, restored, from_store, shown):
    assert model.energy_floor(live, restored, from_store) == shown


@pytest.mark.parametrize(
    "data", [None, [], {"descriptions": {}}, {"descriptions": {}, "values": []}]
)
def test_store_rejects_a_bad_shape(data):
    with pytest.raises(ValueError):
        model.load_payload(data, homie.DeviceTree())


# --- presence: every cell of the table ------------------------------------------------

_V, _U, _X = Presence.VALUE, Presence.UNKNOWN, Presence.UNAVAILABLE
_L, _C, _O = Transport.LOCAL, Transport.CLOUD, Transport.OFFLINE
_M, _S, _E, _K = ValueKind.MEASUREMENT, ValueKind.STATIC, ValueKind.ENERGY, ValueKind.CONTROL

PRESENCE_TABLE = [
    # kind, shown, device up, has value, expected
    (_E, _L, True, True, _V), (_E, _L, False, False, _X), (_E, _O, False, True, _V),
    (_E, _C, True, False, _X),
    (_M, _L, True, True, _V), (_M, _L, True, False, _V), (_M, _L, False, True, _X),
    (_M, _C, True, True, _U), (_M, _C, False, False, _U), (_M, _O, True, True, _X),
    (_S, _L, True, False, _V), (_S, _L, False, True, _V), (_S, _L, False, False, _X),
    (_S, _C, False, True, _V), (_S, _C, False, False, _U), (_S, _O, True, True, _X),
    (_K, _L, True, True, _V), (_K, _L, True, False, _X), (_K, _L, False, True, _X),
    (_K, _C, True, True, _X), (_K, _O, True, True, _X),
]  # fmt: skip


@pytest.mark.parametrize(("kind", "shown", "up", "has_value", "expected"), PRESENCE_TABLE)
def test_presence(kind, shown, up, has_value, expected):
    state = DeviceState.READY if up else DeviceState.LOST
    assert model.presence(kind, shown=shown, device_state=state, has_value=has_value) is expected


def test_presence_alert_counts_as_up_and_a_missing_state_does_not():
    kw = {"shown": _L, "has_value": True}
    assert model.presence(_M, device_state=DeviceState.ALERT, **kw) is _V
    assert model.presence(_M, device_state=None, **kw) is _X


def test_presence_on_the_cloud_uses_the_fallback_reading():
    kw = {"shown": _C, "device_state": None, "has_value": False}
    assert model.presence(_M, fallback_has_value=True, **kw) is _V
    assert model.presence(_M, fallback_has_value=False, **kw) is _U


# --- the debounce window ---------------------------------------------------------------


class FakeLoop:
    def __init__(self) -> None:
        self.soon: list = []
        self.later: list = []

    def call_soon(self, action):
        self.soon.append(action)
        return lambda: self.soon.remove(action)

    def call_later(self, delay, action):
        self.later.append((delay, action))
        return lambda: self.later.remove((delay, action))


def test_a_burst_gives_one_flush():
    loop, flushes = FakeLoop(), []
    coalescer = Coalescer(
        1.0, call_soon=loop.call_soon, call_later=loop.call_later, flush=lambda: flushes.append(1)
    )
    for _ in range(5):
        coalescer.mark()
    assert len(loop.later) == 1 and loop.later[0][0] == 1.0
    _, fire = loop.later.pop()
    fire(None)
    assert flushes == [1]
    coalescer.mark()  # the next message opens a new window
    assert len(loop.later) == 1


def test_interval_zero_flushes_on_the_next_iteration():
    loop, flushes = FakeLoop(), []
    coalescer = Coalescer(
        0.0, call_soon=loop.call_soon, call_later=loop.call_later, flush=lambda: flushes.append(1)
    )
    coalescer.mark()
    coalescer.mark()
    assert len(loop.soon) == 1 and not loop.later
    loop.soon.pop()()
    assert flushes == [1]


def test_stop_cancels_the_pending_flush():
    loop, flushes = FakeLoop(), []
    coalescer = Coalescer(
        1.0, call_soon=loop.call_soon, call_later=loop.call_later, flush=lambda: flushes.append(1)
    )
    coalescer.mark()
    coalescer.stop()
    assert not loop.later
    coalescer.mark()
    assert not loop.later and not flushes


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, 1.0), ("2.5", 2.5), (-3, 0.0), (99, 15.0), (float("nan"), 1.0), (None, 1.0), ("x", 1.0)],
)
def test_clamp_interval(value, expected):
    assert clamp_interval(value, 1.0) == expected


# --- 4b-1: shed forecast and policy (SYNTHETIC) -------------------------------------------

SHED_KEYS = (
    *ROOT_KEYS,
    "shed-forecast/time-to-priority-shed",
    "shed-forecast/total-time-remaining",
    "shed-forecast/full-charge-time-to-priority-shed",
    "shed-forecast/full-charge-total-time-remaining",
    "shed-forecast/confidence",
    "shed/policy",
)
ROOT_SPECIAL.update(
    {
        "shed-forecast/time-to-priority-shed": {"datatype": "integer"},
        "shed-forecast/total-time-remaining": {"datatype": "integer"},
        "shed-forecast/full-charge-time-to-priority-shed": {"datatype": "integer"},
        "shed-forecast/full-charge-total-time-remaining": {"datatype": "integer"},
        "shed-forecast/confidence": {"datatype": "enum", "format": "LOW,MEDIUM,HIGH"},
        "shed/policy": {"datatype": "json"},
    }
)
POLICY = {
    "algorithm": "soc-priority.v1",
    "parameters": {"soc-threshold-shed": 20, "soc-threshold-release": 30.5},
}


@pytest.mark.parametrize(
    "value",
    [
        POLICY,
        json.dumps(POLICY),
        {"version": "soc-priority.v1", **POLICY["parameters"]},
    ],
)
def test_parse_shed_policy_accepts(value):
    assert model.parse_shed_policy(value) == model.ShedPolicy(20.0, 30.5)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "not json",
        "[1, 2]",
        {"algorithm": "soc-priority.v2", "parameters": POLICY["parameters"]},
        {"algorithm": "soc-priority.v1", "parameters": "x"},
        {
            "algorithm": "soc-priority.v1",
            "parameters": {"soc-threshold-shed": 120, "soc-threshold-release": 30},
        },
        {
            "algorithm": "soc-priority.v1",
            "parameters": {"soc-threshold-shed": -1, "soc-threshold-release": 30},
        },
        {
            "algorithm": "soc-priority.v1",
            "parameters": {"soc-threshold-shed": True, "soc-threshold-release": 30},
        },
        {
            "algorithm": "soc-priority.v1",
            "parameters": {"soc-threshold-shed": "20", "soc-threshold-release": 30},
        },
        {"algorithm": "soc-priority.v1", "parameters": {"soc-threshold-shed": 20}},
    ],
)
def test_parse_shed_policy_rejects(value):
    assert model.parse_shed_policy(value) is None


def test_shed_policy_rejection_never_logs_the_payload(caplog):
    caplog.set_level("DEBUG")
    model.parse_shed_policy('{"algorithm": "secret-site-123"}')
    assert "secret-site-123" not in caplog.text


def test_shed_forecast_sensors_and_attributes():

    tree = tree_with(
        root_keys=SHED_KEYS,
        values={
            PANEL: {
                "shed-forecast/time-to-priority-shed": "95",
                "shed-forecast/total-time-remaining": "240",
                "shed-forecast/full-charge-time-to-priority-shed": "400",
                "shed-forecast/full-charge-total-time-remaining": "900",
                "shed-forecast/confidence": "HIGH",
                "shed/policy": json.dumps(POLICY),
            }
        },
    )
    s = snap(tree)
    panel = s.panel
    assert model.row_exists(s, row("time_to_priority_shed"), PANEL)
    assert row("time_to_priority_shed").value(panel) == 95
    assert row("total_time_remaining").value(panel) == 240
    assert row("time_to_priority_shed").attrs(panel) == {
        "full_charge_time_to_priority_shed": 400,
        "confidence": "high",
        "soc_threshold_shed": 20.0,
        "soc_threshold_release": 30.5,
    }
    assert row("total_time_remaining").attrs(panel) == {
        "full_charge_total_time_remaining": 900,
        "confidence": "high",
    }
    assert "shed/policy" not in {u.key for u in s.unmapped}


def test_shed_forecast_absent_without_the_capability():
    s = snap(tree_with())
    assert s.panel.shed_forecast is None and s.panel.shed_policy is None
    assert not model.row_exists(s, row("time_to_priority_shed"), PANEL)


# --- 4b-1: circuit links and the relay rule (SYNTHETIC) -----------------------------------

LINK_KEYS = (
    *CIRCUIT_KEYS,
    "connection/feeds-device-id",
    "connection/feeds-device-type",
    "connection/feeds-device-status",
    "connection/backed-up",
    "connection/count",
    "info/locations",
)
CIRCUIT_SPECIAL.update(
    {
        "connection/feeds-device-id": {"datatype": "string"},
        "connection/feeds-device-type": {"datatype": "string"},
        "connection/feeds-device-status": {"datatype": "enum", "format": "OK,LOST,DEGRADED"},
        "connection/backed-up": {"datatype": "enum", "format": "BACKED_UP,NOT_BACKED_UP"},
        "connection/count": {"datatype": "integer"},
        "info/locations": {"datatype": "string"},
    }
)


def _relay_tree(**circuit_values: str) -> homie.DeviceTree:
    values = {"switch/relay": "CLOSED", "switch/relay-controllable": "true", **circuit_values}
    return tree_with(circuit_keys=LINK_KEYS, values={CIRCUIT: values})


@pytest.mark.parametrize(
    ("values", "has_breaker"),
    [
        ({}, True),
        (
            {
                "connection/feeds-device-id": "x-1",
                "connection/feeds-device-type": "energy.ebus.device.bess",
            },
            False,
        ),
        ({"connection/feeds-device-id": "x-1", "connection/feeds-device-type": "PV"}, False),
        (
            {
                "connection/feeds-device-id": "x-1",
                "connection/feeds-device-type": "energy.ebus.device.evse",
            },
            True,
        ),
        ({"connection/feeds-device-id": "x-1"}, True),  # unknown target
        ({"connection/feeds-device-type": "energy.ebus.device.bess"}, True),  # no id yet
        ({"connection/feeds-role": "STORAGE"}, False),
        ({"connection/feeds-role": "LOADS"}, True),
    ],
)
def test_relay_rule(values, has_breaker):
    s = snap(_relay_tree(**values))
    assert s.circuits[CIRCUIT].has_switch
    assert model.row_exists(s, row("breaker"), CIRCUIT) is has_breaker
    # The relay state stays visible as an attribute either way.
    assert row("power").attrs(s.circuits[CIRCUIT])["relay_state"] == "CLOSED"


def test_link_facts_and_connection_attributes():
    s = snap(
        _relay_tree(
            **{
                "connection/feeds-device-id": "x-1",
                "connection/feeds-device-type": "energy.ebus.device.bess",
                "connection/feeds-device-status": "DEGRADED",
                "connection/backed-up": "BACKED_UP",
                "connection/count": "3",
                "info/locations": '["Garage", " Shed ", ""]',
            }
        )
    )
    circuit = s.circuits[CIRCUIT]
    assert circuit.feeds == model.Link(via_kind="bess", via_id="x-1", status="DEGRADED")
    assert circuit.fed_by is None
    attrs = row("power").attrs(circuit)
    assert attrs["backed_up"] == "backed_up"
    assert attrs["unit_count"] == 3
    assert attrs["location"] == "Garage, Shed"


def test_plain_location_string():
    s = snap(_relay_tree(**{"info/locations": "Kitchen"}))
    assert s.circuits[CIRCUIT].location == "Kitchen"
