"""What an r202639 panel's Homie tree means, free of Home Assistant.

`build_snapshot` turns the device tree into frozen facts: the panel, its lugs
and its circuits, with signs normalised and derived values worked out.
`presence` says what an entity shows for a given transport, and `ROWS` lists
every local entity: where its value comes from, when it exists and its unique
id. The Home Assistant side only adds units, names and device classes.

Grounded in the redacted capture under tests/fixtures/r202639. Anything the
capture does not carry (solar circuits, remote CTs, PV and battery flows) is
modelled from the r202639 spec and covered by synthetic tests.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum, StrEnum, auto
from typing import Any

from . import homie
from .health import Transport
from .homie import DEVICE_TYPE_PREFIX, DeviceDescription, DeviceState, DeviceTree

log = logging.getLogger(__name__)

ROOT_CLASS = "distribution-enclosure"
CIRCUIT_CLASS = "circuit"
LUGS_CLASS = "lugs"
UP_STATES: frozenset[DeviceState] = frozenset({DeviceState.READY, DeviceState.ALERT})

# The panel publishes power-flows/grid negative while importing (the lugs show
# the same moment as positive), so it is flipped to + = import, as HA's energy
# dashboard expects. Confirm against the live panel before release: changing a
# sign later corrupts long-term statistics.
GRID_FLOW_SIGN = -1

DERIVED = "derived"  # capability for values we compute; never a Homie capability
STATE_KEY = "$state"  # Homie-reserved, so it cannot collide with a property key
NET_ENERGY = f"{DERIVED}/net-energy"

# The panel's own properties that identify the household rather than describe
# its electricity: read by nobody, kept out of facts, logs and the Store.
# Circuit names are the user's labels, not PII, and are kept.
PII_KEYS: frozenset[str] = frozenset(
    {
        "info/name",
        "info/address-lines",
        "info/latitude",
        "info/longitude",
        "info/locality",
        "info/region",
        "info/serial-number",
        "info/utility-meter-serial-number",
        "info/country-code",
        "status/postal-code",
        "status/time-zone",
        "status/wifi-ssid",
    }
)

# Identifying on any device (a battery or charger may report its own serial);
# only the panel's `info/name` is the site's name, so names elsewhere are kept.
IDENTIFYING_KEYS: frozenset[str] = PII_KEYS - {"info/name"}


class _NoFallback:
    """What the cloud fallback returns for a value it cannot supply (always, in 4a)."""

    def __repr__(self) -> str:
        return "NO_FALLBACK"


NO_FALLBACK: Any = _NoFallback()


class Role(StrEnum):
    LOAD = "load"  # no feeds-role, or LOADS / SUBPANEL / MIXED / UNUSED
    SOLAR = "solar"
    STORAGE = "storage"  # signed as a load until the battery device exists
    GENERATOR = "generator"  # signed as solar: it produces


_PRODUCERS = frozenset({Role.SOLAR, Role.GENERATOR})


# --- facts ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PowerFlows:
    grid: float | None  # W, + = import (GRID_FLOW_SIGN applied)
    site: float | None
    pv: float | None
    battery: float | None
    published: frozenset[str]  # property ids carrying a value, e.g. {"grid", "site"}


@dataclass(frozen=True, slots=True)
class PcsFacts:
    import_limit: float | None
    binding_constraint: str | None
    attributes: Mapping[str, Any]  # every other published pcs/* value, by property id


@dataclass(frozen=True, slots=True)
class ShedForecast:
    """The panel's forecast of when load shedding starts, in minutes."""

    time_to_priority_shed: int | None
    total_time_remaining: int | None
    full_charge_time_to_priority_shed: int | None
    full_charge_total_time_remaining: int | None
    confidence: str | None  # LOW / MEDIUM / HIGH as published


@dataclass(frozen=True, slots=True)
class ShedPolicy:
    """The battery thresholds of a `soc-priority.v1` shed policy, in percent."""

    soc_threshold_shed: float
    soc_threshold_release: float


@dataclass(frozen=True, slots=True)
class Link:
    """One end of a `connection/feeds-device-*` or `fed-by-device-*` link.

    On a circuit or lugs device it names the other end: `via_kind` is that
    device's class (`bess`, `pv`, ...; from its `$description` when the tree has
    it, else the published type) and `via_id` its Homie id. Never shown in HA.
    """

    via_kind: str | None
    via_id: str
    status: str | None  # OK / LOST / DEGRADED, or None while unpublished


@dataclass(frozen=True, slots=True)
class PanelFacts:
    device_id: str
    state: DeviceState | None
    described: frozenset[str]  # property keys in the $description, minus PII
    model: str | None
    firmware: str | None
    hardware: str | None
    data_model_version: str | None
    vendor: str | None
    main_relay: str | None
    cloud_connection: str | None
    voltage_a: float | None
    voltage_b: float | None
    busbar_current: float | None
    frequency: float | None
    has_main_breaker: bool
    main_breaker_rating: int | None
    pcs: PcsFacts | None
    flows: PowerFlows
    door: str | None  # UNKNOWN / OPEN / CLOSED as published
    ethernet: bool | None
    wifi: bool | None
    shed_forecast: ShedForecast | None  # None unless the capability is described
    shed_policy: ShedPolicy | None  # None unless published and valid


