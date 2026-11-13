"""Tests for the ledger that turns SPAN's metered intervals into running totals.

`energy` imports nothing from Home Assistant precisely so this can run: the suite
has no `homeassistant` installed, and the accounting rules — what may be counted
once, twice, or never — are the part of the energy sensors worth pinning down.
"""

import pytest
from energy import EnergyAccumulator, EnergyLedger, IntegratedRun, SourcedRun, pick_restored
from span_client.models import EnergySample

KEY = "circuit-36/power"

# Quarter-hour bucket bounds, in the epoch milliseconds SPAN reports.
Q0 = 1_755_500_000_000
Q1 = Q0 + 900_000
Q2 = Q1 + 900_000
Q3 = Q2 + 900_000


def sample(start, end, kwh, key=KEY):
    return EnergySample(key=key, start_ms=start, end_ms=end, kwh=kwh)


def primed(ledger, key=KEY):
    """Prime a key so later buckets are counted, and report the total is still 0."""
    ledger.apply([sample(Q0, Q1, 0.5, key)])
    assert ledger.total(key) == 0.0


def test_a_new_key_is_primed_without_counting_its_window():
    # The first poll asks for two hours. Booking all of it would put two hours of
    # energy into the state machine the instant the entity appears.
    ledger = EnergyLedger()
    ledger.apply([sample(Q0, Q1, 0.5), sample(Q1, Q2, 0.25)])

    assert ledger.total(KEY) == 0.0


def test_the_bucket_after_the_primed_one_is_counted_whole():
    ledger = EnergyLedger()
    primed(ledger)

    ledger.apply([sample(Q1, Q2, 0.25)])

    assert ledger.total(KEY) == pytest.approx(0.25)


def test_the_open_bucket_contributes_only_its_growth():
    # The newest bucket is still filling: 17:15-17:30 fetched at 17:20 comes back
    # larger at 17:25. Counting it whole each poll would multiply it.
    ledger = EnergyLedger()
    primed(ledger)

    ledger.apply([sample(Q1, Q2, 0.10)])
    ledger.apply([sample(Q1, Q2, 0.18)])
    ledger.apply([sample(Q1, Q2, 0.30)])

    assert ledger.total(KEY) == pytest.approx(0.30)


def test_a_revised_down_bucket_does_not_run_the_meter_backwards():
    # These feed a total_increasing sensor, where any decrease reads as a meter
    # reset and invents a whole new total's worth of consumption.
    ledger = EnergyLedger()
    primed(ledger)
    ledger.apply([sample(Q1, Q2, 0.30)])

    changed = ledger.apply([sample(Q1, Q2, 0.20)])

    assert changed is False
    assert ledger.total(KEY) == pytest.approx(0.30)


def test_replaying_the_same_window_counts_it_once():
    # Every poll re-fetches the last two hours to pick up revisions, so the
    # overlap is the normal case rather than an error case.
    ledger = EnergyLedger()
    primed(ledger)
    window = [sample(Q1, Q2, 0.25), sample(Q2, Q3, 0.40)]

    ledger.apply(window)
    ledger.apply(window)
    ledger.apply(window)

    assert ledger.total(KEY) == pytest.approx(0.65)


def test_a_coarse_bucket_overlapping_counted_time_is_ignored():
    # A backfill at hourly resolution describes the same time as the live
    # quarter-hours. Whichever arrives first claims it; the other is dropped.
    ledger = EnergyLedger()
    primed(ledger)
    ledger.apply([sample(Q1, Q2, 0.25), sample(Q2, Q3, 0.40)])

    changed = ledger.apply([sample(Q0, Q3, 4.0)])

    assert changed is False
    assert ledger.total(KEY) == pytest.approx(0.65)


def test_buckets_are_applied_oldest_first_whatever_order_they_arrive_in():
    ledger = EnergyLedger()
    primed(ledger)

    ledger.apply([sample(Q2, Q3, 0.40), sample(Q1, Q2, 0.25)])

    assert ledger.total(KEY) == pytest.approx(0.65)


def test_an_idle_interval_still_advances_the_mark():
    # A circuit that drew nothing reports a zero bucket. If that left the mark
    # behind, the next poll would count the interval a second time.
    ledger = EnergyLedger()
    primed(ledger)
    ledger.apply([sample(Q1, Q2, 0.0)])

    ledger.apply([sample(Q1, Q2, 0.0), sample(Q2, Q3, 0.40)])

    assert ledger.total(KEY) == pytest.approx(0.40)


