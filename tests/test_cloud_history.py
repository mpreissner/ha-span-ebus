"""Tests for the metered-energy RPC: what we ask for, and what we read back.

Responses here are synthesized with the protobuf writer to mirror the layout in
`cloud_history`'s docstring — no captured data is committed. The field numbers,
the millisecond timestamps and the double-encoded kilowatt-hours match real
responses, which were validated against the live service during development.
"""

import struct

import pytest
from span_client import cloud_pb as pb
from span_client.cloud_history import (
    BACKFILL_RESOLUTION,
    LIVE_RESOLUTION,
    LIVE_WINDOW_SECONDS,
    MAX_BACKFILL_SECONDS,
    MIN_WINDOW_SECONDS,
    Resolution,
    align_start,
    build_request,
    parse_response,
    plan_windows,
)

PANEL = "panel-resource"
SITE = "site-resource"

HOUR = 3_600_000
NOW = 1_755_500_000_000


def field_double(no: int, value: float) -> bytes:
    """A 64-bit double field — wire type 1. Only ever read in production."""
    return pb._tag(no, 1) + struct.pack("<d", value)


def timestamp(no: int, ms: int) -> bytes:
    return pb.field_message(no, pb.field_varint(1, ms))


def measurement(start_ms, end_ms, *, basic=None, site=None, count=1) -> bytes:
    out = timestamp(1, start_ms) + timestamp(2, end_ms)
    if basic:
        out += pb.field_message(3, b"".join(field_double(no, v) for no, v in basic.items()))
    if site:
        out += pb.field_message(4, b"".join(field_double(no, v) for no, v in site.items()))
    return out + pb.field_varint(6, count)


def aggregation(instance_id: int, measurements: list[bytes], summary: bytes | None = None) -> bytes:
    identifier = pb.field_message(2, pb.field_varint(1, instance_id))
    out = pb.field_message(1, identifier)
    out += b"".join(pb.field_message(2, m) for m in measurements)
    if summary is not None:
        out += pb.field_message(3, summary)
    return out


def response(resources: dict[str, list[bytes]]) -> bytes:
    return b"".join(
        pb.field_message(
            1,
            pb.field_message(1, pb.field_string(1, resource_id))
            + b"".join(pb.field_message(2, a) for a in aggs),
        )
        for resource_id, aggs in resources.items()
    )


# --- the request --------------------------------------------------------------


def test_the_request_carries_the_window_the_caller_asked_for():
    raw = build_request(
        {PANEL: [36]},
        start_ms=NOW - HOUR,
        end_ms=NOW,
        resolution=Resolution.QUARTER_HOURLY,
        time_zone="America/New_York",
    )

    window = pb.parse(raw).get_msg(1)
    assert window.get_msg(1).get_uint(1) == NOW - HOUR
    assert window.get_msg(2).get_uint(1) == NOW
    assert window.get_uint(3) == 2
    assert window.get_str(4) == "America/New_York"


def test_every_identifier_carries_a_bare_metric_id_varint():
    # Encoded as a message instead, the server answers "Metric ID must be
    # provided." Its value is ignored, but its presence and wire type are not.
    raw = build_request(
        {PANEL: [36]}, start_ms=0, end_ms=HOUR, resolution=Resolution.HOURLY, time_zone="UTC"
    )

    identifier = pb.parse(raw).get_msg(2).get_msg(2)
    assert identifier.get_uint(3) == 1
    assert identifier.get_msg(2).get_uint(1) == 36
    trait = identifier.get_msg(1)
    assert (trait.get_uint(1), trait.get_uint(3), trait.get_uint(4)) == (1, 26, 2)


def test_one_request_covers_several_resources_and_many_instances():
    raw = build_request(
        {PANEL: [30, 31, 36], SITE: [401]},
        start_ms=0,
        end_ms=HOUR,
        resolution=Resolution.HOURLY,
        time_zone="UTC",
    )

    resources = pb.parse(raw).get_msgs(2)
    found = {
        r.get_msg(1).get_str(1): [i.get_msg(2).get_uint(1) for i in r.get_msgs(2)]
        for r in resources
    }
    assert found == {PANEL: [30, 31, 36], SITE: [401]}


def test_a_resource_with_no_instances_is_left_out_entirely():
    # An empty ResourceIdWithMetricIdentifiers is rejected, and a panel can
    # legitimately have nothing wanted on it.
    raw = build_request(
        {PANEL: [], SITE: [401]},
        start_ms=0,
        end_ms=HOUR,
        resolution=Resolution.HOURLY,
        time_zone="UTC",
    )

    resources = pb.parse(raw).get_msgs(2)
    assert [r.get_msg(1).get_str(1) for r in resources] == [SITE]


# --- the response -------------------------------------------------------------


def test_a_circuit_series_reads_back_as_buckets_of_kilowatt_hours():
    raw = response(
        {
            PANEL: [
                aggregation(
                    36,
                    [
                        measurement(NOW, NOW + 900_000, basic={1: 0.2818435, 2: 0.0}),
                        measurement(NOW + 900_000, NOW + 1_800_000, basic={1: 0.2211793, 2: 0.0}),
                    ],
                    summary=measurement(NOW, NOW + 1_800_000, basic={1: 0.5030228}),
                )
            ]
        }
    )

    (series,) = parse_response(raw)

    assert (series.resource_id, series.instance_id) == (PANEL, 36)
    assert [b.start_ms for b in series.buckets] == [NOW, NOW + 900_000]
    assert series.buckets[0].values["import"] == pytest.approx(0.2818435)
    assert series.buckets[0].count == 1
    assert series.summary.values["import"] == pytest.approx(0.5030228)


