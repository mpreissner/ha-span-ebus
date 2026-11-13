"""The one-time jump in energy history when a cloud entry gets its panel.

Pure: no Home Assistant imports. A re-pointed register energy entity changes
source from the cloud's ledger to a panel register that counts from install, so
the recorder's sum takes one false step. This module holds the arithmetic and
the record's state machine; `energy_step.py` drives it from HA.

The record is plain JSON (a Store):

    {"t_repoint": iso, "issue_raised": bool, "reported": bool,
     "entities": {entity_id: {"cloud_last", "first_local", "first_at", "start",
                              "step", "sum_before", "status", "reason"}}}

Times are UTC ISO strings. Entity ids are the statistic ids and are never logged.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

# Statuses.
WAITING_FIRST = "waiting_first"
WAITING_STATS = "waiting_stats"
READY = "ready"
QUEUED = "queued"
APPLIED = "applied"
NONE = "none"
SKIPPED = "skipped"

WAITING = frozenset({WAITING_FIRST, WAITING_STATS})
TERMINAL = frozenset({APPLIED, NONE, SKIPPED})

# Skip reasons.
RECORDER_NOT_LOADED = "recorder_not_loaded"
EXCLUDED_FROM_RECORDER = "excluded_from_recorder"
NO_STATISTICS = "no_statistics"
NO_STATISTICS_RECORDED = "no_statistics_recorded"
NO_PRIOR_READING = "no_prior_reading"
UNEXPECTED = "unexpected"
CHANGED_SINCE = "changed_since"

REASONS = (
    EXCLUDED_FROM_RECORDER,
    RECORDER_NOT_LOADED,
    NO_STATISTICS,
    NO_STATISTICS_RECORDED,
    NO_PRIOR_READING,
    UNEXPECTED,
    CHANGED_SINCE,
)

BUCKET = timedelta(minutes=5)
LOOKBACK = timedelta(hours=1)
TIMEOUT = timedelta(hours=24)
MIN_STEP_KWH = 0.001
SANITY_KWH = 0.01
SUM_TOLERANCE_KWH = 1e-6
RESET_FRACTION = 0.9

_TO_KWH = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}


def expected_step(cloud_last: float, first_local: float) -> float:
    """What the recorder's total_increasing compile adds to the sum (kWh).

    A drop below 90% of the last value is a meter reset, so the sum adds the
    whole new value; anything else adds the difference, which may be negative.
    """
    if first_local < RESET_FRACTION * cloud_last:
        return first_local
    return first_local - cloud_last


def floor_bucket(when: datetime) -> datetime:
    """The start of the 5-minute short-term bucket holding `when`, in UTC."""
    when = when.astimezone(UTC)
    return when.replace(minute=when.minute - when.minute % 5, second=0, microsecond=0)


def to_kwh(state: str, unit: str | None) -> float | None:
    """A state as kWh, or None if it isn't a finite number in an energy unit."""
    factor = _TO_KWH.get(unit or "")
    if factor is None:
        return None
    try:
        value = float(state)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value * factor


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat()


def parse_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _blank(status: str, cloud_last: float | None = None, reason: str | None = None) -> dict:
    return {
        "cloud_last": cloud_last,
        "first_local": None,
        "first_at": None,
        "start": None,
        "step": None,
        "sum_before": None,
        "status": status,
        "reason": reason,
    }


def new_record(t_repoint: datetime, cloud_lasts: Mapping[str, float | None]) -> dict[str, Any]:
    """The record written at upgrade: one entity per re-pointed register energy.

    An entity with no known last cloud reading can't have its step computed and
    is skipped with `no_prior_reading`.
    """
    tracked = {
        eid: (
            _blank(WAITING_FIRST, float(last))
            if last is not None
            else _blank(SKIPPED, reason=NO_PRIOR_READING)
        )
        for eid, last in cloud_lasts.items()
    }
    return {
        "t_repoint": _iso(t_repoint),
        "issue_raised": False,
        "reported": False,
        "entities": tracked,
    }