@dataclass(frozen=True, slots=True)
class LugsFacts:
    device_id: str
    state: DeviceState  # effective: a root that is not ready overrides it
    described: frozenset[str]
    direction: str | None
    active_power: float | None  # W, raw: + = import from the utility
    current_a: float | None
    current_b: float | None
    imported_wh: float | None
    exported_wh: float | None
    service_rating: int | None
    overcurrent_protection: int | None
    feeds_role: str | None
    backed_up: str | None
    unit_count: int | None
    location: str | None  # the user's room label, never logged
    feeds: Link | None
    fed_by: Link | None

    @property
    def net_wh(self) -> float | None:
        if self.imported_wh is None or self.exported_wh is None:
            return None
        return _clean(self.imported_wh - self.exported_wh)


@dataclass(frozen=True, slots=True)
class CircuitFacts:
    device_id: str
    state: DeviceState
    described: frozenset[str]
    name: str | None
    spaces: tuple[int, ...] | None
    dedicated: bool | None
    nominal_voltage: float | None
    tags: tuple[str, ...]
    poles: int | None
    rating: int | None
    protection_functions: str | None
    raw_power: float | None  # as published: negative while a load draws
    power: float | None  # + = consuming for a load, + = producing for solar
    current: float | None
    imported_wh: float | None
    exported_wh: float | None
    consumed_wh: float | None  # = exported: from the bus out to the circuit
    produced_wh: float | None  # = imported
    net_wh: float | None
    relay: str | None
    relay_settable: bool
    relay_requester: str | None
    relay_controllable: bool | None
    always_on: bool | None
    priority: str | None
    priority_settable: bool
    priority_options: tuple[str, ...]
    sheddable: bool | None
    pcs_managed: bool | None
    pcs_priority: int | None
    role: Role
    is_remote_ct: bool
    shared_meter_with: tuple[str, ...] | None
    shared_switch_with: tuple[str, ...] | None
    backed_up: str | None
    unit_count: int | None
    location: str | None  # the user's room label, never logged
    feeds: Link | None
    fed_by: Link | None

    @property
    def has_switch(self) -> bool:
        return self.relay_controllable is True and self.relay_settable

    @property
    def has_priority_select(self) -> bool:
        return self.priority_settable


@dataclass(frozen=True, slots=True)
class Unmapped:
    device_class: str
    key: str | None  # None: the whole device class is not modelled


@dataclass(frozen=True, slots=True)
class Snapshot:
    root_id: str
    root_state: DeviceState | None
    panel: PanelFacts | None  # None until the root's $description arrives
    lugs: Mapping[str, LugsFacts]
    circuits: Mapping[str, CircuitFacts]  # ordered by first space, then name
    unmapped: frozenset[Unmapped]
    restored: bool  # every value came from the Store; nothing live yet

    @property
    def upstream(self) -> LugsFacts | None:
        return self._lugs("UPSTREAM")

    @property
    def downstream(self) -> LugsFacts | None:
        return self._lugs("DOWNSTREAM")

    def _lugs(self, direction: str) -> LugsFacts | None:
        found = [lugs for lugs in self.lugs.values() if lugs.direction == direction]
        return found[0] if len(found) == 1 else None


EMPTY = Snapshot("", None, None, {}, {}, frozenset(), restored=False)


# --- building -------------------------------------------------------------------------

_ROOT_READS = frozenset(
    {
        "info/model",
        "info/firmware-version",
        "info/hardware-version",
        "info/data-model-version",
        "info/vendor-name",
        "status/relay",
        "status/cloud-connection",
        "meter/voltage-a",
        "meter/voltage-b",
        "meter/busbar-current",
        "meter/frequency",
        "breaker/rating",
        "door/state",
        "status/ethernet",
        "status/wifi",
        "shed/policy",
    }
)
# `shed/asserted-islanding-state` stays unmapped until the islanding entity
# that carries it exists (4b-2).
_ROOT_READ_CAPABILITIES = frozenset({"pcs", "power-flows", "shed-forecast"})
_CONNECTION_READS = frozenset(
    {
        "connection/backed-up",
        "connection/count",
        "connection/feeds-device-id",
        "connection/feeds-device-type",
        "connection/feeds-device-status",
        "connection/fed-by-device-id",
        "connection/fed-by-device-type",
        "connection/fed-by-device-status",
        "info/locations",
    }
)
_LUGS_READS = (
    frozenset(
        {
            "info/direction",
            "meter/active-power",
            "meter/current-a",
            "meter/current-b",
            "meter/imported-energy",
            "meter/exported-energy",
            "connection/service-rating",
            "connection/overcurrent-protection",
            "connection/feeds-role",
        }
    )
    | _CONNECTION_READS
)
_CIRCUIT_READS = (
    frozenset(
        {
            "info/name",
            "info/spaces",
            "info/dedicated",
            "info/nominal-voltage",
            "info/tags",
            "breaker/poles",
            "breaker/rating",
            "breaker/protection-functions",
            "meter/active-power",
            "meter/current",
            "meter/imported-energy",
            "meter/exported-energy",
            "meter/shared-with-device-ids",
            "switch/relay",
            "switch/relay-requester",
            "switch/relay-controllable",
            "switch/shared-with-device-ids",
            "load-shed/priority",
            "pcs/managed",
            "pcs/priority",
            "connection/feeds-role",
        }
    )
    | _CONNECTION_READS
)


