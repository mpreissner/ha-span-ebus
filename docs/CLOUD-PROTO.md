# SPAN Cloud — Recovered Protobuf Schema

The Ably realtime frames (see [CLOUD-FLOW.md](CLOUD-FLOW.md) §3) are base64
**protobuf** with no public `.proto`. This document is the recovered schema and
the telemetry-decode map — the last blocker for the cloud backend.

## How it was recovered

The SPAN Home Android app (`io.span.android.homeowner`) is React Native compiled
to **Hermes bytecode v96**, using **protobuf-es v1** (Connect-RPC). protobuf-es
does not embed a `FileDescriptorSet`; instead each message registers its schema
at runtime by calling `makeMessageType(typeName, [ {no, name, kind, T, oneof}, … ])`.
Those field literals are stored as Hermes **constant object buffers**, and the
fully-qualified type name is a constant **array buffer** at the end of each
`<Name>$Type` constructor.

Disassembling the bundle (`hbc-disassembler`) resolves both back to plaintext:

```
# Object: {'no': 1, 'name': 'real_power_milliwatts', 'kind': 'scalar', 'T': 17}
…
# Array: ['io.span.traits.metering.common.Power']
```

`tools/extract_proto.py` walks that disassembly, accumulates each function's
`# Object:` field literals, and flushes them under the `# Array:` type name.
Result: **771 messages** across 25 namespaces, every field with its number,
name, kind, scalar type, and `oneof` grouping. The full dump is
[`docs/reference/span-cloud-schema.txt`](reference/span-cloud-schema.txt).

```
python3 tools/extract_proto.py <hbc-disassembly> [name-filter]
```

### What is and isn't recovered

- **Recovered (static, exact):** field numbers, field names, wire kind
  (scalar/message/enum/map/oneof), scalar type + **units** (the SPAN field names
  carry units, e.g. `real_power_milliwatts`), and enough structure to decode any
  frame.
- **Not recovered statically:** for `kind:'message'` / `kind:'enum'` fields, the
  *target* type is a lazy `() => T` closure resolved through module env slots —
  not in the field literal. Cross-references below were resolved by **matching
  the recovered field structure against real decoded frames**
  (`scratchpad/tools/frame_big.txt`), not guessed.

## Telemetry decode path

A realtime frame's inner payload is a tree keyed by **resource id**, carrying
metering traits. The semantic leaf types and their **units**:

### `io.span.traits.metering.common.Power`
| # | field | type | unit |
|---|---|---|---|
| 1 | `real_power_milliwatts` | sint32 | mW (÷1000 → W) |
| 2 | `apparent_power_millivoltamps` | uint32 | mVA |
| 3 | `reactive_power_millivoltampsreactive` | sint32 | mVAR |
| 4 | `true_power_factor_milli` | sint32 | ÷1000 → PF |

### `io.span.traits.metering.common.EnergyAccumulators`
| # | field | type | unit |
|---|---|---|---|
| 1 | `real_energy_lifetime_export_decimilliwatthours` | uint64 | 10⁻⁴ Wh (÷10⁷ → kWh) |
| 2 | `real_energy_lifetime_import_decimilliwatthours` | uint64 | 10⁻⁴ Wh |
| 3 | `apparent_energy_lifetime_export_decimillivoltamphours` | uint64 | 10⁻⁴ VAh |
| 4 | `apparent_energy_lifetime_import_decimillivoltamphours` | uint64 | 10⁻⁴ VAh |

Other scalar leaves: `Current.current_rms_milliamps` (uint32, mA),
`Voltage.voltage_rms_millivolts` (uint32, mV),
`Frequency.line_frequency_millihertz` (uint32, mHz).

### Aggregation containers (field numbers = the split you subscribe to)

`SiteInstantPower` and `SiteEnergy` share the same directional field numbering —
this is confirmed against live frames (`#1 grid`, `#2 home`, `#11 grid_to_home`
all appeared populated):

| # | field |
|---|---|
| 1 | grid |
| 2 | home |
| 3 | solar |
| 4 | battery |
| 5 | generator |
| 6 | balance |
| 7 | residual |
| 11 | grid_to_home |
| 12 | grid_to_battery |
| 21 | solar_to_home |
| 22 | solar_to_battery |
| 23 | solar_to_grid |
| 31 | battery_to_home |
| 32 | battery_to_grid |
| 51 | voltage_l1 *(SiteInstantPower only)* |
| 52 | voltage_l2 |
| 53 | frequency |

`PanelInstant`: `1 feeder, 2 feedthrough, 3 total_branch, 4 balance,
5 panel_in, 6 panel_out, 7 residual, 8 busbar_current, 9 power_balance`.

### The metric envelopes
- `MeterTrait.PowerMetric`: `1 elapsed_since_start_time_millisecs`, `2 metadata`,
  oneof `measurements { 11 single | 12 double | 13 site | 14 panel }`.
- `MeterTrait.EnergyMetric`: `1 elapsed…`, `2 metadata`, `3 stream_id`,
  `4 interval_millisecs`, oneof `accumulations { 11 basic | 12 site | 13 panel }`.
- In `SubscribeAndGetTraitsResponse` / the realtime push, each metric arrives as
  `common.TraitMetric { 1 metric_metadata (MetricInstanceMetadata w/ metric_id),
  2 metric (bytes) }` — **field 2 is the packed sub-message** above.

## One remaining runtime-calibration step

Field numbers and units are exact. The only thing static recovery can't confirm
is **which leaf field the live stream actually populates for each circuit** (the
frame's innermost `{ … }` — whether a given reading lands in `Power.#1` vs a
directional wrapper). This is a single live calibration: subscribe, toggle a
known load, and confirm which field number tracks it. It is a data-labeling
step, not a missing-schema step — the schema itself is fully recovered.

## Frame envelope (positional — Ably push framing, not an `io.span` type)

The outermost frame is Ably-side push framing, decoded positionally:

```
#1  header      { 1:ver 2:? 3:seq 4:? 5:? }
#2  site block  { 1:{1: siteId "<siteId>"}  2:{1: revision} }
#16 payload     { 3:{ 1:kind 2:{1: epoch_millis}  3: <resource-keyed trait tree> } }
```

Inside the trait tree, each entry is `{ 1:{1: resourceId}  … metric messages … }`
— decode the metric messages with the tables above.
