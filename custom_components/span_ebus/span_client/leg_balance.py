"""Leg balance: which single-pole circuits to move between L1 and L2.

Free of Home Assistant. The caller hands in each circuit's spaces and its mean
power per period, and the lugs' mean current per leg; `analyze` estimates each
leg's load, checks that estimate against the lugs, and proposes moves into
empty spaces and swaps that lower the imbalance.

Advice only: nothing here switches, moves or reconfigures anything, and every
response says a licensed electrician must check and do any move.

Circuits are keyed by an opaque `key` the caller chooses; it never appears in
an `Analysis`, a response or a line of text. Names and space numbers do.
"""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, NamedTuple

from .local import model

MIN_GAIN_A = 2.0  # a move must lower "high" by at least this many amps...
MIN_GAIN_PCT = 10.0  # ...and by at least this share of today's "high"
BALANCED_BELOW_A = 5.0  # a "high" under this is balanced: nothing to suggest
MIN_HOURS = 72  # history span needed before anything is suggested
MIN_FIT_POINTS = 48  # periods needed for the metrics and the leg check
DEFAULT_DAYS = 28
SHORT_TERM_DAYS = 10  # the recorder's default keep for 5-minute statistics
MIN_COVERAGE = 0.8
FIT_R = 0.8
FIT_SPREAD_A = 3.0
TOP = 3

VOLTS = 120.0  # nominal leg-to-neutral voltage
HIGH_QUANTILE = 0.95
_PERIOD_HOURS = {"hour": 1.0, "5minute": 5 / 60}
_EPSILON = 1e-9

NOTICE = (
    "Suggestions only. Moving a breaker means working inside the panel. "
    "Have a licensed electrician check and do any move."
)
IMBALANCE_NOTE = "Single-pole circuits only; two-pole (240 V) circuits are ignored."
FEEDS_PANEL_NOTE = "feeds another panel"

Period = Literal["hour", "5minute"]


class Leg(StrEnum):
    A = "A"
    B = "B"

    @property
    def label(self) -> str:
        """How a leg is shown to the user."""
        return "L1" if self is Leg.A else "L2"

    @property
    def other(self) -> Leg:
        return Leg.B if self is Leg.A else Leg.A


