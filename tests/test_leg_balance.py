"""Leg balance: the pure analysis in span_client.leg_balance."""

import random
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from span_client import leg_balance as lb
from span_client.leg_balance import CircuitIn, Exclusion, Leg, LegHistory, MappingCheck, Problem
from span_client.local import model

from tests.test_local_capture import PANEL, capture_tree

T0 = datetime(2026, 1, 5, tzinfo=UTC)
HOUR = timedelta(hours=1)
FIVE = timedelta(minutes=5)
SPACES = lb.panel_spaces("MAIN_40")


def periods(n: int, step: timedelta = HOUR) -> list[datetime]:
    return [T0 + step * i for i in range(n)]


def wavy(seed: int, low: float, high: float, when: list[datetime]) -> dict[datetime, float]:
    rng = random.Random(seed)  # noqa: S311
    return {t: rng.uniform(low, high) for t in when}


def circuit(name: str, spaces: tuple[int, ...], series, **kw) -> CircuitIn:
    values = {
        "key": f"k-{name}",
        "name": name,
        "spaces": spaces,
        "poles": 2 if len(spaces) == 2 else 1,
        "nominal_voltage": 240.0 if len(spaces) == 2 else 120.0,
        "movable": True,
        "reason": None,
        "mean_w": series,
    }
    values.update(kw)
    return CircuitIn(**values)


def lugs_for(
    circuits: list[CircuitIn],
    when: list[datetime],
    *,
    swap: bool = False,
    period: lb.Period = "hour",
    noise: float = 0.0,
) -> LegHistory:
    """Lug currents as the default mapping predicts them (two-pole load on both legs)."""
    rng = random.Random(99)  # noqa: S311
    a: dict[datetime, float] = {}
    b: dict[datetime, float] = {}
    for t in when:
        sum_a = sum_b = 0.0
        for c in circuits:
            watts = c.mean_w.get(t, 0.0)
            if not c.spaces:
                continue
            if len(c.spaces) == 2 or c.poles == 2 or c.nominal_voltage == 240.0:
                sum_a += watts / 240
                sum_b += watts / 240
            elif lb.leg_of(c.spaces[0]) is Leg.A:
                sum_a += watts / 120
            else:
                sum_b += watts / 120
        if noise:
            sum_a, sum_b = rng.uniform(0, noise), rng.uniform(0, noise)
        a[t], b[t] = (sum_b, sum_a) if swap else (sum_a, sum_b)
    return LegHistory(period=period, mean_a=a, mean_b=b)


def site(n: int = 200, step: timedelta = HOUR) -> list[CircuitIn]:
    """Leg B far heavier: moving its varying circuit, not its steady one, evens it out."""
    when = periods(n, step)
    return [
        circuit("Circuit 3", (11,), wavy(1, 1200, 2400, when)),  # B, 10-20 A
        circuit("Circuit 5", (17,), wavy(2, 120, 600, when)),  # A
        circuit("Circuit 7", (19,), wavy(3, 2400, 3000, when)),  # B, 20-25 A
        circuit("Circuit 9", (21,), wavy(4, 240, 480, when)),  # A
    ]


def analyze(circuits, legs="auto", spaces=SPACES, **kw) -> lb.Analysis:
    if legs == "auto":
        some = next(iter(circuits[0].mean_w), None)
        when = sorted({t for c in circuits for t in c.mean_w})
        step = HOUR if some is None or len(when) < 2 else when[1] - when[0]
        legs = lugs_for(circuits, when, period="hour" if step == HOUR else "5minute")
    return lb.analyze(circuits, legs, spaces, **kw)


# --- 1. legs and layouts -------------------------------------------------------------------


def test_leg_of_rows_of_two_alternate():
    for space in (1, 2, 5, 6, 9, 10):
        assert lb.leg_of(space) is Leg.A
        assert lb.leg_of(space, flipped=True) is Leg.B
    for space in (3, 4, 7, 8, 11, 12):
        assert lb.leg_of(space) is Leg.B
        assert lb.leg_of(space, flipped=True) is Leg.A