def test_keys_are_accounted_for_independently():
    ledger = EnergyLedger()
    primed(ledger, "circuit-36/power")
    primed(ledger, "site/grid_import")

    ledger.apply(
        [
            sample(Q1, Q2, 0.25, "circuit-36/power"),
            sample(Q1, Q2, 3.00, "site/grid_import"),
        ]
    )

    assert ledger.total("circuit-36/power") == pytest.approx(0.25)
    assert ledger.total("site/grid_import") == pytest.approx(3.00)


def test_an_unseen_key_has_no_total_at_all():
    # The sensor is unavailable rather than zero until a figure exists: a
    # confident 0 kWh would be indistinguishable from a real one.
    assert EnergyLedger().total("circuit-99/power") is None


# --- the poll cursor ---------------------------------------------------------


def test_the_cursor_starts_at_zero_and_follows_the_newest_interval():
    ledger = EnergyLedger()
    assert ledger.covered_through_ms == 0

    ledger.apply([sample(Q0, Q1, 0.5)])
    assert ledger.covered_through_ms == Q1


def test_an_idle_key_does_not_hold_the_cursor_back():
    # A circuit that never draws reports no buckets at all. Taking the oldest
    # per-key mark would pin the backfill window at that key forever.
    ledger = EnergyLedger()
    ledger.apply([sample(Q0, Q1, 0.5, "circuit-36/power")])
    ledger.apply([sample(Q2, Q3, 0.5, "circuit-37/power")])

    assert ledger.covered_through_ms == Q3


# --- adoption ----------------------------------------------------------------


def test_an_adopted_total_is_carried_forward_not_restarted():
    # The upgrade path: 0.1.12 integrated energy locally, and the entity hands
    # that total over so its history continues instead of dropping to zero.
    ledger = EnergyLedger()

    assert ledger.adopt(KEY, 12.5) is True
    ledger.apply([sample(Q0, Q1, 0.5)])
    ledger.apply([sample(Q1, Q2, 0.25)])

    assert ledger.total(KEY) == pytest.approx(12.75)


def test_adoption_happens_once_however_often_the_entity_is_re_added():
    ledger = EnergyLedger()
    ledger.adopt(KEY, 12.5)
    ledger.apply([sample(Q0, Q1, 0.5)])
    ledger.apply([sample(Q1, Q2, 0.25)])

    assert ledger.adopt(KEY, 12.5) is False
    assert ledger.total(KEY) == pytest.approx(12.75)


def test_a_metered_key_is_never_rebased_onto_a_restored_total():
    ledger = EnergyLedger()
    ledger.apply([sample(Q0, Q1, 0.5)])

    assert ledger.adopt(KEY, 999.0) is False
    assert ledger.total(KEY) == 0.0


# --- persistence -------------------------------------------------------------


def test_a_reloaded_ledger_resumes_where_it_left_off():
    ledger = EnergyLedger()
    primed(ledger)
    ledger.apply([sample(Q1, Q2, 0.25)])

    reloaded = EnergyLedger()
    reloaded.load(ledger.as_dict())

    assert reloaded.total(KEY) == pytest.approx(0.25)
    assert reloaded.covered_through_ms == Q2
    # And the interval it already counted is not counted again on the next poll.
    reloaded.apply([sample(Q1, Q2, 0.25), sample(Q2, Q3, 0.40)])
    assert reloaded.total(KEY) == pytest.approx(0.65)


def test_a_restored_total_outranks_whatever_an_entity_offers():
    ledger = EnergyLedger()
    primed(ledger)
    ledger.apply([sample(Q1, Q2, 0.25)])

    reloaded = EnergyLedger()
    reloaded.load(ledger.as_dict())

    assert reloaded.adopt(KEY, 12.5) is False
    assert reloaded.total(KEY) == pytest.approx(0.25)


def test_an_open_bucket_is_not_re_counted_across_a_restart():
    # The half-filled bucket is the one most likely to be in flight when Home
    # Assistant stops, so how much of it was counted has to survive too.
    ledger = EnergyLedger()
    primed(ledger)
    ledger.apply([sample(Q1, Q2, 0.10)])

    reloaded = EnergyLedger()
    reloaded.load(ledger.as_dict())
    reloaded.apply([sample(Q1, Q2, 0.18)])

    assert reloaded.total(KEY) == pytest.approx(0.18)


def test_nothing_to_load_leaves_an_empty_ledger():
    ledger = EnergyLedger()
    ledger.load(None)
    ledger.load({})

    assert ledger.as_dict() == {}


