"""Homie 5 `$description` parsing, plus compensation for SPAN firmware quirks.

The panel publishes a full JSON schema on `ebus/5/<serial>/$description`. That
document is the authority on what exists; we translate it into `PropertySpec`s.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import DataType, NodeKind, PanelSchema, PropertySpec

log = logging.getLogger(__name__)

# Node IDs are fixed for singleton nodes; anything else is a circuit UUID.
_STATIC_NODES: dict[str, NodeKind] = {
    "core": NodeKind.CORE,
    "lugs-upstream": NodeKind.LUGS,
    "lugs-downstream": NodeKind.LUGS,
    "power-flows": NodeKind.POWER_FLOWS,
    "pcs": NodeKind.PCS,
    "bess": NodeKind.BESS,
    "pv": NodeKind.PV,
    "evse": NodeKind.EVSE,
}

# Properties the firmware declares as kW but actually reports in watts.
# Documented in SPAN-API-Client-Docs/docs/public/mqtt-topic-reference.md and
# confirmed against this panel. Keyed by (node kind, property id).
_UNIT_OVERRIDES: dict[tuple[NodeKind, str], str] = {
    (NodeKind.CIRCUIT, "active-power"): "W",
    (NodeKind.PV, "nameplate-capacity"): "W",
}


def classify_node(node_id: str) -> NodeKind:
    """Map a Homie node id to a node kind. Unknown ids are circuit UUIDs."""
    if node_id in _STATIC_NODES:
        return _STATIC_NODES[node_id]
    # Circuit nodes are 32-char hex UUIDs without dashes.
    stripped = node_id.replace("-", "")
    if len(stripped) == 32 and all(c in "0123456789abcdefABCDEF" for c in stripped):
        return NodeKind.CIRCUIT
    log.warning("unrecognized node id %r; treating as unknown", node_id)
    return NodeKind.UNKNOWN


def effective_unit(kind: NodeKind, property_id: str, declared: str | None) -> str | None:
    """Return the unit to trust, correcting known firmware misdeclarations."""
    override = _UNIT_OVERRIDES.get((kind, property_id))
    if override and declared != override:
        log.debug(
            "unit override: %s/%s declared %r, using %r",
            kind.value,
            property_id,
            declared,
            override,
        )
        return override
    return declared


def parse_description(serial: str, doc: dict[str, Any]) -> PanelSchema:
    """Parse a Homie 5 `$description` document into a `PanelSchema`.

    The Homie 5 shape is::

        {"homie": "5.0", "name": ..., "nodes": {
            "<node-id>": {"name": ..., "properties": {
                "<prop-id>": {"name":..., "datatype":..., "unit":...,
                              "settable":..., "format": "A,B,C"}}}}}
    """
    schema = PanelSchema(serial=serial, homie_version=str(doc.get("homie", "5")))

    nodes = doc.get("nodes") or {}
    if not nodes:
        log.warning("$description for %s contained no nodes", serial)

    for node_id, node in nodes.items():
        kind = classify_node(node_id)
        node_name = node.get("name")

        for prop_id, prop in (node.get("properties") or {}).items():
            try:
                datatype = DataType(str(prop.get("datatype", "string")).lower())
            except ValueError:
                log.warning(
                    "unknown datatype %r on %s/%s; treating as string",
                    prop.get("datatype"),
                    node_id,
                    prop_id,
                )
                datatype = DataType.STRING

            enum_values: tuple[str, ...] = ()
            if datatype is DataType.ENUM and prop.get("format"):
                enum_values = tuple(v.strip() for v in str(prop["format"]).split(",") if v.strip())

            schema.add(
                PropertySpec(
                    node_id=node_id,
                    node_kind=kind,
                    property_id=prop_id,
                    name=prop.get("name") or prop_id,
                    datatype=datatype,
                    unit=effective_unit(kind, prop_id, prop.get("unit")),
                    settable=bool(prop.get("settable", False)),
                    retained=bool(prop.get("retained", True)),
                    enum_values=enum_values,
                    node_name=node_name,
                )
            )

    log.info(
        "parsed schema for %s: %d properties across %d nodes",
        serial,
        len(schema.properties),
        len(nodes),
    )
    return schema


def topic_for(serial: str, spec: PropertySpec, *, command: bool = False) -> str:
    """Build the panel-side Homie topic for a property."""
    base = f"ebus/5/{serial}/{spec.node_id}/{spec.property_id}"
    return f"{base}/set" if command else base


def parse_value_topic(topic: str) -> tuple[str, str] | None:
    """Extract `(serial, property-key)` from a Homie value topic.

    Returns None for topics that are not property values (`$state`,
    `$description`, `/set` commands).
    """
    parts = topic.split("/")
    # ebus / 5 / <serial> / <node> / <prop>
    if len(parts) != 5 or parts[0] != "ebus":
        return None
    _, _, serial, node_id, prop_id = parts
    if node_id.startswith("$") or prop_id.startswith("$"):
        return None
    return serial, f"{node_id}/{prop_id}"