def test_panel_spaces():
    assert lb.panel_spaces("MAIN_40") == range(9, 49)
    assert lb.panel_spaces("MLO_48") == range(1, 49)
    assert lb.panel_spaces("SOMETHING_ELSE") is None
    assert lb.panel_spaces(None) is None
    result = analyze(site(), spaces=None)
    assert result.problem is Problem.NO_LAYOUT
    assert result.moves == ()


# --- 2. two-pole is ignored ----------------------------------------------------------------


@pytest.mark.parametrize(
    "shape",
    [
        {"spaces": (23, 25)},
        {"spaces": (23,), "poles": 2, "nominal_voltage": 120.0},
        {"spaces": (23,), "poles": 1, "nominal_voltage": 240.0},
    ],
)
def test_two_pole_changes_nothing(shape):
    base = site()
    when = sorted(base[0].mean_w)
    # Even one drawing only one leg's worth of load: it is left out entirely.
    shape = dict(shape)
    oven = circuit("Circuit 11", shape.pop("spaces"), wavy(7, 0, 4800, when), **shape)
    before = analyze(base, legs=lugs_for(base, when))
    after = analyze([*base, oven], legs=lugs_for(base, when))
    assert (after.typical, after.high, after.per_leg_mean) == (
        before.typical,
        before.high,
        before.per_leg_mean,
    )
    assert after.moves == before.moves
    assert after.ignored == (lb.Listed("Circuit 11", Exclusion.TWO_POLE),)
    for space in oven.spaces:
        assert space not in after.empty_spaces
        assert all(m.to_space != space for m in after.moves)


def test_two_pole_feeding_a_panel_is_noted():
    base = site()
    when = sorted(base[0].mean_w)
    feeder = circuit("Circuit 13", (23, 25), wavy(8, 0, 2000, when), feeds_panel=True)
    result = analyze([*base, feeder])
    assert result.ignored == (lb.Listed("Circuit 13", Exclusion.TWO_POLE, lb.FEEDS_PANEL_NOTE),)
    item = lb.response(result, 28)["ignored"][0]
    assert item == {"circuit": "Circuit 13", "reason": "two_pole", "note": "feeds another panel"}


# --- 3. single spaces and shared spaces ----------------------------------------------------


def test_shared_space_is_ignored_and_not_counted():
    base = site()
    when = sorted(base[0].mean_w)
    left = circuit("Circuit 15", (27,), wavy(9, 600, 1200, when))
    right = circuit("Circuit 16", (27,), wavy(10, 600, 1200, when))
    before = analyze(base, legs=lugs_for(base, when))
    after = analyze([*base, left, right], legs=lugs_for(base, when))
    assert {item.name for item in after.ignored} == {"Circuit 15", "Circuit 16"}
    assert {item.reason for item in after.ignored} == {Exclusion.SHARED_SPACE}
    assert after.per_leg_mean == before.per_leg_mean
    assert 27 not in after.empty_spaces


def test_no_spaces_is_ignored():
    result = analyze([*site(), circuit("Circuit 17", (), {})])
    assert lb.Listed("Circuit 17", Exclusion.NO_SPACES) in result.ignored


def test_single_space_moves_as_one_unit():
    result = analyze(site())
    move = next(m for m in result.moves if m.kind == "move")
    assert move.circuit == "Circuit 3"
    assert move.from_space == 11
    assert lb.leg_of(move.to_space) is Leg.A


# --- 4. counted but never moved ------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        Exclusion.PROTECTED,
        Exclusion.STORAGE_OR_SOLAR,
        Exclusion.FEEDS_PANEL,
        Exclusion.FED_BY_PANEL,
        Exclusion.REMOTE_CT,
        Exclusion.LOW_COVERAGE,
    ],
)
def test_not_moved_still_counts(reason):
    base = site()
    held = [replace(base[0], movable=False, reason=reason), *base[1:]]
    free = analyze(base)
    result = analyze(held)
    assert result.not_moved == (lb.Listed("Circuit 3", reason),)
    assert result.per_leg_mean == free.per_leg_mean
    assert (result.typical, result.high) == (free.typical, free.high)
    assert all("Circuit 3" not in (m.circuit, m.other) for m in result.moves)


