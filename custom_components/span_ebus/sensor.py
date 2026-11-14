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

A local panel gets the same overhead pair, and integrated site, solar and
battery energies, computed from the whole snapshot rather than one device; see
`SpanDerivedPowerSensor` and `SpanDerivedEnergySensor`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import SpanConfigEntry
from .const import CONF_LOCAL_SERIAL, DOMAIN, MANUFACTURER
from .coordinator import STALE_AFTER_SECONDS, SpanCloudCoordinator
from .energy import EnergyAccumulator, SourcedRun, pick_restored
from .local_entity import (
    PRESENTATION,
    KeptEntry,
    SpanLocalEntity,
    SpanUnmatchedEntity,
    async_setup_local,
    async_track_panel_device,
    kept_cloud_entries,
    panel_device_info,
    round_value,
)
from .span_client import standin
from .span_client.backend import (
    POWER_PROPERTY,
    SITE_NODE,
    is_overhead_feed,
    panel_overhead,
)
from .span_client.cloud_history import SITE_FLOW_ENERGY
from .span_client.local import model
from .span_client.local.health import Transport
from .span_client.local.model import DerivedRow, Presence, Row, Scope, Snapshot, ValueKind
from .span_client.models import NodeKind, PropertySpec
from .transport import SpanTransportManager

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
    runtime = entry.runtime_data
    if runtime.transport is not None:
        transport = runtime.transport
        async_add_entities([SpanTransportSensor(entry, transport)])
        # A cloud entry linked to its panel keeps some cloud entities; a
        # stand-in shows a local row's value in place of that row's own entity.
        kept = kept_cloud_entries(hass, entry, "sensor")
        standins = [(k, s) for k in kept if (s := standin.standin_by_suffix(k.suffix)) is not None]
        skip = standin.suppressed_rows(s for _k, s in standins)
        async_setup_local(entry, transport, async_add_entities, "sensor", _local_sensor, skip)
        async_track_panel_device(hass, entry, transport)
        _setup_derived(entry, transport, async_add_entities, skip)
        if kept:
            _setup_kept(entry, transport, async_add_entities, kept, standins)
        # A local entry's cloud is its fallback: the local entities read it, and
        # it creates none of its own.
        return
    runtime.add_platform(lambda c: _register(c, async_add_entities))


def _register(coordinator: SpanCloudCoordinator, async_add_entities: AddEntitiesCallback) -> None:
    # The overhead pair has one uid per panel, so it is built once, however many
    # feeds qualify.
    overhead_added = False

    @callback
    def add_specs(specs: list[PropertySpec]) -> None:
        nonlocal overhead_added
        entities: list[SensorEntity] = []
        for spec in specs:
            entities.append(SpanSensor(coordinator, spec))
            # Everything SPAN meters earns a kWh companion, so what shows on a
            # power graph can also go on the Energy dashboard.
            if _is_metered(spec):
                entities.append(SpanEnergySensor(coordinator, spec))
            # The main feed arrives once per panel, so it is what gates the one
            # derived pair built on it; a panel without one gets no overhead. A
            # feed classified as feedthrough lugs is the downstream panel's, not
            # this one's main feed, unless it is the panel's only feed.
            if not overhead_added and is_overhead_feed(spec, coordinator.schema):
                overhead_added = True
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


