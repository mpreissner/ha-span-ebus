"""Select platform, on a panel's local API: a circuit's load-shed priority,
and the panel's asserted islanding state.

Only circuits whose priority the panel declares settable get a priority. The
islanding select exists only when the user opted in and the panel has a
battery. The cloud path has no select.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SpanConfigEntry
from .local_entity import SpanLocalEntity, async_setup_local
from .span_client.local import model
from .span_client.local.commands import Control
from .span_client.local.model import Row
from .transport import SpanTransportManager

ISLANDING_ROW = "asserted_islanding"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SpanConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    transport = entry.runtime_data.transport
    if transport is not None:
        async_setup_local(entry, transport, async_add_entities, "select", _local_select)


def _local_select(
    entry: SpanConfigEntry, transport: SpanTransportManager, row: Row, device_id: str
) -> SpanLocalEntity:
    if row.key == ISLANDING_ROW:
        return SpanLocalIslandingSelect(entry, transport, row, device_id)
    return SpanLocalPrioritySelect(entry, transport, row, device_id)


class SpanLocalPrioritySelect(SpanLocalEntity, SelectEntity):
    """When the panel may shed this circuit. Options are the panel's own,
    lower-cased for translation; the panel's spelling goes back on a change.
    """

    _attr_translation_key = "priority"
    _control = Control.PRIORITY

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


class SpanLocalIslandingSelect(SpanLocalEntity, SelectEntity):
    """What the panel is told about the grid: `none` clears an assertion,
    `on_grid` asserts the grid is up. Off-grid is never offered.

    Unavailable unless opted in, and (like SPAN's own integration) while the
    battery reports healthy or the panel already senses it is on grid. An
    assertion in force keeps it available so it can always be cleared.
    """

    _attr_translation_key = ISLANDING_ROW
    _control = Control.ISLANDING

    def __init__(
        self, entry: SpanConfigEntry, transport: SpanTransportManager, row: Row, device_id: str
    ) -> None:
        self._attr_options = [choice.lower() for choice in model.ISLANDING_CHOICES]
        super().__init__(entry, transport, row, device_id)

    @property
    def current_option(self) -> str | None:
        if not isinstance(self._value, str):
            return None
        option = self._value.lower()
        return option if option in self.options else None  # OFF_GRID: no option

    async def async_select_option(self, option: str) -> None:
        await self._async_command(option.upper())