def build_snapshot(tree: DeviceTree, root_id: str, *, restored: bool = False) -> Snapshot:
    """The facts for `root_id` and everything under it. Pure.

    Devices are classified by their description's type, never by id; one whose
    `$description` has not arrived is skipped.
    """
    panel: PanelFacts | None = None
    lugs: dict[str, LugsFacts] = {}
    circuits: list[CircuitFacts] = []
    unmapped: set[Unmapped] = set()
    for description in tree.walk(root_id):
        cls = description.device_class
        if description.device_id == root_id and cls == ROOT_CLASS:
            panel = _panel(tree, description)
            reads = _ROOT_READS | {
                k for k in description.properties if k.split("/")[0] in _ROOT_READ_CAPABILITIES
            }
        elif description.device_id != root_id and cls == LUGS_CLASS:
            lugs[description.device_id] = _lugs(tree, description)
            reads = _LUGS_READS
        elif description.device_id != root_id and cls == CIRCUIT_CLASS:
            circuits.append(_circuit(tree, description))
            reads = _CIRCUIT_READS
        else:
            unmapped.add(Unmapped(cls or description.type, None))
            continue
        unmapped.update(Unmapped(cls, key) for key in _described(description) - reads)
    circuits.sort(key=_circuit_order)
    return Snapshot(
        root_id=root_id,
        root_state=tree.states.get(root_id),
        panel=panel,
        lugs=lugs,
        circuits={c.device_id: c for c in circuits},
        unmapped=frozenset(unmapped),
        restored=restored,
    )


def pii_keys(description: DeviceDescription | None) -> frozenset[str]:
    if description is None or description.device_class != ROOT_CLASS:
        return frozenset()
    return PII_KEYS


def _described(description: DeviceDescription) -> frozenset[str]:
    return frozenset(description.properties) - pii_keys(description)


def _panel(tree: DeviceTree, description: DeviceDescription) -> PanelFacts:
    device_id = description.device_id

    def get(key: str) -> Any:
        return tree.value(device_id, key)

    pcs = None
    if "pcs" in description.capabilities:
        pcs = PcsFacts(
            import_limit=_float(get("pcs/import-limit")),
            binding_constraint=_text(get("pcs/binding-constraint")),
            attributes={
                key.split("/", 1)[1]: value
                for key in description.properties
                if key.startswith("pcs/")
                and key not in ("pcs/import-limit", "pcs/binding-constraint")
                and (value := get(key)) is not None
            },
        )
    flows = {
        key.split("/", 1)[1]: _float(get(key))
        for key in description.properties
        if key.startswith("power-flows/")
    }
    grid = flows.get("grid")
    forecast = None
    if "shed-forecast" in description.capabilities:
        forecast = ShedForecast(
            time_to_priority_shed=_int(get("shed-forecast/time-to-priority-shed")),
            total_time_remaining=_int(get("shed-forecast/total-time-remaining")),
            full_charge_time_to_priority_shed=_int(
                get("shed-forecast/full-charge-time-to-priority-shed")
            ),
            full_charge_total_time_remaining=_int(
                get("shed-forecast/full-charge-total-time-remaining")
            ),
            confidence=_text(get("shed-forecast/confidence")),
        )
    return PanelFacts(
        device_id=device_id,
        state=tree.states.get(device_id),
        described=_described(description),
        model=_text(get("info/model")),
        firmware=_text(get("info/firmware-version")),
        hardware=_text(get("info/hardware-version")),
        data_model_version=_text(get("info/data-model-version")),
        vendor=_text(get("info/vendor-name")),
        main_relay=_text(get("status/relay")),
        cloud_connection=_text(get("status/cloud-connection")),
        voltage_a=_float(get("meter/voltage-a")),
        voltage_b=_float(get("meter/voltage-b")),
        busbar_current=_float(get("meter/busbar-current")),
        frequency=_float(get("meter/frequency")),
        has_main_breaker="breaker/rating" in description.properties,
        main_breaker_rating=_int(get("breaker/rating")),
        pcs=pcs,
        flows=PowerFlows(
            grid=None if grid is None else _clean(grid * GRID_FLOW_SIGN),
            site=flows.get("site"),
            pv=flows.get("pv"),
            battery=flows.get("battery"),
            published=frozenset(k for k, v in flows.items() if v is not None),
        ),
        door=_text(get("door/state")),
        ethernet=_bool(get("status/ethernet")),
        wifi=_bool(get("status/wifi")),
        shed_forecast=forecast,
        shed_policy=parse_shed_policy(get("shed/policy")),
    )


SHED_POLICY_ALGORITHM = "soc-priority.v1"
_policy_rejections_logged: set[str] = set()


def parse_shed_policy(value: Any) -> ShedPolicy | None:
    """The thresholds of a `soc-priority.v1` policy, or None for anything else.

    The schema names the policy in `algorithm`; `version` and `$schema` are
    accepted too. Both thresholds must be numbers from 0 to 100. A rejection is
    logged once per reason at debug, never with the payload.
    """
    if value is None:
        return None
    doc = value
    if isinstance(value, str | bytes):
        try:
            doc = json.loads(value)
        except ValueError:
            return _reject_policy("not JSON")
    if not isinstance(doc, dict):
        return _reject_policy("not an object")
    if SHED_POLICY_ALGORITHM not in (doc.get("algorithm"), doc.get("version"), doc.get("$schema")):
        return _reject_policy("not soc-priority.v1")
    params = doc.get("parameters", doc)
    if not isinstance(params, dict):
        return _reject_policy("no parameters")
    shed, release = params.get("soc-threshold-shed"), params.get("soc-threshold-release")
    if not (_percent(shed) and _percent(release)):
        return _reject_policy("thresholds out of range")
    return ShedPolicy(soc_threshold_shed=float(shed), soc_threshold_release=float(release))


