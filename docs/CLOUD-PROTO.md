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

## The runtime calibration — done (live-validated 2026-08-17)

Field numbers and units are exact, but static recovery cannot say **which leaf
each circuit actually populates**. That calibration is now complete, against a
capture of 12 consecutive frames, and it corrected two readings:

### Which slots a circuit populates depends on its wiring

`InstancePowerStatsSample`: `2 trait_instance_id`, `4 metrics_received_percent`,
oneof `summaries { 11 two_wire | 12 three_wire | 14 panel_power }`.

- **two_wire** (a 120 V branch) carries current and power. Its **voltage slot is
  absent on every sample observed** (184/184) — not zero, absent. Advertising a
  voltage entity for a 120 V branch therefore creates a permanently blank sensor,
  so the backend does not.
- **three_wire** populates `1 line_an, 2 line_bn, 3 combined`. A leg voltage here
  is just another reading of the panel's busbars, so those live on the panel node.
- **panel_power** is **not** a `SingleChannelMeterPower` despite sharing the
  oneof — see below.

### `panel_power` (#14) layout

Field numbers line up with the statically recovered `PanelInstant` above
(`1 feeder, 2 feedthrough, 3 total_branch, 4 balance`), and `feeder` turns out to
be a full `DoubleChannelMeterPower` with frequency appended:

```
1  feeder      { 1: line_an, 2: line_bn, 3: combined, 4: { 3: frequency_mHz } }
2  feedthrough { … }         same shape, all-zero on a panel with no DER
3  total_branch { 2: { 3: power_mW } }
4  balance      { 2: { 3: power_mW } }   ~1% below #3
10 { … }                     all-zero
```

The feeder is the authoritative meter: its combined power equals the site `grid`
flow **exactly, frame for frame**. Live values: `line_an` 119.964 V / 9.133 A,
`line_bn` 119.856 V / 10.189 A, combined 239.820 V / 10.189 A / 4066.568 W,
60.038 Hz. An earlier decoder read this message as a `SingleChannelMeterPower`,
which pushed a power value (~4 000 000) into the frequency slot and left panel
current and voltage empty — the panel is in fact the one node with trustworthy
voltage and frequency.

### A present-but-empty slot means zero

proto3 omits a zero scalar but **still emits the message that holds it**. So
"slot present, empty body" = 0, and "slot absent" = not measured. Conflating them
strands a switched-off circuit at its last non-zero reading, which is exactly
what was observed (instance 51 held ~0.12 W across the frames where its power
slot was present and empty). `cloud_telemetry._leaf_value` keeps the two apart.

### Combined current is the larger leg, not the sum

On the panel feeder and on three-wire circuits alike, `combined.current` equals
whichever of `line_an` / `line_bn` is higher — on every frame, in both directions
(sometimes L1, sometimes L2). That is the right convention for a series path
through a two-pole breaker, and it makes the figure **busbar loading** (compare it
against the main breaker) rather than a total. Per-leg current is populated
separately and is what shows whether the panel is balanced, so the backend
publishes `panel/current_l1` and `panel/current_l2` alongside it. Per-leg
**power** is never populated — only `combined` carries power.

### Combined voltage is unreliable; per-leg voltage is not

The `combined` slot's voltage equals `line_an + line_bn` on most frames but reads
a frame-wide **0.88×** or **0.99×** of that on others — identically for every
circuit in the frame, regardless of load, so it is frame-level staleness rather
than a measurement. The per-leg voltages are steady in every frame. Publish those
and ignore combined voltage.

### What the frames carry

Content frames are `kind=5`, one per second. Only three site flows actually
arrive (`grid`, `grid_to_home`, `home`); 51/52/53 never did, and flows are
advertised only when present, so no blank site entities are created. Envelope
body fields 4 and 5 remain undecoded. In the capture, the first four frames after
the subscribe carried no push envelope at all — which is why the config flow's
probe allows a generous timeout.

Circuit **identity** — which physical circuit a `trait_instance_id` is — is not in
the telemetry stream at all; it comes from the trait snapshot
([CLOUD-FLOW.md §3f](CLOUD-FLOW.md#3f-the-trait-snapshot--circuit-identity-live-validated-2026-08-17)).

## Frame envelope (positional — Ably push framing, not an `io.span` type)

The outermost frame is Ably-side push framing, decoded positionally:

```
#1  header      { 1:ver 2:? 3:seq 4:? 5:? }
#2  site block  { 1:{1: siteId "<siteId>"}  2:{1: revision} }
#16 payload     { 3:{ 1:kind 2:{1: epoch_millis}  3: <resource-keyed trait tree> } }
```

Inside the trait tree, each entry is `{ 1:{1: resourceId}  … metric messages … }`
— decode the metric messages with the tables above.