def entities(record: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = record.get("entities")
    return raw if isinstance(raw, dict) else {}


def with_status(record: Mapping[str, Any], *statuses: str) -> list[str]:
    wanted = set(statuses)
    return sorted(eid for eid, e in entities(record).items() if e.get("status") in wanted)


def skip(entity: dict[str, Any], reason: str) -> None:
    entity["status"] = SKIPPED
    entity["reason"] = reason


def first_local(entity: dict[str, Any], value_kwh: float, at: datetime) -> None:
    """Record the first local reading and work out the step and its bucket."""
    if entity.get("status") != WAITING_FIRST:
        return
    step = expected_step(float(entity["cloud_last"]), value_kwh)
    entity["first_local"] = value_kwh
    entity["first_at"] = _iso(at)
    entity["step"] = step
    if abs(step) < MIN_STEP_KWH:
        entity["status"] = NONE
        return
    entity["start"] = _iso(floor_bucket(at))
    entity["status"] = WAITING_STATS


def stats_window(entity: Mapping[str, Any]) -> tuple[datetime, datetime] | None:
    """The rows to read for the gate: [start - 1 h, start + 5 min)."""
    start = parse_time(entity.get("start"))
    if start is None:
        return None
    return start - LOOKBACK, start + BUCKET


def sum_at(rows: Iterable[tuple[datetime, float]], start: datetime) -> float | None:
    """The sum of the row that starts at `start`, if there is one."""
    for when, total in rows:
        if when == start:
            return total
    return None


def gate(entity: dict[str, Any], rows: Iterable[tuple[datetime, float]]) -> bool:
    """Move a waiting_stats entity on once its bucket is compiled.

    `rows` are (start, sum) in kWh over `stats_window`, oldest first. Returns
    whether the entity changed. If the recorded change into the bucket is
    smaller than the step, the step isn't in the history and the entity is
    skipped as `unexpected`; a negative step always passes.
    """
    start = parse_time(entity.get("start"))
    if entity.get("status") != WAITING_STATS or start is None:
        return False
    rows = sorted(rows)
    sum_b = sum_at(rows, start)
    if sum_b is None:
        return False
    before = [total for when, total in rows if when < start]
    step = float(entity["step"])
    if before and sum_b - before[-1] < step - SANITY_KWH:
        skip(entity, UNEXPECTED)
        return True
    entity["sum_before"] = sum_b
    entity["status"] = READY
    return True


def timed_out(record: Mapping[str, Any], now: datetime) -> bool:
    t_repoint = parse_time(record.get("t_repoint"))
    return t_repoint is not None and now - t_repoint >= TIMEOUT


def time_out(record: Mapping[str, Any]) -> int:
    """Skip everything still waiting; returns how many."""
    waiting = with_status(record, *WAITING)
    for eid in waiting:
        skip(entities(record)[eid], NO_STATISTICS_RECORDED)
    return len(waiting)


# What a re-read of sum_B says about an entity that was ready or queued.
NOT_APPLIED = "not_applied"
ALREADY_APPLIED = "already_applied"
CHANGED = "changed"


def read_sum(entity: Mapping[str, Any], sum_b: float | None) -> str:
    """Whether the bucket's sum is untouched, carries the fix, or was changed."""
    if sum_b is None:
        return CHANGED
    before = float(entity["sum_before"])
    step = float(entity["step"])
    if abs(sum_b - before) <= SUM_TOLERANCE_KWH:
        return NOT_APPLIED
    if abs(sum_b - (before - step)) <= SUM_TOLERANCE_KWH:
        return ALREADY_APPLIED
    return CHANGED


def reverify(entity: dict[str, Any], sum_b: float | None) -> None:
    """At setup, settle a queued entity from the DB; it is never re-queued here."""
    if entity.get("status") != QUEUED:
        return
    match read_sum(entity, sum_b):
        case "already_applied":
            entity["status"] = APPLIED
        case "not_applied":
            entity["status"] = READY
        case _:
            skip(entity, CHANGED_SINCE)


# What the record says to do with the issue.
ISSUE_WAIT = "wait"
ISSUE_RAISE = "raise"
ISSUE_DROP = "drop"  # nothing to fix or report: delete the store, raise nothing


def issue_action(record: Mapping[str, Any]) -> str:
    """Raise once nothing waits and something is ready or skipped.

    Queued entities are settled silently at the next setup and raise only if
    their task was lost (they go back to ready). Skipped ones are reported
    until the user has been through the repair flow (`reported`). Everything
    else terminal, every entity `none` included, means the store goes and
    nothing is shown.
    """
    statuses = {e.get("status") for e in entities(record).values()}
    if statuses & WAITING:
        return ISSUE_WAIT
    if READY in statuses:
        return ISSUE_RAISE
    if QUEUED in statuses:
        return ISSUE_WAIT
    if SKIPPED in statuses and not record.get("reported"):
        return ISSUE_RAISE
    return ISSUE_DROP


def all_terminal(record: Mapping[str, Any]) -> bool:
    return all(e.get("status") in TERMINAL for e in entities(record).values())


def counts(record: Mapping[str, Any]) -> dict[str, int]:
    """Entities per status, for logs."""
    out: dict[str, int] = {}
    for e in entities(record).values():
        status = str(e.get("status"))
        out[status] = out.get(status, 0) + 1
    return out