class SpanTransportSensor(SensorEntity):
    """Where a local panel's readings come from: local, cloud, or nowhere."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "transport"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: SpanConfigEntry, transport: SpanTransportManager) -> None:
        self._transport = transport
        self._attr_options = [t.value for t in Transport]
        serial = entry.data[CONF_LOCAL_SERIAL]
        self._attr_unique_id = f"{serial}_transport"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=entry.title,
            manufacturer=MANUFACTURER,
        )

    @property
    def native_value(self) -> str:
        return self._transport.shown.value

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._transport.async_add_listener(self.async_write_ha_state))


def _local_sensor(
    entry: SpanConfigEntry, transport: SpanTransportManager, row: Row, device_id: str
) -> SensorEntity:
    if row.kind is ValueKind.ENERGY:
        return SpanLocalEnergySensor(entry, transport, row, device_id)
    return SpanLocalSensor(entry, transport, row, device_id)


class SpanLocalSensor(SpanLocalEntity, SensorEntity):
    """A reading from a panel on its local API."""

    def __init__(
        self, entry: SpanConfigEntry, transport: SpanTransportManager, row: Row, device_id: str
    ) -> None:
        look = PRESENTATION[row.key]
        self._attr_native_unit_of_measurement = look.unit
        self._attr_device_class = look.device_class
        self._attr_state_class = look.state_class
        if look.options is not None:
            self._attr_options = list(look.options)
        super().__init__(entry, transport, row, device_id)

    @property
    def native_value(self) -> Any:
        return self._value


class SpanLocalEnergySensor(SpanLocalSensor, RestoreSensor):
    """A panel energy register. Restored across restarts so it never reads 0 or
    goes unavailable while the panel's retained value is on its way.
    """

    _attr_suggested_display_precision = ENERGY_PRECISION

    def __init__(
        self, entry: SpanConfigEntry, transport: SpanTransportManager, row: Row, device_id: str
    ) -> None:
        self._restored: Any = None
        super().__init__(entry, transport, row, device_id)

    def _restored_value(self) -> Any:
        return self._restored

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and isinstance(last.native_value, int | float):
            self._restored = last.native_value
        self._refresh(self._transport.snapshot)


# --- derived rows on a local panel -------------------------------------------------


@callback
def _setup_derived(
    entry: SpanConfigEntry,
    transport: SpanTransportManager,
    async_add_entities: AddEntitiesCallback,
    skip: frozenset[tuple[Scope, str]] = frozenset(),
) -> None:
    """Create the panel's derived rows as their inputs are published.

    Like the other local entities, one is never removed once created. Rows in
    `skip` are shown by a kept cloud entity instead.
    """
    known: set[str] = set()

    @callback
    def add(snapshot: Snapshot) -> None:
        new: list[SensorEntity] = []
        for row in model.DERIVED_ROWS:
            uid = model.panel_uid(transport.panel_key, row.source)
            if uid in known or (Scope.PANEL, row.key) in skip or not row.when(snapshot):
                continue
            known.add(uid)
            cls = SpanDerivedEnergySensor if row.energy else SpanDerivedPowerSensor
            new.append(cls(entry, transport, row))
        if new:
            async_add_entities(new)

    add(transport.snapshot)
    entry.async_on_unload(transport.async_add_model_listener(add))


class _SpanDerivedSensor(SensorEntity):
    """A panel-device value computed from several devices at once.

    Its inputs are read through `model.shown_reader`: the local values while the
    panel is shown LOCAL, the cloud fallback's while CLOUD, none while OFFLINE.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, entry: SpanConfigEntry, transport: SpanTransportManager, row: DerivedRow
    ) -> None:
        self._transport = transport
        self._row = row
        self._attr_translation_key = row.key
        self._attr_unique_id = model.panel_uid(transport.panel_key, row.source)
        self._attr_device_info = panel_device_info(entry, transport.snapshot)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._transport.async_add_model_listener(self._on_snapshot))
        # While on the cloud no local snapshot arrives; the transport notifies
        # its listeners on each cloud update instead.
        self.async_on_remove(self._transport.async_add_listener(self._on_transport))
        self._refresh(self._transport.snapshot)

    def _power(self, snapshot: Snapshot) -> float | None:
        transport = self._transport
        return self._row.power(
            snapshot, model.shown_reader(transport.shown, transport.fallback_value)
        )

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

    def _recompute(self, snapshot: Snapshot) -> bool:
        """Work out what to show. True if it differs from what is shown."""
        raise NotImplementedError