def leg_of(space: int, *, flipped: bool = False) -> Leg:
    """The leg a space is on: rows of two alternate, starting with A at space 1."""
    on_a = ((space - 1) // 2) % 2 == 0
    return Leg.A if on_a != flipped else Leg.B


_LAYOUTS = {"MAIN_40": range(9, 49), "MLO_48": range(1, 49)}


def panel_spaces(model: str | None) -> range | None:
    """The spaces a panel model reports, or None for a layout not known here."""
    return _LAYOUTS.get(model) if model else None


class Exclusion(StrEnum):
    # Ignored: left out of the prediction, the metrics and the moves.
    TWO_POLE = "two_pole"
    SHARED_SPACE = "shared_space"
    NO_SPACES = "no_spaces"
    # Counted on their leg, never moved.
    PROTECTED = "protected"
    STORAGE_OR_SOLAR = "storage_or_solar"
    FEEDS_PANEL = "feeds_panel"
    FED_BY_PANEL = "fed_by_panel"
    REMOTE_CT = "remote_ct"
    LOW_COVERAGE = "low_coverage"


IGNORED = frozenset({Exclusion.TWO_POLE, Exclusion.SHARED_SPACE, Exclusion.NO_SPACES})


class MappingCheck(StrEnum):
    CONFIRMED = "confirmed"
    FLIPPED = "flipped"
    UNCONFIRMED = "unconfirmed"
    UNCHECKED = "unchecked"  # under MIN_FIT_POINTS periods of lug history to check against


class Problem(StrEnum):
    NOT_ENOUGH_HISTORY = "not_enough_history"
    SHORT_HISTORY = "short_history"
    NO_LAYOUT = "no_layout"


@dataclass(frozen=True, slots=True)
class CircuitIn:
    key: str  # opaque, chosen by the caller; never in the output
    name: str  # shown to the user
    spaces: tuple[int, ...]
    poles: int | None
    nominal_voltage: float | None
    movable: bool  # the caller applies the option- and link-based rules
    reason: Exclusion | None  # why not movable
    mean_w: Mapping[datetime, float]  # period start (UTC) -> mean W, + while drawing
    feeds_panel: bool = False  # a branch feed to another panel: noted when ignored


@dataclass(frozen=True, slots=True)
class LegHistory:
    period: Period  # the same for every series in one call
    mean_a: Mapping[datetime, float]  # mean lug current, leg A
    mean_b: Mapping[datetime, float]


class Listed(NamedTuple):
    """A circuit named in the result, with why it is left out or kept still."""

    name: str
    reason: Exclusion
    note: str | None = None


@dataclass(frozen=True, slots=True)
class Move:
    kind: Literal["move", "swap"]
    circuit: str
    from_space: int
    to_space: int
    from_leg: Leg
    to_leg: Leg
    other: str | None  # swaps only
    other_from_space: int | None  # swaps only
    typical_before: float
    typical_after: float
    high_before: float
    high_after: float


@dataclass(frozen=True, slots=True)
class Analysis:
    period: Period
    periods_used: int
    span_hours: float
    mapping: MappingCheck
    fit_r: float | None  # Pearson r of the mapping used; None when not computed
    balanced: bool  # high < balanced_below_a: no moves, everything else filled
    typical: float
    high: float
    heavier: Leg | None
    per_leg_mean: tuple[float, float]  # A, B: single-pole load only
    moves: tuple[Move, ...]
    ignored: tuple[Listed, ...]
    not_moved: tuple[Listed, ...]
    empty_spaces: tuple[int, ...]
    problem: Problem | None  # moves are empty when set
    fit_points: int = 0  # periods the leg check ran over
    lugs_shared: bool = False  # the leg check could not separate another panel's load
    min_hours: int = field(default=MIN_HOURS)


# --- helpers ---------------------------------------------------------------------------


def is_two_pole(circuit: CircuitIn) -> bool:
    if circuit.poles == 2 or len(circuit.spaces) >= 2:
        return True
    return circuit.nominal_voltage is not None and round(circuit.nominal_voltage) == 240


# --- from the panel's facts ------------------------------------------------------------


def load_series(
    circuit: model.CircuitFacts, mean_w: Mapping[datetime, float]
) -> dict[datetime, float]:
    """A circuit's recorded mean power, signed + while it draws.

    The power sensor reads + while producing for solar and a generator, so
    theirs is negated. A remote CT keeps the panel's + = import sign.
    """
    if circuit.role in model.PRODUCERS and not circuit.is_remote_ct:
        return {when: -value for when, value in mean_w.items()}
    return dict(mean_w)


def held_still(circuit: model.CircuitFacts, options: model.ControlOptions) -> Exclusion | None:
    """Why a circuit is counted but never moved, first match wins; else None."""
    if circuit.device_id in options.protected:
        return Exclusion.PROTECTED
    if model.feeds_storage_or_solar(circuit):
        return Exclusion.STORAGE_OR_SOLAR
    if circuit.feeds is not None:
        return Exclusion.FEEDS_PANEL
    if circuit.fed_by is not None:
        return Exclusion.FED_BY_PANEL
    if circuit.is_remote_ct:
        return Exclusion.REMOTE_CT
    return None


Series = Mapping[datetime, float]


def _difference(upstream: Series, feedthrough: Series) -> dict[datetime, float]:
    return {
        when: value - feedthrough[when] for when, value in upstream.items() if when in feedthrough
    }


def lug_history(
    period: Period,
    upstream: tuple[Series, Series] | None,
    feedthrough: tuple[Series, Series] | None,
    *,
    has_feedthrough: bool,
) -> tuple[LegHistory | None, bool]:
    """(this panel's own leg currents, lugs shared) from the recorded lug means.

    With a feedthrough, the upstream lugs also carry the panel it feeds: its
    currents are subtracted when both have history. Without that history the
    upstream currents are used as they are and the lugs are reported shared,
    so the leg check is never confirmed.
    """
    shared = False
    if upstream is None:
        return None, has_feedthrough
    mean_a, mean_b = upstream
    if has_feedthrough:
        if feedthrough is not None and feedthrough[0] and feedthrough[1]:
            mean_a = _difference(mean_a, feedthrough[0])
            mean_b = _difference(mean_b, feedthrough[1])
        else:
            shared = True
    if not mean_a or not mean_b:
        return None, shared
    return LegHistory(period=period, mean_a=dict(mean_a), mean_b=dict(mean_b)), shared


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear interpolation between closest ranks; 0 for no values."""
    if not sorted_values:
        return 0.0
    pos = q * (len(sorted_values) - 1)
    low = math.floor(pos)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (pos - low)


def metrics(diffs: Sequence[float]) -> tuple[float, float]:
    """(typical, high): the median and the 95th percentile of |A - B|."""
    imbalance = sorted(abs(d) for d in diffs)
    if not imbalance:
        return 0.0, 0.0
    return statistics.median(imbalance), _quantile(imbalance, HIGH_QUANTILE)


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 3:
        return None
    try:
        return statistics.correlation(xs, ys)
    except statistics.StatisticsError:
        return None  # one series is constant


def _span_hours(periods: Sequence[datetime], period: Period) -> float:
    if not periods:
        return 0.0
    length = timedelta(hours=_PERIOD_HOURS[period])
    return (periods[-1] - periods[0] + length) / timedelta(hours=1)


# --- the analysis ----------------------------------------------------------------------


@dataclass(slots=True)
class _Counted:
    circuit: CircuitIn
    space: int
    leg: Leg  # with the default mapping
    amps: list[float] = field(default_factory=list)  # per aligned period


def _partner_spaces(space: int, claims: Mapping[int, int], spaces: range | None) -> set[int]:
    """The space a two-pole breaker reporting only `space` also takes.

    Its other pole is in the same column, one row up or down. The side that is
    in range and not claimed by another circuit; both when that is ambiguous.
    """
    candidates = {
        other
        for other in (space - 2, space + 2)
        if (other in spaces if spaces is not None else other >= 1) and other not in claims
    }
    return candidates


def _split(
    circuits: Sequence[CircuitIn], spaces: range | None = None
) -> tuple[list[Listed], list[CircuitIn], set[int]]:
    """Ignored circuits, the single-pole rest, and every claimed space."""
    claims: dict[int, int] = {}
    for circuit in circuits:
        for space in set(circuit.spaces):
            claims[space] = claims.get(space, 0) + 1
    claimed = set(claims)
    for circuit in circuits:
        if len(set(circuit.spaces)) == 1 and is_two_pole(circuit):
            claimed |= _partner_spaces(circuit.spaces[0], claims, spaces)
    ignored: list[Listed] = []
    single: list[CircuitIn] = []
    for circuit in circuits:
        if not circuit.spaces:
            ignored.append(Listed(circuit.name, Exclusion.NO_SPACES))
        elif is_two_pole(circuit):
            note = FEEDS_PANEL_NOTE if circuit.feeds_panel else None
            ignored.append(Listed(circuit.name, Exclusion.TWO_POLE, note))
        elif any(claims[space] > 1 for space in circuit.spaces):
            ignored.append(Listed(circuit.name, Exclusion.SHARED_SPACE))
        else:
            single.append(circuit)
    return ignored, single, claimed


def _aligned(counted: Sequence[_Counted]) -> list[datetime]:
    """Periods where enough of the circuits with any history have a value."""
    with_data = [c for c in counted if c.circuit.mean_w]
    if not with_data:
        return []
    seen: dict[datetime, int] = {}
    for c in with_data:
        for when in c.circuit.mean_w:
            seen[when] = seen.get(when, 0) + 1
    need = MIN_COVERAGE * len(with_data) - _EPSILON
    return sorted(when for when, n in seen.items() if n >= need)


def analyze(
    circuits: Sequence[CircuitIn],
    legs: LegHistory | None,
    spaces: range | None,
    *,
    period: Period | None = None,
    min_gain_a: float = MIN_GAIN_A,
    min_gain_pct: float = MIN_GAIN_PCT,
    balanced_below_a: float = BALANCED_BELOW_A,
    top: int = TOP,
    lugs_shared: bool = False,
) -> Analysis:
    """One panel's leg balance over the history given.

    `period` names the statistics' period when `legs` is None (it defaults to
    `legs.period`, else "hour"). `lugs_shared` says the lugs also carry another
    panel's load that could not be subtracted: the leg check is then never
    confirmed.
    """
    period = legs.period if legs is not None else (period or "hour")
    for circuit in circuits:
        if not circuit.movable and circuit.reason is None:
            raise ValueError("a circuit that is not movable needs a reason")
    ignored, single, claimed = _split(circuits, spaces)
    empty = tuple(s for s in spaces if s not in claimed) if spaces is not None else ()

    counted = [_Counted(c, c.spaces[0], leg_of(c.spaces[0])) for c in single]
    periods = _aligned(counted)
    for c in counted:
        mean_w = c.circuit.mean_w
        c.amps = [mean_w.get(when, 0.0) / VOLTS for when in periods]

    # Coverage counts from the first period the lugs have data for, so recently
    # created statistics don't mark every circuit; without lug data, from the start.
    lug_periods = (
        [p for p in periods if p in legs.mean_a and p in legs.mean_b] if legs is not None else []
    )
    base = [p for p in periods if p >= lug_periods[0]] if lug_periods else periods
    not_moved: list[Listed] = []
    movable: list[_Counted] = []
    for c in counted:
        reason = c.circuit.reason if not c.circuit.movable else None
        if reason is None and base:
            have = sum(1 for p in base if p in c.circuit.mean_w)
            if have < MIN_COVERAGE * len(base) - _EPSILON:
                reason = Exclusion.LOW_COVERAGE
        if reason is None and not base:
            reason = Exclusion.LOW_COVERAGE
        if reason is not None:
            not_moved.append(Listed(c.circuit.name, reason))
        else:
            movable.append(c)

    n = len(periods)
    span = _span_hours(periods, period)
    sum_a = [0.0] * n
    sum_b = [0.0] * n

    def legs_now(flipped: bool) -> None:
        for i in range(n):
            sum_a[i] = sum_b[i] = 0.0
        for c in counted:
            target = sum_a if (c.leg is Leg.A) != flipped else sum_b
            for i, amps in enumerate(c.amps):
                target[i] += amps

    legs_now(False)

    def done(**kwargs: Any) -> Analysis:
        diffs = [a - b for a, b in zip(sum_a, sum_b, strict=True)]
        typical, high = metrics(diffs)
        mean_a = statistics.fmean(sum_a) if n else 0.0
        mean_b = statistics.fmean(sum_b) if n else 0.0
        heavier = None
        if n and not math.isclose(mean_a, mean_b, abs_tol=0.05):
            heavier = Leg.A if mean_a > mean_b else Leg.B
        values: dict[str, Any] = {
            "period": period,
            "periods_used": n,
            "span_hours": span,
            "mapping": MappingCheck.UNCHECKED,
            "fit_r": None,
            "balanced": False,
            "typical": typical,
            "high": high,
            "heavier": heavier,
            "per_leg_mean": (mean_a, mean_b),
            "moves": (),
            "ignored": tuple(ignored),
            "not_moved": tuple(not_moved),
            "empty_spaces": empty,
            "problem": None,
            "lugs_shared": lugs_shared,
        }
        values.update(kwargs)
        return Analysis(**values)

    if spaces is None:
        return done(problem=Problem.NO_LAYOUT)
    if n < MIN_FIT_POINTS:
        return done(problem=Problem.NOT_ENOUGH_HISTORY)

    # The leg check (L9): predicted A - B against the lugs' measured A - B.
    mapping, fit_r, flipped = MappingCheck.UNCHECKED, None, False
    if legs is not None and len(lug_periods) >= MIN_FIT_POINTS:
        index = {when: i for i, when in enumerate(periods)}
        predicted = [sum_a[index[p]] - sum_b[index[p]] for p in lug_periods]
        measured = [legs.mean_a[p] - legs.mean_b[p] for p in lug_periods]
        r = _pearson(predicted, measured)
        spread = max(measured) - min(measured)
        decisive = r is not None and spread >= FIT_SPREAD_A and not lugs_shared
        mapping, fit_r = MappingCheck.UNCONFIRMED, r
        if decisive and r is not None and r >= FIT_R:
            mapping = MappingCheck.CONFIRMED
        elif decisive and r is not None and r <= -FIT_R:
            mapping, fit_r, flipped = MappingCheck.FLIPPED, -r, True
        if flipped:
            legs_now(True)
    elif lugs_shared:
        # The feedthrough's currents have no history: the lugs can't confirm anything.
        mapping = MappingCheck.UNCONFIRMED
    check = {"mapping": mapping, "fit_r": fit_r, "fit_points": len(lug_periods)}

    diffs = [a - b for a, b in zip(sum_a, sum_b, strict=True)]
    typical, high = metrics(diffs)
    balanced = high < balanced_below_a
    short = span < MIN_HOURS - _EPSILON
    if balanced or short:
        problem = Problem.SHORT_HISTORY if short else None
        return done(balanced=balanced, problem=problem, **check)

    moves = _moves(
        counted=counted,
        movable=movable,
        empty=empty,
        diffs=diffs,
        flipped=flipped,
        typical=typical,
        high=high,
        min_gain=max(min_gain_a, min_gain_pct / 100 * high),
    )
    return done(moves=tuple(moves[: max(top, 0)]), **check)


def _moves(
    *,
    counted: Sequence[_Counted],
    movable: Sequence[_Counted],
    empty: Sequence[int],
    diffs: Sequence[float],
    flipped: bool,
    typical: float,
    high: float,
    min_gain: float,
) -> list[Move]:
    """Every single move and swap that passes the threshold, best first."""

    def leg(c: _Counted) -> Leg:
        return c.leg.other if flipped else c.leg

    def score(change: Sequence[float]) -> tuple[float, float]:
        return metrics([d + delta for d, delta in zip(diffs, change, strict=True)])

    def passes(after: tuple[float, float]) -> bool:
        return high - after[1] >= min_gain - _EPSILON and after[0] <= typical + _EPSILON

    found: list[Move] = []
    for c in movable:
        here = leg(c)
        # Moving a circuit off leg A lowers A - B by twice its amps; off B, raises it.
        sign = -2.0 if here is Leg.A else 2.0
        targets = [s for s in empty if leg_of(s, flipped=flipped) is not here]
        if targets:
            after = score([sign * amps for amps in c.amps])
            if passes(after):
                # Every empty space on the other leg scores the same: offer the
                # nearest, in the same column first.
                to = min(targets, key=lambda s: ((s - c.space) % 2 != 0, abs(s - c.space), s))
                found.append(_move("move", c, to, here, None, typical, high, after))
    by_leg = [c for c in movable if leg(c) is Leg.A], [c for c in movable if leg(c) is Leg.B]
    for c in by_leg[0]:
        for o in by_leg[1]:
            after = score([2.0 * (b - a) for a, b in zip(c.amps, o.amps, strict=True)])
            if passes(after):
                found.append(_move("swap", c, o.space, Leg.A, o, typical, high, after))
    found.sort(key=lambda m: (m.high_after, m.typical_after, m.kind, m.circuit, m.to_space))
    return found


def _move(
    kind: Literal["move", "swap"],
    c: _Counted,
    to_space: int,
    from_leg: Leg,
    other: _Counted | None,
    typical: float,
    high: float,
    after: tuple[float, float],
) -> Move:
    return Move(
        kind=kind,
        circuit=c.circuit.name,
        from_space=c.space,
        to_space=to_space,
        from_leg=from_leg,
        to_leg=from_leg.other,
        other=other.circuit.name if other is not None else None,
        other_from_space=other.space if other is not None else None,
        typical_before=typical,
        typical_after=after[0],
        high_before=high,
        high_after=after[1],
    )


# --- text and the response -------------------------------------------------------------


def _amps(value: float) -> str:
    return f"{value:.0f} A"


def explain(move: Move) -> str:
    """One line for a move or a swap."""
    change = (
        f"typical imbalance {_amps(move.typical_before)} → {_amps(move.typical_after)}, "
        f"high {_amps(move.high_before)} → {_amps(move.high_after)}."
    )
    if move.kind == "swap":
        return (
            f"Swap {move.circuit} (space {move.from_space}, {move.from_leg.label}) with "
            f"{move.other} (space {move.other_from_space}, {move.to_leg.label}): {change}"
        )
    return (
        f"Move {move.circuit} (space {move.from_space}, {move.from_leg.label}) to empty "
        f"space {move.to_space} ({move.to_leg.label}): {change}"
    )


def _r(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def mapping_note(analysis: Analysis, *, feeds_over_lugs: bool = False) -> str:
    """What the leg check found, and anything about this panel that weakens it."""
    r = _r(analysis.fit_r)
    if analysis.mapping is MappingCheck.CONFIRMED:
        note = f"Leg assignment matched the panel's own leg currents (r = {r})."
    elif analysis.mapping is MappingCheck.FLIPPED:
        note = (
            "The panel's own leg currents matched the opposite legs "
            f"(r = {r}), so every space's leg is flipped here."
        )
    elif analysis.mapping is MappingCheck.UNCHECKED:
        note = (
            "Leg assignment was not checked: there is not enough history of the panel's own "
            "leg currents. It assumes rows of two spaces alternate between L1 and L2."
        )
    else:
        note = (
            "Leg assignment could not be confirmed against the panel's own leg currents "
            f"(r = {r}). Check it by hand before acting on any suggestion."
        )
    if analysis.lugs_shared:
        note += (
            " This panel's lugs also carry the panel it feeds through its feedthrough, and "
            "the feedthrough currents have no history to subtract (enable the Feedthrough "
            "current A and B sensors)."
        )
    elif feeds_over_lugs:
        note += (
            " Another panel is fed from this panel's lugs: its load shows in the leg "
            "currents but not in these circuits, which can lower the fit."
        )
    return note


def problem_note(analysis: Analysis) -> str | None:
    if analysis.problem is Problem.NO_LAYOUT:
        return "This panel model's space layout is not known, so legs can't be assigned."
    if analysis.problem is Problem.NOT_ENOUGH_HISTORY:
        return (
            f"Only {analysis.periods_used} usable periods of history; "
            f"at least {MIN_FIT_POINTS} are needed."
        )
    if analysis.problem is Problem.SHORT_HISTORY:
        return (
            f"Only {analysis.span_hours:.0f} hours of history were usable; "
            f"suggestions need {analysis.min_hours}."
        )
    return None


def _one(value: float) -> float:
    return round(value, 1)


def _suggestion(move: Move) -> dict[str, Any]:
    out: dict[str, Any] = {
        "text": explain(move),
        "kind": move.kind,
        "circuit": move.circuit,
        "from_space": move.from_space,
        "to_space": move.to_space,
        "typical_after_a": _one(move.typical_after),
        "high_after_a": _one(move.high_after),
    }
    if move.kind == "swap":
        out["other"] = move.other
        out["other_from_space"] = move.other_from_space
    return out


def _listed(item: Listed) -> dict[str, Any]:
    out: dict[str, Any] = {"circuit": item.name, "reason": str(item.reason)}
    if item.note:
        out["note"] = item.note
    return out


def response(analysis: Analysis, days: int, *, feeds_over_lugs: bool = False) -> dict[str, Any]:
    """The action's response: plain values, names and spaces only."""
    mean_a, mean_b = analysis.per_leg_mean
    return {
        "notice": NOTICE,
        "window": {
            "days": days,
            "period": analysis.period,
            "periods_used": analysis.periods_used,
            "span_hours": _one(analysis.span_hours),
        },
        "balanced": analysis.balanced,
        "mapping": str(analysis.mapping),
        "mapping_note": mapping_note(analysis, feeds_over_lugs=feeds_over_lugs),
        "imbalance": {
            "typical_a": _one(analysis.typical),
            "high_a": _one(analysis.high),
            "heavier": analysis.heavier.label if analysis.heavier is not None else None,
            "mean_l1_a": _one(mean_a),
            "mean_l2_a": _one(mean_b),
        },
        "imbalance_note": IMBALANCE_NOTE,
        "suggestions": [_suggestion(move) for move in analysis.moves],
        "ignored": [_listed(item) for item in analysis.ignored],
        "not_moved": [_listed(item) for item in analysis.not_moved],
        "empty_spaces": list(analysis.empty_spaces),
        "problem": str(analysis.problem) if analysis.problem is not None else None,
        "problem_note": problem_note(analysis),
    }


