"""Binary sensor platform: the panel's door, network links and PCS, on its local API.

The cloud path has no binary sensors.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SpanConfigEntry
from .local_entity import PRESENTATION, SpanLocalEntity, async_setup_local
from .span_client.local.model import Row
from .transport import SpanTransportManager


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SpanConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    transport = entry.runtime_data.transport
    if transport is not None:
        async_setup_local(
            entry, transport, async_add_entities, "binary_sensor", SpanLocalBinarySensor
        )


class SpanLocalBinarySensor(SpanLocalEntity, BinarySensorEntity):
    """An on/off reading from a panel on its local API."""

    def __init__(
        self, entry: SpanConfigEntry, transport: SpanTransportManager, row: Row, device_id: str
    ) -> None:
        device_class = PRESENTATION[row.key].device_class
        if isinstance(device_class, BinarySensorDeviceClass):
            self._attr_device_class = device_class
        super().__init__(entry, transport, row, device_id)

    @property
    def is_on(self) -> bool | None:
        """The row's value when it is a bool; unknown otherwise, never a guess."""
        return self._value if isinstance(self._value, bool) else None