# --- 5. history ---------------------------------------------------------------------------


def test_not_enough_history():
    result = analyze(site(47))
    assert result.problem is Problem.NOT_ENOUGH_HISTORY
    assert result.mapping is MappingCheck.UNCHECKED
    assert result.periods_used == 47
    assert result.moves == ()
    assert lb.response(result, 28)["window"]["periods_used"] == 47


def test_short_hourly_history_reads_five_minute_statistics():
    hourly = analyze(site(63))
    assert hourly.span_hours == 63
    assert lb.use_short_term(hourly)

    five = analyze(site(63 * 12, FIVE))
    assert five.period == "5minute"
    assert five.problem is Problem.SHORT_HISTORY
    assert five.mapping is MappingCheck.CONFIRMED
    assert five.fit_r is not None and five.fit_r > 0.99
    assert five.typical > 0 and five.high > 0
    assert five.moves == ()
    out = lb.response(five, 28)
    assert out["problem"] == "short_history"
    assert out["problem_note"] == ("Only 63 hours of history were usable; suggestions need 72.")


def test_long_hourly_history_needs_no_five_minute_read():
    result = analyze(site(72))
    assert result.period == "hour"
    assert not lb.use_short_term(result)
    assert result.problem is None


def test_coverage_counts_from_the_first_lug_period():
    base = site(300)
    when = sorted(base[0].mean_w)
    late = when[100:]  # the circuit's statistics start with the lugs'
    newer = circuit("Circuit 19", (25,), wavy(11, 120, 240, late))
    circuits = [*base, newer]
    result = analyze(circuits, legs=lugs_for(circuits, late))
    assert all(item.name != "Circuit 19" for item in result.not_moved)
    # Without lug history to start from, the same circuit covers only two thirds.
    unchecked = analyze(circuits, legs=None)
    assert lb.Listed("Circuit 19", Exclusion.LOW_COVERAGE) in unchecked.not_moved


def test_a_period_under_80_percent_coverage_is_dropped():
    when = periods(100)
    circuits = [circuit(f"Circuit {i}", (9 + i,), wavy(i, 100, 500, when)) for i in range(19)]
    gap, kept = when[10], when[20]
    # 15 of 19 (79 %) at one period, 16 of 19 (84 %) at another.
    thinned = [
        replace(c, mean_w={t: w for t, w in c.mean_w.items() if not (t == gap and i < 4)})
        for i, c in enumerate(circuits)
    ]
    thinned = [
        replace(c, mean_w={t: w for t, w in c.mean_w.items() if not (t == kept and i < 3)})
        for i, c in enumerate(thinned)
    ]
    result = analyze(thinned, legs=None)
    assert result.periods_used == 99


def test_balanced_still_fills_everything():
    when = periods(100)
    circuits = [
        circuit("Circuit 1", (9,), wavy(1, 100, 500, when)),
        circuit("Circuit 2", (11,), wavy(2, 100, 500, when)),
    ]
    result = analyze(circuits)
    assert result.high < lb.BALANCED_BELOW_A
    assert result.balanced
    assert result.moves == ()
    assert result.mapping is MappingCheck.CONFIRMED
    assert result.fit_r is not None
    assert result.typical > 0
    assert all(mean > 0 for mean in result.per_leg_mean)
    assert lb.response(result, 28)["balanced"] is True


def test_dev_shaped_fixture_confirms_and_negated_flips():
    when = periods(200)
    circuits = [
        circuit(f"Circuit {space}", (space,), wavy(space, 0, 1500, when)) for space in range(17, 41)
    ]
    assert analyze(circuits).mapping is MappingCheck.CONFIRMED
    flipped = analyze(circuits, legs=lugs_for(circuits, when, swap=True))
    assert flipped.mapping is MappingCheck.FLIPPED