# --- the button's notification ---------------------------------------------------------

READ_FAILED = (
    "Couldn't read or analyze this panel's recorded history. Check that the recorder is "
    "running, then try again."
)
NO_RESULT = "There's no result for this panel yet."
_DEFAULT_PANEL_NAME = "SPAN panel"
_PERIOD_WORDS = {"hour": "hourly", "5minute": "5-minute"}
_IGNORED_WORDS = {
    Exclusion.TWO_POLE: "two-pole",
    Exclusion.SHARED_SPACE: "shared space",
    Exclusion.NO_SPACES: "no space",
}
_NOT_MOVED_WORDS = {
    Exclusion.PROTECTED: "protected",
    Exclusion.STORAGE_OR_SOLAR: "battery or solar",
    Exclusion.FEEDS_PANEL: "feeds another panel",
    Exclusion.FED_BY_PANEL: "fed by another panel",
    Exclusion.REMOTE_CT: "remote CT",
    Exclusion.LOW_COVERAGE: "low coverage",
}
_MARKDOWN = re.compile(r"([\\`*_{}\[\]<>#|~])")


def notification_title(name_by_user: str | None) -> str:
    """The notification's title: the user's own name for the panel, else a fixed one.

    Never the entry title or the device's default name: a local default names the host.
    """
    return f"Leg balance: {name_by_user or _DEFAULT_PANEL_NAME}"


