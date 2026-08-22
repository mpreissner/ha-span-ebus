# Spec — measured energy (SPAN's own kWh, not ours)

**Status:** implemented. Decoded and live-validated against a production
account (read-only RPCs). Supersedes the locally integrated energy shipped in
0.1.12.
**Scope:** replace the Riemann-integrated kWh companions with the energy SPAN's
panel actually meters, keeping the same entities so history carries over.

## 1. Why

The realtime channel publishes instantaneous power and nothing else, so 0.1.12
integrated it: a trapezoidal sum over the frames, per watt property
(`energy.EnergyAccumulator`). It works, but it is a derived figure — it drifts
from what the SPAN app shows, it loses whatever happens while Home Assistant is
down, and it starts from zero on a fresh install.

SPAN meters energy itself. The app's usage screens are drawn from one RPC on the
same mobilefrontend service the integration already speaks, and it answers with
per-circuit and site-level kilowatt-hours, bucketed at a resolution the caller
picks. Nothing else changes: same auth, same host, same client.

## 2. The RPC

```
/io.span.services.mobilefrontend.MobileFrontendService/GetHistoryAggregation
```

Request and response layout are documented in full in
`span_client/cloud_history.py`; the short version:

```
Request  { 1 TimeWindow{ 1 start{1 ms}, 2 end{1 ms}, 3 resolution, 4 tz },
           2 resources[]{ 1 resource_id{1 id}, 2 identifiers[]{ 1 trait_metadata,
                                                                2 instance{1 id},
                                                                3 metric_id } } }
Response { 1 per-resource[]{ 1 resource_id,
                             2 per-instance[]{ 1 identifier,
                                               2 measurements[],   # one per bucket
                                               3 summary } } }
```

Three things cost time to find and are easy to get wrong again:

* `metric_id` (#3) is a **bare varint**. Encoded as a message — which is what the
  schema's `MetricInstanceMetadata` suggests — the server answers *"Metric ID
  must be provided."* Its value is ignored; it is echoed back normalized.
* Times are **milliseconds**. Seconds are not rejected outright: the server
  computes a window a few seconds long and complains that it is under the
  minimum for the resolution, which is how the unit was pinned down.
* The instance ids are the **telemetry instance ids**. A circuit publishing power
  as instance 36 is metered as instance 36, on the panel's resource id. Both
  resource ids come out of the realtime frames themselves.

### Resolutions

| level | bucket | minimum window | freshness |
|---|---|---|---|
| 1 | minutely | 1 h | current minute, partial |
| 2 | 15-minutely | 2 h | current quarter, partial |
| 3 | hourly | 2 h | lags ~1 h |
| 4 | daily | 1 d | lags ~1 h |
| 6 | monthly | 28 d | lags ~1 h |

Levels 5 and 7 are rejected as UNRECOGNIZED. A window under the minimum is
refused with the requirement quoted back (*"Invalid time range for 15-minutely:
1h 59m < 2h"*). Buckets are aligned to the **window start**, not to the calendar
— a "monthly" request from the 12th buckets the 12th to the 12th — so `tz` only
matters for day and month boundaries, and any bucket may be partial at the live
end.

**Only the two fine resolutions are current.** Hourly and coarser are rolled up
behind by about an hour, which rules them out for a live sensor and rules them in
for backfill.

### What is metered

Live account, one MAIN 40 panel, checked instance by instance:

| resource | instances | measurement |
|---|---|---|
| panel (`ed53…`) | 30–56 — every branch circuit | `BasicMeasurement { 1 import_kWh, 2 export_kWh }` |
| site (`c798…`) | 401 — one, named in every power frame's envelope | `SiteMeasurement { 1 grid_import … 11 grid_to_home }` |

Instances the panel *does* publish power for but does **not** meter: **1** (the
panel's own metering block) and **2** (the main feed). Their energy is the site's
to within a rounding error, but SPAN does not report it as theirs and this
integration will not pretend otherwise — those two nodes lose their energy
companion. An unmetered instance is simply absent from the response; it is not
an error, which is what makes a request built blindly from the frame safe.

## 3. Design

### Which entities

An energy companion exists exactly where SPAN meters something:

| power key | source |
|---|---|
| `circuit-<n>/power` | panel resource, instance `<n>`, `import` |
| `site/grid` | site, `grid_import` |
| `site/home` | site, `home_import` |
| `site/battery` | site, `battery_import` |
| `site/solar` | site, `solar_to_grid + solar_to_home + solar_to_battery` |
| `site/grid_to_home`, `site/battery_to_home`, `site/solar_to_*` | site, the same-named field |

A signed flow maps to its **import** half, because consumption is what an energy
sensor is asked for and a `total_increasing` entity may not run backwards; the
opposite direction is metered separately and would be its own entity if SPAN
published a power flow for it. `site/solar` is the one derived entry —
`SiteMeasurement` has no total-production field, but production is exactly what
left the array.

`panel/power`, `feed-*/power` and the flows SPAN does not meter
(`generator`, `balance`, `residual`, `grid_to_battery`, `battery_to_grid`) get no
energy companion. Unique ids are unchanged (`{serial}_{power_key}_energy`), so
every entity that survives keeps its history.

### Accumulating

Buckets, not totals. The sensor advances by what SPAN metered in each interval:

```
state per key: total_kwh, last_start_ms, last_end_ms, counted_kwh
for each bucket, oldest first, coarsest resolution first:
    same (start, end) as last  -> add value - counted; counted = value
    start >= last_end          -> add value;           remember (start, end, value)
    otherwise (overlap)        -> ignore
```

Two properties fall out of this. The live bucket is **partial** and keeps
growing, so it is counted incrementally rather than waited for — the sensor moves
every poll instead of stepping every 15 minutes. And a bucket the server later
revises downward is ignored rather than subtracted, which a `total_increasing`
sensor requires.

Deltas, not absolute totals, is a deliberate choice: 0.1.12's entities already
carry an integrated total, and rebasing them onto SPAN's lifetime figure would
book the whole difference — hundreds of kWh — as consumption in one hour of the
Energy dashboard. Continuing the same counter with measured increments means the
upgrade is invisible, and every watt-hour added after it is SPAN's own number.

### Polling

* Every **60 s**, 15-minutely over the last 2 h 5 min. ~18 kB and ~0.6 s per
  call for a 40-space panel — an order of magnitude less than the realtime
  stream carries in the same minute.
* The 2 h window is not just the minimum: it is also the repair window. Anything
  short of two hours of downtime — a reload, a restart, a lost connection — is
  re-covered by the next ordinary poll with no special case.
* Past that, one **hourly** pass over the gap (capped at 7 days, ~300 kB) runs
  first, then the ordinary 15-minutely window closes the last hour that hourly
  has not rolled up yet. Coarse-then-fine is why the accumulator ignores
  overlaps rather than trusting arrival order.
* Beyond the cap the gap stays a gap. Inventing energy for a window we did not
  see is the one thing worse than missing it.

### State

The ledger (`total_kwh` and cursor per key) is persisted in a
`helpers.storage.Store` on the config entry, written on a delay so a 60 s poll
does not mean a 60 s disk write. It is the entity's memory: `RestoreSensor` is
kept only to adopt a pre-0.1.13 integrated total the first time a key is seen,
so upgrading continues the counter instead of restarting it at zero.

Energy entities stay available on their last total when a poll fails. A
cumulative total that has stopped advancing is still true — unlike a power
reading, which is why the *stream* liveness rule exists — and blinking them
unavailable would tear a hole in long-term statistics for what may be a
30-second cloud hiccup.

## 4. Validation

Against the live account, read-only, on 2026-08-21:

* 28 series returned for one request naming 29 instances + the site (18 kB).
* Site to date: `grid_import` 317.9979 kWh, `home_import` 301.3086 kWh,
  `grid_to_home` 317.9979 kWh — the same figure from daily, monthly and
  15-minutely requests, which is what proves `summary` is the window total.
* Circuit 36: 0.2818435 kWh on 08-18, 0.2211793 kWh on 08-19.
* Minutely today (1035 buckets, 138 kB) summed to 35.187 kWh against a daily
  bucket of 30.685 kWh for the same day — the ~1 h rollup lag, measured.

## 5. Out of scope

Backfilling history *before* the integration was installed (HA has an
`async_add_external_statistics` path for it, and SPAN keeps months), and the
`soe` measurement (#5) for battery state of charge.