class SpanDerivedPowerSensor(_SpanDerivedSensor):
    """The panel's overhead in W, computed locally (see `model.local_overhead_w`)."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, entry: SpanConfigEntry, transport: SpanTransportManager, row: DerivedRow
    ) -> None:
        super().__init__(entry, transport, row)
        self._presence = Presence.UNAVAILABLE
        self._value: float | None = None
        self._shown: tuple[Presence, float | None] | None = None
        self._recompute(transport.snapshot)

    def _recompute(self, snapshot: Snapshot) -> bool:
        value = round_value(self._power(snapshot), UnitOfPower.WATT)
        presence = model.derived_presence(
            self._transport.shown, snapshot.root_state, value is not None
        )
        if presence is not Presence.VALUE:
            value = None
        self._presence, self._value = presence, value
        shown = (presence, value)
        changed = shown != self._shown
        self._shown = shown
        return changed

    @property
    def native_value(self) -> float | None:
        return self._value

    @property
    def available(self) -> bool:
        return self._presence is not Presence.UNAVAILABLE


@dataclass
class _IntegratedData(ExtraStoredData):
    """The stored total, readable as sensor data, plus its last sample: when, and the watts."""

    total_kwh: float
    last_at: str | None
    last_w: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "native_value": self.total_kwh,
            "native_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
            "last_at": self.last_at,
            "last_w": self.last_w,
        }


class SpanDerivedEnergySensor(_SpanDerivedSensor, RestoreSensor):
    """kWh integrated from a derived power: the positive part, trapezoidally.

    Sampled once per coalesced snapshot, at the time its newest message arrived,
    from whichever source the panel is shown on; a failover does not reset it. A
    missing input breaks the run, and a break or silence longer than
    `MAX_INTEGRATION_GAP_SECONDS` is not bridged. Always available: a total that
    has stopped advancing is still true.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = ENERGY_PRECISION

    def __init__(
        self, entry: SpanConfigEntry, transport: SpanTransportManager, row: DerivedRow
    ) -> None:
        super().__init__(entry, transport, row)
        self._entry = entry
        self._run = SourcedRun(MAX_INTEGRATION_GAP_SECONDS)
        # UTC ISO time the total last took a sample, persisted with it. The 5d
        # backfill (spec §8.3) reads it at setup to find the stretch HA was down.
        self._last_at: str | None = None
        # The last gap the run refused to bridge, in seconds; 5d records it as a
        # backfill gap ending at the sample that reported it.
        self._last_gap: float | None = None
        # The power of the last sample taken, persisted too: whether the panel
        # was drawing either side of a gap (an all-zero cloud answer is then no
        # answer).
        self._last_w: float | None = None
        # Set from the restore: the first sample taken ends the stretch HA was down.
        self._down_since: datetime | None = None
        self._written: float | None = None
        # Nothing is integrated until the stored total is back.
        self._restoring = True

    async def async_added_to_hass(self) -> None:
        """Pick the total back up before any sample can be integrated into it.

        The recorder is consulted too: if it holds a larger total than the
        restore cache (a crash after a state write, before the cache was saved),
        that one wins, since a `total_increasing` sensor must not go back.
        """
        last = await self.async_get_last_sensor_data()
        cached = pick_restored(None if last is None else last.native_value, None)
        extra = await self.async_get_last_extra_data()
        if extra is not None:
            data = extra.as_dict()
            last_at = data.get("last_at")
            self._last_at = last_at if isinstance(last_at, str) else None
            last_w = data.get("last_w")
            if isinstance(last_w, int | float) and not isinstance(last_w, bool):
                self._last_w = float(last_w)
        recorded, recorded_at = await self._async_recorded()
        total = pick_restored(cached, recorded)
        if total > cached:
            self._last_at = recorded_at
        self._run.total_kwh = total
        self._down_since = dt_util.parse_datetime(self._last_at) if self._last_at else None
        self._restoring = False
        await super().async_added_to_hass()

    async def _async_recorded(self) -> tuple[str | None, str | None]:
        """The last state the recorder holds for this entity, and when, raw."""
        hass = self.hass
        if "recorder" not in hass.config.components:
            return None, None
        try:
            from homeassistant.components.recorder import get_instance, history

            changes = await get_instance(hass).async_add_executor_job(
                history.get_last_state_changes, hass, 1, self.entity_id
            )
        except Exception as exc:  # noqa: BLE001 — a cross-check must not block setup
            _LOGGER.debug("%s: recorder cross-check skipped: %s", self._row.key, type(exc).__name__)
            return None, None
        states = changes.get(self.entity_id) or []
        if not states:
            return None, None
        state = states[-1]
        when = getattr(state, "last_reported", None) or state.last_updated
        return state.state, when.isoformat()

    @property
    def extra_restore_state_data(self) -> _IntegratedData:
        return _IntegratedData(self._run.total_kwh, self._last_at, self._last_w)

    def _recompute(self, snapshot: Snapshot) -> bool:
        if self._restoring:
            return False
        shown = self._transport.shown
        timestamp = model.derived_timestamp(snapshot, shown, time.monotonic())
        if timestamp is not None:
            # A change of transport, OFFLINE included, breaks the run: nothing is
            # integrated across a failover or a failback.
            power = None if shown is Transport.OFFLINE else self._power(snapshot)
            taken, gap = self._run.sample_from(shown, power, timestamp)
            if taken and power is not None:
                now = dt_util.utcnow()
                at = now - timedelta(seconds=max(0.0, time.monotonic() - timestamp))
                drew = self._last_w is not None and self._last_w > 0 and power > 0
                if self._down_since is not None:
                    self._record_gap(self._down_since, at, drew)
                    self._down_since = None
                if gap is not None:
                    self._last_gap = gap
                    _LOGGER.debug("%s: not bridging a %.0f s gap", self._row.key, gap)
                    self._record_gap(at - timedelta(seconds=gap), at, drew)
                self._last_at = now.isoformat()
                self._last_w = power
        value = round(self._run.total_kwh, ENERGY_PRECISION)
        if value == self._written:
            return False
        self._written = value
        return True

    def _record_gap(self, start: datetime, end: datetime, draw: bool) -> None:
        """Hand a stretch this total missed to the cloud backfill, if the entry has one."""
        runner = getattr(self._entry.runtime_data, "backfill", None)
        if runner is not None and self.unique_id is not None:
            runner.record_gap(self.unique_id, self._row.key, start, end, draw=draw)

    @property
    def native_value(self) -> float:
        return round(self._run.total_kwh, ENERGY_PRECISION)

    @property
    def available(self) -> bool:
        return True


