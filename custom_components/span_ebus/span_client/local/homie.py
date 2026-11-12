"""Homie 5 device tree, as SPAN publishes it from r202639 (data model 1.0).

The panel is a parent device with child devices: lugs, every circuit, and each
commissioned battery, inverter or charger. A MID can sit one level deeper, under
the battery. Every device publishes on its own topics:

    ebus/5/<device-id>/$state
    ebus/5/<device-id>/$description          JSON, see parse_description
    ebus/5/<device-id>/<capability>/<property>
    ebus/5/<device-id>/<capability>/<property>/set      (we publish only here)

Device ids are opaque (circuit ids are not UUIDs on every model, and vendor
serials contain hyphens), so a device is classified by the `type` in its
`$description` and never by its id.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..models import DataType

log = logging.getLogger(__name__)

HOMIE_DOMAIN = "ebus"
HOMIE_PREFIX = f"{HOMIE_DOMAIN}/5"
DEVICE_TYPE_PREFIX = "energy.ebus.device."

# Every topic the integration needs, for any device on the panel's broker. The
# broker only carries one panel, so a wildcard over devices is not a leak.
SUBSCRIPTIONS = (
    f"{HOMIE_PREFIX}/+/$state",
    f"{HOMIE_PREFIX}/+/$description",
    f"{HOMIE_PREFIX}/+/+/+",
)


class DeviceState(StrEnum):
    """Homie 5 `$state` values. `UNKNOWN` covers anything else."""

    INIT = "init"
    READY = "ready"
    DISCONNECTED = "disconnected"
    SLEEPING = "sleeping"
    LOST = "lost"
    ALERT = "alert"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, payload: str) -> DeviceState:
        try:
            return cls(payload.strip().lower())
        except ValueError:
            return cls.UNKNOWN


@dataclass(frozen=True)
class PropertyDef:
    """One property of one capability, as a device's `$description` declares it."""

    capability: str
    property_id: str
    name: str
    datatype: DataType
    unit: str | None = None
    settable: bool = False
    retained: bool = True
    format: str | None = None

    @property
    def key(self) -> str:
        """`<capability>/<property>`, unique within a device and its topic suffix."""
        return f"{self.capability}/{self.property_id}"

    @property
    def enum_values(self) -> tuple[str, ...]:
        if self.datatype is not DataType.ENUM or not self.format:
            return ()
        return tuple(v.strip() for v in self.format.split(",") if v.strip())


@dataclass(frozen=True)
class DeviceDescription:
    """A parsed `$description`.

    `device_class` is the short class name (`circuit`, `bess`, ...), the same
    keys `GET /api/v2/homie/schema` uses. An unrecognised type keeps its full
    string in `type` and an empty `device_class`.
    """

    device_id: str
    type: str
    name: str | None
    version: int | None
    children: tuple[str, ...] = ()
    parent: str | None = None
    root: str | None = None
    properties: dict[str, PropertyDef] = field(default_factory=dict)

    @property
    def device_class(self) -> str:
        if self.type.startswith(DEVICE_TYPE_PREFIX):
            return self.type[len(DEVICE_TYPE_PREFIX) :]
        return ""

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(p.capability for p in self.properties.values())


class TopicKind(StrEnum):
    STATE = "state"
    DESCRIPTION = "description"
    VALUE = "value"
    SET = "set"
    TARGET = "target"
    OTHER = "other"


@dataclass(frozen=True)
class Topic:
    kind: TopicKind
    device_id: str
    key: str | None = None  # `<capability>/<property>` for value, set and target


def parse_topic(topic: str) -> Topic | None:
    """Classify a topic under `ebus/5/`. None if it is not ours at all."""
    parts = topic.split("/")
    if len(parts) < 4 or parts[0] != HOMIE_DOMAIN or parts[1] != "5":
        return None
    device_id = parts[2]
    rest = parts[3:]
    if len(rest) == 1:
        if rest[0] == "$state":
            return Topic(TopicKind.STATE, device_id)
        if rest[0] == "$description":
            return Topic(TopicKind.DESCRIPTION, device_id)
        return Topic(TopicKind.OTHER, device_id)
    capability, prop = rest[0], rest[1]
    if capability.startswith("$") or prop.startswith("$"):
        return Topic(TopicKind.OTHER, device_id)
    key = f"{capability}/{prop}"
    if len(rest) == 2:
        return Topic(TopicKind.VALUE, device_id, key)
    if len(rest) == 3 and rest[2] == "set":
        return Topic(TopicKind.SET, device_id, key)
    if len(rest) == 3 and rest[2] == "$target":
        return Topic(TopicKind.TARGET, device_id, key)
    return Topic(TopicKind.OTHER, device_id, key)


