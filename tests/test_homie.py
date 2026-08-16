"""Tests for Homie 5 schema parsing and firmware quirk compensation."""

from span_bridge.homie import (
    classify_node,
    effective_unit,
    parse_description,
    parse_value_topic,
    topic_for,
)
from span_bridge.models import DataType, NodeKind

SERIAL = "<local-serial>"
CIRCUIT_UUID = "ac3dccda46a94b98878a227df6fed588"

DESCRIPTION = {
    "homie": "5.0",
    "name": "SPAN Panel",
    "nodes": {
        "core": {
            "name": "Panel Core",
            "properties": {
                "l1-voltage": {"name": "L1 Voltage", "datatype": "float", "unit": "V"},
                "door": {
                    "name": "Door",
                    "datatype": "enum",
                    "format": "UNKNOWN,OPEN,CLOSED",
                },
                "dominant-power-source": {
                    "name": "Dominant Power Source",
                    "datatype": "enum",
                    "format": "GRID,BATTERY,PV,GENERATOR,NONE,UNKNOWN",
                    "settable": True,
                },
                "software-version": {"name": "Software Version", "datatype": "string"},
                "grid-islandable": {"name": "Grid Islandable", "datatype": "boolean"},
            },
        },
        "lugs-upstream": {
            "name": "Upstream Lugs",
            "properties": {
                "active-power": {"name": "Active Power", "datatype": "float", "unit": "W"},
                "imported-energy": {
                    "name": "Imported Energy",
                    "datatype": "float",
                    "unit": "Wh",
                },
            },
        },
        CIRCUIT_UUID: {
            "name": "Circuit",
            "properties": {
                "name": {"name": "Name", "datatype": "string"},
                # Firmware declares kW here but reports watts.
                "active-power": {"name": "Active Power", "datatype": "float", "unit": "kW"},
                "relay": {
                    "name": "Relay",
                    "datatype": "enum",
                    "format": "UNKNOWN,OPEN,CLOSED",
                    "settable": True,
                },
                "shed-priority": {
                    "name": "Shed Priority",
                    "datatype": "enum",
                    "format": "UNKNOWN,OFF_GRID,SOC_THRESHOLD,NEVER",
                    "settable": True,
                },
            },
        },
    },
}


def test_classify_node_static_and_circuit():
    assert classify_node("core") is NodeKind.CORE
    assert classify_node("lugs-upstream") is NodeKind.LUGS
    assert classify_node("power-flows") is NodeKind.POWER_FLOWS
    assert classify_node(CIRCUIT_UUID) is NodeKind.CIRCUIT


def test_classify_node_unknown():
    assert classify_node("something-else") is NodeKind.UNKNOWN


def test_circuit_active_power_unit_corrected_to_watts():
    """The schema says kW; the panel actually sends watts."""
    assert effective_unit(NodeKind.CIRCUIT, "active-power", "kW") == "W"
    assert effective_unit(NodeKind.PV, "nameplate-capacity", "kW") == "W"


def test_lugs_active_power_unit_left_alone():
    """Only the two documented misdeclarations are overridden."""
    assert effective_unit(NodeKind.LUGS, "active-power", "W") == "W"
    assert effective_unit(NodeKind.CORE, "l1-voltage", "V") == "V"


def test_parse_description_counts():
    schema = parse_description(SERIAL, DESCRIPTION)
    assert schema.serial == SERIAL
    assert len(schema.properties) == 11
    assert len(schema.circuits()) == 1


def test_parse_description_applies_unit_override():
    schema = parse_description(SERIAL, DESCRIPTION)
    circuit_power = schema.properties[f"{CIRCUIT_UUID}/active-power"]
    assert circuit_power.unit == "W"
    assert circuit_power.node_kind is NodeKind.CIRCUIT


def test_parse_description_enum_format():
    schema = parse_description(SERIAL, DESCRIPTION)
    relay = schema.properties[f"{CIRCUIT_UUID}/relay"]
    assert relay.datatype is DataType.ENUM
    assert relay.enum_values == ("UNKNOWN", "OPEN", "CLOSED")
    assert relay.settable is True


def test_parse_description_settable_flags():
    schema = parse_description(SERIAL, DESCRIPTION)
    settable = {k for k, v in schema.properties.items() if v.settable}
    assert settable == {
        "core/dominant-power-source",
        f"{CIRCUIT_UUID}/relay",
        f"{CIRCUIT_UUID}/shed-priority",
    }


def test_resolve_node_names_labels_circuits():
    schema = parse_description(SERIAL, DESCRIPTION)
    resolved = schema.resolve_node_names({f"{CIRCUIT_UUID}/name": "Kitchen Outlets"})
    assert resolved.properties[f"{CIRCUIT_UUID}/relay"].node_name == "Kitchen Outlets"
    # Untouched when no name has been observed.
    assert schema.properties[f"{CIRCUIT_UUID}/relay"].node_name == "Circuit"


def test_topic_for_value_and_command():
    schema = parse_description(SERIAL, DESCRIPTION)
    relay = schema.properties[f"{CIRCUIT_UUID}/relay"]
    assert topic_for(SERIAL, relay) == f"ebus/5/{SERIAL}/{CIRCUIT_UUID}/relay"
    assert topic_for(SERIAL, relay, command=True) == f"ebus/5/{SERIAL}/{CIRCUIT_UUID}/relay/set"


def test_parse_value_topic_extracts_key():
    assert parse_value_topic(f"ebus/5/{SERIAL}/core/l1-voltage") == (SERIAL, "core/l1-voltage")


def test_parse_value_topic_ignores_metadata_and_commands():
    assert parse_value_topic(f"ebus/5/{SERIAL}/$state") is None
    assert parse_value_topic(f"ebus/5/{SERIAL}/$description") is None
    # /set has six segments, so it is not a value topic.
    assert parse_value_topic(f"ebus/5/{SERIAL}/core/relay/set") is None
    assert parse_value_topic("unrelated/topic") is None