# --- a linked cloud entry's kept cloud entities -------------------------------------

_ROWS_BY_KEY = {(row.scope, row.key): row for row in model.ROWS}
_DERIVED_BY_KEY = {row.key: row for row in model.DERIVED_ROWS}


def _device_class(value: str | None) -> SensorDeviceClass | None:
    try:
        return None if value is None else SensorDeviceClass(value)
    except ValueError:
        return None


@callback
def _setup_kept(
    entry: SpanConfigEntry,
    transport: SpanTransportManager,
    async_add_entities: AddEntitiesCallback,
    kept: list[KeptEntry],
    standins: list[tuple[KeptEntry, standin.StandIn]],
) -> None:
    """Build the kept cloud sensors: stand-ins once their source row exists,
    fallback-only and unmatched ones now.
    """
    now: list[SensorEntity] = []
    for k in kept:
        if k.kind == standin.UNMATCHED:
            now.append(SpanUnmatchedSensor(entry, transport, k))
        elif k.kind == standin.FALLBACK:
            if standin.is_energy(k.suffix):
                now.append(SpanFallbackOnlyEnergySensor(entry, transport, k))
            else:
                now.append(SpanFallbackOnlySensor(entry, transport, k))
    if now:
        async_add_entities(now)
    if not standins:
        return
    waiting = dict(standins)

    @callback
    def add(snapshot: Snapshot) -> None:
        new: list[SensorEntity] = []
        for k, s in list(waiting.items()):
            entity = _standin_entity(entry, transport, snapshot, k, s)
            if entity is not None:
                del waiting[k]
                new.append(entity)
        if new:
            async_add_entities(new)

    add(transport.snapshot)
    if waiting:
        entry.async_on_unload(transport.async_add_model_listener(add))


def _standin_entity(
    entry: SpanConfigEntry,
    transport: SpanTransportManager,
    snapshot: Snapshot,
    kept: KeptEntry,
    stand: standin.StandIn,
) -> SensorEntity | None:
    """The stand-in, once its source row is one the panel publishes; else None."""
    if stand.energy:
        derived = _DERIVED_BY_KEY[stand.source_row]
        if not derived.when(snapshot):
            return None
        return SpanStandInEnergySensor(entry, transport, derived, kept)
    row = _ROWS_BY_KEY[(stand.scope, stand.source_row)]
    for device_id in model.row_devices(snapshot, row):
        if model.row_exists(snapshot, row, device_id):
            return SpanStandInSensor(entry, transport, row, device_id, kept, stand)
    return None


class SpanStandInSensor(SpanLocalSensor):
    """A kept cloud sensor showing a local row's value through a fixed transform.

    It follows its source row in every state, the cloud fallback included, and
    keeps the cloud entity's name, look and place on the panel device.
    """

    def __init__(
        self,
        entry: SpanConfigEntry,
        transport: SpanTransportManager,
        row: Row,
        device_id: str,
        kept: KeptEntry,
        stand: standin.StandIn,
    ) -> None:
        self._transform = stand.transform
        super().__init__(entry, transport, row, device_id)
        self._attr_unique_id = kept.unique_id
        self._attr_name = kept.name
        self._attr_translation_key = None
        self._attr_entity_category = None
        self._attr_device_info = panel_device_info(entry, transport.snapshot)

    def _present(self, value: Any) -> Any:
        if isinstance(value, int | float) and not isinstance(value, bool):
            value = self._transform(value)
        return super()._present(value)