def test_a_missing_circuit_hour_counts_zero():
    when = periods(100)
    full = [
        circuit("Circuit 1", (9,), wavy(1, 100, 500, when)),
        circuit("Circuit 2", (11,), wavy(2, 100, 500, when)),
        circuit("Circuit 3", (13,), wavy(3, 100, 500, when)),
        circuit("Circuit 4", (15,), wavy(4, 100, 500, when)),
        circuit("Circuit 5", (17,), wavy(5, 100, 500, when)),
    ]
    gap = when[5]
    holed = [replace(full[0], mean_w={t: w for t, w in full[0].mean_w.items() if t != gap})]
    zeroed = [replace(full[0], mean_w={**full[0].mean_w, gap: 0.0})]
    a = analyze([*holed, *full[1:]], legs=None)
    b = analyze([*zeroed, *full[1:]], legs=None)
    assert a.periods_used == b.periods_used == 100
    assert (a.typical, a.high, a.per_leg_mean) == (b.typical, b.high, b.per_leg_mean)


# --- 6. the metric ------------------------------------------------------------------------


def test_metrics_by_hand():
    typical, high = lb.metrics([1.0, -2.0, 3.0, -10.0])
    assert typical == 2.5
    # p95 of [1, 2, 3, 10]: rank 0.95 * 3 = 2.85, between 3 and 10.
    assert high == pytest.approx(3 + 7 * 0.85)
    assert lb.metrics([]) == (0.0, 0.0)


# --- 7. moves -----------------------------------------------------------------------------


def test_heavy_leg_b_circuit_moves_to_an_empty_leg_a_space():
    circuits = site()
    result = analyze(circuits)
    move = next(m for m in result.moves if m.kind == "move")
    assert (move.circuit, move.from_space, move.from_leg, move.to_leg) == (
        "Circuit 3",
        11,
        Leg.B,
        Leg.A,
    )
    assert move.to_space == 10  # the nearest empty leg-A space
    when = sorted(circuits[0].mean_w)
    by_name = {c.name: c for c in circuits}

    def amps(name, t):
        return by_name[name].mean_w[t] / 120

    diffs = [
        amps("Circuit 3", t) + amps("Circuit 5", t) + amps("Circuit 9", t) - amps("Circuit 7", t)
        for t in when
    ]
    typical, high = lb.metrics(diffs)
    assert move.typical_after == pytest.approx(typical)
    assert move.high_after == pytest.approx(high)
    assert (move.typical_before, move.high_before) == (result.typical, result.high)


def test_swaps_ignore_breaker_ratings():
    when = periods(200)
    circuits = [
        circuit("Circuit 1", (11,), wavy(1, 1800, 2400, when)),  # B, 15-20 A
        circuit("Circuit 2", (9,), wavy(2, 600, 900, when)),  # A, 5-7.5 A
    ]
    steady = circuit(
        "Steady", (12,), dict.fromkeys(when, 1200.0), movable=False, reason=Exclusion.PROTECTED
    )  # B, 10 A
    # Every other space filled, so no move target exists and only a swap can help.
    filler = [
        circuit(
            f"Filler {s}", (s,), dict.fromkeys(when, 0.0), movable=False, reason=Exclusion.PROTECTED
        )
        for s in SPACES
        if s not in (9, 11, 12)
    ]
    result = analyze([*circuits, steady, *filler])
    assert [m.kind for m in result.moves] == ["swap"]
    swap = result.moves[0]
    assert {swap.circuit, swap.other} == {"Circuit 1", "Circuit 2"}
    # The swap is offered however the two breakers are rated; with empty spaces, a move too.
    free = analyze([*circuits, steady])
    assert free.moves[0].kind == "swap"
    assert any(m.kind == "move" and m.circuit == "Circuit 1" for m in free.moves)


