"""Tests for the Homie 5 device-tree parser.

The descriptions are hand-written in the r202639 shape (parent/child devices,
capability nodes, `energy.ebus.device.<class>` types) with made-up ids.
"""

import json

import pytest
from span_client.local import homie
from span_client.local.homie import DeviceState, TopicKind
from span_client.models import DataType

PANEL = "nt-0000-test"
LUGS = f"{PANEL}-lugs-up"
CIRCUIT = "0a1b2c3d4e5f"
BESS = f"{PANEL}-bess-0001"
MID = f"{PANEL}-mid-0001"


def description(type_: str, nodes: dict, **extra) -> bytes:
    return json.dumps(
        {
            "homie": "5.0",
            "version": 7,
            "name": type_,
            "type": f"energy.ebus.device.{type_}",
            "nodes": nodes,
            **extra,
        }
    ).encode()


PANEL_DESC = description(
    "distribution-enclosure",
    {
        "info": {"properties": {"serial-number": {"datatype": "string"}}},
        "shed": {
            "properties": {
                "asserted-islanding-state": {
                    "datatype": "enum",
                    "format": "NONE,ON_GRID,OFF_GRID",
                    "settable": True,
                }
            }
        },
    },
    children=[LUGS, CIRCUIT, BESS],
)
CIRCUIT_DESC = description(
    "circuit",
    {
        "switch": {
            "properties": {
                "relay": {"datatype": "enum", "format": "UNKNOWN,OPEN,CLOSED", "settable": True}
            }
        },
        "meter": {
            "properties": {
                "active-power": {"datatype": "float", "unit": "W"},
                "imported-energy": {"datatype": "float", "unit": "Wh"},
            }
        },
        "info": {"properties": {"name": {"datatype": "string"}, "space": {"datatype": "integer"}}},
        "status": {"properties": {"tripped": {"datatype": "boolean"}}},
    },
    parent=PANEL,
    root=PANEL,
)
LUGS_DESC = description("lugs", {}, parent=PANEL, root=PANEL)
BESS_DESC = description(
    "bess",
    {"status": {"properties": {"soe": {"datatype": "float", "unit": "kWh"}}}},
    parent=PANEL,
    root=PANEL,
    children=[MID],
)
MID_DESC = description("mid", {}, parent=BESS, root=PANEL)


def loaded_tree() -> homie.DeviceTree:
    tree = homie.DeviceTree()
    for device, desc in (
        (PANEL, PANEL_DESC),
        (CIRCUIT, CIRCUIT_DESC),
        (LUGS, LUGS_DESC),
        (BESS, BESS_DESC),
        (MID, MID_DESC),
    ):
        tree.apply(f"ebus/5/{device}/$description", desc)
        tree.apply(f"ebus/5/{device}/$state", b"ready")
    return tree


# --- topics ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("topic", "kind", "device", "key"),
    [
        (f"ebus/5/{PANEL}/$state", TopicKind.STATE, PANEL, None),
        (f"ebus/5/{BESS}/$description", TopicKind.DESCRIPTION, BESS, None),
        (f"ebus/5/{CIRCUIT}/meter/active-power", TopicKind.VALUE, CIRCUIT, "meter/active-power"),
        (f"ebus/5/{CIRCUIT}/switch/relay/set", TopicKind.SET, CIRCUIT, "switch/relay"),
        (f"ebus/5/{CIRCUIT}/switch/relay/$target", TopicKind.TARGET, CIRCUIT, "switch/relay"),
        (f"ebus/5/{PANEL}/$log", TopicKind.OTHER, PANEL, None),
        (f"ebus/5/{PANEL}/info/$name", TopicKind.OTHER, PANEL, None),
    ],
)
def test_parse_topic(topic, kind, device, key):
    parsed = homie.parse_topic(topic)
    assert parsed == homie.Topic(kind, device, key)


@pytest.mark.parametrize("topic", ["ebus/4/x/$state", "other/5/x/$state", "ebus/5/x", "ebus"])
def test_parse_topic_ignores_foreign(topic):
    assert homie.parse_topic(topic) is None


