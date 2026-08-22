"""Tests for the ledger that turns SPAN's metered intervals into running totals.

`energy` imports nothing from Home Assistant precisely so this can run: the suite
has no `homeassistant` installed, and the accounting rules — what may be counted
once, twice, or never — are the part of the energy sensors worth pinning down.
"""

import pytest
from energy import EnergyLedger
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
