"""Repair flows: the energy-step fix after a cloud entry gets its panel."""

from __future__ import annotations

from typing import Any

from homeassistant import data_entry_flow
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant

from .energy_step import ISSUE_KEY, EnergyStepTracker


def _tracker(hass: HomeAssistant, entry_id: str) -> EnergyStepTracker | None:
    entry = hass.config_entries.async_get_entry(entry_id)
    runtime = getattr(entry, "runtime_data", None) if entry is not None else None
    tracker = getattr(runtime, "energy_step", None)
    return tracker if isinstance(tracker, EnergyStepTracker) and tracker.active else None


class EnergyStepRepairFlow(RepairsFlow):
    """Confirm, take the steps out, then show what changed."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        tracker = _tracker(self.hass, self._entry_id)
        if tracker is None:
            return self.async_abort(reason="not_running")
        if user_input is not None:
            await tracker.async_apply()
            return await self.async_step_result()
        return self.async_show_form(
            step_id="confirm", description_placeholders=await tracker.confirm_placeholders()
        )

    async def async_step_result(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        tracker = _tracker(self.hass, self._entry_id)
        if tracker is None:
            return self.async_create_entry(data={})
        if user_input is not None:
            await tracker.async_finish()
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="result", description_placeholders=await tracker.result_placeholders()
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    entry_id = (data or {}).get("entry_id")
    if issue_id.startswith(f"{ISSUE_KEY}_") and isinstance(entry_id, str):
        return EnergyStepRepairFlow(entry_id)
    return ConfirmRepairFlow()
