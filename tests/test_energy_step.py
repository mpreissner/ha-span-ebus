"""The one-time energy jump: arithmetic, gate, idempotency and the issue."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from span_client import energy_step as es

T0 = datetime(2026, 3, 8, 9, 0, tzinfo=UTC)
EID = "sensor.kitchen_energy"


def record(**lasts):
    return es.new_record(T0, lasts or {EID: 1000.0})


def entity(rec, eid=EID):
    return es.entities(rec)[eid]


# --- expected_step ----------------------------------------------------------------


def test_step_increase():
    assert es.expected_step(1000.0, 1250.0) == pytest.approx(250.0)


def test_step_dip_within_ten_percent_is_negative():
    assert es.expected_step(1000.0, 950.0) == pytest.approx(-50.0)
    assert es.expected_step(1000.0, 900.0) == pytest.approx(-100.0)


def test_step_reset_below_ninety_percent_is_the_new_value():
    assert es.expected_step(1000.0, 899.9) == pytest.approx(899.9)
    assert es.expected_step(1000.0, 3.0) == pytest.approx(3.0)


def test_step_from_zero():
    assert es.expected_step(0.0, 5.0) == pytest.approx(5.0)


# --- bucket ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("when", "bucket"),
    [
        (datetime(2026, 1, 1, 10, 0, tzinfo=UTC), datetime(2026, 1, 1, 10, 0, tzinfo=UTC)),
        (
            datetime(2026, 1, 1, 10, 4, 59, 900000, tzinfo=UTC),
            datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        ),
        (datetime(2026, 1, 1, 10, 5, tzinfo=UTC), datetime(2026, 1, 1, 10, 5, tzinfo=UTC)),
        (datetime(2026, 1, 1, 10, 59, 59, tzinfo=UTC), datetime(2026, 1, 1, 10, 55, tzinfo=UTC)),
    ],
)
def test_floor_bucket(when, bucket):
    assert es.floor_bucket(when) == bucket


def test_floor_bucket_is_utc_across_an_offset_change():
    # A local clock at a half-hour offset, either side of a DST-like change.
    before = datetime(2026, 3, 8, 1, 58, tzinfo=timezone(timedelta(hours=-5, minutes=-30)))
    after = datetime(2026, 3, 8, 3, 2, tzinfo=timezone(timedelta(hours=-4, minutes=-30)))
    assert es.floor_bucket(before) == datetime(2026, 3, 8, 7, 25, tzinfo=UTC)
    assert es.floor_bucket(after) == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
    assert es.floor_bucket(before).tzinfo is UTC


# --- units -----------------------------------------------------------------------


def test_to_kwh():
    assert es.to_kwh("1500", "Wh") == pytest.approx(1.5)
    assert es.to_kwh("2.5", "kWh") == 2.5
    assert es.to_kwh("0.001", "MWh") == pytest.approx(1.0)
    assert es.to_kwh("1", "W") is None
    assert es.to_kwh("unavailable", "kWh") is None
    assert es.to_kwh("unknown", "kWh") is None
    assert es.to_kwh("nan", "kWh") is None
    assert es.to_kwh("1", None) is None


# --- record and first reading ------------------------------------------------------


def test_new_record_skips_entities_with_no_prior_reading():
    rec = es.new_record(T0, {"sensor.a": 10.0, "sensor.b": None})
    assert entity(rec, "sensor.a")["status"] == es.WAITING_FIRST
    assert entity(rec, "sensor.b")["status"] == es.SKIPPED
    assert entity(rec, "sensor.b")["reason"] == es.NO_PRIOR_READING
    assert es.parse_time(rec["t_repoint"]) == T0
    assert rec["issue_raised"] is False


def test_first_local_sets_step_and_bucket():
    rec = record()
    at = datetime(2026, 3, 8, 9, 7, 30, tzinfo=UTC)
    es.first_local(entity(rec), 1250.0, at)
    e = entity(rec)
    assert e["status"] == es.WAITING_STATS
    assert e["step"] == pytest.approx(250.0)
    assert es.parse_time(e["start"]) == datetime(2026, 3, 8, 9, 5, tzinfo=UTC)
    assert es.parse_time(e["first_at"]) == at


def test_first_local_only_once():
    rec = record()
    es.first_local(entity(rec), 1250.0, T0)
    es.first_local(entity(rec), 9999.0, T0 + timedelta(minutes=10))
    assert entity(rec)["first_local"] == 1250.0


def test_the_restored_cloud_total_is_not_the_first_local_reading():
    rec = record()
    assert es.first_local(entity(rec), 1000.0005, T0) is True
    assert entity(rec)["status"] == es.WAITING_FIRST
    assert entity(rec)["start"] is None
    assert es.first_local(entity(rec), 1000.0005, T0) is False
    at = T0 + timedelta(minutes=2)
    assert es.first_local(entity(rec), 1250.0, at) is True
    e = entity(rec)
    assert e["status"] == es.WAITING_STATS
    assert e["first_local"] == 1250.0
    assert e["step"] == pytest.approx(250.0)
    assert es.parse_time(e["first_at"]) == at


def test_only_the_echo_at_the_time_out_settles_none():
    rec = es.new_record(T0, {"sensor.a": 10.0, "sensor.b": 10.0})
    es.first_local(entity(rec, "sensor.a"), 10.0, T0)
    assert es.time_out(rec) == 2
    assert entity(rec, "sensor.a")["status"] == es.NONE
    assert entity(rec, "sensor.b")["reason"] == es.NO_STATISTICS_RECORDED


# --- gate ----------------------------------------------------------------------------


def waiting(step_to=1250.0, at=T0):
    rec = record()
    es.first_local(entity(rec), step_to, at)
    return rec


def rows(*pairs):
    return [(T0 + timedelta(minutes=m), s) for m, s in pairs]


def test_gate_waits_for_the_bucket():
    rec = waiting()
    assert es.gate(entity(rec), rows((-5, 100.0))) is False
    assert entity(rec)["status"] == es.WAITING_STATS


def test_gate_ready_when_the_bucket_holds_the_step():
    rec = waiting()
    assert es.gate(entity(rec), rows((-10, 90.0), (-5, 100.0), (0, 350.0))) is True
    assert entity(rec)["status"] == es.READY
    assert entity(rec)["sum_before"] == 350.0


def test_gate_ready_with_no_earlier_row():
    rec = waiting()
    es.gate(entity(rec), rows((0, 350.0)))
    assert entity(rec)["status"] == es.READY


def test_gate_unexpected_when_the_history_lacks_the_step():
    rec = waiting()
    es.gate(entity(rec), rows((-5, 100.0), (0, 101.0)))
    assert entity(rec)["status"] == es.SKIPPED
    assert entity(rec)["reason"] == es.UNEXPECTED


def test_gate_passes_a_dip():
    rec = waiting(step_to=950.0)
    es.gate(entity(rec), rows((-5, 100.0), (0, 50.0)))
    assert entity(rec)["status"] == es.READY


def test_stats_window():
    rec = waiting()
    start, end = es.stats_window(entity(rec))
    assert (start, end) == (T0 - timedelta(hours=1), T0 + timedelta(minutes=5))


def test_timeout_skips_everything_still_waiting():
    rec = es.new_record(T0, {"sensor.a": 10.0, "sensor.b": 10.0})
    es.first_local(entity(rec, "sensor.b"), 20.0, T0)
    assert not es.timed_out(rec, T0 + timedelta(hours=23, minutes=59))
    assert es.timed_out(rec, T0 + timedelta(hours=24))
    assert es.time_out(rec) == 2
    assert {e["reason"] for e in es.entities(rec).values()} == {es.NO_STATISTICS_RECORDED}


# --- applying once -------------------------------------------------------------------


def ready():
    rec = waiting()
    es.gate(entity(rec), rows((-5, 100.0), (0, 350.0)))
    return rec


def test_read_sum():
    e = entity(ready())
    assert es.read_sum(e, 350.0) == es.NOT_APPLIED
    assert es.read_sum(e, 350.0 + 1e-7) == es.NOT_APPLIED
    assert es.read_sum(e, 100.0) == es.ALREADY_APPLIED
    assert es.read_sum(e, 200.0) == es.CHANGED
    assert es.read_sum(e, None) == es.CHANGED


@pytest.mark.parametrize(
    ("sum_b", "status", "reason"),
    [
        (100.0, es.APPLIED, None),
        (350.0, es.READY, None),
        (123.0, es.SKIPPED, es.CHANGED_SINCE),
    ],
)
def test_reverify_never_requeues(sum_b, status, reason):
    rec = ready()
    entity(rec)["status"] = es.QUEUED
    es.reverify(entity(rec), sum_b)
    assert entity(rec)["status"] == status
    assert entity(rec)["reason"] == reason


def test_reverify_leaves_other_statuses():
    rec = ready()
    es.reverify(entity(rec), 100.0)
    assert entity(rec)["status"] == es.READY


# --- the issue -----------------------------------------------------------------------


def statuses(*names, reported=False):
    rec = es.new_record(T0, {f"sensor.e{i}": 1.0 for i in range(len(names))})
    for i, name in enumerate(names):
        entity(rec, f"sensor.e{i}")["status"] = name
    rec["reported"] = reported
    return rec


@pytest.mark.parametrize(
    ("names", "reported", "action"),
    [
        ((es.WAITING_FIRST, es.READY), False, es.ISSUE_WAIT),
        ((es.WAITING_STATS, es.SKIPPED), False, es.ISSUE_WAIT),
        ((es.READY, es.NONE), False, es.ISSUE_RAISE),
        ((es.SKIPPED, es.NONE), False, es.ISSUE_RAISE),
        ((es.NONE, es.NONE), False, es.ISSUE_DROP),
        ((es.QUEUED, es.SKIPPED), True, es.ISSUE_WAIT),
        ((es.APPLIED, es.SKIPPED), True, es.ISSUE_DROP),
        ((es.READY, es.SKIPPED), True, es.ISSUE_RAISE),  # a lost task comes back
        ((es.APPLIED,), False, es.ISSUE_DROP),
    ],
)
def test_issue_action(names, reported, action):
    assert es.issue_action(statuses(*names, reported=reported)) == action


def test_all_terminal_and_counts():
    rec = statuses(es.APPLIED, es.SKIPPED, es.NONE)
    assert es.all_terminal(rec)
    rec = statuses(es.APPLIED, es.QUEUED)
    assert not es.all_terminal(rec)
    assert es.counts(rec) == {es.APPLIED: 1, es.QUEUED: 1}
    assert es.with_status(rec, es.QUEUED) == ["sensor.e1"]


# --- strings -------------------------------------------------------------------------


def test_every_reason_has_plain_text():
    with open("custom_components/span_ebus/strings.json") as f:
        strings = json.load(f)
    options = strings["selector"]["energy_step_reason"]["options"]
    assert set(options) == {*es.REASONS, "nothing"}
    issue = strings["issues"]["energy_step"]
    assert "{title}" in issue["title"]
    confirm = issue["fix_flow"]["step"]["confirm"]["description"]
    for placeholder in ("{title}", "{hour}", "{entities}", "{skipped}"):
        assert placeholder in confirm
    result = issue["fix_flow"]["step"]["result"]["description"]
    for placeholder in ("{applied}", "{skipped_count}", "{skipped}"):
        assert placeholder in result