def _percent(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and 0 <= value <= 100


def _reject_policy(reason: str) -> None:
    if reason not in _policy_rejections_logged:
        _policy_rejections_logged.add(reason)
        log.debug("Ignoring the panel's shed/policy: %s", reason)
    return None


def _lugs(tree: DeviceTree, description: DeviceDescription) -> LugsFacts:
    device_id = description.device_id

    def get(key: str) -> Any:
        return tree.value(device_id, key)

    return LugsFacts(
        device_id=device_id,
        state=tree.effective_state(device_id),
        described=_described(description),
        direction=_text(get("info/direction")),
        active_power=_float(get("meter/active-power")),
        current_a=_float(get("meter/current-a")),
        current_b=_float(get("meter/current-b")),
        imported_wh=_float(get("meter/imported-energy")),
        exported_wh=_float(get("meter/exported-energy")),
        service_rating=_int(get("connection/service-rating")),
        overcurrent_protection=_int(get("connection/overcurrent-protection")),
        feeds_role=_text(get("connection/feeds-role")),
        **_connection(tree, get),
    )


def _circuit(tree: DeviceTree, description: DeviceDescription) -> CircuitFacts:
    device_id = description.device_id
    props = description.properties

    def get(key: str) -> Any:
        return tree.value(device_id, key)

    raw_spaces = get("info/spaces")
    is_remote_ct = "switch/relay" not in props and ("info/spaces" not in props or raw_spaces == "")
    role = _role(_text(get("connection/feeds-role")))
    produces = role in _PRODUCERS and not is_remote_ct

    raw_power = _float(get("meter/active-power"))
    # A load draws as negative and a remote CT is already + = import, so only
    # the producers and the remote CT keep the panel's sign.
    if raw_power is None:
        power = None
    elif produces or is_remote_ct:
        power = _clean(raw_power)
    else:
        power = _clean(-raw_power)

    imported = _float(get("meter/imported-energy"))
    exported = _float(get("meter/exported-energy"))
    if imported is None or exported is None:
        net = None
    elif produces:
        net = _clean(imported - exported)
    else:
        net = _clean(exported - imported)

    controllable = _bool(get("switch/relay-controllable"))
    priority = _text(get("load-shed/priority"))
    priority_def = props.get("load-shed/priority")
    relay_def = props.get("switch/relay")
    if priority is None or priority == "UNKNOWN" or controllable is None:
        sheddable = None
    else:
        sheddable = controllable and priority != "NEVER"

    return CircuitFacts(
        device_id=device_id,
        state=tree.effective_state(device_id),
        described=_described(description),
        name=_text(get("info/name")),
        spaces=_spaces(raw_spaces),
        dedicated=_bool(get("info/dedicated")),
        nominal_voltage=_float(get("info/nominal-voltage")),
        tags=_split(get("info/tags")) or (),
        poles=_int(get("breaker/poles")),
        rating=_int(get("breaker/rating")),
        protection_functions=_text(get("breaker/protection-functions")),
        raw_power=raw_power,
        power=power,
        current=_float(get("meter/current")),
        imported_wh=imported,
        exported_wh=exported,
        consumed_wh=exported,
        produced_wh=imported,
        net_wh=net,
        relay=_text(get("switch/relay")),
        relay_settable=relay_def is not None and relay_def.settable,
        relay_requester=_text(get("switch/relay-requester")),
        relay_controllable=controllable,
        always_on=None if controllable is None else not controllable,
        priority=priority,
        priority_settable=priority_def is not None and priority_def.settable,
        priority_options=tuple(
            v for v in (priority_def.enum_values if priority_def else ()) if v != "UNKNOWN"
        ),
        sheddable=sheddable,
        pcs_managed=_bool(get("pcs/managed")),
        pcs_priority=_int(get("pcs/priority")),
        role=role,
        is_remote_ct=is_remote_ct,
        shared_meter_with=_split(get("meter/shared-with-device-ids")),
        shared_switch_with=_split(get("switch/shared-with-device-ids")),
        **_connection(tree, get),
    )


def _connection(tree: DeviceTree, get: Callable[[str], Any]) -> dict[str, Any]:
    """The `connection/*` and `info/locations` facts circuits and lugs share."""
    return {
        "backed_up": _text(get("connection/backed-up")),
        "unit_count": _int(get("connection/count")),
        "location": _location(get("info/locations")),
        "feeds": _link(tree, get, "feeds-device"),
        "fed_by": _link(tree, get, "fed-by-device"),
    }


def _link(tree: DeviceTree, get: Callable[[str], Any], prefix: str) -> Link | None:
    """The link a device publishes, or None until its id is published."""
    target = _text(get(f"connection/{prefix}-id"))
    if target is None:
        return None
    described = tree.descriptions.get(target)
    if described is not None and described.device_class:
        kind: str | None = described.device_class
    else:
        kind = _device_kind(_text(get(f"connection/{prefix}-type")))
    return Link(via_kind=kind, via_id=target, status=_text(get(f"connection/{prefix}-status")))


def _device_kind(published: str | None) -> str | None:
    """`energy.ebus.device.bess`, `bess` or `BESS` -> `bess`."""
    if published is None:
        return None
    kind = published.strip().lower()
    return kind.removeprefix(DEVICE_TYPE_PREFIX) or None


def _location(value: Any) -> str | None:
    """`info/locations` as one label: a string, or a list joined with commas."""
    if isinstance(value, str) and value.lstrip().startswith("["):
        with contextlib.suppress(ValueError):
            value = json.loads(value)
    if isinstance(value, list):
        parts = [part.strip() for part in value if isinstance(part, str) and part.strip()]
        return ", ".join(parts) or None
    return _text(value.strip() if isinstance(value, str) else value)


def circuit_label(circuit: CircuitFacts) -> str:
    """The name entities are prefixed with: the user's label, else the spaces."""
    if circuit.name:
        return circuit.name
    if circuit.spaces:
        return f"Circuit {'/'.join(str(space) for space in circuit.spaces)}"
    return "Circuit"


def _circuit_order(circuit: CircuitFacts) -> tuple[float, str, str]:
    first = circuit.spaces[0] if circuit.spaces else math.inf
    return (first, circuit.name or "", circuit.device_id)


def _role(feeds_role: str | None) -> Role:
    match feeds_role:
        case "SOLAR":
            return Role.SOLAR
        case "STORAGE":
            return Role.STORAGE
        case "GENERATOR":
            return Role.GENERATOR
    return Role.LOAD


def _spaces(value: Any) -> tuple[int, ...] | None:
    """`"9,11"` -> (9, 11). Anything that does not parse whole is None, never partial."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return tuple(int(part) for part in value.split(","))
    except ValueError:
        return None


def _split(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, str):
        return None
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _clean(value: float) -> float:
    """Drop the sign of a zero, so a negated idle reading is not shown as -0."""
    return value + 0.0


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return _clean(float(value))


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


# --- presence: what an entity shows, given the transport ------------------------------


class ValueKind(Enum):
    MEASUREMENT = auto()  # moves: power, current, voltage, flows, relay state, enums
    STATIC = auto()  # configuration: ratings, firmware
    ENERGY = auto()  # panel registers
    CONTROL = auto()  # switch, select


class Presence(Enum):
    VALUE = auto()  # available; shows the value, which may itself be None (unknown)
    UNKNOWN = auto()  # available, state unknown
    UNAVAILABLE = auto()


def presence(
    kind: ValueKind,
    *,
    shown: Transport,
    device_state: DeviceState | None,
    has_value: bool,
    fallback_has_value: bool = False,
) -> Presence:
    """What an entity shows. Energy holds its last register whatever the link
    does; a measurement never shows a stale or invented number.
    """
    if kind is ValueKind.ENERGY:
        return Presence.VALUE if has_value else Presence.UNAVAILABLE
    if shown is Transport.OFFLINE:
        return Presence.UNAVAILABLE
    up = shown is Transport.LOCAL and device_state in UP_STATES
    if kind is ValueKind.CONTROL:
        return Presence.VALUE if up and has_value else Presence.UNAVAILABLE
    if kind is ValueKind.STATIC:
        if up or has_value:
            return Presence.VALUE
        return Presence.UNAVAILABLE if shown is Transport.LOCAL else Presence.UNKNOWN
    if shown is Transport.LOCAL:
        return Presence.VALUE if up else Presence.UNAVAILABLE
    return Presence.VALUE if fallback_has_value else Presence.UNKNOWN


# --- identity -------------------------------------------------------------------------


def panel_uid(panel_key: str, key: str) -> str:
    return f"{panel_key}_{ROOT_CLASS}/{key}"


def lugs_uid(panel_key: str, lugs_id: str, key: str) -> str:
    return f"{panel_key}_{LUGS_CLASS}/{lugs_id}/{key}"


def circuit_uid(panel_key: str, circuit_id: str, key: str) -> str:
    return f"{panel_key}_{CIRCUIT_CLASS}/{circuit_id}/{key}"


# --- the entity catalogue -------------------------------------------------------------


class Scope(StrEnum):
    PANEL = "panel"
    UPSTREAM = "upstream"
    DOWNSTREAM = "downstream"
    CIRCUIT = "circuit"


type Facts = PanelFacts | LugsFacts | CircuitFacts


@dataclass(frozen=True, slots=True)
class Row:
    """One local entity, minus its presentation.

    `needs` must all be in the device's description for the entity to exist
    and stay available; `when` is any further condition for creating it (a
    value published, a control settable). `value` reads the facts.
    """

    key: str  # translation key for panel rows, a stable name for circuit rows
    platform: str  # "sensor", "binary_sensor", "switch" or "select"
    scope: Scope
    source: str  # the property key the unique id ends in
    kind: ValueKind
    value: Callable[[Any], Any]
    needs: tuple[str, ...] = ()
    when: Callable[[Any], bool] | None = None
    attrs: Callable[[Any], dict[str, Any]] | None = None
    ignores_state: bool = False  # the panel status shows the state itself
    extra: Mapping[str, Any] = field(default_factory=dict)


def _kwh(wh: float | None) -> float | None:
    return None if wh is None else wh / 1000


def _lower(value: str | None) -> str | None:
    return None if value is None else value.lower()


def _pcs_attrs(panel: PanelFacts) -> dict[str, Any]:
    if panel.pcs is None:
        return {}
    return {key.replace("-", "_"): value for key, value in panel.pcs.attributes.items()}


def _circuit_attrs(c: CircuitFacts) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "tabs": list(c.spaces) if c.spaces is not None else None,
        "voltage": c.nominal_voltage,
        "poles": c.poles,
        "always_on": c.always_on,
        "relay_state": c.relay,
        "relay_requester": c.relay_requester,
        "shed_priority": c.priority,
        "is_sheddable": c.sheddable,
        "pcs_managed": c.pcs_managed,
        "pcs_priority": c.pcs_priority,
        "protection_functions": (
            [c.protection_functions] if c.protection_functions is not None else None
        ),
        "dedicated": c.dedicated,
        "tags": list(c.tags) if c.tags else None,
        "role": c.role.value if c.role is not Role.LOAD else None,
        "shared_meter_with": list(c.shared_meter_with) if c.shared_meter_with else None,
        "shared_switch_with": list(c.shared_switch_with) if c.shared_switch_with else None,
        **_connection_attrs(c),
    }
    return {key: value for key, value in attrs.items() if value is not None}


def _connection_attrs(facts: LugsFacts | CircuitFacts) -> dict[str, Any]:
    """Backup, unit count and room, each only once published. Link ids stay hidden."""
    return _present(
        {
            "backed_up": _lower(facts.backed_up),
            "unit_count": facts.unit_count,
            "location": facts.location,
        }
    )


# Switching off the circuit that charges a battery or carries solar can cut
# backup power or fault the inverter, so it gets no relay switch (spec §6.4).
_NO_RELAY_TARGETS = frozenset({"bess", "pv"})


def feeds_storage_or_solar(c: CircuitFacts) -> bool:
    if c.role is Role.STORAGE:
        return True
    return c.feeds is not None and c.feeds.via_kind in _NO_RELAY_TARGETS


def _pcs_flag(name: str) -> Callable[[PanelFacts], bool | None]:
    return lambda p: _bool(p.pcs.attributes.get(name)) if p.pcs else None


def _door(p: PanelFacts) -> bool | None:
    return {"OPEN": True, "CLOSED": False}.get(p.door) if p.door else None


def _forecast(read: Callable[[ShedForecast], Any]) -> Callable[[PanelFacts], Any]:
    return lambda p: read(p.shed_forecast) if p.shed_forecast else None


def _shed_attrs(p: PanelFacts) -> dict[str, Any]:
    forecast, policy = p.shed_forecast, p.shed_policy
    return _present(
        {
            "full_charge_time_to_priority_shed": (
                forecast.full_charge_time_to_priority_shed if forecast else None
            ),
            "confidence": _lower(forecast.confidence) if forecast else None,
            "soc_threshold_shed": policy.soc_threshold_shed if policy else None,
            "soc_threshold_release": policy.soc_threshold_release if policy else None,
        }
    )


def _remaining_attrs(p: PanelFacts) -> dict[str, Any]:
    forecast = p.shed_forecast
    if forecast is None:
        return {}
    return _present(
        {
            "full_charge_total_time_remaining": forecast.full_charge_total_time_remaining,
            "confidence": _lower(forecast.confidence),
        }
    )


def _flow(name: str) -> Callable[[PanelFacts], bool]:
    return lambda p: name in p.flows.published


def _published(read: Callable[[Any], Any]) -> Callable[[Any], bool]:
    return lambda facts: read(facts) is not None


_M, _S, _E, _C = ValueKind.MEASUREMENT, ValueKind.STATIC, ValueKind.ENERGY, ValueKind.CONTROL
_IMPORTED, _EXPORTED = "meter/imported-energy", "meter/exported-energy"


def _panel_rows() -> tuple[Row, ...]:
    p = Scope.PANEL
    return (
        Row("panel_status", "sensor", p, STATE_KEY, _M, lambda f: _state(f.state),
            ignores_state=True),
        Row("grid_power", "sensor", p, "power-flows/grid", _M, lambda f: f.flows.grid,
            ("power-flows/grid",), _flow("grid")),
        Row("site_power", "sensor", p, "power-flows/site", _M, lambda f: f.flows.site,
            ("power-flows/site",), _flow("site")),
        Row("pv_power", "sensor", p, "power-flows/pv", _M, lambda f: f.flows.pv,
            ("power-flows/pv",), _flow("pv")),
        Row("battery_power", "sensor", p, "power-flows/battery", _M,
            lambda f: f.flows.battery, ("power-flows/battery",), _flow("battery")),
        Row("frequency", "sensor", p, "meter/frequency", _M, lambda f: f.frequency,
            ("meter/frequency",)),
        Row("voltage_a", "sensor", p, "meter/voltage-a", _M, lambda f: f.voltage_a,
            ("meter/voltage-a",)),
        Row("voltage_b", "sensor", p, "meter/voltage-b", _M, lambda f: f.voltage_b,
            ("meter/voltage-b",)),
        Row("busbar_current", "sensor", p, "meter/busbar-current", _M,
            lambda f: f.busbar_current, ("meter/busbar-current",)),
        Row("main_breaker_rating", "sensor", p, "breaker/rating", _S,
            lambda f: f.main_breaker_rating, ("breaker/rating",)),
        Row("main_relay", "sensor", p, "status/relay", _M, lambda f: _lower(f.main_relay),
            ("status/relay",), _published(lambda f: f.main_relay)),
        Row("vendor_cloud", "sensor", p, "status/cloud-connection", _M,
            lambda f: _lower(f.cloud_connection), ("status/cloud-connection",)),
        Row("software_version", "sensor", p, "info/firmware-version", _S,
            lambda f: f.firmware, ("info/firmware-version",)),
        Row("pcs_import_limit", "sensor", p, "pcs/import-limit", _M,
            lambda f: f.pcs.import_limit if f.pcs else None, ("pcs/import-limit",),
            attrs=_pcs_attrs),
        Row("pcs_binding_constraint", "sensor", p, "pcs/binding-constraint", _M,
            lambda f: _lower(f.pcs.binding_constraint) if f.pcs else None,
            ("pcs/binding-constraint",)),
        Row("door", "binary_sensor", p, "door/state", _M, _door, ("door/state",)),
        Row("ethernet", "binary_sensor", p, "status/ethernet", _M, lambda f: f.ethernet,
            ("status/ethernet",)),
        Row("wifi", "binary_sensor", p, "status/wifi", _M, lambda f: f.wifi,
            ("status/wifi",)),
        Row("pcs_active", "binary_sensor", p, "pcs/active", _M, _pcs_flag("active"),
            ("pcs/active",)),
        Row("pcs_enabled", "binary_sensor", p, "pcs/enabled", _S, _pcs_flag("enabled"),
            ("pcs/enabled",)),
        Row("time_to_priority_shed", "sensor", p, "shed-forecast/time-to-priority-shed", _M,
            _forecast(lambda s: s.time_to_priority_shed),
            ("shed-forecast/time-to-priority-shed",), attrs=_shed_attrs),
        Row("total_time_remaining", "sensor", p, "shed-forecast/total-time-remaining", _M,
            _forecast(lambda s: s.total_time_remaining),
            ("shed-forecast/total-time-remaining",), attrs=_remaining_attrs),
    )  # fmt: skip


def _lugs_rows() -> tuple[Row, ...]:
    up, down = Scope.UPSTREAM, Scope.DOWNSTREAM
    return (
        Row("current_power", "sensor", up, "meter/active-power", _M,
            lambda f: f.active_power, ("meter/active-power",), attrs=_connection_attrs),
        Row("main_consumed_energy", "sensor", up, _IMPORTED, _E,
            lambda f: _kwh(f.imported_wh), (_IMPORTED,)),
        Row("main_produced_energy", "sensor", up, _EXPORTED, _E,
            lambda f: _kwh(f.exported_wh), (_EXPORTED,)),
        Row("main_net_energy", "sensor", up, NET_ENERGY, _E, lambda f: _kwh(f.net_wh),
            (_IMPORTED, _EXPORTED)),
        Row("upstream_current_a", "sensor", up, "meter/current-a", _M,
            lambda f: f.current_a, ("meter/current-a",)),
        Row("upstream_current_b", "sensor", up, "meter/current-b", _M,
            lambda f: f.current_b, ("meter/current-b",)),
        Row("service_rating", "sensor", up, "connection/service-rating", _S,
            lambda f: f.service_rating, ("connection/service-rating",),
            _published(lambda f: f.service_rating)),
        Row("upstream_protection_rating", "sensor", up, "connection/overcurrent-protection",
            _S, lambda f: f.overcurrent_protection, ("connection/overcurrent-protection",),
            _published(lambda f: f.overcurrent_protection)),
        Row("feedthrough_power", "sensor", down, "meter/active-power", _M,
            lambda f: f.active_power, ("meter/active-power",),
            _published(lambda f: f.active_power), attrs=_connection_attrs),
        Row("feedthrough_imported_energy", "sensor", down, _IMPORTED, _E,
            lambda f: _kwh(f.imported_wh), (_IMPORTED,), _published(lambda f: f.imported_wh)),
        Row("feedthrough_exported_energy", "sensor", down, _EXPORTED, _E,
            lambda f: _kwh(f.exported_wh), (_EXPORTED,), _published(lambda f: f.exported_wh)),
        Row("feedthrough_current_a", "sensor", down, "meter/current-a", _M,
            lambda f: f.current_a, ("meter/current-a",)),
        Row("feedthrough_current_b", "sensor", down, "meter/current-b", _M,
            lambda f: f.current_b, ("meter/current-b",)),
    )  # fmt: skip


def _circuit_rows() -> tuple[Row, ...]:
    c = Scope.CIRCUIT
    return (
        Row("power", "sensor", c, "meter/active-power", _M, lambda f: f.power,
            ("meter/active-power",), attrs=_circuit_attrs),
        Row("current", "sensor", c, "meter/current", _M, lambda f: f.current,
            ("meter/current",)),
        Row("consumed_energy", "sensor", c, _EXPORTED, _E, lambda f: _kwh(f.consumed_wh),
            (_EXPORTED,)),
        Row("produced_energy", "sensor", c, _IMPORTED, _E, lambda f: _kwh(f.produced_wh),
            (_IMPORTED,)),
        Row("net_energy", "sensor", c, NET_ENERGY, _E, lambda f: _kwh(f.net_wh),
            (_IMPORTED, _EXPORTED)),
        Row("breaker_rating", "sensor", c, "breaker/rating", _S, lambda f: f.rating,
            ("breaker/rating",)),
        Row("breaker", "switch", c, "switch/relay", _C, lambda f: f.relay,
            ("switch/relay",), lambda f: f.has_switch and not feeds_storage_or_solar(f),
            attrs=lambda f: _present({"relay_requester": f.relay_requester})),
        Row("priority", "select", c, "load-shed/priority", _C, lambda f: f.priority,
            ("load-shed/priority",), lambda f: f.has_priority_select,
            attrs=lambda f: _present(
                {"shed_priority": f.priority, "is_sheddable": f.sheddable}
            )),
    )  # fmt: skip


def _state(state: DeviceState | None) -> str | None:
    return None if state is None else state.value


def _present(attrs: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in attrs.items() if value is not None}


ROWS: tuple[Row, ...] = _panel_rows() + _lugs_rows() + _circuit_rows()


def row_devices(snapshot: Snapshot, row: Row) -> list[str]:
    """The device ids a row applies to in this snapshot."""
    match row.scope:
        case Scope.PANEL:
            return [snapshot.panel.device_id] if snapshot.panel else []
        case Scope.UPSTREAM:
            return [snapshot.upstream.device_id] if snapshot.upstream else []
        case Scope.DOWNSTREAM:
            return [snapshot.downstream.device_id] if snapshot.downstream else []
    return list(snapshot.circuits)


def row_facts(snapshot: Snapshot, row: Row, device_id: str) -> Any:
    """The facts a row reads for `device_id`, or None if the device is gone."""
    if row.scope is Scope.PANEL:
        panel = snapshot.panel
        return panel if panel is not None and panel.device_id == device_id else None
    if row.scope is Scope.CIRCUIT:
        return snapshot.circuits.get(device_id)
    return snapshot.lugs.get(device_id)


def row_uid(row: Row, panel_key: str, device_id: str) -> str:
    if row.scope is Scope.PANEL:
        return panel_uid(panel_key, row.source)
    if row.scope is Scope.CIRCUIT:
        return circuit_uid(panel_key, device_id, row.source)
    return lugs_uid(panel_key, device_id, row.source)


def row_described(facts: Any, row: Row) -> bool:
    """Whether the device still declares everything the row reads."""
    return facts is not None and all(key in facts.described for key in row.needs)


def row_exists(snapshot: Snapshot, row: Row, device_id: str) -> bool:
    """Whether the entity for this row and device should be created."""
    facts = row_facts(snapshot, row, device_id)
    return row_described(facts, row) and (row.when is None or row.when(facts))


# --- restoring the tree across restarts -----------------------------------------------

STORE_VERSION = 1


def energy_floor(value: Any, restored: Any, from_store: bool) -> Any:
    """An energy register's value, given the entity's own restored state.

    The tree Store is saved at most every few minutes, so after a crash it can be
    older than the recorder's last state; a total_increasing sensor must not step
    back to it. Live values always win.
    """
    if not isinstance(restored, int | float):
        return value
    if value is None:
        return restored
    if from_store and isinstance(value, int | float) and restored > value:
        return restored
    return value


def store_payload(tree: DeviceTree, root_id: str | None = None) -> dict[str, Any]:
    """The tree as JSON-able data for HA's Store: descriptions and values.

    No `$state` (a restored device must not look ready), and no PII values: the
    panel's PII keys, and its identifying keys (serials, location) on any device.
    With `root_id` described, only devices under it are kept, so departed ones
    do not linger.
    """
    keep: set[str] | None = None
    if root_id is not None and root_id in tree.descriptions:
        keep = {description.device_id for description in tree.walk(root_id)}
    descriptions = {
        device_id: _description_doc(description)
        for device_id, description in tree.descriptions.items()
        if keep is None or device_id in keep
    }
    values: dict[str, dict[str, str]] = {}
    for device_id in tree.devices_with_values():
        if keep is not None and device_id not in keep:
            continue
        # A device whose description has not arrived could be the panel.
        pii = (
            pii_keys(tree.descriptions.get(device_id))
            or (PII_KEYS if device_id not in tree.descriptions else frozenset())
            or IDENTIFYING_KEYS
        )
        kept = {
            key: payload.decode("utf-8", "replace")
            for key, payload in tree.payloads(device_id).items()
            if key not in pii
        }
        if kept:
            values[device_id] = kept
    return {"descriptions": descriptions, "values": values}


def load_payload(data: Any, tree: DeviceTree) -> None:
    """Replay a `store_payload` into `tree`. Raises ValueError on a bad shape."""
    if not isinstance(data, dict):
        raise ValueError("stored tree is not an object")
    descriptions = data.get("descriptions")
    values = data.get("values")
    if not isinstance(descriptions, dict) or not isinstance(values, dict):
        raise ValueError("stored tree is missing its sections")
    for device_id, doc in descriptions.items():
        tree.apply(homie.description_topic(device_id), json.dumps(doc).encode())
    for device_id, props in values.items():
        if not isinstance(props, dict):
            raise ValueError("stored values are not an object")
        pii = pii_keys(tree.descriptions.get(device_id))
        for key, text in props.items():
            if isinstance(text, str) and key not in pii:
                tree.apply(homie.value_topic(device_id, key), text.encode())


def _description_doc(description: DeviceDescription) -> dict[str, Any]:
    """Rebuild a `$description` from its parsed form; it parses back the same."""
    nodes: dict[str, dict[str, Any]] = {}
    for definition in description.properties.values():
        prop: dict[str, Any] = {
            "name": definition.name,
            "datatype": definition.datatype.value,
            "settable": definition.settable,
            "retained": definition.retained,
        }
        if definition.unit is not None:
            prop["unit"] = definition.unit
        if definition.format is not None:
            prop["format"] = definition.format
        node = nodes.setdefault(definition.capability, {"properties": {}})
        node["properties"][definition.property_id] = prop
    doc: dict[str, Any] = {
        "type": description.type,
        "nodes": nodes,
        "children": list(description.children),
    }
    for key in ("name", "version", "parent", "root"):
        if (value := getattr(description, key)) is not None:
            doc[key] = value
    return doc
