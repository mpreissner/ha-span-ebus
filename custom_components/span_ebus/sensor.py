"""Sensor platform — one entity per SPAN property, created as the schema arrives."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SpanConfigEntry
from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import SpanCloudCoordinator
from .span_client.models import PropertySpec

# unit -> (device_class, native_unit, state_class)
_UNIT_MAP = {
    "W": (SensorDeviceClass.POWER, UnitOfPower.WATT, SensorStateClass.MEASUREMENT),
    "A": (SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE, SensorStateClass.MEASUREMENT),
    "V": (SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT, SensorStateClass.MEASUREMENT),
    "Hz": (SensorDeviceClass.FREQUENCY, UnitOfFrequency.HERTZ, SensorStateClass.MEASUREMENT),
    "Wh": (SensorDeviceClass.ENERGY, UnitOfEnergy.WATT_HOUR, SensorStateClass.TOTAL_INCREASING),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SpanConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    @callback
    def add_specs(specs: list[PropertySpec]) -> None:
        async_add_entities(SpanSensor(coordinator, spec) for spec in specs)

    # Settable properties belong to a control platform (the relay is a switch);
    # everything the panel merely reports is a sensor.
    coordinator.register_entity_adder(add_specs, lambda spec: not spec.settable)


class SpanSensor(CoordinatorEntity[SpanCloudCoordinator], SensorEntity):
    """A single SPAN property, updated by the push coordinator."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SpanCloudCoordinator, spec: PropertySpec) -> None:
        super().__init__(coordinator)
        self._key = spec.key
        serial = coordinator.schema.serial if coordinator.schema else "span-cloud"

        node_label = spec.node_name or spec.node_id
        self._attr_name = f"{node_label} {spec.property_id}".replace("_", " ")
        self._attr_unique_id = f"{serial}_{spec.key}"

        device_class, native_unit, state_class = _UNIT_MAP.get(
            spec.unit or "", (None, spec.unit, None)
        )
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = native_unit
        self._attr_state_class = state_class

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=f"SPAN Panel {serial}",
            manufacturer=MANUFACTURER,
            model=MODEL,
            serial_number=serial,
        )

    @property
    def native_value(self) -> float | None:
        reading = self.coordinator.data.get(self._key)
        if reading is None:
            return None
        try:
            return float(reading.value)
        except (TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        return super().available and self._key in (self.coordinator.data or {})
