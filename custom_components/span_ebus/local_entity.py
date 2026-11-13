"""Entities for a panel added over its local API, fed by `model.Snapshot`s.

What each entity reads, when it exists and its unique id are `model.ROWS`,
free of Home Assistant. This adds the presentation (units, device classes,
names) and the rules every local entity shares: availability from
`model.presence`, rounding, and writing state only when it changed.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_call_later

from .const import CONF_LOCAL_SERIAL, DOMAIN, MANUFACTURER
from .span_client.fallback import RELAY
from .span_client.local import model
from .span_client.local.health import Transport
from .span_client.local.homie import DeviceState
from .span_client.local.model import Presence, Row, Scope, Snapshot, ValueKind

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .transport import SpanTransportManager

_LOGGER = logging.getLogger(__name__)

# Decimal places kept per unit, so a reading that only jitters below what
# anyone reads does not write a recorder row every second. Energy is not
# rounded: it is shown to sensor.ENERGY_PRECISION and statistics need the register.
ROUNDING: dict[str, int] = {
    UnitOfPower.WATT: 0,
    UnitOfElectricCurrent.AMPERE: 1,
    UnitOfElectricPotential.VOLT: 1,
    UnitOfFrequency.HERTZ: 2,
}
# How long a switch or select shows what it was set to before the panel confirms.
CONFIRM_WINDOW = 10.0


@dataclass(frozen=True, slots=True, kw_only=True)
class Presentation:
    """How one row looks in Home Assistant."""

    unit: str | None = None
    device_class: SensorDeviceClass | BinarySensorDeviceClass | None = None
    state_class: SensorStateClass | None = None
    category: EntityCategory | None = None
    options: tuple[str, ...] | None = None
    enabled: bool | Callable[[Any], bool] = True
    suffix: str | None = None  # circuit rows: "<circuit name> <suffix>"
    digits: int | None = None  # overrides ROUNDING for this row


_W, _A, _V, _HZ, _KWH = (
    UnitOfPower.WATT,
    UnitOfElectricCurrent.AMPERE,
    UnitOfElectricPotential.VOLT,
    UnitOfFrequency.HERTZ,
    UnitOfEnergy.KILO_WATT_HOUR,
)
_D = EntityCategory.DIAGNOSTIC
_MEAS, _TOTAL, _INC = (
    SensorStateClass.MEASUREMENT,
    SensorStateClass.TOTAL,
    SensorStateClass.TOTAL_INCREASING,
)
_POWER = Presentation(unit=_W, device_class=SensorDeviceClass.POWER, state_class=_MEAS)
_CURRENT = Presentation(unit=_A, device_class=SensorDeviceClass.CURRENT, state_class=_MEAS)
_CURRENT_D = Presentation(
    unit=_A, device_class=SensorDeviceClass.CURRENT, state_class=_MEAS, category=_D
)
_RATING = Presentation(unit=_A, device_class=SensorDeviceClass.CURRENT, category=_D)
_VOLTAGE = Presentation(
    unit=_V, device_class=SensorDeviceClass.VOLTAGE, state_class=_MEAS, category=_D
)
_ENERGY_IN = Presentation(unit=_KWH, device_class=SensorDeviceClass.ENERGY, state_class=_INC)
_ENERGY_NET = Presentation(
    unit=_KWH, device_class=SensorDeviceClass.ENERGY, state_class=_TOTAL, enabled=False
)

_DURATION = Presentation(
    unit=UnitOfTime.MINUTES, device_class=SensorDeviceClass.DURATION, state_class=_MEAS
)


def _enum(*options: str) -> Presentation:
    return Presentation(device_class=SensorDeviceClass.ENUM, category=_D, options=options)


_DIAG_CONNECTIVITY = Presentation(device_class=BinarySensorDeviceClass.CONNECTIVITY, category=_D)


def _produces(circuit: model.CircuitFacts) -> bool:
    return circuit.role in (model.Role.SOLAR, model.Role.GENERATOR)


PRESENTATION: dict[str, Presentation] = {
    # panel
    "panel_status": _enum(*(state.value for state in DeviceState)),
    "current_power": _POWER,
    "main_consumed_energy": _ENERGY_IN,
    "main_produced_energy": _ENERGY_IN,
    "main_net_energy": _ENERGY_NET,
    "feedthrough_power": Presentation(
        unit=_W, device_class=SensorDeviceClass.POWER, state_class=_MEAS, enabled=False
    ),
    "feedthrough_imported_energy": Presentation(
        unit=_KWH, device_class=SensorDeviceClass.ENERGY, state_class=_INC, enabled=False
    ),
    "feedthrough_exported_energy": Presentation(
        unit=_KWH, device_class=SensorDeviceClass.ENERGY, state_class=_INC, enabled=False
    ),
    "grid_power": _POWER,
    "site_power": _POWER,
    "pv_power": _POWER,
    "battery_power": _POWER,
    "frequency": Presentation(
        unit=_HZ, device_class=SensorDeviceClass.FREQUENCY, state_class=_MEAS
    ),
    "voltage_a": _VOLTAGE,
    "voltage_b": _VOLTAGE,
    "busbar_current": _CURRENT_D,
    "upstream_current_a": _CURRENT_D,
    "upstream_current_b": _CURRENT_D,
    "feedthrough_current_a": Presentation(
        unit=_A, device_class=SensorDeviceClass.CURRENT, state_class=_MEAS, category=_D,
        enabled=False,
    ),
    "feedthrough_current_b": Presentation(
        unit=_A, device_class=SensorDeviceClass.CURRENT, state_class=_MEAS, category=_D,
        enabled=False,
    ),
    "main_breaker_rating": _RATING,
    "service_rating": _RATING,
    "upstream_protection_rating": _RATING,
    "main_relay": _enum("unknown", "open", "closed"),
    "vendor_cloud": _enum("unknown", "unconnected", "connected"),
    "software_version": Presentation(category=_D),
    "pcs_import_limit": _CURRENT_D,
    "pcs_binding_constraint": _enum(
        "fsr", "doe", "voltage", "off_grid", "requested", "operator", "none", "unknown"
    ),
    "door": Presentation(device_class=BinarySensorDeviceClass.DOOR, category=_D),
    "ethernet": Presentation(device_class=BinarySensorDeviceClass.CONNECTIVITY, category=_D),
    "wifi": Presentation(device_class=BinarySensorDeviceClass.CONNECTIVITY, category=_D),
    "pcs_active": Presentation(device_class=BinarySensorDeviceClass.RUNNING, category=_D),
    "pcs_enabled": Presentation(category=_D, enabled=False),
    "time_to_priority_shed": _DURATION,
    "total_time_remaining": _DURATION,
    # circuits
    "power": Presentation(
        unit=_W, device_class=SensorDeviceClass.POWER, state_class=_MEAS, suffix="power"
    ),
    "current": Presentation(
        unit=_A, device_class=SensorDeviceClass.CURRENT, state_class=_MEAS, enabled=False,
        suffix="current",
    ),
    "consumed_energy": Presentation(
        unit=_KWH, device_class=SensorDeviceClass.ENERGY, state_class=_INC,
        suffix="consumed energy",
    ),
    "produced_energy": Presentation(
        unit=_KWH, device_class=SensorDeviceClass.ENERGY, state_class=_INC, enabled=_produces,
        suffix="produced energy",
    ),
    "net_energy": Presentation(
        unit=_KWH, device_class=SensorDeviceClass.ENERGY, state_class=_TOTAL, enabled=False,
        suffix="net energy",
    ),
    "breaker_rating": Presentation(
        unit=_A, device_class=SensorDeviceClass.CURRENT, category=_D, enabled=False,
        suffix="breaker rating",
    ),
    "breaker": Presentation(suffix="breaker"),
    "priority": Presentation(category=EntityCategory.CONFIG, suffix="priority"),
    # islanding, on the panel: only with a MID or battery
    "grid_islandable": Presentation(device_class=BinarySensorDeviceClass.POWER, category=_D),
    "mid:grid/grid-forming-entity": _enum(*model.GRID_FORMING_OPTIONS),
    # battery (+ = discharging, SPAN's raw sign)
    "bess:meter/active-power": _POWER,
    "bess:soc/soc": Presentation(
        unit=PERCENTAGE, device_class=SensorDeviceClass.BATTERY, state_class=_MEAS, digits=0
    ),
    "bess:soc/soe": Presentation(
        unit=_KWH, device_class=SensorDeviceClass.ENERGY_STORAGE, state_class=_MEAS,
        category=_D, digits=2,
    ),
    "bess:info/nameplate-capacity": Presentation(
        unit=_KWH, device_class=SensorDeviceClass.ENERGY_STORAGE, category=_D, enabled=False
    ),
    "bess:status/communication-state": Presentation(
        device_class=SensorDeviceClass.ENUM, category=_D, enabled=False,
        options=("ok", "degraded", "lost", "unknown"),
    ),
    "bess:derived/connected": _DIAG_CONNECTIVITY,
    # solar
    "pv:info/nominal-power": Presentation(
        unit=_W, device_class=SensorDeviceClass.POWER, category=_D, enabled=False
    ),
    "pv:derived/panel-link": _DIAG_CONNECTIVITY,
    # EV charger (read only; the current limit and lock arrive with their platforms)
    "evse:status/status": Presentation(
        device_class=SensorDeviceClass.ENUM, options=model.EVSE_STATUS_OPTIONS
    ),
    "evse:derived/charging": Presentation(
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING
    ),
    "evse:derived/ev-connected": Presentation(device_class=BinarySensorDeviceClass.PLUG),
    "evse:meter/advertised-current": Presentation(
        unit=_A, device_class=SensorDeviceClass.CURRENT, state_class=_MEAS, digits=1
    ),
    "evse:config/max-charge-current": _RATING,
    "evse:derived/panel-link": _DIAG_CONNECTIVITY,
    # MID
    "mid:grid/grid-state": _enum("up", "down", "degraded", "unknown"),
    "mid:grid/islanding-state": _enum("on_grid", "off_grid", "unknown"),
}  # fmt: skip


def round_value(value: Any, unit: str | None, digits: int | None = None) -> Any:
    """Round a reading to what its unit is worth showing. -0 is shown as 0.
    `digits` overrides the unit's default.
    """
    if digits is None:
        digits = ROUNDING.get(unit) if unit is not None else None
    if digits is None or isinstance(value, bool) or not isinstance(value, int | float):
        return value
    if digits == 0:
        return round(value)
    return round(value, digits) + 0.0


def panel_device_info(entry: ConfigEntry, snapshot: Snapshot | None = None) -> DeviceInfo:
    """The panel's device. The Transport sensor passes the same identifiers."""
    serial = entry.data[CONF_LOCAL_SERIAL]
    info = DeviceInfo(
        identifiers={(DOMAIN, serial)},
        name=entry.title,
        manufacturer=MANUFACTURER,
        serial_number=serial,
    )
    panel = snapshot.panel if snapshot is not None else None
    if panel is not None:
        if panel.model:
            info["model"] = panel.model.replace("_", " ")
        if panel.firmware:
            info["sw_version"] = panel.firmware
        if panel.hardware:
            info["hw_version"] = panel.hardware
    return info


