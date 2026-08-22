"""Sensor platform — one entity per SPAN property, created as the schema arrives.

Alongside each metered power reading sits a kWh **energy** sensor. SPAN's
realtime channel carries instantaneous power only, and Home Assistant's Energy
dashboard measures in kWh — so a panel full of working power sensors shows up
under "Device power consumption" and is invisible under "Device energy
consumption", which is what these close. The kilowatt-hours are the panel's own,
read back from SPAN's meters by the coordinator; see `SpanEnergySensor`.
"""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    RestoreSensor,
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
from .span_client.backend import POWER_PROPERTY, SITE_NODE
from .span_client.cloud_history import SITE_FLOW_ENERGY
from .span_client.models import NodeKind, PropertySpec

_LOGGER = logging.getLogger(__name__)

# Decimal places an energy total is shown to. 0.001 kWh is one watt-hour, which
# is finer than SPAN's own app reports and about the granularity at which a
# quarter-hour bucket moves on a lightly loaded circuit.
ENERGY_PRECISION = 3


def _is_metered(spec: PropertySpec) -> bool:
    """Whether SPAN meters the energy behind this power reading.

    Not everything that reports watts is metered. Branch circuits are, and the
    site's directional flows are; the panel's own metering block and the main
    feed are not — they publish power and return nothing at all from the history
    RPC. Building energy entities for them would mean two permanently unknown
    sensors on every panel, so they are left out. Their energy is the site's,
    which is metered and does get an entity.
    """
    if spec.unit != "W":
        return False
    if spec.node_kind is NodeKind.CIRCUIT:
        return spec.property_id == POWER_PROPERTY
    return spec.node_id == SITE_NODE and spec.property_id in SITE_FLOW_ENERGY


def _device_info(serial: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, serial)},
        name=f"SPAN Panel {serial}",
        manufacturer=MANUFACTURER,
        model=MODEL,
        serial_number=serial,
    )


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
        entities: list[SensorEntity] = []
        for spec in specs:
            entities.append(SpanSensor(coordinator, spec))
            # Everything SPAN meters earns a kWh companion, so what shows on a
            # power graph can also go on the Energy dashboard.
            if _is_metered(spec):
                entities.append(SpanEnergySensor(coordinator, spec))
        async_add_entities(entities)

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

        self._attr_device_info = _device_info(serial)

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
        return (
            super().available
            and self.coordinator.stream_is_live
            and self._key in (self.coordinator.data or {})
        )


class SpanEnergySensor(RestoreSensor):
    """Energy metered by SPAN for one node, as a running total.

    The panel meters energy itself; the coordinator reads it back in intervals
    and keeps the running total (see `energy.EnergyLedger`). This entity is the
    view onto one key of that ledger, which is why it is not a
    `CoordinatorEntity`: the readings it would be woken for arrive twice a
    second and have nothing to do with a figure that moves once a minute.

    Availability is simply "there is a total". A cumulative reading that has
    stopped advancing is still true — unlike a power reading, which is why the
    stream-liveness rule exists for those — and blinking these unavailable
    during a cloud hiccup would tear a hole in long-term statistics.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = ENERGY_PRECISION

    def __init__(self, coordinator: SpanCloudCoordinator, spec: PropertySpec) -> None:
        self._coordinator = coordinator
        self._key = spec.key
        serial = coordinator.schema.serial if coordinator.schema else "span-cloud"

        node_label = spec.node_name or spec.node_id
        # "power" is the node's own reading, so "Kitchen energy" rather than
        # "Kitchen power energy"; the site's directional flows keep their name.
        qualifier = "" if spec.property_id == POWER_PROPERTY else f" {spec.property_id}"
        self._attr_name = f"{node_label}{qualifier} energy".replace("_", " ")
        self._attr_unique_id = f"{serial}_{spec.key}_energy"
        self._attr_device_info = _device_info(serial)

    async def async_added_to_hass(self) -> None:
        """Offer the ledger whatever total this entity was carrying, then follow it.

        Before 0.1.13 the total lived here, integrated from the power stream.
        Handing it over means an upgrade continues the same counter with measured
        numbers instead of restarting it at zero; the ledger ignores the offer
        once it has a total of its own, so this is a one-time handover and not a
        rewind on every restart.
        """
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            try:
                self._coordinator.adopt_energy_total(self._key, float(last.native_value))
            except (TypeError, ValueError):
                _LOGGER.debug(
                    "%s: ignoring unrestorable stored total %r",
                    self.entity_id,
                    last.native_value,
                )
        self.async_on_remove(self._coordinator.async_add_energy_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> float | None:
        return self._coordinator.energy_total(self._key)

    @property
    def available(self) -> bool:
        return self.native_value is not None
