"""Switch platform — one entity per breaker whose relay we can address.

A circuit only gets a switch if the trait snapshot resolved its switch trait, so
the panel's own metering nodes and any circuit we could not address stay
read-only. See docs/specs/circuit-control.md for the command path.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SpanConfigEntry
from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import SpanCloudCoordinator
from .span_client.backend import RELAY_PROPERTY
from .span_client.models import PropertySpec

# SPAN's own vocabulary: a closed relay is an energized circuit.
STATE_CLOSED = "CLOSED"
STATE_OPEN = "OPEN"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SpanConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    @callback
    def add_specs(specs: list[PropertySpec]) -> None:
        async_add_entities(SpanRelaySwitch(coordinator, spec) for spec in specs)

    coordinator.register_entity_adder(
        add_specs,
        lambda spec: spec.settable and spec.property_id == RELAY_PROPERTY,
    )


class SpanRelaySwitch(CoordinatorEntity[SpanCloudCoordinator], SwitchEntity):
    """One breaker's relay."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator: SpanCloudCoordinator, spec: PropertySpec) -> None:
        super().__init__(coordinator)
        self._key = spec.key
        serial = coordinator.schema.serial if coordinator.schema else "span-cloud"

        node_label = spec.node_name or spec.node_id
        self._attr_name = f"{node_label} breaker".replace("_", " ")
        self._attr_unique_id = f"{serial}_{spec.key}"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=f"SPAN Panel {serial}",
            manufacturer=MANUFACTURER,
            model=MODEL,
            serial_number=serial,
        )

    @property
    def is_on(self) -> bool | None:
        """True when the relay is closed. `None` while the panel says UNKNOWN.

        Anything other than the two real states is reported as unknown rather
        than guessed at, so a stale or unreadable snapshot cannot make a live
        breaker look off.
        """
        reading = self.coordinator.data.get(self._key)
        if reading is None:
            return None
        if reading.value == STATE_CLOSED:
            return True
        if reading.value == STATE_OPEN:
            return False
        return None

    @property
    def available(self) -> bool:
        # Liveness gates the switch too: commanding a breaker off the back of a
        # frozen relay state is worse than refusing while the stream is down.
        return (
            super().available
            and self.coordinator.stream_is_live
            and self._key in (self.coordinator.data or {})
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(self._key, STATE_CLOSED)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(self._key, STATE_OPEN)