def set_topic(device_id: str, key: str) -> str:
    """The topic a command for `<capability>/<property>` is published on."""
    return f"{HOMIE_PREFIX}/{device_id}/{key}/set"


def value_topic(device_id: str, key: str) -> str:
    """The topic a device publishes `<capability>/<property>` on."""
    return f"{HOMIE_PREFIX}/{device_id}/{key}"


def description_topic(device_id: str) -> str:
    return f"{HOMIE_PREFIX}/{device_id}/$description"


def parse_description(device_id: str, payload: str | bytes | dict[str, Any]) -> DeviceDescription:
    """Parse one device's `$description`.

    Raises ValueError if the payload is not a Homie 5 device description.
    Unknown datatypes are kept as strings rather than dropped, so a firmware
    that adds a type still gets its properties shown.
    """
    if isinstance(payload, dict):
        doc = payload
    else:
        try:
            doc = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("$description is not JSON") from exc
    if not isinstance(doc, dict):
        raise ValueError("$description is not an object")

    nodes = doc.get("nodes") or {}
    if not isinstance(nodes, dict):
        raise ValueError("$description nodes is not an object")
    properties: dict[str, PropertyDef] = {}
    for capability, node in nodes.items():
        if not isinstance(node, dict):
            continue
        props = node.get("properties") or {}
        if not isinstance(props, dict):
            raise ValueError("$description properties is not an object")
        for prop_id, prop in props.items():
            if not isinstance(prop, dict):
                continue
            definition = PropertyDef(
                capability=capability,
                property_id=prop_id,
                name=_opt_text(prop.get("name")) or prop_id,
                datatype=_datatype(prop.get("datatype"), capability, prop_id),
                unit=_opt_text(prop.get("unit")),
                settable=prop.get("settable") is True,
                retained=prop.get("retained") is not False,
                format=_opt_text(prop.get("format")),
            )
            properties[definition.key] = definition

    children = doc.get("children") or []
    if not isinstance(children, list) or not all(isinstance(c, str) for c in children):
        raise ValueError("$description children is not a list of ids")
    version = doc.get("version")
    return DeviceDescription(
        device_id=device_id,
        type=_opt_text(doc.get("type")) or "",
        name=_opt_text(doc.get("name")),
        version=version if isinstance(version, int) and not isinstance(version, bool) else None,
        children=tuple(children),
        parent=_opt_text(doc.get("parent")),
        root=_opt_text(doc.get("root")),
        properties=properties,
    )


def _opt_text(value: Any) -> str | None:
    """A non-empty string, or None for anything else."""
    return value if isinstance(value, str) and value else None


def _datatype(raw: Any, capability: str, prop_id: str) -> DataType:
    try:
        return DataType(str(raw or "string").lower())
    except ValueError:
        log.debug("unknown datatype %r on %s/%s; using string", raw, capability, prop_id)
        return DataType.STRING


def decode_value(definition: PropertyDef | None, payload: bytes) -> Any:
    """Turn a property payload into a Python value.

    Homie 5 rules: an empty payload means the value was cleared (None), and a
    single NUL byte is how an empty string is sent. A value that does not parse
    as its declared type is returned as None and logged at debug, since a
    wrong 0 would be worse than an unknown.
    """
    if payload == b"":
        return None
    if payload == b"\x00":
        # An empty string; meaningless for numbers, booleans and the like.
        if definition is None or definition.datatype is DataType.STRING:
            return ""
        return None
    text = payload.decode("utf-8", errors="replace")
    if definition is None:
        return text
    datatype = definition.datatype
    try:
        if datatype is DataType.INTEGER:
            return int(text)
        if datatype is DataType.FLOAT:
            return float(text)
        if datatype is DataType.BOOLEAN:
            lowered = text.strip().lower()
            if lowered in ("true", "false"):
                return lowered == "true"
            raise ValueError(text)
        if datatype is DataType.JSON:
            return json.loads(text)
    except ValueError:
        log.debug("unparseable %s for %s: %r", datatype.value, definition.key, text[:40])
        return None
    return text


