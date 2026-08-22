"""Normalized data model, independent of which backend produced it.

Backends emit `Reading`s and a `PanelSchema`. Everything downstream (discovery,
publishing) works only against these types, so swapping the ebus backend for the
legacy gRPC one — or a cloud scraper — changes nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class DataType(StrEnum):
    """Homie 5 property datatypes."""

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    ENUM = "enum"
    COLOR = "color"
    DATETIME = "datetime"
    DURATION = "duration"
    JSON = "json"


class NodeKind(StrEnum):
    """Node types the panel exposes. `CIRCUIT` nodes are keyed by UUID."""

    CORE = "core"
    LUGS = "lugs"
    CIRCUIT = "circuit"
    POWER_FLOWS = "power-flows"
    PCS = "pcs"
    BESS = "bess"
    PV = "pv"
    EVSE = "evse"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PropertySpec:
    """One property on one node — the unit of translation to a HA entity."""

    node_id: str
    node_kind: NodeKind
    property_id: str
    name: str
    datatype: DataType
    unit: str | None = None
    settable: bool = False
    retained: bool = True
    enum_values: tuple[str, ...] = ()
    # Node display name, e.g. a circuit's user-assigned label. Resolved after
    # the schema is parsed, since a circuit's `name` arrives as a property.
    node_name: str | None = None

    @property
    def key(self) -> str:
        """Stable identifier, unique within a panel."""
        return f"{self.node_id}/{self.property_id}"


@dataclass(frozen=True)
class EnergySample:
    """Energy the panel metered over one interval, in kWh.

    `key` is the power property this is the energy for (`circuit-36/power`,
    `site/grid`), so the two travel together; `[start_ms, end_ms)` is the
    interval SPAN aggregated over. The most recent interval is normally still
    open, so the same bounds can come back with a larger `kwh` — see
    `energy.EnergyLedger`, which is what turns these into a running total.
    """

    key: str
    start_ms: int
    end_ms: int
    kwh: float


@dataclass(frozen=True)
class Reading:
    """A single property value observed at a point in time."""

    key: str
    value: str
    timestamp: float


@dataclass
class PanelSchema:
    """Everything we know about one panel's structure."""

    serial: str
    homie_version: str = "5"
    properties: dict[str, PropertySpec] = field(default_factory=dict)
    # Firmware/hardware detail lifted from the `core` node once it reports.
    software_version: str | None = None
    hardware_version: str | None = None

    def add(self, spec: PropertySpec) -> None:
        self.properties[spec.key] = spec

    def circuits(self) -> dict[str, list[PropertySpec]]:
        """Group circuit properties by circuit UUID."""
        out: dict[str, list[PropertySpec]] = {}
        for spec in self.properties.values():
            if spec.node_kind is NodeKind.CIRCUIT:
                out.setdefault(spec.node_id, []).append(spec)
        return out

    def resolve_node_names(self, readings: dict[str, str]) -> PanelSchema:
        """Return a copy with `node_name` filled in from observed `name` values.

        Circuit nodes are keyed by opaque UUID; their human label arrives as the
        `name` property. Entity naming is much better once that has landed, so
        the bridge re-runs this whenever a `name` property changes.
        """
        from dataclasses import replace

        names = {
            spec.node_id: readings[spec.key]
            for spec in self.properties.values()
            if spec.property_id == "name" and spec.key in readings
        }
        if not names:
            return self

        resolved = PanelSchema(
            serial=self.serial,
            homie_version=self.homie_version,
            software_version=self.software_version,
            hardware_version=self.hardware_version,
        )
        for key, spec in self.properties.items():
            node_name = names.get(spec.node_id, spec.node_name)
            resolved.properties[key] = replace(spec, node_name=node_name)
        return resolved