def _md(text: Any) -> str:
    """A circuit name as literal text in markdown, on one line."""
    flat = str(text).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return _MARKDOWN.sub(r"\\\1", flat)


def _a(value: Any) -> str:
    return f"{float(value):.1f} A"


def _circuits(n: int) -> str:
    return f"{n} circuit" if n == 1 else f"{n} circuits"


def _reasons(items: Any, words: Mapping[Exclusion, str]) -> tuple[int, list[str]]:
    """How many items, and their reasons in a fixed order, each once."""
    present = {item.get("reason") for item in items}
    return len(items), [word for reason, word in words.items() if str(reason) in present]


def _suggestion_line(item: Mapping[str, Any], flipped: bool, typical: Any, high: Any) -> str:
    def where(space: int) -> str:
        return f"space {space} ({leg_of(space, flipped=flipped).label})"

    if item.get("kind") == "swap":
        what = (
            f"Swap **{_md(item['circuit'])}** ({where(item['from_space'])}) with "
            f"**{_md(item['other'])}** ({where(item['other_from_space'])})"
        )
    else:
        what = (
            f"Move **{_md(item['circuit'])}** from {where(item['from_space'])} to empty "
            f"{where(item['to_space'])}"
        )
    return (
        f"{what}: high {_a(high)} → {_a(item['high_after_a'])}, "
        f"typical {_a(typical)} → {_a(item['typical_after_a'])}."
    )