_SUBDEVICE_NAMES = {"bess": "Battery", "pv": "Solar", "evse": "EV charger", "mid": "MID"}


def device_info_for(
    entry: ConfigEntry, panel_key: str, ref: model.DeviceRef, snapshot: Snapshot | None = None
) -> DeviceInfo:
    """The device an entity sits on. Subdevices hang off the panel and never
    carry a serial number.
    """
    identifier = model.device_identifier(panel_key, ref)
    if identifier is None:
        return panel_device_info(entry, snapshot)
    if ref.kind == model.REMOTE_CT:
        name = ref.name_hint or "Remote CT"
    else:
        name = f"{entry.title} {_SUBDEVICE_NAMES.get(ref.kind, ref.kind)}"
        if ref.name_hint:
            name = f"{name} ({ref.name_hint})"
    info = DeviceInfo(
        identifiers={(DOMAIN, identifier)},
        name=name,
        via_device=(DOMAIN, entry.data[CONF_LOCAL_SERIAL]),
    )
    for key, value in (
        ("manufacturer", ref.manufacturer),
        ("model", ref.model),
        ("sw_version", ref.sw_version),
        ("hw_version", ref.hw_version),
    ):
        if value:
            info[key] = value
    return info


@callback
def async_track_panel_device(
    hass: HomeAssistant, entry: ConfigEntry, transport: SpanTransportManager
) -> None:
    """Keep the device's model and versions current, writing only when they change
    (a firmware update), not on every snapshot.
    """
    last: tuple[str | None, str | None, str | None] | None = None

    @callback
    def update(snapshot: Snapshot) -> None:
        nonlocal last
        panel = snapshot.panel
        if panel is None:
            return
        info = panel_device_info(entry, snapshot)
        current = (info.get("model"), info.get("sw_version"), info.get("hw_version"))
        if current == last:
            return
        registry = dr.async_get(hass)
        (identifier,) = info["identifiers"]
        if hasattr(registry, "async_get_device_by_identifier"):  # HA 2026.x and later
            device = registry.async_get_device_by_identifier(identifier, entry.entry_id)
        else:
            device = registry.async_get_device(identifiers={identifier})
        if device is None:  # created by the first entity; caught on a later snapshot
            return
        last = current
        changes = {
            key: value
            for key, value in zip(("model", "sw_version", "hw_version"), current, strict=True)
            if value is not None and getattr(device, key) != value
        }
        if changes:
            registry.async_update_device(device.id, **changes)

    entry.async_on_unload(transport.async_add_model_listener(update))


