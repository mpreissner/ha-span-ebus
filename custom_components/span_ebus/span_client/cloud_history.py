"""SPAN's own energy meters, read back as kilowatt-hours.

The realtime channel carries instantaneous power and nothing else — no counter,
no interval total — so for a long time the only energy this integration could
offer was a Riemann sum over the frames. SPAN measures energy itself, though: the
app's usage screens are drawn from one RPC on the same mobilefrontend service,

    /io.span.services.mobilefrontend.MobileFrontendService/GetHistoryAggregation

which answers with the panel's metered kWh, bucketed at a resolution the caller
picks. That is what this module builds and reads, so the energy sensors can
report what the meter says instead of what an integrator inferred.

Request:

    GetHistoryAggregationRequest {
      1 time_window : TimeWindow {
          1 start_time { 1 epoch_millis }
          2 end_time   { 1 epoch_millis }
          3 resolution_level
          4 time_zone (IANA name)
        }
      2 resources[] : ResourceIdWithMetricIdentifiers {
          1 resource_id : ResourceId { 1 id }
          2 metrics_identifiers[] : MetricInstanceMetadata {
              1 trait_metadata { 1 vendor_id, 3 trait_id, 4 version }
              2 instance_id    { 1 id }
              3 metric_id      (a bare varint)
            }
        }
    }

Response:

    GetHistoryAggregationResponse {
      1 aggregations[] : ResourceHistoryAggregation {
          1 resource_id
          2 aggregations[] : TraitHistoryAggregation {
              1 metric_identifier   (the identifier from the request, echoed)
              2 measurements[]      one Measurement per bucket
              3 summary             one Measurement over the whole window
            }
        }
    }

    Measurement {
      1 start_time { 1 epoch_millis }   2 end_time { 1 epoch_millis }
      3 basic : BasicMeasurement  (oneof)   4 site : SiteMeasurement  (oneof)
      6 count   7 resolution_level   8 first_data_time   9 last_data_time
    }

Three details cost real time to find and are easy to get wrong again:

* **`metric_id` (#3) is a bare varint**, not a message. Encoded any other way
  the server answers "Metric ID must be provided." Its *value* is ignored — the
  server echoes its own — but it has to be there.
* **Times are milliseconds.** Sending seconds is not rejected outright; the
  server just computes a window microscopically short and complains that it is
  under the minimum for the resolution, which is how the unit was pinned down.
* **The instance ids are the telemetry instance ids.** A circuit publishing
  power as instance 36 is metered as instance 36 on the panel's resource; the
  site's aggregate flows live on the *site* resource under a single instance of
  their own, named in every realtime frame's envelope.

Field names and numbers come from the schema recovered from the SPAN Home app
(docs/reference/span-cloud-schema.txt); the layout above was confirmed against
live responses. This module is Home Assistant-free and knows nothing about
entities: it builds bytes and returns numbers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum

from .cloud_pb import Message, field_message, field_string, field_varint, parse

# The metering trait, as declared by the realtime frames that carry power for
# these same instances: vendor 1 (SPAN), trait 26, version 2. `product_id` (#2)
# is part of the message but the server does not mind it missing.
VENDOR_SPAN = 1
TRAIT_METERING = 26
TRAIT_VERSION = 2

# `metric_id` must be present and is otherwise ignored — see the module docstring.
METRIC_ID = 1

# The RPC we speak.
METHOD = "GetHistoryAggregation"


class Resolution(IntEnum):
    """Bucket size, and the minimum window the server accepts for each.

    Levels 5 and 7 exist in the enum but the server rejects them as
    UNRECOGNIZED. A window shorter than the minimum is refused with the
    requirement quoted back, e.g. "Invalid time range for minutely: 1h < 1h".
    """

    MINUTELY = 1
    QUARTER_HOURLY = 2
    HOURLY = 3
    DAILY = 4
    MONTHLY = 6


# Shortest window each resolution will answer for, in seconds.
MIN_WINDOW_SECONDS: dict[Resolution, int] = {
    Resolution.MINUTELY: 3_600,
    Resolution.QUARTER_HOURLY: 7_200,
    Resolution.HOURLY: 7_200,
    Resolution.DAILY: 86_400,
    Resolution.MONTHLY: 28 * 86_400,
}

# BasicMeasurement (#3): one circuit's metered energy. Fields 3-5 are the
# grid/battery/solar percentages of that energy and are not energy themselves.
IMPORT = "import"
EXPORT = "export"
_BASIC_FIELDS = {1: IMPORT, 2: EXPORT}

# SiteMeasurement (#4): the site's directional energy, one field per flow.
_SITE_FIELDS = {
    1: "grid_import",
    2: "grid_export",
    3: "home_import",
    4: "home_export",
    5: "battery_import",
    6: "battery_export",
    7: "solar_to_grid",
    8: "solar_to_home",
    9: "solar_to_battery",
    10: "battery_to_home",
    11: "grid_to_home",
}

# Realtime site flow (cloud_telemetry.SITE_FLOWS) -> the SiteMeasurement
# component(s) that meter it, so a power flow and its energy carry the same name.
#
# A signed flow maps to its *import* half: `site/grid` is positive when the house
# draws from the grid, and consumption is the quantity an energy sensor is asked
# for. The reverse direction is metered separately (`grid_export`) and would be
# its own entity if SPAN published a power flow for it.
#
# `solar` is the one derived entry: SiteMeasurement has no total-production
# field, but production is exactly what left the array, so the three
# `solar_to_*` components sum to it.
SITE_FLOW_ENERGY: dict[str, tuple[str, ...]] = {
    "grid": ("grid_import",),
    "home": ("home_import",),
    "battery": ("battery_import",),
    "solar": ("solar_to_grid", "solar_to_home", "solar_to_battery"),
    "grid_to_home": ("grid_to_home",),
    "battery_to_home": ("battery_to_home",),
    "solar_to_home": ("solar_to_home",),
    "solar_to_grid": ("solar_to_grid",),
    "solar_to_battery": ("solar_to_battery",),
}


@dataclass(frozen=True)
class Bucket:
    """One measurement interval: `[start_ms, end_ms)` and what was metered in it.

    `values` is keyed by component name — `import`/`export` for a circuit,
    `grid_import`/`grid_to_home`/… for the site — in kilowatt-hours. `count` is
    how many underlying samples the bucket aggregated; zero means the interval
    exists but the panel reported nothing for it.
    """

    start_ms: int
    end_ms: int
    values: Mapping[str, float]
    count: int = 0

    def total(self, components: Sequence[str]) -> float | None:
        """Sum `components`, or None if the bucket carries none of them."""
        parts = [self.values[name] for name in components if name in self.values]
        return sum(parts) if parts else None


@dataclass(frozen=True)
class Series:
    """Every bucket returned for one metric instance, plus the window summary."""

    resource_id: str
    instance_id: int
    buckets: tuple[Bucket, ...] = ()
    summary: Bucket | None = None


# --- window planning ----------------------------------------------------------

# The live poll: 15-minutely, the finest resolution whose minimum window is
# still small enough to ask for every minute. The window is two hours and a bit
# rather than the bare two-hour minimum, because it is also the repair window —
# anything shorter than this much downtime is made up by the next ordinary poll,
# with no special case at all.
LIVE_RESOLUTION = Resolution.QUARTER_HOURLY
LIVE_WINDOW_SECONDS = 7_500

# Past that, one hourly pass over the gap. Hourly and coarser lag about an hour
# behind real time, which is what rules them out for the live poll and rules
# them in here; the cap keeps a long outage from asking for a year.
BACKFILL_RESOLUTION = Resolution.HOURLY
MAX_BACKFILL_SECONDS = 7 * 86_400

# Bucket size per resolution, for aligning a window start to a boundary. Daily
# and monthly are absent on purpose: their boundaries are the time zone's, not
# arithmetic.
_ALIGN_SECONDS: dict[Resolution, int] = {
    Resolution.MINUTELY: 60,
    Resolution.QUARTER_HOURLY: 900,
    Resolution.HOURLY: 3_600,
}


def align_start(ms: int, resolution: Resolution) -> int:
    """Round a window start down to a bucket boundary.

    Buckets are cut from the window start, not from the clock, so an unaligned
    request returns quarter-hours running :05, :20, :35. Aligning the start
    makes the two resolutions' boundaries coincide, which is what lets a
    backfill hand over to the live window without a seam.
    """
    step = _ALIGN_SECONDS.get(resolution)
    if not step:
        return ms
    return ms - ms % (step * 1000)


def plan_windows(*, now_ms: int, covered_through_ms: int = 0) -> list[tuple[Resolution, int, int]]:
    """Which requests to make, coarsest first, to cover up to `now_ms`.

    Normally one: the live 15-minutely window. When more than that window has
    gone uncounted — a restart, a long outage — an hourly pass over the gap goes
    first, and the caller must apply the results in this order, since the
    accumulator gives the time to whichever bucket claims it first.

    The hourly window is always at least the server's two-hour minimum, reaching
    further back if the gap is shorter than that. Re-asking for time already
    counted is free; the accumulator ignores it.
    """
    live_start = align_start(now_ms - LIVE_WINDOW_SECONDS * 1000, LIVE_RESOLUTION)
    plans: list[tuple[Resolution, int, int]] = []

    if 0 < covered_through_ms < live_start:
        oldest = max(covered_through_ms, now_ms - MAX_BACKFILL_SECONDS * 1000)
        minimum = MIN_WINDOW_SECONDS[BACKFILL_RESOLUTION] * 1000
        start = min(align_start(oldest, BACKFILL_RESOLUTION), live_start - minimum)
        plans.append((BACKFILL_RESOLUTION, start, live_start))

    plans.append((LIVE_RESOLUTION, live_start, now_ms))
    return plans


def build_request(
    instances: Mapping[str, Iterable[int]],
    *,
    start_ms: int,
    end_ms: int,
    resolution: Resolution,
    time_zone: str,
) -> bytes:
    """A GetHistoryAggregationRequest for every (resource, instance) named.

    `instances` maps a resource id to the metric instances wanted on it; one
    request may carry several resources and many instances each, and the panel's
    27 circuits plus the site aggregate fit comfortably in ~4 kB.

    The caller owns the window. `MIN_WINDOW_SECONDS` says how short the server
    will let it be for the chosen resolution, and `time_zone` is the IANA name
    the day and month boundaries are drawn on — it matters at daily resolution
    and above, and is harmless below.
    """
    trait = (
        field_varint(1, VENDOR_SPAN)
        + field_varint(3, TRAIT_METERING)
        + field_varint(4, TRAIT_VERSION)
    )

    window = (
        field_message(1, field_varint(1, start_ms))
        + field_message(2, field_varint(1, end_ms))
        + field_varint(3, int(resolution))
        + field_string(4, time_zone)
    )

    body = field_message(1, window)
    for resource_id, ids in instances.items():
        identifiers = b"".join(
            field_message(
                2,
                field_message(1, trait)
                + field_message(2, field_varint(1, instance_id))
                + field_varint(3, METRIC_ID),
            )
            for instance_id in ids
        )
        if not identifiers:
            continue
        body += field_message(2, field_message(1, field_string(1, resource_id)) + identifiers)
    return body


def parse_response(raw: bytes) -> list[Series]:
    """Read a GetHistoryAggregationResponse into one `Series` per instance.

    Tolerant by design: an aggregation we cannot identify, or a measurement of a
    kind we do not read, is skipped rather than raised on. The server answers
    only for the identifiers it recognizes, so a series simply being absent is
    normal and means "nothing metered here".
    """
    out: list[Series] = []
    for resource in parse(raw).get_msgs(1):
        resource_id = _resource_id(resource)
        for aggregation in resource.get_msgs(2):
            instance_id = _instance_id(aggregation)
            if instance_id is None:
                continue
            buckets = tuple(
                bucket
                for bucket in (_bucket(m) for m in aggregation.get_msgs(2))
                if bucket is not None
            )
            summary_msg = aggregation.get_msg(3)
            out.append(
                Series(
                    resource_id=resource_id,
                    instance_id=instance_id,
                    buckets=buckets,
                    summary=_bucket(summary_msg) if summary_msg is not None else None,
                )
            )
    return out


def _resource_id(resource: Message) -> str:
    inner = resource.get_msg(1)
    return (inner.get_str(1) or "") if inner is not None else ""


def _instance_id(aggregation: Message) -> int | None:
    """The instance id out of the echoed MetricInstanceMetadata."""
    identifier = aggregation.get_msg(1)
    if identifier is None:
        return None
    instance = identifier.get_msg(2)
    return None if instance is None else instance.get_uint(1)


def _bucket(measurement: Message) -> Bucket | None:
    """One Measurement -> a Bucket, or None if it carries no energy we read."""
    values: dict[str, float] = {}
    basic = measurement.get_msg(3)
    if basic is not None:
        values.update(_doubles(basic, _BASIC_FIELDS))
    site = measurement.get_msg(4)
    if site is not None:
        values.update(_doubles(site, _SITE_FIELDS))
    if not values:
        # A state-of-energy measurement (#5, batteries) or an empty oneof. Not
        # energy, and not something to invent a zero for.
        return None
    return Bucket(
        start_ms=_epoch_millis(measurement, 1),
        end_ms=_epoch_millis(measurement, 2),
        values=values,
        count=measurement.get_uint(6, 0) or 0,
    )


def _doubles(msg: Message, names: Mapping[int, str]) -> dict[str, float]:
    """The named double fields that are present, skipping malformed ones."""
    out: dict[str, float] = {}
    for no, name in names.items():
        if no not in msg:
            continue
        value = msg.get_double(no)
        if value is not None:
            out[name] = value
    return out


def _epoch_millis(msg: Message, no: int) -> int:
    """A `Timestamp { 1 epoch_millis }` field, or 0 if absent."""
    inner = msg.get_msg(no)
    return 0 if inner is None else (inner.get_uint(1, 0) or 0)