def test_the_site_series_reads_back_one_value_per_flow():
    raw = response(
        {SITE: [aggregation(401, [measurement(NOW, NOW + HOUR, site={1: 3.5, 3: 3.2, 11: 3.5})])]}
    )

    (series,) = parse_response(raw)

    assert series.buckets[0].values == {
        "grid_import": pytest.approx(3.5),
        "home_import": pytest.approx(3.2),
        "grid_to_home": pytest.approx(3.5),
    }


def test_a_bucket_totals_the_components_it_is_asked_for():
    # `solar` has no field of its own: production is what left the array, so the
    # three solar_to_* components are summed into it.
    raw = response(
        {SITE: [aggregation(401, [measurement(NOW, NOW + HOUR, site={7: 1.0, 8: 2.0, 9: 0.5})])]}
    )

    bucket = parse_response(raw)[0].buckets[0]

    assert bucket.total(("solar_to_grid", "solar_to_home", "solar_to_battery")) == pytest.approx(
        3.5
    )
    assert bucket.total(("grid_import",)) is None


def test_a_measurement_carrying_no_energy_is_dropped_not_zeroed():
    # State-of-energy measurements (#5, batteries) travel in the same field. A
    # zero for them would be a number we never measured.
    raw = response(
        {
            PANEL: [
                aggregation(
                    36,
                    [
                        pb.field_message(5, field_double(1, 0.8)),
                        measurement(NOW, NOW + HOUR, basic={1: 0.25}),
                    ],
                )
            ]
        }
    )

    (series,) = parse_response(raw)

    assert len(series.buckets) == 1
    assert series.buckets[0].values["import"] == pytest.approx(0.25)


def test_an_aggregation_without_an_identifier_is_skipped():
    raw = response({PANEL: [pb.field_message(2, measurement(NOW, NOW + HOUR, basic={1: 9.0}))]})

    assert parse_response(raw) == []


def test_an_empty_response_is_not_an_error():
    # The server answers only for identifiers it recognizes, so asking about an
    # unmetered instance comes back silent rather than failing.
    assert parse_response(b"") == []


# --- window planning ----------------------------------------------------------


def test_a_window_start_is_rounded_down_to_a_bucket_boundary():
    # Buckets are cut from the window start, so an unaligned request returns
    # quarter-hours running :05, :20, :35 and never lines up with a backfill.
    unaligned = NOW + 5 * 60_000 + 1234
    assert align_start(unaligned, Resolution.QUARTER_HOURLY) % 900_000 == 0
    assert align_start(unaligned, Resolution.HOURLY) % HOUR == 0
    # Daily and monthly boundaries belong to the time zone, not to arithmetic.
    assert align_start(unaligned, Resolution.DAILY) == unaligned


def test_the_ordinary_poll_is_one_live_window():
    (plan,) = plan_windows(now_ms=NOW, covered_through_ms=NOW - 600_000)

    resolution, start, end = plan
    assert resolution is LIVE_RESOLUTION
    assert end == NOW
    assert start == align_start(NOW - LIVE_WINDOW_SECONDS * 1000, LIVE_RESOLUTION)


def test_a_first_ever_poll_asks_only_for_the_live_window():
    # Nothing is covered yet, so there is no gap to fill — and the ledger will
    # prime on this window rather than count it.
    assert len(plan_windows(now_ms=NOW, covered_through_ms=0)) == 1


def test_a_long_gap_is_backfilled_hourly_before_the_live_window():
    plans = plan_windows(now_ms=NOW, covered_through_ms=NOW - 3 * 86_400_000)

    (backfill_res, backfill_start, backfill_end), (live_res, live_start, _) = plans
    # Coarsest first: whichever bucket claims a stretch of time keeps it, so the
    # hourly pass must be applied before the quarter-hours refine its tail.
    assert (backfill_res, live_res) == (BACKFILL_RESOLUTION, LIVE_RESOLUTION)
    assert backfill_start == align_start(NOW - 3 * 86_400_000, BACKFILL_RESOLUTION)
    assert backfill_end == live_start


def test_a_short_gap_reaches_backwards_to_meet_the_server_minimum():
    # Hourly refuses a window under two hours. Extending forwards instead would
    # swallow time the live window is about to measure properly.
    gap_start = NOW - 3 * HOUR
    (backfill, live) = plan_windows(now_ms=NOW, covered_through_ms=gap_start)

    _, start, end = backfill
    assert end - start >= MIN_WINDOW_SECONDS[BACKFILL_RESOLUTION] * 1000
    assert start <= gap_start
    assert end == live[1]


def test_an_ancient_cursor_does_not_ask_for_a_year_of_history():
    plans = plan_windows(now_ms=NOW, covered_through_ms=NOW - 400 * 86_400_000)

    _, start, _ = plans[0]
    assert start >= align_start(NOW - MAX_BACKFILL_SECONDS * 1000, BACKFILL_RESOLUTION)
