"""Logbook lines for control commands: "OPEN requested: refused (protected)".

The context the event was fired with tells the logbook who or what sent it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN
from .span_client.local.commands import EVENT, describe


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, Any]]], None],
) -> None:
    @callback
    def describe_command(event: Event) -> dict[str, Any]:
        data = event.data
        entity_id = data.get("entity_id")
        state = hass.states.get(entity_id) if entity_id else None
        name = state.name if state is not None else entity_id
        return {"name": name, "message": describe(data), "entity_id": entity_id}

    async_describe_event(DOMAIN, EVENT, describe_command)