def encode_value(definition: PropertyDef, value: Any) -> str:
    """Format a value for a `/set` publish. Raises ValueError if it does not fit."""
    if value is None:
        raise ValueError(f"{definition.key} needs a value")
    datatype = definition.datatype
    try:
        text = _encode(definition, datatype, value)
    except (TypeError, OverflowError):
        raise ValueError(f"{definition.key} cannot take {type(value).__name__}") from None
    # An empty payload would clear the property; Homie sends "" as one NUL.
    return text or "\x00"


def _encode(definition: PropertyDef, datatype: DataType, value: Any) -> str:
    if datatype is DataType.BOOLEAN:
        if not isinstance(value, bool):
            raise ValueError(f"{definition.key} expects a boolean")
        return "true" if value else "false"
    if datatype is DataType.INTEGER:
        if isinstance(value, bool) or int(value) != value:
            raise ValueError(f"{definition.key} expects an integer")
        return str(int(value))
    if datatype is DataType.FLOAT:
        return repr(float(value))
    if datatype is DataType.ENUM:
        text = str(value)
        if definition.enum_values and text not in definition.enum_values:
            raise ValueError(f"{definition.key} does not accept {text!r}")
        return text
    if datatype is DataType.JSON:
        return json.dumps(value, separators=(",", ":"))
    return str(value)


class DeviceTree:
    """Every device seen on the broker: descriptions, states and values.

    Values can arrive before their device's `$description` (the broker replays
    retained topics in no particular order), so they are kept raw and decoded
    on read, against whichever description is current at that time.
    """

    def __init__(self) -> None:
        self.descriptions: dict[str, DeviceDescription] = {}
        self.states: dict[str, DeviceState] = {}
        self._raw: dict[str, dict[str, bytes]] = {}

    def apply(self, topic: str, payload: bytes) -> Topic | None:
        """Record one message. Returns its parsed topic, or None if ignored."""
        parsed = parse_topic(topic)
        if parsed is None:
            return None
        if parsed.kind is TopicKind.STATE:
            if payload == b"":
                self.states.pop(parsed.device_id, None)
            else:
                self.states[parsed.device_id] = DeviceState.parse(
                    payload.decode("utf-8", "replace")
                )
        elif parsed.kind is TopicKind.DESCRIPTION:
            if payload == b"":
                # The device is gone; its values go with it.
                self.descriptions.pop(parsed.device_id, None)
                self._raw.pop(parsed.device_id, None)
            else:
                try:
                    self.descriptions[parsed.device_id] = parse_description(
                        parsed.device_id, payload
                    )
                except ValueError:
                    log.warning("ignoring an unparseable $description")
                    return None
        elif parsed.kind is TopicKind.VALUE and parsed.key:
            values = self._raw.setdefault(parsed.device_id, {})
            if payload == b"":
                values.pop(parsed.key, None)
            else:
                values[parsed.key] = payload
        return parsed

    def value(self, device_id: str, key: str) -> Any:
        raw = self._raw.get(device_id, {}).get(key)
        if raw is None:
            return None
        description = self.descriptions.get(device_id)
        definition = description.properties.get(key) if description else None
        return decode_value(definition, raw)

    def payloads(self, device_id: str) -> dict[str, bytes]:
        """A device's undecoded values, as they arrived."""
        return dict(self._raw.get(device_id, {}))

    def devices_with_values(self) -> list[str]:
        return list(self._raw)

    def values(self, device_id: str) -> dict[str, Any]:
        return {key: self.value(device_id, key) for key in self._raw.get(device_id, {})}

    def roots(self) -> list[str]:
        """Devices with no parent: normally just the panel."""
        return [d.device_id for d in self.descriptions.values() if not d.parent]

    def walk(self, root_id: str) -> list[DeviceDescription]:
        """`root_id` and everything below it, parents before children.

        Only described devices are returned. A child listed by its parent but
        not yet described is skipped until its `$description` arrives.
        """
        found: list[DeviceDescription] = []
        seen: set[str] = set()
        queue = [root_id]
        while queue:
            device_id = queue.pop(0)
            if device_id in seen:
                continue
            seen.add(device_id)
            description = self.descriptions.get(device_id)
            if description is None:
                continue
            found.append(description)
            queue.extend(description.children)
        return found

    def effective_state(self, device_id: str) -> DeviceState:
        """Homie 5: a root that is not ready overrides every device under it."""
        description = self.descriptions.get(device_id)
        root = description.root if description and description.root else device_id
        root_state = self.states.get(root, DeviceState.UNKNOWN)
        if root != device_id and root_state is not DeviceState.READY:
            return root_state
        return self.states.get(device_id, DeviceState.UNKNOWN)
