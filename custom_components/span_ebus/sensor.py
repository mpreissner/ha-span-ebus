"""Sensor platform — one entity per SPAN property, created as the schema arrives.

Alongside each power reading sits a kWh **energy** sensor, integrated here rather
than reported by the panel. SPAN's realtime channel carries instantaneous power
only, and Home Assistant's Energy dashboard measures in kWh — so a panel full of
working power sensors shows up under "Device power consumption" and is invisible
under "Device energy consumption", which is what these close. See
`SpanEnergySensor` for what that costs in accuracy.
"""

from __future__ import annotations

import contextlib
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
from .coordinator import STALE_AFTER_SECONDS, SpanCloudCoordinator
from .energy import EnergyAccumulator
from .span_client.models import PropertySpec

_LOGGER = logging.getLogger(__name__)

# How long a gap in the stream may be before the energy sensors refuse to bridge
# it. Tied to the coordinator's staleness threshold on purpose: past that the
# entities went unavailable, and inventing energy for a window we have no
# readings from would put a fabricated step into long-term statistics.
MAX_INTEGRATION_GAP_SECONDS = STALE_AFTER_SECONDS

# Decimal places an energy total is shown to, and — see
# `SpanEnergySensor._handle_coordinator_update` — the granularity at which it is
# written to the state machine at all. 0.001 kWh is one watt-hour.
ENERGY_PRECISION = 3


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
            # Every watt reading earns a kWh companion, so anything that shows on
            # a power graph can also be put on the Energy dashboard.
            if spec.unit == "W":
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


class SpanEnergySensor(CoordinatorEntity[SpanCloudCoordinator], RestoreSensor):
    """Energy consumed by one node, integrated from its power reading.

    A Riemann sum over the frames as they arrive — see `EnergyAccumulator` for
    the arithmetic and the two rules that keep it publishable as a
    `total_increasing` sensor. At the ~1-2 Hz the stream runs at the granularity
    is fine for a quantity that moves as slowly as a house's load, but it is
    still a derived figure: it will not agree to the last watt-hour with SPAN's
    own app, and it starts from zero on a fresh install rather than from the
    panel's lifetime total.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = ENERGY_PRECISION

    def __init__(self, coordinator: SpanCloudCoordinator, spec: PropertySpec) -> None:
        super().__init__(coordinator)
        self._key = spec.key
        serial = coordinator.schema.serial if coordinator.schema else "span-cloud"

        node_label = spec.node_name or spec.node_id
        # "power" is the node's own reading, so "Kitchen energy" rather than
        # "Kitchen power energy"; the site's directional flows keep their name.
        qualifier = "" if spec.property_id == "power" else f" {spec.property_id}"
        self._attr_name = f"{node_label}{qualifier} energy".replace("_", " ")
        self._attr_unique_id = f"{serial}_{spec.key}_energy"
        self._attr_device_info = _device_info(serial)

        self._accumulator = EnergyAccumulator(MAX_INTEGRATION_GAP_SECONDS)
        # What the state machine was last told, so an unchanged total is not
        # rewritten. `None`/`False` guarantee the first update goes out.
        self._written_value: float | None = None
        self._written_available = False

    async def async_added_to_hass(self) -> None:
        """Pick the total back up where the last run left it.

        Without this every restart resets the meter, and a `total_increasing`
        sensor dropping to zero is read as a meter reset — so the dashboard would
        keep the history but start the daily total over mid-day, every time.
        """
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is None or last.native_value is None:
            return
        try:
            self._accumulator.total_kwh = float(last.native_value)
        except (TypeError, ValueError):
            _LOGGER.debug(
                "%s: ignoring unrestorable stored total %r", self.entity_id, last.native_value
            )

    @callback
    def _handle_coordinator_update(self) -> None:
        reading = (self.coordinator.data or {}).get(self._key)
        if reading is not None:
            # A non-numeric reading on a watt property should not stop the entity
            # writing state; it just contributes nothing to the total.
            with contextlib.suppress(TypeError, ValueError):
                self._accumulator.add(float(reading.value), reading.timestamp)

        # The stream pushes one to two frames a second, and there are as many
        # energy entities as power ones. Writing state on every frame would
        # double the recorder's load in order to report that a total moved by a
        # third of a milliwatt-hour, so state goes out when the figure actually
        # changes at the precision it is shown to — roughly every two seconds on
        # a 1 kW circuit, and almost never on an idle one. Availability is pushed
        # the moment it flips, either way, since that is what takes the entity
        # in and out of the dashboard.
        value = round(self._accumulator.total_kwh, ENERGY_PRECISION)
        available = self.available
        if value == self._written_value and available == self._written_available:
            return
        self._written_value = value
        self._written_available = available
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float:
        return self._accumulator.total_kwh

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.stream_is_live
            and self._key in (self.coordinator.data or {})
        )