def test_a_move_target_is_one_unclaimed_space_on_the_other_leg():
    circuits = site()
    result = analyze(circuits)
    claimed = {s for c in circuits for s in c.spaces}
    for m in (m for m in result.moves if m.kind == "move"):
        assert m.to_space not in claimed
        assert m.to_space in result.empty_spaces
        assert lb.leg_of(m.to_space) is m.to_leg is not m.from_leg


def test_small_gain_gives_nothing():
    result = analyze(site(), min_gain_a=1000)
    assert result.moves == ()
    assert not result.balanced
    balanced = analyze(site(), balanced_below_a=1000)
    assert balanced.balanced
    assert balanced.moves == ()


def test_defaults_are_the_named_constants():
    import inspect

    params = inspect.signature(lb.analyze).parameters
    assert params["min_gain_a"].default == lb.MIN_GAIN_A == 2.0
    assert params["min_gain_pct"].default == lb.MIN_GAIN_PCT == 10.0
    assert params["balanced_below_a"].default == lb.BALANCED_BELOW_A == 5.0
    assert params["top"].default == lb.TOP == 3


def test_ordered_by_high_after_and_top_respected():
    when = periods(200)
    circuits = [
        circuit(f"Circuit {s}", (s,), wavy(s, 600, 1800, when))
        for s in (11, 12, 15, 16, 19, 20)  # all on B
    ]
    result = analyze(circuits)
    assert len(result.moves) == 3
    highs = [m.high_after for m in result.moves]
    assert highs == sorted(highs)
    assert len(analyze(circuits, top=1).moves) == 1


# --- 8. the leg check ---------------------------------------------------------------------


def test_lugs_from_the_default_mapping_confirm_it():
    result = analyze(site())
    assert result.mapping is MappingCheck.CONFIRMED
    assert result.fit_r == pytest.approx(1.0)


def test_swapped_lugs_flip_and_moves_follow():
    circuits = site()
    when = sorted(circuits[0].mean_w)
    result = analyze(circuits, legs=lugs_for(circuits, when, swap=True))
    assert result.mapping is MappingCheck.FLIPPED
    assert result.fit_r == pytest.approx(1.0)
    move = next(m for m in result.moves if m.kind == "move")
    # Space 11 is on A once flipped, so Circuit 3 moves to a flipped-B space.
    assert move.from_leg is Leg.A
    assert lb.leg_of(move.to_space, flipped=True) is Leg.B
    assert "flipped" in lb.response(result, 28)["mapping_note"]


def test_noise_is_unconfirmed_and_moves_are_still_shown():
    circuits = site()
    when = sorted(circuits[0].mean_w)
    result = analyze(circuits, legs=lugs_for(circuits, when, noise=30))
    assert result.mapping is MappingCheck.UNCONFIRMED
    assert result.moves
    assert "Check it by hand" in lb.response(result, 28)["mapping_note"]


def test_no_lug_history_is_unchecked():
    result = analyze(site(), legs=None)
    assert result.mapping is MappingCheck.UNCHECKED
    assert result.fit_r is None
    assert result.moves


def test_small_measured_spread_is_unconfirmed():
    when = periods(200)
    circuits = [
        circuit("Circuit 1", (11,), wavy(1, 1200, 1400, when)),  # 10-11.7 A: under 3 A
        circuit("Circuit 2", (9,), wavy(2, 10, 20, when)),
    ]
    result = analyze(circuits)
    assert result.mapping is MappingCheck.UNCONFIRMED


def test_shared_lugs_are_never_confirmed():
    result = analyze(site(), lugs_shared=True)
    assert result.mapping is MappingCheck.UNCONFIRMED
    assert result.fit_r == pytest.approx(1.0)
    assert "feedthrough" in lb.response(result, 28)["mapping_note"]
    note = lb.response(analyze(site()), 28, feeds_over_lugs=True)["mapping_note"]
    assert "fed from this panel's lugs" in note


