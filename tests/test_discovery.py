"""Tests for the Homie → Home Assistant MQTT Discovery mapping."""

from span_bridge import discovery
from span_bridge.homie import parse_description

from .test_homie import CIRCUIT_UUID, DESCRIPTION, SERIAL


def build():
    schema = parse_description(SERIAL, DESCRIPTION)
    return schema.resolve_node_names({f"{CIRCUIT_UUID}/name": "Kitchen Outlets"})


def spec_for(schema, key):
    return schema.properties[key]


def test_component_selection():
    schema = build()
    assert discovery.component_for(spec_for(schema, "core/l1-voltage")) == "sensor"
    assert discovery.component_for(spec_for(schema, "core/grid-islandable")) == "binary_sensor"
    assert discovery.component_for(spec_for(schema, "core/door")) == "sensor"
    # Settable enums: relay is a switch, the rest are selects.
    assert discovery.component_for(spec_for(schema, f"{CIRCUIT_UUID}/relay")) == "switch"
    assert discovery.component_for(spec_for(schema, f"{CIRCUIT_UUID}/shed-priority")) == "select"
    assert discovery.component_for(spec_for(schema, "core/dominant-power-source")) == "select"


def test_power_sensor_gets_device_and_state_class():
    schema = build()
    payload = discovery.discovery_payload(schema, spec_for(schema, "lugs-upstream/active-power"))
    assert payload["device_class"] == "power"
    assert payload["state_class"] == "measurement"
    assert payload["unit_of_measurement"] == "W"


def test_energy_counter_is_total_increasing():
    schema = build()
    payload = discovery.discovery_payload(
        schema, spec_for(schema, "lugs-upstream/imported-energy")
    )
    assert payload["device_class"] == "energy"
    assert payload["state_class"] == "total_increasing"


def test_circuit_power_uses_corrected_watt_unit():
    schema = build()
    payload = discovery.discovery_payload(
        schema, spec_for(schema, f"{CIRCUIT_UUID}/active-power")
    )
    assert payload["unit_of_measurement"] == "W"
    assert payload["device_class"] == "power"


def test_relay_switch_maps_closed_to_on():
    """SPAN semantics: a CLOSED relay is an energized circuit."""
    schema = build()
    payload = discovery.discovery_payload(schema, spec_for(schema, f"{CIRCUIT_UUID}/relay"))
    assert payload["payload_on"] == "CLOSED"
    assert payload["payload_off"] == "OPEN"
    assert payload["state_on"] == "CLOSED"
    assert payload["state_off"] == "OPEN"
    assert payload["command_topic"].endswith("/set")


def test_select_omits_unknown_from_options():
    """UNKNOWN is a reported state but never a valid command."""
    schema = build()
    payload = discovery.discovery_payload(
        schema, spec_for(schema, f"{CIRCUIT_UUID}/shed-priority")
    )
    assert "UNKNOWN" not in payload["options"]
    assert set(payload["options"]) == {"OFF_GRID", "SOC_THRESHOLD", "NEVER"}


def test_read_only_enum_becomes_enum_sensor():
    schema = build()
    payload = discovery.discovery_payload(schema, spec_for(schema, "core/door"))
    assert payload["device_class"] == "enum"
    assert payload["options"] == ["UNKNOWN", "OPEN", "CLOSED"]
    assert "command_topic" not in payload


def test_circuit_is_its_own_device_linked_to_panel():
    schema = build()
    payload = discovery.discovery_payload(schema, spec_for(schema, f"{CIRCUIT_UUID}/relay"))
    device = payload["device"]
    assert device["name"] == "Kitchen Outlets"
    assert device["via_device"] == f"span_{discovery.slug(SERIAL)}"


def test_core_properties_attach_to_panel_device():
    schema = build()
    payload = discovery.discovery_payload(schema, spec_for(schema, "core/l1-voltage"))
    assert payload["device"]["identifiers"] == [f"span_{discovery.slug(SERIAL)}"]


def test_version_properties_are_diagnostic():
    schema = build()
    payload = discovery.discovery_payload(schema, spec_for(schema, "core/software-version"))
    assert payload["entity_category"] == "diagnostic"


def test_unique_ids_are_unique():
    schema = build()
    ids = [discovery.unique_id(SERIAL, s) for s in schema.properties.values()]
    assert len(ids) == len(set(ids))


def test_singleton_node_names_are_disambiguated():
    """Two lug nodes share the panel device, so names must not collide."""
    schema = build()
    name = discovery.entity_name(spec_for(schema, "lugs-upstream/active-power"))
    assert "Upstream" in name


def test_build_all_covers_every_property():
    schema = build()
    configs = discovery.build_all("homeassistant", schema)
    assert len(configs) == len(schema.properties)
    for topic, payload in configs:
        assert topic.startswith("homeassistant/")
        assert topic.endswith("/config")
        assert payload["availability_topic"] == discovery.availability_topic(SERIAL)
