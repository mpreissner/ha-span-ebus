"""Sensor platform — one entity per SPAN property, created as the schema arrives.

Alongside each metered power reading sits a kWh **energy** sensor. SPAN's
realtime channel carries instantaneous power only, and Home Assistant's Energy
dashboard measures in kWh — so a panel full of working power sensors shows up
under "Device power consumption" and is invisible under "Device energy
consumption", which is what these close. The kilowatt-hours are the panel's own,
read back from SPAN's meters by the coordinator; see `SpanEnergySensor`.

The panel's overhead is the exception. It is derived on this side, so there is
no meter to read back, and its energy is integrated from its power instead; see
`SpanPanelOverheadEnergySensor`.
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
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SpanConfigEntry
from .coordinator import STALE_AFTER_SECONDS, SpanCloudCoordinator
from .energy import EnergyAccumulator
from .span_client.backend import POWER_PROPERTY, SITE_NODE, is_main_feed_power, panel_overhead
from .span_client.cloud_history import SITE_FLOW_ENERGY
from .span_client.models import NodeKind, PropertySpec

_LOGGER = logging.getLogger(__name__)

# Decimal places an energy total is shown to. 0.001 kWh is one watt-hour, which
# is finer than SPAN's own app reports and about the granularity at which a
# quarter-hour bucket moves on a lightly loaded circuit.
ENERGY_PRECISION = 3

# How long a gap in the stream may be before the integrated overhead energy
# refuses to bridge it. Tied to the coordinator's staleness threshold on purpose:
# past that the power entities went unavailable, and inventing energy for a
# window there are no readings from would put a fabricated step into long-term
# statistics.
MAX_INTEGRATION_GAP_SECONDS = STALE_AFTER_SECONDS


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
    for coordinator in entry.runtime_data:
        _register(coordinator, async_add_entities)


def _register(coordinator: SpanCloudCoordinator, async_add_entities: AddEntitiesCallback) -> None:
    @callback
    def add_specs(specs: list[PropertySpec]) -> None:
        entities: list[SensorEntity] = []
        for spec in specs:
            entities.append(SpanSensor(coordinator, spec))
            # Everything SPAN meters earns a kWh companion, so what shows on a
            # power graph can also go on the Energy dashboard.
            if _is_metered(spec):
                entities.append(SpanEnergySensor(coordinator, spec))
            # The main feed arrives once per panel, so it is what gates the one
            # derived pair built on it; a panel without one gets no overhead.
            if is_main_feed_power(spec):
                entities.append(SpanPanelOverheadSensor(coordinator))
                entities.append(SpanPanelOverheadEnergySensor(coordinator, spec))
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
        serial = coordinator.serial

        node_label = spec.node_name or spec.node_id
        self._attr_name = f"{node_label} {spec.property_id}".replace("_", " ")
        self._attr_unique_id = f"{serial}_{spec.key}"

        device_class, native_unit, state_class = _UNIT_MAP.get(
            spec.unit or "", (None, spec.unit, None)
        )
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = native_unit
        self._attr_state_class = state_class

        self._attr_device_info = coordinator.device_info()

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
        serial = coordinator.serial

        node_label = spec.node_name or spec.node_id
        # "power" is the node's own reading, so "Kitchen energy" rather than
        # "Kitchen power energy"; the site's directional flows keep their name.
        qualifier = "" if spec.property_id == POWER_PROPERTY else f" {spec.property_id}"
        self._attr_name = f"{node_label}{qualifier} energy".replace("_", " ")
        self._attr_unique_id = f"{serial}_{spec.key}_energy"
        self._attr_device_info = coordinator.device_info()

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


class SpanPanelOverheadSensor(CoordinatorEntity[SpanCloudCoordinator], SensorEntity):
    """Power the panel takes in that its branch circuits do not account for.

    Derived rather than reported: the main feed less the sum of every branch
    meter (see `backend.panel_overhead`). The circuits are read from the schema
    at each update, so a circuit that appears later is subtracted from then on.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Panel overhead"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: SpanCloudCoordinator) -> None:
        super().__init__(coordinator)
        serial = coordinator.serial
        self._attr_unique_id = f"{serial}_panel/overhead"
        self._attr_device_info = coordinator.device_info()

    @property
    def native_value(self) -> float | None:
        schema = self.coordinator.schema
        if schema is None:
            return None
        return panel_overhead(schema, self.coordinator.data or {})

    @property
    def available(self) -> bool:
        return (
            super().available and self.coordinator.stream_is_live and self.native_value is not None
        )


class SpanPanelOverheadEnergySensor(CoordinatorEntity[SpanCloudCoordinator], RestoreSensor):
    """Energy behind the Panel overhead reading, integrated from it.

    The one energy total here that SPAN did not measure. Overhead is a
    difference between meters, and the meter it is taken from — the main feed —
    returns nothing from the history RPC, so there is no interval to read back
    and no ledger key to follow. What is left is a trapezoidal sum over the
    frames as they arrive (see `EnergyAccumulator`), which suits this figure
    better than it did the circuits it was retired for: overhead is a steady
    dozen watts, not a load that switches between samples.

    It exists so overhead can be tracked as a device on the Energy dashboard,
    which wants a power sensor and an energy one. It starts from zero on install
    and will not account for time the stream was down.

    Availability follows the other energy sensors: there is always a total, and
    a total that has stopped advancing is still true.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Panel overhead energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = ENERGY_PRECISION

    def __init__(self, coordinator: SpanCloudCoordinator, feed: PropertySpec) -> None:
        super().__init__(coordinator)
        # Every reading in a frame carries the frame's stamp, so the main feed's
        # is the time the overhead figure was true at.
        self._feed_key = feed.key
        serial = coordinator.serial
        self._attr_unique_id = f"{serial}_panel/overhead_energy"
        self._attr_device_info = coordinator.device_info()

        self._accumulator = EnergyAccumulator(MAX_INTEGRATION_GAP_SECONDS)
        # What the state machine was last told, so an unchanged total is not
        # rewritten. `None` guarantees the first update goes out.
        self._written_value: float | None = None

    async def async_added_to_hass(self) -> None:
        """Pick the total back up where the last run left it.

        Without this every restart resets the meter, and a `total_increasing`
        sensor dropping to zero is read as a meter reset. Restored before the
        coordinator is listened to, so no frame can be integrated into a total
        that is about to be replaced.
        """
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            try:
                self._accumulator.total_kwh = float(last.native_value)
            except (TypeError, ValueError):
                _LOGGER.debug(
                    "%s: ignoring unrestorable stored total %r",
                    self.entity_id,
                    last.native_value,
                )
        await super().async_added_to_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        schema = self.coordinator.schema
        readings = self.coordinator.data or {}
        feed = readings.get(self._feed_key)
        watts = None if schema is None else panel_overhead(schema, readings)
        if feed is not None and watts is not None:
            self._accumulator.add(watts, feed.timestamp)

        # The stream pushes one to two frames a second. State goes out only when
        # the figure changes at the precision it is shown to — every few minutes
        # at a dozen watts — rather than once per frame to report a few
        # microwatt-hours.
        value = round(self._accumulator.total_kwh, ENERGY_PRECISION)
        if value == self._written_value:
            return
        self._written_value = value
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float:
        return self._accumulator.total_kwh

    @property
    def available(self) -> bool:
        return True
