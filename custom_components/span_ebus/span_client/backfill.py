"""Cloud gap backfill: what to fetch, when, and how to write it once.

Pure: no Home Assistant imports. A local integrated energy row (site, solar,
battery) counts nothing while it isn't integrating: HA was down, local and
cloud were both down, or its source row stopped. SPAN's cloud metered those
hours anyway, so once they have settled this fills them into the row's
statistics. `backfill.py` at the integration root drives it from HA.

The record is plain JSON (a Store):

    {"gaps": [{"id", "row", "target", "start", "end", "status", "attempts",
               "next_try", "hours", "sum_before", "reason", "draw", "settled_at"}],
     "last_request": iso | None}

`row` is the derived row the cloud components come from; `target` is the
entity whose statistics take the gap (its unique id): a kept cloud stand-in
integrates the same row as the local one, but has its own sum. Times are UTC
ISO strings. `hours` is [[hour start ISO, kWh], ...] and
`sum_before` maps an hourly row start ISO to its sum (kWh) before queueing.
Nothing here is ever logged except counts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .cloud_history import (
    MAX_BACKFILL_SECONDS,
    MIN_WINDOW_SECONDS,
    SITE_FLOW_ENERGY,
    Bucket,
    Resolution,
    align_start,
)

# Statuses.
PENDING = "pending"  # waiting to be fetched
READY = "ready"  # fetched; `hours` known, not yet queued
QUEUED = "queued"  # adjustments handed to the recorder, not yet proven
APPLIED = "applied"
FAILED = "failed"
EXPIRED = "expired"
CHANGED_SINCE = "changed_since"

SETTLED = frozenset({APPLIED, FAILED, EXPIRED, CHANGED_SINCE})
# Gaps a newly recorded gap may be merged into.
_MERGEABLE = frozenset({PENDING})

# Reasons, for diagnostics counts only.
NO_DATA = "no_data"
FETCH_FAILED = "fetch_failed"
NO_STATISTICS = "no_statistics"

# Only gaps the integrator refused: anything shorter it bridged itself.
MIN_GAP_SECONDS = 300
# SPAN fills buckets in late; this also guarantees every hour the gap touches
# has been compiled by the recorder.
SETTLE = timedelta(minutes=75)
# One history request per entry per this long.
MIN_REQUEST_INTERVAL = timedelta(minutes=15)
# Delay before the next try, after the 1st, 2nd, 3rd and 4th failed attempt.
# The attempt after the last of these is the final one.
RETRY_DELAYS = (
    timedelta(minutes=15),
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(hours=24),
)
MAX_AGE = timedelta(seconds=MAX_BACKFILL_SECONDS)
# Settled gaps stay this long, so diagnostics can show counts.
KEEP_SETTLED = timedelta(days=7)
# Above this padded window one hourly request replaces the quarter-hourly one.
QUARTER_HOURLY_MAX = timedelta(hours=24)
SUM_TOLERANCE_KWH = 1e-6
# An hour this small can't be told from rounding in the sums, so it isn't written.
MIN_HOUR_KWH = 2 * SUM_TOLERANCE_KWH
HOUR = timedelta(hours=1)

# The integrated rows the cloud meters, by row key, and the SiteMeasurement
# components that make up each. Overhead energy has no cloud source.
ROW_FIELDS: dict[str, tuple[str, ...]] = {
    "site_energy": SITE_FLOW_ENERGY["home"],
    "pv_energy": SITE_FLOW_ENERGY["solar"],
    "battery_energy": SITE_FLOW_ENERGY["battery"],
}
ALL_FIELDS = tuple(sorted({name for parts in ROW_FIELDS.values() for name in parts}))

_STEP_SECONDS = {Resolution.QUARTER_HOURLY: 900, Resolution.HOURLY: 3_600}


def should_run(mode: str, *, has_tokens: bool, has_target: bool, single_panel_site: bool) -> bool:
    """Whether an entry gets a backfill runner at all.

    Only a local-first entry that holds cloud tokens and knows its site's history
    target. A local-only entry never talks to the cloud, so it never gets one. A
    site of several panels gets none either: how the cloud splits its history
    between them is unknown.
    """
    return mode == "local_first" and has_tokens and has_target and single_panel_site


# --- time helpers ------------------------------------------------------------------


def iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat()


def parse_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return when.astimezone(UTC) if when.tzinfo else when.replace(tzinfo=UTC)


def _ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def _from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def floor_hour(when: datetime) -> datetime:
    return when.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


# --- the record ---------------------------------------------------------------------


def new_record() -> dict[str, Any]:
    return {"gaps": [], "last_request": None}


def gaps(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = record.get("gaps")
    return raw if isinstance(raw, list) else []


def with_status(record: Mapping[str, Any], *statuses: str) -> list[dict[str, Any]]:
    wanted = set(statuses)
    return [g for g in gaps(record) if g.get("status") in wanted]


def find(record: Mapping[str, Any], gap_id: str) -> dict[str, Any] | None:
    return next((g for g in gaps(record) if g.get("id") == gap_id), None)


def counts(record: Mapping[str, Any]) -> dict[str, int]:
    """Gaps per status, for logs and diagnostics."""
    out: dict[str, int] = {}
    for g in gaps(record):
        status = str(g.get("status"))
        out[status] = out.get(status, 0) + 1
    return out


def target_of(gap: Mapping[str, Any]) -> str | None:
    """The entity a gap belongs to; a gap without one belongs to its row."""
    return gap.get("target") or gap.get("row")


def span(gap: Mapping[str, Any]) -> tuple[datetime, datetime] | None:
    start, end = parse_time(gap.get("start")), parse_time(gap.get("end"))
    if start is None or end is None or end <= start:
        return None
    return start, end


def _new_gap(row: str, target: str, start: datetime, end: datetime, draw: bool) -> dict[str, Any]:
    return {
        "id": f"{target}|{iso(start)}",
        "row": row,
        "target": target,
        "start": iso(start),
        "end": iso(end),
        "status": PENDING,
        "attempts": 0,
        "next_try": iso(end + SETTLE),
        "hours": None,
        "sum_before": None,
        "reason": None,
        "draw": draw,
        "settled_at": None,
    }


def add_gap(
    record: dict[str, Any],
    row: str,
    start: datetime,
    end: datetime,
    *,
    now: datetime,
    draw: bool = False,
    target: str | None = None,
) -> bool:
    """Record a gap the row's integrator didn't integrate; returns whether it was kept.

    `target` is the entity that missed it (default: the row). Gaps under
    `MIN_GAP_SECONDS`, of a row the cloud doesn't meter, or wholly older than
    `MAX_AGE` are dropped; an older start is clipped. A gap that overlaps (or
    touches) a pending gap of the same target is merged into it. A part covered
    by a gap of that target already fetched or written is cut off, so the same
    hours are never filled twice.
    """
    if row not in ROW_FIELDS:
        return False
    target = target or row
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    start = max(start, now - MAX_AGE)
    for other in gaps(record):
        if target_of(other) != target or other.get("status") in _MERGEABLE:
            continue
        bounds = span(other)
        if bounds is None or other.get("status") in (FAILED, EXPIRED):
            continue
        o_start, o_end = bounds
        if o_start <= start < o_end:
            start = o_end
        if o_start < end <= o_end:
            end = o_start
    if (end - start).total_seconds() < MIN_GAP_SECONDS:
        return False

    record.setdefault("gaps", [])
    merged = _new_gap(row, target, start, end, draw)
    for other in list(gaps(record)):
        if target_of(other) != target or other.get("status") not in _MERGEABLE:
            continue
        bounds = span(other)
        if bounds is None or bounds[1] < start or end < bounds[0]:
            continue
        lo, hi = min(start, bounds[0]), max(end, bounds[1])
        earlier_first = bounds[0] <= start
        merged.update(
            id=other["id"] if earlier_first else merged["id"],
            start=iso(lo),
            end=iso(hi),
            attempts=int(other.get("attempts") or 0),
            next_try=iso(max(hi + SETTLE, parse_time(other.get("next_try")) or hi)),
            draw=bool(other.get("draw")) and merged["draw"],
        )
        start, end = lo, hi
        record["gaps"].remove(other)
    record["gaps"].append(merged)
    return True


def restart_gap(down_since: datetime, back_at: datetime) -> datetime | None:
    """Where the gap a total missed while Home Assistant was down starts, or None.

    None when it's longer than `MAX_AGE` (the cloud's history can't fill it
    whole) or doesn't run forward (the clock moved).
    """
    length = back_at - down_since
    if length <= timedelta(0) or length > MAX_AGE:
        return None
    return down_since


def _settle(gap: dict[str, Any], status: str, now: datetime, reason: str | None = None) -> None:
    gap["status"] = status
    gap["settled_at"] = iso(now)
    if reason is not None:
        gap["reason"] = reason


def tidy(record: dict[str, Any], now: datetime) -> bool:
    """Clip or expire old pending gaps and prune old settled ones; returns whether it changed.

    Only pending gaps age out: once fetched, the hours are known and no longer
    depend on the cloud's retention.
    """
    changed = False
    horizon = now - MAX_AGE
    for gap in with_status(record, PENDING):
        bounds = span(gap)
        if bounds is None or bounds[1] <= horizon:
            _settle(gap, EXPIRED, now)
            changed = True
        elif bounds[0] < horizon:
            gap["start"] = iso(horizon)
            changed = True
    keep = []
    for gap in gaps(record):
        settled_at = parse_time(gap.get("settled_at"))
        if (
            gap.get("status") in SETTLED
            and settled_at is not None
            and now - settled_at >= KEEP_SETTLED
        ):
            changed = True
            continue
        keep.append(gap)
    if changed:
        record["gaps"] = keep
    return changed


def give_up(gap: dict[str, Any], now: datetime, reason: str) -> None:
    """Nothing can ever be written (the entity isn't recorded, or has no sum)."""
    _settle(gap, FAILED, now, reason)


def fail(gap: dict[str, Any], now: datetime, reason: str) -> None:
    """A failed or empty attempt: schedule the next try, or give up."""
    attempts = int(gap.get("attempts") or 0) + 1
    gap["attempts"] = attempts
    gap["reason"] = reason
    if attempts > len(RETRY_DELAYS):
        _settle(gap, FAILED, now)
        return
    gap["next_try"] = iso(now + RETRY_DELAYS[attempts - 1])


# --- scheduling ---------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    """What to do now, and when to look again (None: nothing to wait for)."""

    fetch: tuple[str, ...]
    write: tuple[str, ...]
    wake_at: datetime | None


def plan(record: Mapping[str, Any], *, now: datetime, cloud_shown: bool) -> Plan:
    """Due gaps to fetch, ready gaps to write, and the next wake-up.

    Nothing is fetched while the cloud is what's shown; failback kicks again.
    A fetch waits for the gap's `next_try` (its settle time, then the retry
    delays) and the per-entry rate limit.
    """
    write = tuple(g["id"] for g in with_status(record, READY))
    tries = {
        g["id"]: when
        for g in with_status(record, PENDING)
        if (when := parse_time(g.get("next_try"))) is not None
    }
    if not tries:
        return Plan((), write, None)
    if cloud_shown:
        return Plan((), write, None)

    last = parse_time(record.get("last_request"))
    allowed_at = last + MIN_REQUEST_INTERVAL if last is not None else now
    if allowed_at > now:
        return Plan((), write, max(min(tries.values()), allowed_at))

    due = tuple(gid for gid, when in tries.items() if when <= now)
    later = [when for gid, when in tries.items() if gid not in due]
    if not due:
        return Plan((), write, min(later))
    wake_at = max(min(later), now + MIN_REQUEST_INTERVAL) if later else None
    return Plan(due, write, wake_at)


# --- the request window --------------------------------------------------------------


def window(spans: Iterable[tuple[datetime, datetime]]) -> tuple[Resolution, int, int]:
    """One request covering every span: (resolution, start_ms, end_ms).

    Quarter-hourly when the padded window is 24 h or less, else hourly. The
    start is aligned to a bucket boundary, the end rounded up to one, and the
    window padded back to the resolution's minimum.
    """
    spans = list(spans)
    if not spans:
        raise ValueError("no spans")
    lo = min(s for s, _ in spans)
    hi = max(e for _, e in spans)
    for resolution in (Resolution.QUARTER_HOURLY, Resolution.HOURLY):
        step_ms = _STEP_SECONDS[resolution] * 1000
        end_ms = -(-_ms(hi) // step_ms) * step_ms
        start_ms = align_start(_ms(lo), resolution)
        minimum = MIN_WINDOW_SECONDS[resolution] * 1000
        if end_ms - start_ms < minimum:
            start_ms = align_start(end_ms - minimum, resolution)
        if (
            resolution is Resolution.HOURLY
            or end_ms - start_ms <= QUARTER_HOURLY_MAX.total_seconds() * 1000
        ):
            return resolution, start_ms, end_ms
    raise AssertionError("unreachable")


# --- allocation ----------------------------------------------------------------------


def _overlap(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
    return max(0.0, (min(a1, b1) - max(a0, b0)).total_seconds())


def allocate(
    buckets: Sequence[Bucket],
    gap_start: datetime,
    gap_end: datetime,
    components: Sequence[str],
) -> list[tuple[datetime, float]]:
    """The gap's energy as (UTC hour start, kWh), oldest first.

    Hours of `MIN_HOUR_KWH` or less are dropped: once written, they couldn't be
    told apart in the sums, so proving which adjustments landed would be
    ambiguous.

    Each bucket's kWh is prorated by the share of the bucket inside
    `[gap_start, gap_end)` and then by the share of that part in each UTC hour,
    assuming even power within the bucket. A component missing from a bucket
    is zero: the wire format omits zero doubles.
    """
    per_hour: dict[datetime, float] = {}
    for bucket in buckets:
        b0, b1 = _from_ms(bucket.start_ms), _from_ms(bucket.end_ms)
        length = (b1 - b0).total_seconds()
        if length <= 0:
            continue
        kwh = bucket.total(components) or 0.0
        if kwh <= 0:
            continue
        lo, hi = max(b0, gap_start), min(b1, gap_end)
        hour = floor_hour(lo)
        while hour < hi:
            share = _overlap(lo, hi, hour, hour + HOUR) / length
            if share > 0:
                per_hour[hour] = per_hour.get(hour, 0.0) + kwh * share
            hour += HOUR
    return [(h, per_hour[h]) for h in sorted(per_hour) if per_hour[h] > MIN_HOUR_KWH]


def no_data(
    buckets: Sequence[Bucket],
    gap_start: datetime,
    gap_end: datetime,
    *,
    draw: bool,
) -> bool:
    """Whether the cloud's answer can't be trusted for this gap.

    It can't when part of `[gap_start, gap_end)` has no bucket, when a bucket
    there aggregated no samples, or when every bucket there is zero across
    every field while local power showed draw at both ends of the gap.
    """
    inside = sorted(
        (b for b in buckets if _from_ms(b.start_ms) < gap_end and _from_ms(b.end_ms) > gap_start),
        key=lambda b: b.start_ms,
    )
    covered = gap_start
    for bucket in inside:
        if _from_ms(bucket.start_ms) > covered:
            return True
        if bucket.count <= 0:
            return True
        covered = max(covered, _from_ms(bucket.end_ms))
    if covered < gap_end:
        return True
    return draw and all(not any(b.values.get(f) for f in ALL_FIELDS) for b in inside)


def hours_of(gap: Mapping[str, Any]) -> list[tuple[datetime, float]]:
    out = []
    for raw in gap.get("hours") or ():
        when = parse_time(raw[0]) if isinstance(raw, list | tuple) and len(raw) == 2 else None
        if when is not None:
            out.append((when, float(raw[1])))
    return out


def fetched(gap: dict[str, Any], buckets: Sequence[Bucket], *, now: datetime) -> bool:
    """Apply a fetch result to a pending gap; returns whether it is ready to write.

    No data is a failed attempt. A gap the cloud says used nothing (all zero
    without local draw) has nothing to write and is applied as it stands.
    """
    bounds = span(gap)
    if bounds is None or gap.get("status") != PENDING:
        return False
    start, end = bounds
    if no_data(buckets, start, end, draw=bool(gap.get("draw"))):
        fail(gap, now, NO_DATA)
        return False
    hours = allocate(buckets, start, end, ROW_FIELDS[gap["row"]])
    if not hours:
        _settle(gap, APPLIED, now)
        return False
    gap["hours"] = [[iso(h), kwh] for h, kwh in hours]
    gap["status"] = READY
    gap["reason"] = None
    return True


# --- writing and proving it -----------------------------------------------------------


def read_window(gap: Mapping[str, Any]) -> tuple[datetime, datetime] | None:
    """The hourly rows to read: a day either side of the gap's hours."""
    hours = hours_of(gap)
    if not hours:
        return None
    return hours[0][0] - timedelta(days=1), hours[-1][0] + HOUR + timedelta(days=1)


def snapshot(
    gap: Mapping[str, Any], rows: Iterable[tuple[datetime, float]]
) -> dict[str, float] | None:
    """The sums to keep as `sum_before`: the gap's rows, the one before and the one after.

    The row before can only change if someone else touched the statistic. The
    row after rises by every adjustment that lands, so it pins down how many
    did even when the row of the last hour is missing.
    None when no row covers the gap's hours yet (the recorder hasn't compiled
    them), so nothing can be proven and nothing is queued.
    """
    hours = hours_of(gap)
    if not hours:
        return None
    first, last = hours[0][0], hours[-1][0]
    rows = sorted(rows)
    inside = [(t, s) for t, s in rows if first <= t <= last]
    if not inside:
        return None
    before = [(t, s) for t, s in rows if t < first]
    after = [(t, s) for t, s in rows if t > last]
    picked = before[-1:] + inside + after[:1]
    return {iso(t): float(s) for t, s in picked}


def ready_to_write(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ready gaps one batch may queue: the oldest per statistic.

    A gap's adjustments shift every later row of its statistic, so a second
    gap on it would be snapshotted, and then judged, against rows that move
    under it. It waits for a later run, as does every gap on a statistic
    with a gap still queued.
    """
    busy = {target_of(g) for g in with_status(record, QUEUED)}
    ready = sorted(
        with_status(record, READY),
        key=lambda g: parse_time(g.get("start")) or datetime.max.replace(tzinfo=UTC),
    )
    picked: list[dict[str, Any]] = []
    for gap in ready:
        target = target_of(gap)
        if target in busy:
            continue
        busy.add(target)
        picked.append(gap)
    return picked


def queue(gap: dict[str, Any], sums: Mapping[str, float]) -> None:
    gap["sum_before"] = dict(sums)
    gap["status"] = QUEUED


# What the rows say about a queued gap.
@dataclass(frozen=True)
class Verdict:
    """`applied` adjustments took effect, in order; None means another pattern."""

    applied: int | None
    total: int


def expected_rise(hours: Sequence[tuple[datetime, float]], row_at: datetime, j: int) -> float:
    """How much the row at `row_at` rises once the first `j` adjustments apply."""
    return sum(kwh for h, kwh in hours[:j] if h <= row_at)


def classify(gap: Mapping[str, Any], rows: Iterable[tuple[datetime, float]]) -> Verdict:
    """Which prefix of the adjustments the rows show, if any.

    Every row in `sum_before` must still exist and have risen by exactly what
    the first `j` adjustments add to it, for one `j`. Hours are never below
    `MIN_HOUR_KWH`, so at most one `j` fits.
    """
    hours = hours_of(gap)
    before = gap.get("sum_before") or {}
    now_sums = {iso(t): float(s) for t, s in rows}
    deltas: list[tuple[datetime, float]] = []
    for key, was in before.items():
        when = parse_time(key)
        if when is None or key not in now_sums:
            return Verdict(None, len(hours))
        deltas.append((when, now_sums[key] - float(was)))
    if not deltas:
        return Verdict(None, len(hours))
    for j in range(len(hours), -1, -1):
        if all(
            abs(delta - expected_rise(hours, when, j)) <= SUM_TOLERANCE_KWH
            for when, delta in deltas
        ):
            return Verdict(j, len(hours))
    return Verdict(None, len(hours))


def settle_queued(gap: dict[str, Any], verdict: Verdict, *, now: datetime, drained: bool) -> None:
    """Move a queued gap on from what the rows show.

    All applied: done. Another pattern: someone else changed the statistic,
    never touched again. A partial prefix is only acted on once the recorder's
    queue is `drained`, when nothing more of it can land: the rest goes back
    to ready (the applied prefix is dropped from `hours`). Before that the
    recorder may still be working through the queue, so the gap stays queued.
    """
    if gap.get("status") != QUEUED:
        return
    if verdict.applied is None:
        _settle(gap, CHANGED_SINCE, now, CHANGED_SINCE)
        return
    if verdict.applied == verdict.total:
        _settle(gap, APPLIED, now)
        return
    if not drained:
        return
    gap["hours"] = (gap.get("hours") or [])[verdict.applied :]
    gap["sum_before"] = None
    gap["status"] = READY
