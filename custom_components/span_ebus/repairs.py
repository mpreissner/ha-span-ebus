"""Repair flows: the energy-step fix, and removing old cloud entities."""

from __future__ import annotations

from typing import Any

from homeassistant import data_entry_flow
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from . import legacy_repair
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


class LegacyRepairFlow(RepairsFlow):
    """Show what goes, then remove exactly what was shown and still qualifies."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id
        self._confirmed: tuple[str, ...] = ()

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_running")
        if user_input is not None:
            legacy_repair.async_apply(self.hass, entry, self._confirmed)
            return self.async_create_entry(data={})
        listed = legacy_repair.current(self.hass, entry)
        if not listed:
            legacy_repair.async_check(self.hass, entry)
            return self.async_abort(reason="nothing_left")
        self._confirmed = listed.entity_ids
        return self.async_show_form(
            step_id="confirm",
            description_placeholders=await legacy_repair.confirm_placeholders(
                self.hass, entry, listed
            ),
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    entry_id = (data or {}).get("entry_id")
    if issue_id.startswith(f"{ISSUE_KEY}_") and isinstance(entry_id, str):
        return EnergyStepRepairFlow(entry_id)
    if issue_id.startswith(f"{legacy_repair.ISSUE_KEY}_") and isinstance(entry_id, str):
        return LegacyRepairFlow(entry_id)
    return ConfirmRepairFlow()