def notification_markdown(response: Mapping[str, Any] | None) -> str:
    """The button's notification, from the action's response; None means the read failed.

    Circuit names and space numbers only, never ids, serials or hosts. The notice is
    always last.
    """
    blocks: list[str] = []
    if response is None:
        blocks.append(READ_FAILED)
    else:
        blocks.extend(_result_blocks(response))
    blocks.append(f"_{NOTICE}_")
    return "\n\n".join(blocks)


def _result_blocks(response: Mapping[str, Any]) -> list[str]:
    problem = response.get("problem")
    note = response.get("problem_note")
    if problem in (Problem.NOT_ENOUGH_HISTORY, Problem.NO_LAYOUT):
        return [note or NO_RESULT]
    blocks: list[str] = []
    if problem == Problem.SHORT_HISTORY and note:
        blocks.append(note)

    window = response["window"]
    period = _PERIOD_WORDS.get(window["period"], str(window["period"]))
    blocks.append(
        f"**Window:** {window['periods_used']} {period} periods over "
        f"{float(window['span_hours']):.1f} h (asked for {window['days']} days)."
    )
    blocks.append(f"**Leg mapping:** {response['mapping']}. {response['mapping_note']}")
    imbalance = response["imbalance"]
    heavier = f", {imbalance['heavier']} heavier" if imbalance.get("heavier") else ""
    blocks.append(
        f"**Imbalance:** typical {_a(imbalance['typical_a'])}, "
        f"high {_a(imbalance['high_a'])}{heavier}."
    )

    suggestions = response.get("suggestions") or []
    if response.get("balanced"):
        blocks.append(
            f"The legs are balanced (high imbalance under {BALANCED_BELOW_A:.1f} A). "
            "No moves suggested."
        )
    elif suggestions:
        flipped = response.get("mapping") == MappingCheck.FLIPPED
        blocks.append(
            "\n".join(
                f"{n}. "
                + _suggestion_line(item, flipped, imbalance["typical_a"], imbalance["high_a"])
                for n, item in enumerate(suggestions, start=1)
            )
        )
    elif problem != Problem.SHORT_HISTORY:
        blocks.append("No move clears the minimum gain.")

    parts = []
    ignored, why = _reasons(response.get("ignored") or [], _IGNORED_WORDS)
    if ignored:
        parts.append(f"Ignored: {_circuits(ignored)} ({', '.join(why)}).")
    held, why = _reasons(response.get("not_moved") or [], _NOT_MOVED_WORDS)
    if held:
        parts.append(f"Not moved: {_circuits(held)} ({', '.join(why)}).")
    if parts:
        blocks.append(" ".join(parts))
    return blocks


def use_short_term(hourly: Analysis) -> bool:
    """Whether the hourly history is too short, so 5-minute statistics are read."""
    return hourly.problem is not Problem.NO_LAYOUT and hourly.span_hours < MIN_HOURS - _EPSILON