def test_set_topic_round_trips():
    topic = homie.set_topic(CIRCUIT, "switch/relay")
    assert topic == f"ebus/5/{CIRCUIT}/switch/relay/set"
    assert homie.parse_topic(topic) == homie.Topic(TopicKind.SET, CIRCUIT, "switch/relay")


# --- descriptions ---------------------------------------------------------


def test_parse_description_fields():
    desc = homie.parse_description(CIRCUIT, CIRCUIT_DESC)
    assert desc.device_class == "circuit"
    assert desc.parent == PANEL
    assert desc.root == PANEL
    assert desc.version == 7
    assert desc.capabilities == {"switch", "meter", "info", "status"}
    relay = desc.properties["switch/relay"]
    assert relay.settable
    assert relay.datatype is DataType.ENUM
    assert relay.enum_values == ("UNKNOWN", "OPEN", "CLOSED")
    assert desc.properties["meter/active-power"].unit == "W"


def test_unknown_datatype_falls_back_to_string():
    doc = {
        "type": "energy.ebus.device.circuit",
        "nodes": {"x": {"properties": {"y": {"datatype": "quaternion"}}}},
    }
    assert homie.parse_description("d", doc).properties["x/y"].datatype is DataType.STRING


def test_unknown_type_has_no_class():
    assert homie.parse_description("d", {"type": "vendor.thing"}).device_class == ""


@pytest.mark.parametrize("payload", [b"not json", b"[1, 2]"])
def test_bad_description_raises(payload):
    with pytest.raises(ValueError):
        homie.parse_description("d", payload)


# --- values ---------------------------------------------------------------


def prop(datatype: DataType, fmt: str | None = None) -> homie.PropertyDef:
    return homie.PropertyDef("c", "p", "p", datatype, format=fmt)


@pytest.mark.parametrize(
    ("datatype", "payload", "expected"),
    [
        (DataType.FLOAT, b"-123.5", -123.5),
        (DataType.INTEGER, b"42", 42),
        (DataType.BOOLEAN, b"true", True),
        (DataType.BOOLEAN, b"false", False),
        (DataType.ENUM, b"CLOSED", "CLOSED"),
        (DataType.JSON, b'{"a": 1}', {"a": 1}),
        (DataType.STRING, b"\x00", ""),
        (DataType.FLOAT, b"", None),
        (DataType.FLOAT, b"nan-ish", None),
        (DataType.INTEGER, b"4.5", None),
        (DataType.BOOLEAN, b"yes", None),
    ],
)
def test_decode_value(datatype, payload, expected):
    assert homie.decode_value(prop(datatype), payload) == expected


def test_decode_without_definition_is_text():
    assert homie.decode_value(None, b"12") == "12"


def test_encode_value():
    assert homie.encode_value(prop(DataType.ENUM, "OPEN,CLOSED"), "OPEN") == "OPEN"
    assert homie.encode_value(prop(DataType.INTEGER), 32) == "32"
    assert homie.encode_value(prop(DataType.BOOLEAN), True) == "true"
    with pytest.raises(ValueError):
        homie.encode_value(prop(DataType.ENUM, "OPEN,CLOSED"), "AJAR")
    with pytest.raises(ValueError):
        homie.encode_value(prop(DataType.INTEGER), 31.5)
    with pytest.raises(ValueError):
        homie.encode_value(prop(DataType.BOOLEAN), 1)


# --- tree -----------------------------------------------------------------


def test_values_before_description_decode_once_described():
    tree = homie.DeviceTree()
    tree.apply(f"ebus/5/{CIRCUIT}/meter/active-power", b"-150.0")
    assert tree.value(CIRCUIT, "meter/active-power") == "-150.0"
    tree.apply(f"ebus/5/{CIRCUIT}/$description", CIRCUIT_DESC)
    assert tree.value(CIRCUIT, "meter/active-power") == -150.0


