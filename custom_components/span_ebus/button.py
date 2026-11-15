"""Button platform: Analyze leg balance, on a panel's local API.

A press runs the same analysis as the `analyze_leg_balance` action, with its
defaults, and posts the result as a persistent notification. Read-only: it
reads recorded statistics and sends nothing to the panel, so it is always
available and any user may press it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SpanConfigEntry
from .leg_balance import async_notify
from .local_entity import panel_device_info
from .span_client.identity import leg_balance_uid

if TYPE_CHECKING:
    from .transport import SpanTransportManager


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SpanConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    transport = entry.runtime_data.transport
    if transport is None:
        return  # a cloud-only entry has nothing to analyze
    async_add_entities([SpanLegBalanceButton(entry, transport)])


class SpanLegBalanceButton(ButtonEntity):
    """Analyze leg balance: one per panel with a transport."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "analyze_leg_balance"

    def __init__(self, entry: SpanConfigEntry, transport: SpanTransportManager) -> None:
        self._entry = entry
        self._attr_unique_id = leg_balance_uid(transport.panel_key)
        self._attr_device_info = panel_device_info(entry, transport.snapshot)

    async def async_press(self) -> None:
        await async_notify(self.hass, self._entry)
