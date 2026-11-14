"""Cloud gap backfill: gap records, scheduling, window, allocation and proving writes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from span_client import backfill as bf
from span_client.cloud_history import Bucket, Resolution

T0 = datetime(2026, 3, 8, 9, 0, tzinfo=UTC)
MIN = timedelta(minutes=1)
H = timedelta(hours=1)


def ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def bucket(start: datetime, minutes: int, count: int = 1, **values: float) -> Bucket:
    return Bucket(ms(start), ms(start + minutes * MIN), values, count)


def record_with(*spans, row="site_energy", now=None, draw=False):
    rec = bf.new_record()
    for start, end in spans:
        assert bf.add_gap(rec, row, start, end, now=now or end, draw=draw)
    return rec


# --- whether to run --------------------------------------------------------------


def test_runner_only_on_local_first_with_tokens_and_target():
    ok = {"has_tokens": True, "has_target": True, "single_panel_site": True}
    assert bf.should_run("local_first", **ok)
    assert not bf.should_run("local_only", **ok)
    assert not bf.should_run("local_first", **{**ok, "has_tokens": False})
    assert not bf.should_run("local_first", **{**ok, "has_target": False})


def test_runner_never_on_a_multi_panel_site():
    # How the cloud aggregates a site of several panels is unknown.
    assert not bf.should_run(
        "local_first", has_tokens=True, has_target=True, single_panel_site=False
    )


def test_mode_values_match_the_transport_modes():
    from span_client.local.health import Mode

    assert Mode.LOCAL_FIRST.value == "local_first"
    assert Mode.LOCAL_ONLY.value == "local_only"


# --- gap records -------------------------------------------------------------------


def test_short_gap_is_not_recorded():
    rec = bf.new_record()
    assert not bf.add_gap(rec, "site_energy", T0, T0 + timedelta(seconds=299), now=T0 + H)
    assert bf.add_gap(rec, "site_energy", T0, T0 + timedelta(seconds=300), now=T0 + H)


def test_row_without_a_cloud_field_is_not_recorded():
    rec = bf.new_record()
    assert not bf.add_gap(rec, "overhead_energy", T0, T0 + H, now=T0 + H)
    assert bf.gaps(rec) == []


def test_new_gap_shape_and_settle_time():
    rec = record_with((T0, T0 + H))
    (gap,) = bf.gaps(rec)
    assert gap["id"] == f"site_energy|{T0.isoformat()}"
    assert gap["status"] == bf.PENDING
    assert gap["attempts"] == 0
    assert bf.parse_time(gap["next_try"]) == T0 + H + bf.SETTLE
    assert gap["hours"] is None and gap["sum_before"] is None


def test_gaps_of_different_targets_on_one_row_stay_apart():
    rec = bf.new_record()
    assert bf.add_gap(rec, "site_energy", T0, T0 + H, now=T0 + H, target="uid-local")
    assert bf.add_gap(rec, "site_energy", T0, T0 + H, now=T0 + H, target="uid-standin")
    local, standin = bf.gaps(rec)
    assert (local["row"], standin["row"]) == ("site_energy", "site_energy")
    assert bf.target_of(local) == "uid-local" and bf.target_of(standin) == "uid-standin"
    assert local["id"] != standin["id"]


def test_a_written_gap_cuts_only_its_own_target():
    rec = bf.new_record()
    bf.add_gap(rec, "site_energy", T0, T0 + H, now=T0 + H, target="uid-local")
    bf.gaps(rec)[0]["status"] = bf.APPLIED
    assert bf.add_gap(rec, "site_energy", T0, T0 + H, now=T0 + H, target="uid-standin")
    assert not bf.add_gap(rec, "site_energy", T0, T0 + H, now=T0 + H, target="uid-local")


def test_a_gap_stored_without_a_target_belongs_to_its_row():
    assert bf.target_of({"row": "pv_energy"}) == "pv_energy"


def test_overlapping_gaps_of_a_row_merge():
    rec = record_with((T0, T0 + H), (T0 + 30 * MIN, T0 + 2 * H))
    (gap,) = bf.gaps(rec)
    assert bf.span(gap) == (T0, T0 + 2 * H)
    assert gap["id"].endswith(T0.isoformat())
    assert bf.parse_time(gap["next_try"]) == T0 + 2 * H + bf.SETTLE


def test_gaps_of_different_rows_do_not_merge():
    rec = record_with((T0, T0 + H))
    bf.add_gap(rec, "pv_energy", T0, T0 + H, now=T0 + H)
    assert len(bf.gaps(rec)) == 2


def test_disjoint_gaps_stay_apart():
    rec = record_with((T0, T0 + H), (T0 + 2 * H, T0 + 3 * H))
    assert len(bf.gaps(rec)) == 2


def test_merge_keeps_draw_only_if_both_had_it():
    rec = bf.new_record()
    bf.add_gap(rec, "site_energy", T0, T0 + H, now=T0 + H, draw=True)
    bf.add_gap(rec, "site_energy", T0 + H, T0 + 2 * H, now=T0 + 2 * H, draw=False)
    (gap,) = bf.gaps(rec)
    assert gap["draw"] is False


def test_part_already_fetched_is_not_recorded_again():
    rec = record_with((T0, T0 + H))
    bf.gaps(rec)[0]["status"] = bf.QUEUED
    assert bf.add_gap(rec, "site_energy", T0 + 30 * MIN, T0 + 2 * H, now=T0 + 2 * H)
    assert bf.span(bf.gaps(rec)[1]) == (T0 + H, T0 + 2 * H)
    assert not bf.add_gap(rec, "site_energy", T0 + 10 * MIN, T0 + 50 * MIN, now=T0 + 2 * H)


def test_gap_older_than_seven_days_is_clipped():
    now = T0 + timedelta(days=10)
    rec = bf.new_record()
    assert bf.add_gap(rec, "site_energy", T0, now - H, now=now)
    assert bf.span(bf.gaps(rec)[0])[0] == now - bf.MAX_AGE


def test_gap_wholly_older_than_seven_days_is_dropped():
    now = T0 + timedelta(days=10)
    rec = bf.new_record()
    assert not bf.add_gap(rec, "site_energy", T0, T0 + H, now=now)


def test_tidy_clips_then_expires_pending_gaps():
    rec = record_with((T0, T0 + 2 * H))
    assert bf.tidy(rec, T0 + bf.MAX_AGE + H)
    (gap,) = bf.gaps(rec)
    assert gap["status"] == bf.PENDING
    assert bf.span(gap)[0] == T0 + H
    assert bf.tidy(rec, T0 + bf.MAX_AGE + 2 * H)
    assert gap["status"] == bf.EXPIRED


def test_tidy_does_not_age_out_fetched_gaps():
    rec = record_with((T0, T0 + H))
    bf.gaps(rec)[0]["status"] = bf.READY
    assert not bf.tidy(rec, T0 + timedelta(days=30))
    assert bf.gaps(rec)[0]["status"] == bf.READY


def test_tidy_prunes_settled_gaps_after_seven_days():
    rec = record_with((T0, T0 + H))
    gap = bf.gaps(rec)[0]
    bf._settle(gap, bf.FAILED, T0 + 2 * H)
    assert not bf.tidy(rec, T0 + 2 * H + bf.KEEP_SETTLED - MIN)
    assert bf.tidy(rec, T0 + 2 * H + bf.KEEP_SETTLED)
    assert bf.gaps(rec) == []


def test_counts():
    rec = record_with((T0, T0 + H), (T0 + 3 * H, T0 + 4 * H))
    bf.gaps(rec)[0]["status"] = bf.QUEUED
    assert bf.counts(rec) == {bf.QUEUED: 1, bf.PENDING: 1}


# --- retries -----------------------------------------------------------------------


def test_retry_delays_then_failed():
    rec = record_with((T0, T0 + H))
    gap = bf.gaps(rec)[0]
    now = T0 + 3 * H
    for delay in bf.RETRY_DELAYS:
        bf.fail(gap, now, bf.NO_DATA)
        assert gap["status"] == bf.PENDING
        assert bf.parse_time(gap["next_try"]) == now + delay
    bf.fail(gap, now, bf.NO_DATA)
    assert gap["status"] == bf.FAILED
    assert gap["attempts"] == len(bf.RETRY_DELAYS) + 1
    assert gap["reason"] == bf.NO_DATA


def test_give_up_settles_at_once_and_is_pruned_later():
    rec = record_with((T0, T0 + H))
    gap = bf.gaps(rec)[0]
    now = T0 + 3 * H
    bf.give_up(gap, now, bf.NO_STATISTICS)
    assert gap["status"] == bf.FAILED
    assert gap["reason"] == bf.NO_STATISTICS
    assert gap["attempts"] == 0
    assert bf.tidy(rec, now + bf.KEEP_SETTLED)
    assert bf.gaps(rec) == []


# --- scheduling ---------------------------------------------------------------------


def test_plan_waits_for_settle():
    rec = record_with((T0, T0 + H))
    settle = T0 + H + bf.SETTLE
    p = bf.plan(rec, now=settle - MIN, cloud_shown=False)
    assert p.fetch == () and p.wake_at == settle
    p = bf.plan(rec, now=settle, cloud_shown=False)
    assert p.fetch == (bf.gaps(rec)[0]["id"],)


def test_plan_fetches_nothing_while_cloud_is_shown():
    rec = record_with((T0, T0 + H))
    p = bf.plan(rec, now=T0 + 5 * H, cloud_shown=True)
    assert p.fetch == () and p.wake_at is None


def test_plan_rate_limit():
    rec = record_with((T0, T0 + H))
    now = T0 + 5 * H
    rec["last_request"] = bf.iso(now - 5 * MIN)
    p = bf.plan(rec, now=now, cloud_shown=False)
    assert p.fetch == () and p.wake_at == now + 10 * MIN
    p = bf.plan(rec, now=now + 10 * MIN, cloud_shown=False)
    assert len(p.fetch) == 1


def test_plan_one_request_for_every_due_gap():
    rec = record_with((T0, T0 + H))
    bf.add_gap(rec, "pv_energy", T0, T0 + H, now=T0 + H)
    p = bf.plan(rec, now=T0 + 5 * H, cloud_shown=False)
    assert len(p.fetch) == 2 and p.wake_at is None


def test_plan_wakes_for_a_later_gap_no_sooner_than_the_rate_limit():
    rec = record_with((T0, T0 + H), (T0 + 2 * H, T0 + 4 * H))
    now = T0 + H + bf.SETTLE
    p = bf.plan(rec, now=now, cloud_shown=False)
    assert len(p.fetch) == 1
    assert p.wake_at == T0 + 4 * H + bf.SETTLE


def test_plan_writes_ready_gaps_even_while_cloud_is_shown():
    rec = record_with((T0, T0 + H))
    bf.gaps(rec)[0]["status"] = bf.READY
    p = bf.plan(rec, now=T0 + 5 * H, cloud_shown=True)
    assert p.write == (bf.gaps(rec)[0]["id"],)


def test_plan_empty():
    p = bf.plan(bf.new_record(), now=T0, cloud_shown=False)
    assert p == bf.Plan((), (), None)


# --- window ------------------------------------------------------------------------


def test_window_short_gap_is_quarter_hourly_and_padded():
    res, start, end = bf.window([(T0 + 7 * MIN, T0 + 20 * MIN)])
    assert res is Resolution.QUARTER_HOURLY
    assert end == ms(T0 + 30 * MIN)
    assert end - start >= 7_200_000
    assert start % 900_000 == 0


def test_window_up_to_a_day_is_quarter_hourly():
    res, start, end = bf.window([(T0, T0 + 23 * H)])
    assert res is Resolution.QUARTER_HOURLY
    assert (start, end) == (ms(T0), ms(T0 + 23 * H))


def test_window_over_a_day_is_hourly():
    res, start, end = bf.window([(T0 + 10 * MIN, T0 + 30 * H + 5 * MIN)])
    assert res is Resolution.HOURLY
    assert (start, end) == (ms(T0), ms(T0 + 31 * H))


def test_window_covers_every_span():
    res, start, end = bf.window([(T0 + 2 * H, T0 + 3 * H), (T0, T0 + 30 * MIN)])
    assert res is Resolution.QUARTER_HOURLY
    assert start <= ms(T0) and end >= ms(T0 + 3 * H)


def test_window_needs_a_span():
    with pytest.raises(ValueError):
        bf.window([])


# --- allocation ----------------------------------------------------------------------


HOME = bf.ROW_FIELDS["site_energy"]
SOLAR = bf.ROW_FIELDS["pv_energy"]


def test_row_fields_map_through_site_flow_energy():
    assert HOME == ("home_import",)
    assert SOLAR == ("solar_to_grid", "solar_to_home", "solar_to_battery")
    assert bf.ROW_FIELDS["battery_energy"] == ("battery_import",)


def test_allocate_whole_buckets_by_hour():
    buckets = [bucket(T0 + i * 15 * MIN, 15, home_import=0.25) for i in range(8)]
    assert bf.allocate(buckets, T0, T0 + 2 * H, HOME) == [
        (T0, pytest.approx(1.0)),
        (T0 + H, pytest.approx(1.0)),
    ]


def test_allocate_prorates_edge_buckets():
    buckets = [bucket(T0, 15, home_import=0.3), bucket(T0 + 15 * MIN, 15, home_import=0.3)]
    # Gap is [T0+5, T0+20): two thirds of the first, one third of the second.
    out = bf.allocate(buckets, T0 + 5 * MIN, T0 + 20 * MIN, HOME)
    assert out == [(T0, pytest.approx(0.2 + 0.1))]


def test_allocate_splits_an_hourly_bucket_across_utc_hours():
    # A bucket starting half past (a half-hour time zone) spreads over two hours.
    b = bucket(T0 + 30 * MIN, 60, home_import=1.0)
    out = bf.allocate([b], T0, T0 + 2 * H, HOME)
    assert out == [(T0, pytest.approx(0.5)), (T0 + H, pytest.approx(0.5))]


def test_allocate_sums_components_and_drops_zero_hours():
    buckets = [
        bucket(T0, 60, solar_to_grid=0.5, solar_to_home=0.25, solar_to_battery=0.25),
        bucket(T0 + H, 60, home_import=1.0),
    ]
    assert bf.allocate(buckets, T0, T0 + 2 * H, SOLAR) == [(T0, pytest.approx(1.0))]


def test_allocate_missing_component_is_zero():
    assert bf.allocate([bucket(T0, 60, home_import=1.0)], T0, T0 + H, SOLAR) == []


def test_allocate_ignores_buckets_outside_the_gap():
    buckets = [bucket(T0 - H, 60, home_import=5.0), bucket(T0, 60, home_import=1.0)]
    assert bf.allocate(buckets, T0, T0 + H, HOME) == [(T0, pytest.approx(1.0))]


# --- no data ------------------------------------------------------------------------


def test_no_data_when_a_bucket_is_missing():
    buckets = [bucket(T0, 15, home_import=1.0), bucket(T0 + 30 * MIN, 15, home_import=1.0)]
    assert bf.no_data(buckets, T0, T0 + 45 * MIN, draw=False)


def test_no_data_when_the_end_is_not_covered():
    assert bf.no_data([bucket(T0, 15, home_import=1.0)], T0, T0 + 20 * MIN, draw=False)


def test_no_data_when_a_bucket_has_no_samples():
    assert bf.no_data([bucket(T0, 60, count=0, home_import=1.0)], T0, T0 + H, draw=False)


def test_all_zero_is_no_data_only_with_local_draw():
    buckets = [bucket(T0, 60, grid_import=0.0)]
    assert bf.no_data(buckets, T0, T0 + H, draw=True)
    assert not bf.no_data(buckets, T0, T0 + H, draw=False)


def test_covered_and_nonzero_is_data():
    buckets = [bucket(T0, 60, home_import=1.0)]
    assert not bf.no_data(buckets, T0 + 5 * MIN, T0 + 50 * MIN, draw=True)


# --- fetched -------------------------------------------------------------------------


def test_fetched_makes_the_gap_ready():
    rec = record_with((T0, T0 + 2 * H))
    gap = bf.gaps(rec)[0]
    buckets = [bucket(T0, 60, home_import=1.0), bucket(T0 + H, 60, home_import=2.0)]
    assert bf.fetched(gap, buckets, now=T0 + 5 * H)
    assert gap["status"] == bf.READY
    assert bf.hours_of(gap) == [(T0, 1.0), (T0 + H, 2.0)]


def test_fetched_without_data_is_a_failed_attempt():
    rec = record_with((T0, T0 + 2 * H))
    gap = bf.gaps(rec)[0]
    assert not bf.fetched(gap, [bucket(T0, 60, home_import=1.0)], now=T0 + 5 * H)
    assert gap["status"] == bf.PENDING and gap["attempts"] == 1
    assert gap["reason"] == bf.NO_DATA


def test_fetched_with_nothing_used_is_applied_without_writing():
    rec = record_with((T0, T0 + H), row="battery_energy")
    gap = bf.gaps(rec)[0]
    assert not bf.fetched(gap, [bucket(T0, 60, home_import=1.0)], now=T0 + 5 * H)
    assert gap["status"] == bf.APPLIED
    assert gap["hours"] is None


# --- proving writes --------------------------------------------------------------------


def ready_gap(hours):
    rec = record_with((T0, T0 + 3 * H))
    gap = bf.gaps(rec)[0]
    gap["hours"] = [[bf.iso(h), k] for h, k in hours]
    gap["status"] = bf.READY
    return gap


HOURS = [(T0, 1.0), (T0 + H, 2.0), (T0 + 2 * H, 4.0)]
ROWS = [(T0 - 2 * H, 8.0), (T0 - H, 10.0), (T0, 10.5), (T0 + H, 11.0), (T0 + 2 * H, 11.5)]


def risen(j):
    """ROWS after the first `j` adjustments of HOURS."""
    return [(t, s + bf.expected_rise(HOURS, t, j)) for t, s in ROWS]


def test_snapshot_keeps_the_row_before_and_every_gap_hour():
    gap = ready_gap(HOURS)
    assert bf.read_window(gap) == (T0 - timedelta(days=1), T0 + 3 * H)
    snap = bf.snapshot(gap, ROWS)
    assert list(snap) == [bf.iso(t) for t, _ in ROWS[1:]]


def test_one_gap_per_statistic_per_batch():
    rec = bf.new_record()
    now = T0 + 6 * H
    assert bf.add_gap(rec, "site_energy", T0 + 3 * H, T0 + 4 * H, now=now, target="uid-a")
    assert bf.add_gap(rec, "site_energy", T0, T0 + H, now=now, target="uid-a")
    assert bf.add_gap(rec, "site_energy", T0, T0 + H, now=now, target="uid-b")
    later_a, early_a, only_b = bf.gaps(rec)
    for gap in bf.gaps(rec):
        gap["status"] = bf.READY
    # The oldest gap of each statistic; the other waits for the next run.
    assert bf.ready_to_write(rec) == [early_a, only_b]
    early_a["status"] = bf.QUEUED
    # Nothing more on a statistic while one of its gaps is still queued.
    assert bf.ready_to_write(rec) == [only_b]
    early_a["status"] = bf.APPLIED
    assert bf.ready_to_write(rec) == [only_b, later_a]


def test_snapshot_needs_a_compiled_row():
    gap = ready_gap(HOURS)
    assert bf.snapshot(gap, ROWS[:2]) is None


def test_queue_records_sums():
    gap = ready_gap(HOURS)
    bf.queue(gap, bf.snapshot(gap, ROWS))
    assert gap["status"] == bf.QUEUED
    assert gap["sum_before"][bf.iso(T0)] == 10.5


@pytest.mark.parametrize("j", [0, 1, 2, 3])
def test_classify_finds_the_applied_prefix(j):
    gap = ready_gap(HOURS)
    bf.queue(gap, bf.snapshot(gap, ROWS))
    assert bf.classify(gap, risen(j)) == bf.Verdict(j, 3)


def test_classify_any_other_pattern_is_none():
    gap = ready_gap(HOURS)
    bf.queue(gap, bf.snapshot(gap, ROWS))
    rows = risen(3)
    rows[1] = (rows[1][0], rows[1][1] + 0.5)  # the row before the gap moved
    assert bf.classify(gap, rows).applied is None
    rows = risen(3)
    rows[3] = (rows[3][0], rows[3][1] + 1.0)
    assert bf.classify(gap, rows).applied is None


def test_classify_missing_row_is_none():
    gap = ready_gap(HOURS)
    bf.queue(gap, bf.snapshot(gap, ROWS))
    assert bf.classify(gap, risen(3)[:-1]).applied is None


def test_missing_hour_row_lands_in_the_next_one():
    # No row was compiled for T0+1h; its energy shows in the T0+2h row.
    rows = [r for r in ROWS if r[0] != T0 + H]
    gap = ready_gap(HOURS)
    bf.queue(gap, bf.snapshot(gap, rows))
    after = [(t, s + bf.expected_rise(HOURS, t, 3)) for t, s in rows]
    assert after[-1][1] == pytest.approx(11.5 + 7.0)
    assert bf.classify(gap, after) == bf.Verdict(3, 3)


def test_settle_applied():
    gap = ready_gap(HOURS)
    bf.queue(gap, {})
    bf.settle_queued(gap, bf.Verdict(3, 3), now=T0, at_setup=False)
    assert gap["status"] == bf.APPLIED


def test_settle_changed_since_is_final():
    gap = ready_gap(HOURS)
    bf.queue(gap, {})
    bf.settle_queued(gap, bf.Verdict(None, 3), now=T0, at_setup=False)
    assert gap["status"] == bf.CHANGED_SINCE
    bf.settle_queued(gap, bf.Verdict(3, 3), now=T0, at_setup=True)
    assert gap["status"] == bf.CHANGED_SINCE


def test_partial_prefix_waits_during_a_run():
    gap = ready_gap(HOURS)
    bf.queue(gap, {})
    bf.settle_queued(gap, bf.Verdict(1, 3), now=T0, at_setup=False)
    assert gap["status"] == bf.QUEUED


def test_partial_prefix_is_never_written_again_in_the_same_session():
    rec = record_with((T0, T0 + 3 * H))
    gap = bf.gaps(rec)[0]
    gap["hours"] = [[bf.iso(h), k] for h, k in HOURS]
    gap["status"] = bf.READY
    bf.queue(gap, bf.snapshot(gap, ROWS))
    later = T0 + 6 * H
    for minutes in (0, 15, 60, 360):
        now = later + timedelta(minutes=minutes)
        bf.settle_queued(gap, bf.Verdict(1, 3), now=now, at_setup=False)
        assert gap["status"] == bf.QUEUED
        assert bf.plan(rec, now=now, cloud_shown=False) == bf.Plan((), (), None)
        # The same stretch reported again is cut off, not recorded afresh.
        assert not bf.add_gap(rec, "site_energy", T0, T0 + 3 * H, now=now)
    assert bf.hours_of(gap) == HOURS


@pytest.mark.parametrize(("j", "left"), [(0, 3), (2, 1)])
def test_partial_prefix_at_setup_requeues_only_the_rest(j, left):
    gap = ready_gap(HOURS)
    bf.queue(gap, {"x": 1.0})
    bf.settle_queued(gap, bf.Verdict(j, 3), now=T0, at_setup=True)
    assert gap["status"] == bf.READY
    assert gap["sum_before"] is None
    assert bf.hours_of(gap) == HOURS[j:]
    assert len(bf.hours_of(gap)) == left


def test_record_round_trips_through_json():
    import json

    rec = record_with((T0, T0 + H))
    gap = bf.gaps(rec)[0]
    bf.fetched(gap, [bucket(T0, 60, home_import=1.0)], now=T0 + 3 * H)
    again = json.loads(json.dumps(rec))
    assert bf.hours_of(bf.gaps(again)[0]) == [(T0, 1.0)]