def test_empty_payload_clears_a_value():
    tree = loaded_tree()
    tree.apply(f"ebus/5/{CIRCUIT}/switch/relay", b"CLOSED")
    assert tree.values(CIRCUIT) == {"switch/relay": "CLOSED"}
    tree.apply(f"ebus/5/{CIRCUIT}/switch/relay", b"")
    assert tree.values(CIRCUIT) == {}


def test_walk_reaches_grandchildren_parents_first():
    tree = loaded_tree()
    assert tree.roots() == [PANEL]
    order = [d.device_id for d in tree.walk(PANEL)]
    assert order == [PANEL, LUGS, CIRCUIT, BESS, MID]


def test_walk_skips_undescribed_children():
    tree = homie.DeviceTree()
    tree.apply(f"ebus/5/{PANEL}/$description", PANEL_DESC)
    tree.apply(f"ebus/5/{CIRCUIT}/$description", CIRCUIT_DESC)
    assert [d.device_id for d in tree.walk(PANEL)] == [PANEL, CIRCUIT]


def test_root_state_overrides_children():
    tree = loaded_tree()
    assert tree.effective_state(MID) is DeviceState.READY
    tree.apply(f"ebus/5/{PANEL}/$state", b"lost")
    assert tree.effective_state(MID) is DeviceState.LOST
    assert tree.effective_state(PANEL) is DeviceState.LOST


def test_unknown_state():
    assert DeviceState.parse("rebooting") is DeviceState.UNKNOWN


def test_bad_description_is_ignored_not_raised():
    tree = homie.DeviceTree()
    assert tree.apply(f"ebus/5/{PANEL}/$description", b"{") is None
    assert tree.descriptions == {}


def test_cleared_description_removes_device():
    tree = loaded_tree()
    tree.apply(f"ebus/5/{MID}/$description", b"")
    assert MID not in tree.descriptions


@pytest.mark.parametrize(
    "doc",
    [
        {"type": "energy.ebus.device.circuit", "nodes": [1]},
        {"type": "energy.ebus.device.circuit", "nodes": {"x": {"properties": [1]}}},
        {"type": "energy.ebus.device.circuit", "children": 3},
        {"type": "energy.ebus.device.circuit", "children": "abc"},
        {"type": "energy.ebus.device.circuit", "children": [1, 2]},
    ],
)
def test_malformed_description_is_value_error(doc):
    with pytest.raises(ValueError):
        homie.parse_description("d", doc)


def test_non_string_description_fields_are_dropped():
    doc = {
        "type": 7,
        "name": ["x"],
        "parent": {},
        "nodes": {"x": {"properties": {"y": {"datatype": "enum", "format": 5, "unit": 1}}}},
    }
    desc = homie.parse_description("d", doc)
    assert (desc.type, desc.name, desc.parent) == ("", None, None)
    assert desc.properties["x/y"].enum_values == ()
    assert desc.properties["x/y"].unit is None


@pytest.mark.parametrize("datatype", [DataType.INTEGER, DataType.FLOAT, DataType.BOOLEAN])
def test_nul_is_not_a_number(datatype):
    assert homie.decode_value(prop(datatype), b"\x00") is None


def test_empty_string_is_sent_as_nul_not_cleared():
    assert homie.encode_value(prop(DataType.STRING), "") == "\x00"


@pytest.mark.parametrize(
    ("datatype", "value"),
    [
        (DataType.STRING, None),
        (DataType.FLOAT, None),
        (DataType.FLOAT, "x"),
        (DataType.INTEGER, []),
    ],
)
def test_encode_bad_values_are_value_error(datatype, value):
    with pytest.raises(ValueError):
        homie.encode_value(prop(datatype), value)


def test_empty_state_clears_it():
    tree = loaded_tree()
    tree.apply(f"ebus/5/{CIRCUIT}/$state", b"")
    assert CIRCUIT not in tree.states


def test_cleared_description_drops_values():
    tree = loaded_tree()
    tree.apply(f"ebus/5/{CIRCUIT}/meter/active-power", b"5")
    tree.apply(f"ebus/5/{CIRCUIT}/$description", b"")
    assert tree.values(CIRCUIT) == {}
