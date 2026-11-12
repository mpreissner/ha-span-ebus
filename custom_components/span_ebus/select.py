"""Select platform: a circuit's load-shed priority, on a panel's local API.

Only circuits whose priority the panel declares settable get one. The cloud
path has no select.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SpanConfigEntry
from .local_entity import SpanLocalEntity, async_setup_local
from .span_client.local import model
from .span_client.local.model import Row
from .transport import SpanTransportManager


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SpanConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    transport = entry.runtime_data.transport
    if transport is not None:
        async_setup_local(entry, transport, async_add_entities, "select", SpanLocalPrioritySelect)


class SpanLocalPrioritySelect(SpanLocalEntity, SelectEntity):
    """When the panel may shed this circuit. Options are the panel's own,
    lower-cased for translation; the panel's spelling goes back on a change.
    """

    _attr_translation_key = "priority"

    def __init__(
        self, entry: SpanConfigEntry, transport: SpanTransportManager, row: Row, device_id: str
    ) -> None:
        facts = model.row_facts(transport.snapshot, row, device_id)
        options = facts.priority_options if facts is not None else ()
        self._panel_values = {option.lower(): option for option in options}
        self._attr_options = list(self._panel_values)
        super().__init__(entry, transport, row, device_id)

    @property
    def current_option(self) -> str | None:
        if not isinstance(self._value, str):
            return None
        option = self._value.lower()
        return option if option in self.options else None

    async def async_select_option(self, option: str) -> None:
        await self._async_command(self._panel_values.get(option, option.upper()))