def test_an_unreadable_stored_key_is_dropped_rather_than_poisoning_the_rest():
    ledger = EnergyLedger()
    ledger.load(
        {
            "circuit-36/power": {"total": "not a number", "start": Q0, "end": Q1},
            "circuit-37/power": "nonsense",
            "site/grid_import": {"total": 3.0, "start": Q0, "end": Q1, "counted": 3.0},
        }
    )

    assert ledger.total("circuit-36/power") is None
    assert ledger.total("circuit-37/power") is None
    assert ledger.total("site/grid_import") == pytest.approx(3.0)


# --- integrated energy ---------------------------------------------------------
#
# The panel's overhead is derived on this side and has no metered interval to
# read back, so its energy is a sum over its power samples.


def test_a_steady_kilowatt_for_an_hour_is_a_kilowatt_hour():
    acc = EnergyAccumulator(max_gap_seconds=7200.0)
    acc.add(1000.0, 0.0)
    acc.add(1000.0, 3600.0)

    assert acc.total_kwh == pytest.approx(1.0)


def test_the_first_sample_only_sets_a_baseline():
    # There is no interval to close yet, so nothing is added however large it is.
    acc = EnergyAccumulator(max_gap_seconds=300.0)
    acc.add(5000.0, 100.0)

    assert acc.total_kwh == 0.0


def test_a_ramp_is_integrated_as_a_trapezoid_not_a_step():
    # 0 W -> 2000 W over an hour is 1 kWh by area; holding either endpoint would
    # give 0 or 2.
    acc = EnergyAccumulator(max_gap_seconds=7200.0)
    acc.add(0.0, 0.0)
    acc.add(2000.0, 3600.0)

    assert acc.total_kwh == pytest.approx(1.0)


def test_a_negative_sample_cannot_run_the_meter_backwards():
    # A total_increasing sensor that decreases reads as a meter reset.
    acc = EnergyAccumulator(max_gap_seconds=7200.0)
    acc.add(-3000.0, 0.0)
    acc.add(-3000.0, 3600.0)

    assert acc.total_kwh == 0.0


def test_a_gap_longer_than_the_limit_is_not_bridged():
    # The stream was down; there are no readings for that window, and guessing
    # one would write a fabricated step into long-term statistics.
    acc = EnergyAccumulator(max_gap_seconds=300.0)
    acc.add(2000.0, 0.0)
    acc.add(2000.0, 1000.0)

    assert acc.total_kwh == 0.0

    # ...but the sample still becomes the baseline, so accounting resumes at once.
    acc.add(2000.0, 1180.0)
    assert acc.total_kwh == pytest.approx(0.1)


def test_a_minute_between_samples_is_bridged():
    # A panel on its cellular backup publishes once a minute; that cadence is
    # ordinary and has to keep counting.
    acc = EnergyAccumulator(max_gap_seconds=300.0)
    acc.add(12.0, 0.0)
    acc.add(12.0, 60.0)

    assert acc.total_kwh == pytest.approx(0.0002)


def test_a_repeated_sample_adds_nothing_and_keeps_the_baseline():
    # Entities are woken for more than new frames — a relay command re-announces
    # the readings already held — so the same stamp arrives twice routinely.
    acc = EnergyAccumulator(max_gap_seconds=300.0)
    acc.add(1000.0, 100.0)
    acc.add(1000.0, 100.0)
    assert acc.total_kwh == 0.0

    acc.add(1000.0, 50.0)
    assert acc.total_kwh == 0.0

    # The baseline is still the sample at 100, not the stale one at 50.
    acc.add(1000.0, 136.0)
    assert acc.total_kwh == pytest.approx(0.01)


def test_a_restored_total_is_carried_forward_not_restarted():
    acc = EnergyAccumulator(max_gap_seconds=300.0, total_kwh=12.5)
    acc.add(3600.0, 0.0)
    acc.add(3600.0, 10.0)

    assert acc.total_kwh == pytest.approx(12.51)


def test_a_refused_gap_is_reported_with_its_length():
    acc = EnergyAccumulator(max_gap_seconds=300.0)
    assert acc.add(2000.0, 0.0) is None  # baseline
    assert acc.add(2000.0, 300.0) is None  # exactly the limit is bridged
    assert acc.add(2000.0, 1000.0) == pytest.approx(700.0)
    assert acc.add(2000.0, 1000.0) is None  # a repeat is not a gap


def test_an_interrupted_run_only_re_establishes_the_baseline():
    acc = EnergyAccumulator(max_gap_seconds=300.0)
    acc.add(3600.0, 0.0)
    acc.interrupt()
    acc.add(3600.0, 10.0)
    assert acc.total_kwh == 0.0
    acc.add(3600.0, 20.0)
    assert acc.total_kwh == pytest.approx(0.01)