class SpanStandInEnergySensor(SpanDerivedEnergySensor):
    """A kept cloud energy total, integrated from a derived local power.

    Picks up from its own restored total, so the cloud's history runs on.
    """

    def __init__(
        self,
        entry: SpanConfigEntry,
        transport: SpanTransportManager,
        row: DerivedRow,
        kept: KeptEntry,
    ) -> None:
        super().__init__(entry, transport, row)
        self._attr_unique_id = kept.unique_id
        self._attr_name = kept.name
        self._attr_translation_key = None


class _SpanKeptSensor(SensorEntity):
    """A kept cloud sensor with no local row, on the panel device."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    # Fed by no local row: the legacy repair may list it (legacy_repair.live).
    kept_without_local_row = True

    def __init__(
        self, entry: SpanConfigEntry, transport: SpanTransportManager, kept: KeptEntry
    ) -> None:
        self._transport = transport
        self._key = kept.suffix
        unit, state_class = standin.look(kept.device_class, kept.suffix)
        self._attr_unique_id = kept.unique_id
        self._attr_name = kept.name
        self._attr_device_class = _device_class(kept.device_class)
        self._attr_native_unit_of_measurement = unit
        self._attr_state_class = None if state_class is None else SensorStateClass(state_class)
        self._attr_device_info = panel_device_info(entry, transport.snapshot)


class SpanUnmatchedSensor(SpanUnmatchedEntity, _SpanKeptSensor):
    """A sensor of a circuit the link could not pair: always unavailable."""

    def __init__(
        self, entry: SpanConfigEntry, transport: SpanTransportManager, kept: KeptEntry
    ) -> None:
        _SpanKeptSensor.__init__(self, entry, transport, kept)


class SpanFallbackOnlySensor(_SpanKeptSensor):
    """A cloud reading with no local source: shown only while on the cloud.

    Unavailable whenever the cloud is not carrying the entry (local up,
    offline, or local only); unknown while it is but has no reading.
    """

    def __init__(
        self, entry: SpanConfigEntry, transport: SpanTransportManager, kept: KeptEntry
    ) -> None:
        super().__init__(entry, transport, kept)
        self._shown: tuple[bool, Any] | None = None
        self._attr_available = False
        self._attr_native_value = None
        self._recompute()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._transport.async_add_listener(self._on_transport))
        self._recompute()

    @callback
    def _on_transport(self) -> None:
        if self._recompute() and self.hass is not None:
            self.async_write_ha_state()

    def _recompute(self) -> bool:
        reading = self._transport.cloud_reading(self._key)
        if reading is model.NO_FALLBACK:
            shown: tuple[bool, Any] = (False, None)
        else:
            try:
                value = None if reading is None else float(reading)
            except (TypeError, ValueError):
                value = None
            shown = (True, round_value(value, self._attr_native_unit_of_measurement))
        changed = shown != self._shown
        self._shown = shown
        self._attr_available, self._attr_native_value = shown
        return changed


class SpanFallbackOnlyEnergySensor(_SpanKeptSensor, RestoreSensor):
    """A cloud energy total with no local source: held, and grown on the cloud.

    Always shows its total (see `standin.HeldTotal`), so the Energy dashboard
    sees no gap and no reset while the panel is local.
    """

    _attr_suggested_display_precision = ENERGY_PRECISION

    def __init__(
        self, entry: SpanConfigEntry, transport: SpanTransportManager, kept: KeptEntry
    ) -> None:
        super().__init__(entry, transport, kept)
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._ledger_key = standin.ledger_key(kept.suffix)
        self._held = standin.HeldTotal()
        self._written: float | None = None

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_sensor_data()
        if last is not None and isinstance(last.native_value, int | float):
            self._held.total = float(last.native_value)
        await super().async_added_to_hass()
        self.async_on_remove(self._transport.async_add_listener(self._on_transport))
        self._on_transport()

    @callback
    def _on_transport(self) -> None:
        self._held.sample(self._transport.cloud_energy(self._ledger_key))
        value = self.native_value
        if value == self._written:
            return
        self._written = value
        if self.hass is not None:
            self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        total = self._held.total
        return None if total is None else round(total, ENERGY_PRECISION)

    @property
    def available(self) -> bool:
        return True