type EntityFactory = Callable[[ConfigEntry, SpanTransportManager, Row, str], Entity]


@callback
def async_setup_local(
    entry: ConfigEntry,
    transport: SpanTransportManager,
    async_add_entities: AddEntitiesCallback,
    platform: str,
    factory: EntityFactory,
) -> None:
    """Create this platform's local entities now and as devices are described.

    Entities are never removed here: one whose device or property vanishes
    becomes unavailable.
    """
    known: set[str] = set()

    @callback
    def add(snapshot: Snapshot) -> None:
        new: list[Entity] = []
        for row in model.ROWS:
            if row.platform != platform:
                continue
            for device_id in model.row_devices(snapshot, row):
                uid = model.row_uid(row, transport.panel_key, device_id)
                if uid in known or not model.row_exists(snapshot, row, device_id):
                    continue
                known.add(uid)
                new.append(factory(entry, transport, row, device_id))
        if new:
            async_add_entities(new)

    add(transport.snapshot)
    entry.async_on_unload(transport.async_add_model_listener(add))


class SpanLocalEntity(Entity):
    """One row for one device. Recomputes on every snapshot and transport
    change, and writes state only when what it shows changed.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, entry: ConfigEntry, transport: SpanTransportManager, row: Row, device_id: str
    ) -> None:
        self._transport = transport
        self._row = row
        self._device_id = device_id
        self._look = PRESENTATION[row.key]
        self._shown: tuple[Any, ...] | None = None
        self._attrs: dict[str, Any] = {}
        self._presence = Presence.UNAVAILABLE
        self._value: Any = None
        self._pending: tuple[Any, float] | None = None
        self._cancel_confirm: CALLBACK_TYPE | None = None
        snapshot = transport.snapshot
        facts = model.row_facts(snapshot, row, device_id)
        self._attr_unique_id = model.row_uid(row, transport.panel_key, device_id)
        # Fixed at construction: the device, the EVSE name hint and the
        # remote-CT name do not follow later renames or a second charger.
        self._ref = model.row_device(snapshot, row, device_id)
        self._attr_device_info = device_info_for(entry, transport.panel_key, self._ref, snapshot)
        self._attr_entity_category = self._look.category
        enabled = self._look.enabled
        self._attr_entity_registry_enabled_default = (
            enabled(facts) if callable(enabled) else enabled
        )
        if row.scope is not Scope.CIRCUIT:
            self._attr_translation_key = model.translation_key(row)
        self._recompute(snapshot)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._transport.async_add_model_listener(self._on_snapshot))
        self.async_on_remove(self._transport.async_add_listener(self._on_transport))
        self.async_on_remove(self._cancel_pending)
        # The snapshot may have moved on since __init__ computed it.
        self._recompute(self._transport.snapshot)

    @property
    def available(self) -> bool:
        return self._presence is not Presence.UNAVAILABLE

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self._attrs or None

    # --- computing what to show --------------------------------------------

    @callback
    def _on_snapshot(self, snapshot: Snapshot) -> None:
        self._refresh(snapshot)

    @callback
    def _on_transport(self) -> None:
        self._refresh(self._transport.snapshot)

    @callback
    def _refresh(self, snapshot: Snapshot) -> None:
        if self._recompute(snapshot) and self.hass is not None:
            self.async_write_ha_state()

    def _restored_value(self) -> Any:
        """A value to fall back on when the tree has none; energy only."""
        return None

    def _recompute(self, snapshot: Snapshot) -> bool:
        """Work out what to show. True if it differs from what is shown."""
        row, transport = self._row, self._transport
        facts = model.row_facts(snapshot, row, self._device_id)
        attrs: dict[str, Any] = {}
        value: Any = None
        if not model.row_described(facts, row):
            presence = Presence.UNAVAILABLE
        else:
            value = self._present(row.value(facts))
            if row.kind is ValueKind.ENERGY:
                value = model.energy_floor(value, self._restored_value(), snapshot.restored)
                value = transport.continue_energy(row.scope, self._device_id, row.source, value)
            fallback = model.NO_FALLBACK
            if transport.shown is Transport.CLOUD and (
                row.kind is ValueKind.MEASUREMENT
                or (row.kind is ValueKind.CONTROL and row.source == RELAY)
            ):
                fallback = transport.fallback_value(row.scope, self._device_id, row.source)
            state = DeviceState.READY if row.ignores_state else facts.state
            presence = model.presence(
                row.kind,
                shown=transport.shown,
                device_state=state,
                has_value=value is not None,
                fallback_has_value=fallback is not model.NO_FALLBACK and fallback is not None,
            )
            if fallback is not model.NO_FALLBACK:
                value = self._present(fallback)
            if presence is Presence.VALUE and row.attrs is not None:
                attrs = row.attrs(facts)
            if row.scope is Scope.CIRCUIT and self._look.suffix:
                if self._ref.kind == model.REMOTE_CT:  # its own device carries the label
                    self._attr_name = self._look.suffix.capitalize()
                else:
                    self._attr_name = f"{model.circuit_label(facts)} {self._look.suffix}"
        if self._blocked(snapshot):
            presence = Presence.UNAVAILABLE
        if presence is not Presence.VALUE:
            value = None
        if row.kind is ValueKind.CONTROL:
            value = self._confirm(presence, value)
        self._presence, self._value, self._attrs = presence, value, attrs
        shown = (presence, value, attrs, getattr(self, "_attr_name", None))
        changed = shown != self._shown
        self._shown = shown
        return changed

    def _blocked(self, snapshot: Snapshot) -> bool:
        """True when this entity must stay unavailable and refuse commands."""
        return False

    def _present(self, value: Any) -> Any:
        if self._look.options is not None and value not in self._look.options:
            return None
        return round_value(value, self._look.unit, self._look.digits)

    # --- switch and select: optimistic until the panel confirms ------------

    def _confirm(self, presence: Presence, value: Any) -> Any:
        if self._pending is None:
            return value
        if presence is Presence.UNAVAILABLE:
            self._cancel_pending()
            return value
        wanted, deadline = self._pending
        if value == wanted:
            self._cancel_pending()
            return value
        if time.monotonic() >= deadline:
            _LOGGER.debug("%s not confirmed within %.0f s", self._row.source, CONFIRM_WINDOW)
            self._cancel_pending()
            return value
        return wanted

    @callback
    def _cancel_pending(self) -> None:
        self._pending = None
        cancel, self._cancel_confirm = self._cancel_confirm, None
        if cancel is not None:
            cancel()

    async def _async_command(self, value: Any) -> None:
        """Send a value; show it until the panel confirms or the window passes.
        An error leaves the state as it was.
        """
        if self._blocked(self._transport.snapshot):
            raise HomeAssistantError(
                "This circuit feeds a battery or solar inverter; its relay is not switched"
            )
        await self._transport.async_send_command(self._device_id, self._row.source, value)
        self._cancel_pending()
        self._pending = (value, time.monotonic() + CONFIRM_WINDOW)

        @callback
        def expire(_now: object) -> None:
            # The timer is the window: it can fire a hair before the monotonic
            # deadline, so drop the pending value here rather than re-checking.
            self._cancel_confirm = None
            if self._pending is not None:
                _LOGGER.debug("%s not confirmed within %.0f s", self._row.source, CONFIRM_WINDOW)
                self._pending = None
            self._refresh(self._transport.snapshot)

        self._cancel_confirm = async_call_later(self.hass, CONFIRM_WINDOW, expire)
        self._refresh(self._transport.snapshot)
