"""Translate `PropertySpec`s into Home Assistant MQTT Discovery payloads.

Topic layout we publish on the *HA* broker:

    span-bridge/<serial>/status                    availability (LWT)
    span-bridge/<serial>/<node>/<property>         state
    span-bridge/<serial>/<node>/<property>/set     command (settable only)

Discovery configs go to `<prefix>/<component>/<node_id>/<object_id>/config`.
"""

from __future__ import annotations

import re
from typing import Any

from .models import DataType, NodeKind, PanelSchema, PropertySpec

STATE_ROOT = "span-bridge"

# Unit → (device_class, state_class). Energy counters are total_increasing so
# HA handles counter resets across firmware updates without spurious spikes.
_UNIT_MAP: dict[str, tuple[str | None, str | None]] = {
    "W": ("power", "measurement"),
    "kW": ("power", "measurement"),
    "Wh": ("energy", "total_increasing"),
    "kWh": ("energy", "total_increasing"),
    "V": ("voltage", "measurement"),
    "A": ("current", "measurement"),
    "%": (None, "measurement"),
    "Hz": ("frequency", "measurement"),
}

# Settable enums that deserve a richer control than a switch.
_SELECT_PROPERTIES = {"shed-priority", "dominant-power-source"}

# Properties that describe the device rather than its state. Surfacing these as
# entities clutters the UI; they land in device attributes instead.
_DIAGNOSTIC_PROPERTIES = {
    "vendor-name",
    "product-name",
    "part-number",
    "serial-number",
    "hardware-version",
    "software-version",
    "model",
    "postal-code",
    "time-zone",
    "wifi-ssid",
}


def slug(value: str) -> str:
    """Lowercase, underscore-separated identifier safe for MQTT and HA."""
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")


def state_topic(serial: str, spec: PropertySpec) -> str:
    return f"{STATE_ROOT}/{serial}/{spec.node_id}/{spec.property_id}"


def command_topic(serial: str, spec: PropertySpec) -> str:
    return f"{state_topic(serial, spec)}/set"


def availability_topic(serial: str) -> str:
    return f"{STATE_ROOT}/{serial}/status"


def unique_id(serial: str, spec: PropertySpec) -> str:
    return f"span_{slug(serial)}_{slug(spec.node_id)}_{slug(spec.property_id)}"


def component_for(spec: PropertySpec) -> str:
    """Pick the HA component (entity domain) for a property."""
    if spec.settable:
        if spec.property_id in _SELECT_PROPERTIES:
            return "select"
        if spec.property_id == "relay":
            return "switch"
        if spec.datatype is DataType.BOOLEAN:
            return "switch"
        if spec.datatype is DataType.ENUM:
            return "select"
        return "number" if spec.datatype in (DataType.FLOAT, DataType.INTEGER) else "text"

    if spec.datatype is DataType.BOOLEAN:
        return "binary_sensor"
    return "sensor"


def _device_for(serial: str, spec: PropertySpec, panel_device: dict) -> dict:
    """Circuits become their own HA devices, linked to the panel via `via_device`.

    A 40-space panel produces hundreds of entities; grouping each circuit as a
    device keeps the UI navigable and makes per-circuit dashboards trivial.
    """
    if spec.node_kind is not NodeKind.CIRCUIT:
        return panel_device

    label = spec.node_name or f"Circuit {spec.node_id[:8]}"
    return {
        "identifiers": [f"span_{slug(serial)}_{slug(spec.node_id)}"],
        "name": label,
        "manufacturer": "SPAN.io",
        "model": "Circuit",
        "via_device": f"span_{slug(serial)}",
    }


def panel_device(schema: PanelSchema) -> dict:
    return {
        "identifiers": [f"span_{slug(schema.serial)}"],
        "name": f"SPAN Panel {schema.serial.upper()}",
        "manufacturer": "SPAN.io",
        "model": schema.hardware_version or "MAIN 40",
        "sw_version": schema.software_version,
        "serial_number": schema.serial,
    }


def entity_name(spec: PropertySpec) -> str:
    """Human-readable entity name.

    HA prefixes the device name automatically, so a circuit's power entity reads
    as "Kitchen Outlets Active Power" without repeating the circuit name here.
    """
    base = spec.name or spec.property_id
    pretty = base.replace("-", " ").replace("_", " ").strip()
    pretty = pretty[:1].upper() + pretty[1:] if pretty else spec.property_id

    if spec.node_kind in (NodeKind.CIRCUIT, NodeKind.CORE):
        return pretty
    # Singleton nodes share a device, so disambiguate: "Lugs Upstream Active Power".
    node_label = (spec.node_name or spec.node_id).replace("-", " ").title()
    return f"{node_label} {pretty}"


def discovery_payload(schema: PanelSchema, spec: PropertySpec) -> dict[str, Any]:
    """Build the HA MQTT Discovery config for one property."""
    serial = schema.serial
    component = component_for(spec)

    payload: dict[str, Any] = {
        "name": entity_name(spec),
        "unique_id": unique_id(serial, spec),
        "object_id": unique_id(serial, spec),
        "state_topic": state_topic(serial, spec),
        "availability_topic": availability_topic(serial),
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": _device_for(serial, spec, panel_device(schema)),
    }

    if spec.property_id in _DIAGNOSTIC_PROPERTIES:
        payload["entity_category"] = "diagnostic"

    if spec.unit:
        device_class, state_class = _UNIT_MAP.get(spec.unit, (None, None))
        payload["unit_of_measurement"] = spec.unit
        if device_class:
            payload["device_class"] = device_class
        if state_class and component == "sensor":
            payload["state_class"] = state_class

    # State of charge is a percentage but semantically a battery level.
    if spec.property_id == "soc" and spec.node_kind is NodeKind.BESS:
        payload["device_class"] = "battery"

    if component == "binary_sensor":
        payload["payload_on"] = "true"
        payload["payload_off"] = "false"

    elif component == "switch":
        payload["command_topic"] = command_topic(serial, spec)
        if spec.property_id == "relay":
            # SPAN semantics: CLOSED = energized. An "on" switch is a closed relay.
            payload["payload_on"] = "CLOSED"
            payload["payload_off"] = "OPEN"
            payload["state_on"] = "CLOSED"
            payload["state_off"] = "OPEN"
            payload["device_class"] = "switch"
        else:
            payload["payload_on"] = "true"
            payload["payload_off"] = "false"

    elif component == "select":
        payload["command_topic"] = command_topic(serial, spec)
        # UNKNOWN is a reported state, never a valid command.
        payload["options"] = [v for v in spec.enum_values if v != "UNKNOWN"]

    elif component == "sensor" and spec.datatype is DataType.ENUM:
        payload["device_class"] = "enum"
        payload["options"] = list(spec.enum_values)

    return payload


def discovery_topic(prefix: str, schema: PanelSchema, spec: PropertySpec) -> str:
    component = component_for(spec)
    node = f"span_{slug(schema.serial)}"
    return f"{prefix}/{component}/{node}/{unique_id(schema.serial, spec)}/config"


def build_all(prefix: str, schema: PanelSchema) -> list[tuple[str, dict[str, Any]]]:
    """Every (discovery_topic, payload) pair for a panel."""
    return [
        (discovery_topic(prefix, schema, spec), discovery_payload(schema, spec))
        for spec in schema.properties.values()
    ]