# --- 9. explain ---------------------------------------------------------------------------


def _move(**kw) -> lb.Move:
    values = {
        "kind": "move",
        "circuit": "Kitchen",
        "from_space": 14,
        "to_space": 21,
        "from_leg": Leg.B,
        "to_leg": Leg.A,
        "other": None,
        "other_from_space": None,
        "typical_before": 12.1,
        "typical_after": 4.2,
        "high_before": 31.4,
        "high_after": 15.0,
    }
    values.update(kw)
    return lb.Move(**values)


def test_explain_a_move():
    assert lb.explain(_move()) == (
        "Move Kitchen (space 14, L2) to empty space 21 (L1): "
        "typical imbalance 12 A → 4 A, high 31 A → 15 A."
    )


def test_explain_a_swap():
    swap = _move(
        kind="swap",
        to_space=9,
        other="Dryer",
        other_from_space=9,
        from_leg=Leg.A,
        to_leg=Leg.B,
        from_space=17,
    )
    assert lb.explain(swap) == (
        "Swap Kitchen (space 17, L1) with Dryer (space 9, L2): "
        "typical imbalance 12 A → 4 A, high 31 A → 15 A."
    )


# --- 10. the capture ----------------------------------------------------------------------


def test_capture_smoke():
    snapshot = model.build_snapshot(capture_tree(), PANEL)
    assert snapshot.panel is not None
    spaces = lb.panel_spaces(snapshot.panel.model)
    assert spaces == range(9, 49)
    when = periods(100)
    circuits = [
        CircuitIn(
            key=f"c{i}",
            name=model.circuit_label(c),
            spaces=tuple(c.spaces or ()),
            poles=c.poles,
            nominal_voltage=c.nominal_voltage,
            movable=True,
            reason=None,
            mean_w=dict.fromkeys(when, 100.0),
        )
        for i, c in enumerate(snapshot.circuits.values())
    ]
    result = lb.analyze(circuits, None, spaces)
    assert result.empty_spaces == (41, 42, 43, 44, 45, 47)
    two_pole = {
        tuple(c.spaces) for c in circuits if lb.Listed(c.name, Exclusion.TWO_POLE) in result.ignored
    }
    assert two_pole == {(9, 11), (10, 12), (13, 15), (14, 16), (46, 48)}
    assert len(result.ignored) == 5
    singles = [c for c in circuits if len(c.spaces) == 1]
    on_a = sum(1 for c in singles if lb.leg_of(c.spaces[0]) is Leg.A)
    mean_a, mean_b = result.per_leg_mean
    assert mean_a == pytest.approx(on_a * 100 / 120)
    assert mean_b == pytest.approx((len(singles) - on_a) * 100 / 120)


# --- 11. privacy --------------------------------------------------------------------------


def test_response_carries_the_notice_and_no_ids():
    circuits = site()
    out = lb.response(analyze(circuits), 28)
    assert out["notice"] == (
        "Suggestions only. Moving a breaker means working inside the panel. "
        "Have a licensed electrician check and do any move."
    )
    assert out["imbalance_note"] == lb.IMBALANCE_NOTE
    assert out["suggestions"]
    text = repr(out)
    for c in circuits:
        assert c.key not in text
    assert "ebus/" not in text
    assert not re.search(r"\b[0-9a-f]{12,}\b", text)
    assert set(out) == {
        "notice",
        "window",
        "balanced",
        "mapping",
        "mapping_note",
        "imbalance",
        "imbalance_note",
        "suggestions",
        "ignored",
        "not_moved",
        "empty_spaces",
        "problem",
        "problem_note",
    }


def test_every_response_has_the_notice():
    for result in (
        analyze(site(), spaces=None),
        analyze(site(10)),
        analyze(site(63 * 12, FIVE)),
        analyze(site(), balanced_below_a=1000),
    ):
        assert lb.response(result, 28)["notice"] == lb.NOTICE
