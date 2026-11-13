"""The only module that touches the recorder.

Recorder imports stay inside the functions: the recorder is an after-dependency,
not a dependency, so the integration loads without it. Every read runs on the
recorder's executor and asks for kWh, so the caller does no unit math; the
adjust passes kWh too and the recorder converts.

Statistic ids are entity ids, which are not logged here.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Literal

from homeassistant.core import HomeAssistant

RECORDER = "recorder"
_UNITS = {"energy": "kWh"}


def loaded(hass: HomeAssistant) -> bool:
    return RECORDER in hass.config.components


def is_recorded(hass: HomeAssistant, entity_id: str) -> bool:
    from homeassistant.components.recorder import is_entity_recorded

    return bool(is_entity_recorded(hass, entity_id))


def _instance(hass: HomeAssistant) -> Any:
    from homeassistant.components.recorder import get_instance

    return get_instance(hass)


def _start(value: Any) -> datetime:
    """A row's start: a timestamp on current HA, a datetime on older."""
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    return datetime.fromtimestamp(float(value), UTC)


async def has_sum(hass: HomeAssistant, statistic_id: str) -> bool:
    """Whether the statistic exists and keeps a sum."""
    from homeassistant.components.recorder.statistics import get_metadata

    meta = await _instance(hass).async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={statistic_id})
    )
    found = meta.get(statistic_id)
    return bool(found and found[1].get("has_sum"))


async def last_short_term(
    hass: HomeAssistant, statistic_id: str, types: Iterable[str]
) -> dict[str, Any] | None:
    """The newest 5-minute row of the statistic, in kWh, or None."""
    from homeassistant.components.recorder.statistics import get_last_short_term_statistics

    wanted = set(types)
    rows = await _instance(hass).async_add_executor_job(
        lambda: get_last_short_term_statistics(hass, 1, statistic_id, True, wanted)
    )
    found = rows.get(statistic_id) or []
    return dict(found[0]) if found else None


async def sums(
    hass: HomeAssistant,
    statistic_id: str,
    start: datetime,
    end: datetime,
    period: Literal["5minute", "hour"],
) -> list[tuple[datetime, float]]:
    """(row start in UTC, sum in kWh) for rows in [start, end), oldest first."""
    from homeassistant.components.recorder.statistics import statistics_during_period

    rows = await _instance(hass).async_add_executor_job(
        lambda: statistics_during_period(hass, start, end, {statistic_id}, period, _UNITS, {"sum"})
    )
    out = [
        (_start(row["start"]), float(row["sum"]))
        for row in rows.get(statistic_id) or []
        if row.get("sum") is not None
    ]
    return sorted((when, total) for when, total in out if start <= when < end)


def adjust(hass: HomeAssistant, statistic_id: str, start: datetime, kwh: float) -> None:
    """Queue adding `kwh` to the statistic's sum from `start` on. Not idempotent."""
    _instance(hass).async_adjust_statistics(statistic_id, start, kwh, "kWh")
