"""Number platform: an EV charger's charge-current limit, on a panel's local API.

The bounds are whole amps from the charger's own declarations and its
published ceiling, re-read at every call. While the ceiling is unknown the
number is unavailable: Home Assistant's default range would accept unsafe
input. The cloud path has no numbers.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SpanConfigEntry
from .const import DOMAIN
from .local_entity import SpanLocalEntity, async_setup_local
from .span_client.local import model
from .span_client.local.commands import Control, Refusal
from .span_client.local.model import Snapshot
from .transport import ControlRefused


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SpanConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    transport = entry.runtime_data.transport
    if transport is not None:
        async_setup_local(entry, transport, async_add_entities, "number", SpanLocalEvseLimit)


class SpanLocalEvseLimit(SpanLocalEntity, NumberEntity):
    """The most current the charger may draw. Shows the user limit while one is
    published, else the charger's ceiling (attribute `effective`).
    """

    _attr_device_class = NumberDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _control = Control.EVSE_LIMIT

    def _bounds(self, snapshot: Snapshot) -> tuple[int, int] | None:
        facts = model.row_facts(snapshot, self._row, self._device_id)
        return None if facts is None else model.evse_limit_bounds(facts)

    def _blocked(self, snapshot: Snapshot) -> bool:
        bounds = self._bounds(snapshot)
        if bounds is not None:
            self._attr_native_min_value, self._attr_native_max_value = bounds
        return bounds is None or super()._blocked(snapshot)

    @property
    def native_value(self) -> float | None:
        return self._value

    def _check(self, value: Any) -> None:
        facts = model.row_facts(self._transport.snapshot, self._row, self._device_id)
        problem = model.evse_limit_problem(facts, value)
        if problem == model.EVSE_LIMIT_GATED:
            raise ControlRefused(Refusal.GATED)
        if problem is not None:
            low, high = model.evse_limit_bounds(facts) or (0, 0)
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="evse_limit_out_of_range",
                translation_placeholders={"min": str(low), "max": str(high)},
            )

    async def async_set_native_value(self, value: float) -> None:
        facts = model.row_facts(self._transport.snapshot, self._row, self._device_id)
        if model.evse_limit_problem(facts, value) is None:
            value = model.evse_limit_payload(value)
        await self._async_command(value)