def test_a_missing_reading_breaks_the_run():
    run = IntegratedRun(max_gap_seconds=300.0)
    assert run.sample(3600.0, 0.0) == (True, None)
    assert run.sample(None, 10.0) == (False, None)
    # The ten seconds either side of the missing reading are not credited.
    assert run.sample(3600.0, 20.0) == (True, None)
    assert run.total_kwh == 0.0
    run.sample(3600.0, 30.0)
    assert run.total_kwh == pytest.approx(0.01)


def test_a_long_break_is_reported_as_a_gap():
    run = IntegratedRun(max_gap_seconds=300.0)
    run.sample(1000.0, 0.0)
    run.sample(None, 100.0)
    taken, gap = run.sample(1000.0, 500.0)
    assert taken
    assert gap == pytest.approx(500.0)
    assert run.total_kwh == 0.0


def test_a_long_silence_is_reported_as_a_gap():
    run = IntegratedRun(max_gap_seconds=300.0)
    run.sample(1000.0, 0.0)
    assert run.sample(1000.0, 900.0) == (True, pytest.approx(900.0))
    assert run.total_kwh == 0.0


def test_a_repeated_stamp_is_not_taken():
    run = IntegratedRun(max_gap_seconds=300.0)
    run.sample(1000.0, 10.0)
    assert run.sample(1000.0, 10.0) == (False, None)
    assert run.sample(1000.0, 5.0) == (False, None)


def test_a_run_carries_a_restored_total_and_only_counts_the_positive_part():
    run = IntegratedRun(max_gap_seconds=300.0, total_kwh=4.0)
    run.total_kwh = 5.0
    run.sample(-3600.0, 0.0)
    run.sample(3600.0, 10.0)
    # Trapezoid of max(0, -3600) and 3600 over ten seconds.
    assert run.total_kwh == pytest.approx(5.005)


# --- a run that changes source, and resuming a total -----------------------------


def test_a_failover_is_not_bridged():
    run = SourcedRun(300.0)
    run.sample_from("local", 3600.0, 0.0)
    run.sample_from("local", 3600.0, 10.0)
    assert run.total_kwh == pytest.approx(0.01)
    # The cloud's first reading only sets a baseline: the 20 s the switch took
    # is not credited with the average either side.
    assert run.sample_from("cloud", 3600.0, 30.0) == (True, None)
    assert run.total_kwh == pytest.approx(0.01)
    run.sample_from("cloud", 3600.0, 40.0)
    assert run.total_kwh == pytest.approx(0.02)


def test_a_failback_is_not_bridged():
    run = SourcedRun(300.0)
    run.sample_from("cloud", 3600.0, 0.0)
    run.sample_from("local", 3600.0, 10.0)
    run.sample_from("local", 3600.0, 20.0)
    assert run.total_kwh == pytest.approx(0.01)


def test_offline_between_sources_breaks_the_run_and_reports_a_long_gap():
    run = SourcedRun(300.0)
    run.sample_from("local", 3600.0, 0.0)
    assert run.sample_from("offline", None, 100.0) == (False, None)
    assert run.sample_from("cloud", 3600.0, 400.0) == (True, 400.0)
    assert run.total_kwh == 0.0


def test_a_short_switch_is_not_a_gap():
    # Gaps under the limit are never recorded (a normal failover is not one).
    run = SourcedRun(300.0)
    run.sample_from("local", 1000.0, 0.0)
    assert run.sample_from("cloud", 1000.0, 20.0) == (True, None)


def test_the_same_source_integrates_as_before():
    run = SourcedRun(300.0, total_kwh=1.0)
    run.sample_from("local", 3600.0, 0.0)
    run.sample_from("local", 3600.0, 10.0)
    assert run.total_kwh == pytest.approx(1.01)


@pytest.mark.parametrize(
    ("cache", "recorded", "expected"),
    [
        (1.5, "2.25", 2.25),  # the recorder is ahead after a crash
        (2.5, "2.25", 2.5),  # the cache is ahead
        (2.5, "unknown", 2.5),
        (2.5, "unavailable", 2.5),
        (2.5, "garbage", 2.5),
        (2.5, "nan", 2.5),
        (2.5, "-4", 2.5),
        (2.5, None, 2.5),  # no recorder state
        (None, "3.0", 3.0),  # no cache
        (None, None, 0.0),
        (None, "unknown", 0.0),
        ("unknown", 1.0, 1.0),
        (True, None, 0.0),
    ],
)
def test_pick_restored(cache, recorded, expected):
    assert pick_restored(cache, recorded) == expected
