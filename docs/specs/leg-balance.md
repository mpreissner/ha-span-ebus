# Leg balance: suggest circuit moves between L1 and L2

Status: spec rev 4, local only (never committed). Base: dev @ 6f96d4e (rev 4 addendum at the end).
Rev 2 applies the user's answers to Q1-Q6 (§10): tandems are ordinary single-pole circuits,
two-pole circuits are ignored entirely, there is no rating rule, and the action is the only
presentation. Rev 3 records the confirmed leg mapping (§5.3) and handles short
history (L4).
Author: architect-pr5. Scope: an on-demand, advisory analysis of one panel's leg balance.
No new entities, no unique-id or statistics changes, nothing new written to the recorder.
MemPalace was unavailable (connection failure); this rests on code, captures and memory files.

## 1. The request

> "I'd like to be able to have this integration suggest circuit movements to better balance
> the legs of the service. because we know what position each breaker is in, we know whether
> a circuit is on one phase or another. by comparing historical data, especially when there
> are L1/L2 imbalances, we might be able to suggest relocation of specific circuits to
> better balance it."

The output is advice for a licensed electrician to act on. The integration never moves,
switches or reconfigures anything.

## 2. Evidence

### 2.1 Space to leg (UNVERIFIED for SPAN's r202639 numbering)

- **Convention used by the official integration.** `span-panel-api`
  `phase_validation.get_tab_phase` (SpanPanel/span-panel-api, `src/span_panel_api/phase_validation.py`):
  `row = (space - 1) // 2`; `L1 if row % 2 == 0 else L2`. So 1, 2 → L1; 3, 4 → L2; 5, 6 → L1;
  ... Odd spaces are the left column, even the right, and both columns alternate every row.
  This was written for Gen2 tab numbering (1-32). Nothing in it covers MAIN 40 or MLO 48.
- **Against it.** The official integration's own `helpers.construct_voltage_attribute`
  docstring quotes the eBus spec: `info/spaces` identifies slots "without assuming a
  numbering convention". So reading a leg from a space number is a layout assumption,
  not something the panel publishes.
- **Numbering.** MAIN 40 reports 9-48; spaces 1-8 are the main-breaker module.
  MLO 48 reports 1-48 (memory: panel-space-numbering). If the bus keeps its row pattern
  through the main-breaker module, space 9 is row 4 and sits on **L1**, the same leg as
  1, 2, 5, 6 and 10. Whether it does is UNVERIFIED. If the module shifts the bus by one row,
  every leg flips. That is why §5.4 validates the mapping and §5.5 has the user check it.
- **Capture check** (`tests/fixtures/r202639/capture.json`, MAIN 40, one snapshot).
  - Two-pole circuits sit on 9/11, 10/12, 13/15, 14/16 and 46/48: same column, adjacent rows.
    That matches "legs alternate every row" and rules out "legs alternate by column".
  - Under the convention above, single-pole circuit currents sum to A 14.9 A and B 8.3 A.
    Their power, at 120 V, is 1096 W and 601 W (9.1 A and 5.0 A).
  - The upstream lugs read `current-a` 7.1 A and `current-b` 6.0 A.
  - The sign agrees (A is the heavier leg) but the size doesn't. Circuit power totals 1697 W
    against 1265 W at the lugs, so the topics were not captured at the same instant.
    This is weak support, not proof.
- **Which leg is "L1".** The panel's `meter/current-a` maps to cloud `current_l1` (line_an),
  and `current-b` to `current_l2` (line_bn); see `span_client/identity.py` and `fallback.py`.
  This spec calls the legs **A/B** in code and **L1/L2** in user text, with A = L1.
  That A is the electrician's L1 is UNVERIFIED.

### 2.2 What a circuit gives us

| Fact | Local (`CircuitFacts`) | Cloud (`CircuitInfo`) |
|---|---|---|
| Spaces | `spaces` (`info/spaces`) | `spaces` (displayed numbers) |
| Poles | `poles` (`breaker/poles`) | none; use the number of spaces |
| Rating | `rating` (`breaker/rating`) | `breaker_amps` |
| Nominal voltage | `nominal_voltage` | none |
| Power | `power` sensor, measurement, recorded | power sensor, recorded |
| Current | `current` sensor, disabled by default on new installs | none per circuit |
| Role, links | `role`, `feeds`, `fed_by`, `is_remote_ct` | partial |

- **Two-pole circuits** have `poles == 2` or two spaces. On a split-phase panel they span
  both legs.
- **Tandem breakers** (user, rev 2). SPAN allows them but can't tell their two branches
  apart: a tandem shows up as one circuit in one space. Both branches are on that space's
  leg, so it is an ordinary single-pole circuit that moves as one unit. No capture shows
  two circuits claiming the same space. If the model ever does, both are ignored with the
  reason `shared_space` (defensive only; not expected).
- **Breaker rating** is not used (user, rev 2). It is only the most a circuit may draw;
  suggestions rest on measured usage.
- **Empty spaces** are the panel's range minus every space a circuit claims. The MAIN 40
  capture leaves 41-45 and 47 empty. Whether "unclaimed" always means "no breaker
  installed" is UNVERIFIED; the text calls them "spaces with no circuit reported".

### 2.3 Per-leg measurements

- **Local.** Only the lugs report per-leg values: `upstream_current_a` and
  `upstream_current_b` (`meter/current-a`, `meter/current-b`). They are measurement
  sensors in A, enabled by default and diagnostic, so they have long-term statistics.
  `feedthrough_current_a/b` exist but are disabled by default. There is **no per-leg power**.
  `busbar_current` is a single value.
- **Cloud.** The panel feeder (`DoubleChannelMeterPower`) has line_an and line_bn currents
  and per-leg voltages. Per-leg power is never populated. The combined current is the larger
  leg, not the sum (CLOUD-PROTO.md).
- **Per circuit there is no per-leg value.** For a two-pole circuit with a neutral
  (a range, dryer or sub-panel), the legs can carry different currents, and the panel
  reports one number. Which number that is (the larger leg, a mean, or leg A) is UNVERIFIED.
  Rev 2 sidesteps this: two-pole circuits are left out of the analysis (L2). Most real
  240 V appliances draw only at 240 V even with a neutral wired, and a balanced 240 V draw
  cancels out of A - B.

So per-leg load is **estimated** from circuit power, and **checked** against the lugs'
per-leg current.

### 2.4 History available without writing anything new

- Circuit `power` sensors are recorded measurement sensors, so they already have
  5-minute short-term statistics (kept for about 10 days by default) and hourly long-term
  statistics with mean, min and max (kept forever).
- The same holds for `upstream_current_a/b`.
- The recorder decision (memory: recorder-db-growth-decision) rounds W to whole numbers and
  A to 0.1, and writes only on change. Statistics are time-weighted means over states, so
  rounding to a whole watt costs nothing here.
- `recorder_api.sums` already wraps `statistics_during_period` on the executor. A sibling
  that asks for `{"mean", "max"}` is all the HA layer needs.

## 3. Decisions (recommendation first)

| # | Decision | Why |
|---|---|---|
| L1 | **Leg of a space:** `row = (space - 1) // 2`, leg A when `row` is even, on both MAIN 40 and MLO 48. Space 9 is on L1. **Confirmed on the dev panel (MAIN 40)** against its own lug currents (§5.3). L9 still runs on every call and can flip it for another panel; §5.5 lets the user confirm by hand. | It matches the official integration's convention, the capture's two-pole layout, the user's expectation, and the measured fit (r = 0.99; flipped is about 12x worse). One panel model is confirmed; MLO 48 is unconfirmed, so the self-check stays. |
| L2 | **Two-pole circuits** (`poles == 2`, or two spaces, or nominal 240 V) are **ignored completely**: not in the per-leg prediction, not in the metrics, not in the moves, and their spaces are not offered as targets. They are listed as `ignored: two_pole`. | User, rev 2. A balanced 240 V draw cancels out of A - B, and real 240 V appliances often draw only at 240 V even with a neutral. Counting half on each leg adds nothing to A - B and only pollutes the per-leg means. |
| L3 | **History source: recorder statistics, read on demand.** Hourly means of every circuit `power` statistic and of `upstream_current_a/b`. No buffer of our own, no new entity, no new writes. | Hourly mean is linear, so per-leg sums of circuit means are exact per-leg hourly means. It costs one query per circuit per run. |
| L4 | **Window: the last 28 days by default** (`DEFAULT_DAYS`), settable 7-90 in the service call. The window is only an upper bound: the analysis uses whatever periods exist inside it, and the response says how many it used and over what span. Periods where under 80 % of the counted circuits have data are dropped. Coverage (L8) is measured from the first period with lug data, not from the window start, so recently created statistics don't mark every circuit `low_coverage`. **Short history:** if the hourly data spans less than `MIN_HOURS` (72 h), read 5-minute short-term statistics for the same window (they exist only for the last ~10 days) and use those instead, reported as `period: 5minute`. Then: fewer than `MIN_FIT_POINTS` (48) usable periods → `problem: not_enough_history`, mapping `unchecked`, metrics shown only if any exist. At least 48 periods but a span under 72 h → validation and metrics are shown, no suggestions, `problem: short_history` with the hours used and the 72 h needed. | Four weeks covers weekday/weekend and some weather. 72 hours is the floor for a stable median before anyone is told to move a breaker. The dev panel had only ~63 h of hourly statistics; its 5-minute statistics (754 points over the same span) fitted just as well (§5.3), so validating on short history is safe while suggestions wait. One fallback rule, no blending of the two periods. |
| L5 | **Metric.** Per hour h: `imb_h = |A_h - B_h|` in amps, where `A_h` is the sum over leg-A single-pole circuits of `mean power_h / 120 V` (two-pole circuits ignored, L2). The per-leg means in the response therefore cover single-pole load only, and the response says so. Report and score on two numbers: **typical** = median of `imb_h`, **high** = 95th percentile of `imb_h`. Score = high, then typical as tie-break. | Time above a threshold depends on a threshold the user hasn't given; coincident peak needs sub-hour data the statistics don't keep per leg. p95 is the honest proxy for "when it matters". The per-leg means use nominal 120 V; measured voltage is within about 1 % (capture 119.9/120.0) and the cloud's per-leg voltages are reliable but not needed. |
| L6 | **Moves.** (a) Move one movable single-pole circuit (a tandem counts as one) into an empty space on the other leg. (b) Swap two movable single-pole circuits on opposite legs. (c) Nothing else in v1: no chains, no three-way moves. **No rating rule** for either kind. The only physical rule: a single-pole circuit goes into a single space that no circuit claims. Return the best **three** single moves, each scored independently against today's layout. | User, rev 2: a rating is only the most a circuit may draw; the suggestion rests on measured usage, and the electrician checks wire and breaker. Single moves are explainable; a combined plan is a follow-up (F3). |
| L7 | **Threshold.** Named constants in the pure module: `MIN_GAIN_A = 2.0`, `MIN_GAIN_PCT = 10.0`, `BALANCED_BELOW_A = 5.0`. Suggest a move only if it lowers **high** by at least `max(MIN_GAIN_A, MIN_GAIN_PCT % of high)` and doesn't raise **typical**. If today's **high** is under `BALANCED_BELOW_A`, report `balanced: true` and suggest nothing; the response still carries the window, the mapping verdict with its r, the imbalance metrics and the ignored/not-moved lists. The service call may override the A values. | Avoids churn over an amp or two. The user gave no figure (Q4), so these stay the defaults. |
| L8 | Two groups, each listed in the result with its reason. **Ignored** (left out of everything, L2): two-pole circuits (`two_pole`), circuits sharing a space (`shared_space`, defensive), circuits with no spaces (`no_spaces`). **Counted but never moved** (single-pole, on their leg in the prediction and metrics): circuits in the user's `protected` option; `feeds_storage_or_solar`; single-pole circuits with `feeds` or `fed_by`; `is_remote_ct`; circuits with less than 80 % history coverage (counted where they have data). | Feeds and storage/solar change topology, not load; protected is the user's own "don't touch". The reasons make the result explainable. |
| L9 | **Validation.** Compare `d_h = A_h - B_h` (predicted, A) with `m_h = mean(upstream_current_a)_h - mean(upstream_current_b)_h` (measured). Fit both parities. A parity is **confirmed** when Pearson r >= 0.8 over at least `MIN_FIT_POINTS` (48) periods of the chosen period (L4) and the measured `m_h` spans at least 3 A; the other parity then has r <= -0.8, because flipping the legs negates d. If the default parity fits with r <= -0.8, use the flipped mapping and say so. Otherwise the mapping is **unconfirmed**: suggestions are still shown under a warning. Kept although the dev panel confirmed the default (user, rev 2; §5.3). | Differences cancel balanced 240 V loads and panel overhead that sit equally on both legs, which is also why ignoring two-pole circuits (L2) costs the fit little. A 120/240 V load's unequal split, or a two-pole sub-panel feed, shows up in `m_h` but not `d_h`: that lowers r, and can only push the verdict to unconfirmed, never to a wrong flip unless it dominates. A flip is a sign change, so one fit decides it. |
| L10 | **Presentation: the action only** (rev 4 adds a button and notification, see the addendum), `span_ebus.analyze_leg_balance`, `SupportsResponse.ONLY`. No sensor, no attributes, no Repairs issue, no notification, no button. | User, rev 2. No DB growth (L3) and no nagging. Results go to the caller (Developer Tools > Actions, or a script that writes them to a markdown card). |
| L11 | **Per panel.** One call analyses one panel (one local entry or one device). There is no site-wide balance. | Each panel has its own bus. Its lugs measure that bus, whether it is own-supply, behind DER, or fed from lugs or a breaker (§6). |
| L12 | **Local entries only in v1.** Cloud-only entries return "not supported yet". | Cloud circuits have no poles, protected option or links, and their sensors aren't rounded yet. The pure core takes plain inputs, so cloud is F2. |
| L13 | **Every result says the advice is not an instruction:** "Suggestions only. Moving a breaker means working inside the panel. Have a licensed electrician check and do any move." | The user asked for advice; the work is regulated and dangerous. |

## 4. Module layout

### 4.1 Pure core: `custom_components/span_ebus/span_client/leg_balance.py`

No Home Assistant import (same rule as `span_client/local/commands.py`).
It holds no device ids, serials or hosts. Circuits are keyed by an opaque `key` that the
HA layer chooses, and that key never leaves the HA layer.

```python
class Leg(StrEnum): A = "A"; B = "B"

def leg_of(space: int, *, flipped: bool = False) -> Leg
def panel_spaces(model: str | None) -> range | None      # MAIN_40 -> 9..48, MLO_48 -> 1..48, else None

@dataclass(frozen=True, slots=True)
class CircuitIn:
    key: str                     # opaque, chosen by the caller
    name: str                    # shown to the user
    spaces: tuple[int, ...]
    poles: int | None
    nominal_voltage: float | None    # with poles and spaces, decides two-pole (L2)
    movable: bool                # the HA layer applies L8's option-based rules
    reason: Exclusion | None     # why not movable
    mean_w: Mapping[datetime, float]     # period start (UTC) -> mean W (hour or 5 minutes)

@dataclass(frozen=True, slots=True)
class LegHistory:
    period: Literal["hour", "5minute"]   # L4; the same for every series in one call
    mean_a: Mapping[datetime, float]     # mean upstream_current_a
    mean_b: Mapping[datetime, float]

MIN_GAIN_A = 2.0          # L7
MIN_GAIN_PCT = 10.0
BALANCED_BELOW_A = 5.0
MIN_HOURS = 72            # L4: span needed for suggestions
MIN_FIT_POINTS = 48       # L4/L9: periods needed for validation and metrics
DEFAULT_DAYS = 28
SHORT_TERM_DAYS = 10      # recorder default keep for 5-minute statistics
MIN_COVERAGE = 0.8
FIT_R = 0.8               # L9
FIT_SPREAD_A = 3.0
TOP = 3

class Exclusion(StrEnum):
    # ignored: out of prediction, metrics and moves
    TWO_POLE, SHARED_SPACE, NO_SPACES
    # counted, never moved
    PROTECTED, STORAGE_OR_SOLAR, FEEDS_PANEL, FED_BY_PANEL, REMOTE_CT, LOW_COVERAGE
IGNORED = frozenset({Exclusion.TWO_POLE, Exclusion.SHARED_SPACE, Exclusion.NO_SPACES})

class MappingCheck(StrEnum): CONFIRMED, FLIPPED, UNCONFIRMED, UNCHECKED  # UNCHECKED: no lug history

@dataclass(frozen=True, slots=True)
class Move:
    kind: Literal["move", "swap"]
    circuit: str; from_space: int; to_space: int; from_leg: Leg; to_leg: Leg
    other: str | None; other_from_space: int | None     # swaps only
    typical_before: float; typical_after: float; high_before: float; high_after: float

@dataclass(frozen=True, slots=True)
class Analysis:
    period: str; periods_used: int; span_hours: float
    mapping: MappingCheck; fit_r: float | None
    balanced: bool         # high < balanced_below_a: no moves, everything else filled
    typical: float; high: float; heavier: Leg | None
    per_leg_mean: tuple[float, float]
    moves: tuple[Move, ...]
    ignored: tuple[tuple[str, Exclusion], ...]        # (circuit name, reason in IGNORED)
    not_moved: tuple[tuple[str, Exclusion], ...]      # (circuit name, other reasons)
    empty_spaces: tuple[int, ...]
    problem: str | None    # "not_enough_history", "short_history", "no_layout"; moves empty when set

def analyze(circuits: Sequence[CircuitIn], legs: LegHistory | None, spaces: range,
            *, min_gain_a: float = MIN_GAIN_A, min_gain_pct: float = MIN_GAIN_PCT,
            balanced_below_a: float = BALANCED_BELOW_A, top: int = TOP) -> Analysis
def explain(move: Move) -> str
```

`explain` gives one line, for example:
"Move Kitchen (space 14, L2) to empty space 21 (L1): typical imbalance 12 A → 4 A,
high 31 A → 15 A."

**Steps inside `analyze`:**
1. Drop the ignored circuits (two-pole, shared space, no spaces) into `ignored`. Their
   spaces stay occupied: they are never offered as targets. Assign the rest a leg (L1).
2. Align periods. Keep a period only when at least 80 % of the remaining circuits have a
   value; a circuit missing that period counts 0 W. Fewer than `MIN_FIT_POINTS` → stop
   with `not_enough_history` (still report `periods_used`).
3. Validate (L9). This may set `flipped`.
4. Compute `imb_h`, typical and high (L5). If `high < balanced_below_a`, set `balanced`
   and stop: validation and metrics are already filled. If the span is under
   `MIN_HOURS`, stop with `short_history`, also after filling them.
5. Enumerate moves (L6): every movable circuit × every empty space on the other leg, and
   every pair of movable circuits on opposite legs. No rating filter.
   This is O(movable × (empty + movable)), under 2000 candidates on
   a 48-space panel, each re-scored over at most 2160 hours. Re-score by subtracting the
   moved circuit's per-hour amps from one leg and adding them to the other; no full
   recompute. Keep the moves that pass L7, and return the top `top` by `high_after`.

### 4.2 HA layer

- **`services.py` / `services.yaml`:** register `analyze_leg_balance`, with
  `SupportsResponse.ONLY` and `_pick` reused from `rotate_credentials`.
  - Fields: `config_entry_id` or `device_id` (panel); `days` (7-90, default 28);
    `min_gain_a`; `balanced_below_a`.
  - The service is not admin-only: it is read-only.
- **New `leg_balance.py`** (HA side, thin):
  - Find the entry's circuits and their `power` entity ids through the entity registry,
    by unique id. Find the lugs' `upstream_current_a/b` entities the same way.
  - Read `mean` statistics through a new `recorder_api.means(hass, ids, start, end, period)`.
    It makes one `statistics_during_period` call for all ids, `types={"mean"}`, units W
    and A. First `period="hour"` over the window; if that spans under `MIN_HOURS`, a
    second call with `period="5minute"` over the last `min(days, SHORT_TERM_DAYS)` days
    (L4). At most two reads per call.
  - Build `CircuitIn` from `CircuitFacts` and set `movable`/`reason` from `ControlOptions`
    and the model helpers (L8).
  - Call `analyze` on the executor (it is CPU-only but can take ~100 ms), then shape the
    response (§4.3).
  - A circuit whose power entity isn't recorded (excluded in `recorder:` config) gets
    `LOW_COVERAGE`.
- **Strings:** a service name and description, and one field description per field, in
  `strings.json` and `translations/en.json`, kept identical. The response text (`explain`,
  the warning, the L13 notice) comes from the pure module in English, like the logbook
  lines in `commands.describe`.
- **Logging:** one debug line per call: hour count, mapping verdict, number of moves.
  No names, spaces, ids or hosts.

### 4.3 Response shape

```yaml
notice: "Suggestions only. Moving a breaker means working inside the panel. Have a licensed electrician check and do any move."
window: {days: 28, period: hour, periods_used: 640, span_hours: 668}
balanced: false
mapping: confirmed        # confirmed | flipped | unconfirmed | unchecked
mapping_note: "Leg assignment matched the panel's own leg currents (r = 0.91)."
imbalance: {typical_a: 12.1, high_a: 31.4, heavier: L2, mean_l1_a: 18.0, mean_l2_a: 27.6}
imbalance_note: "Single-pole circuits only; two-pole (240 V) circuits are ignored."
suggestions:
  - text: "Move Kitchen (space 14, L2) to empty space 21 (L1): typical imbalance 12 A → 4 A, high 31 A → 15 A."
    kind: move
    circuit: Kitchen
    from_space: 14
    to_space: 21
    typical_after_a: 4.2
    high_after_a: 15.0
ignored:
  - {circuit: Oven, reason: two_pole}
not_moved:
  - {circuit: Battery, reason: storage_or_solar}
empty_spaces: [41, 42, 43, 44, 45, 47]
problem: null
```

Circuit names and space numbers are fine to show. No device id, serial, host or entity id
appears anywhere in the response.

## 5. Making the leg mapping trustworthy

### 5.1 Default
L1 (row parity). Space 9 is on L1 on a MAIN 40, and space 1 is on L1 on an MLO 48.
The user expects space 9 on L1 (rev 2).

### 5.2 Why not trust it blindly
The eBus spec says space numbers carry no convention (§2.1). The main-breaker module may
or may not shift the bus pattern, and the only capture is one non-simultaneous snapshot.

### 5.3 Data that would settle it without the user
**CONFIRMED on the dev panel (MAIN 40, spaces 9-48)**, 2026-11-15, by team-lead with a
read-only recorder query. Prediction per period: the sum over single-pole circuits of
±mean W / 120 V, legs from L1's rule; two-pole circuits left out (L2). Measured: lug
current L1 − L2.

| Data | Points | r (as-is) | Mean abs. error as-is | Flipped |
|---|---|---|---|---|
| Hourly statistics | 63 (~2.6 days; the statistics are recent) | 0.987 | 0.19 A | 2.49 A |
| 5-minute statistics | 754 | 0.988 | 0.22 A | 2.73 A |

- Layout: single-pole circuits on 17-40; two-pole on 9/11, 10/12, 13/15, 14/16, 46/48.
  Ignoring the two-pole circuits cost the fit nothing measurable.
- This panel's imbalance is small: median |A − B| about 1 A, p95 3.0 A hourly and 4.4 A
  on 5-minute data. Under `BALANCED_BELOW_A` (5 A) the action reports `balanced: true`
  with the metrics and the confirmed mapping, and no suggestions. That is the expected
  outcome here; on 63 h of history it would also be `short_history`.
- Still unverified: MLO 48 and any panel with a different main-breaker module.

### 5.4 Automatic check
L9 runs on every call, and the response says which parity it used and why.

### 5.5 How the user can verify it by hand (no tools, about 5 minutes)
1. Pick a resistive 120 V load of at least 1000 W, such as a space heater or a kettle,
   on a single-pole circuit whose space number you know. Example: space 21.
2. In Home Assistant, open the panel device and add **Upstream current A** and
   **Upstream current B** to a history graph.
3. With the load off, note both values. Switch it on for one minute.
4. The leg whose current rises by about the load's watts ÷ 120 (about 8 A for 1000 W)
   is the leg that space is on.
5. Compare with the rule: on a MAIN 40, spaces 9, 10, 13, 14, 17, 18, 21, 22 ... are
   leg A (L1), and 11, 12, 15, 16, 19, 20, 23, 24 ... are leg B (L2).
   If space 21 moved current B instead, every leg on this panel is flipped.
   Report it: the automatic check should already say "flipped".

Repeat once in the other column (an even space) to rule out column-wise wiring.

## 6. Topology (per panel)

| Supply (`feeds.py`) | What the lugs' A/B measure | Effect |
|---|---|---|
| `own_supply` | This panel's bus, from the utility. | As specified. |
| `behind_der` (the user's site) | This panel's bus, after the solar/battery upstream. | The same. DER upstream doesn't change which circuits load which leg. Advice is still about this bus; the service side may differ, and that's out of scope. |
| `lugs` (fed from another panel's lugs) | This panel's bus. | The same. The upstream panel's own analysis sees this panel's load in its lug currents but not in its circuits. Its validation fit gets worse (L9 may say unconfirmed). Note this in `mapping_note` when this entry is known to feed another panel. |
| `branch` (fed from another panel's two-pole breaker) | This panel's bus. | The same for this panel. On the upstream panel, the feeding breaker is two-pole, so it is ignored (`two_pole`, L2) like any other two-pole circuit. The downstream panel's own imbalance reaches the upstream lugs through it, so it lowers the upstream fit (L9) but never enters the upstream prediction. Its `ignored` entry adds the note "feeds another panel". |

- **A panel with a feedthrough** to a downstream panel: its upstream lugs carry both panels'
  load. If `feedthrough_current_a/b` have statistics (they're disabled by default), validate
  against upstream minus feedthrough. Otherwise validation reports `unconfirmed`, with a note.
- **Multi-panel sites:** each panel is analysed alone. Cross-panel moves are out of scope.

## 7. Privacy

- Shown: circuit names, space numbers, legs, amps.
- Never shown or logged: device ids, serials, hosts, entity ids, unique ids, `location`.
- Test fixtures use invented names ("Circuit 3") and invented spaces.

## 8. Test plan (pure module, no HA)

`tests/span_client/test_leg_balance.py`:

1. **`leg_of`:** 1, 2, 5, 6, 9, 10 → A; 3, 4, 7, 8, 11, 12 → B; `flipped` inverts each.
   **`panel_spaces`:** MAIN_40 → 9..48, MLO_48 → 1..48, unknown → None (`problem: no_layout`).
2. **Two-pole is ignored:** adding a two-pole circuit with any history (even one drawing
   only on one leg's worth) changes neither the per-leg means, `imb_h`, typical, high nor
   the moves. It appears in `ignored` as `two_pole`. Its spaces are never a move target.
   `poles == 2`, two spaces, and nominal 240 V each make a circuit two-pole.
3. **Tandem / single space:** a single-pole circuit in one space moves as one unit.
   Two circuits claiming one space → both in `ignored` as `shared_space`, neither counted.
4. **Not moved:** each counted-but-not-moved reason appears once in `not_moved`, the
   circuit still counts on its leg, and no move names it.
5. **History:**
   - 47 usable periods → `not_enough_history`, mapping `unchecked`, `periods_used: 47`.
   - 63 usable hours (the dev case) → hourly spans under 72 h, so 5-minute data is
     used; validation and metrics are filled, no moves, `short_history`.
   - Hourly spanning ≥ 72 h → no 5-minute read; `period: hour`.
   - A circuit whose statistics start with the lug statistics is not `low_coverage`
     because the window reaches further back.
   - An hour with 79 % coverage is dropped.
   - **Balanced:** a series with high < 5 A gives `balanced: true`, no moves, and still a
     mapping verdict, `fit_r`, typical, high and per-leg means.
   - **Dev-shaped fixture:** synthetic single-pole series on 17-40 whose lug difference
     matches the prediction gives `confirmed`; negating it gives `flipped`.
   - A missing circuit hour counts 0.
6. **Metric:** a hand-built 4-hour series checks typical (median) and high (p95) against
   values computed by hand.
7. **Moves:**
   - A heavy leg-B circuit and an empty leg-A space give that move with the expected
     before/after.
   - A swap between circuits of different ratings (15 A and 20 A) is proposed when it
     scores; a move from a 20 A circuit into any empty space is proposed (no rating rule).
   - A move target is always a single unclaimed space on the other leg.
   - Gain below `MIN_GAIN_A` gives nothing; `high < BALANCED_BELOW_A` gives "balanced".
   - The defaults equal the named constants; overrides from the call are honoured.
   - Results are ordered by `high_after`; `top` is respected.
8. **Validation:**
   - A synthetic lug series generated from the default mapping gives `confirmed`.
   - The same series with A/B swapped gives `flipped`, and the moves follow the flipped legs.
   - Noise gives `unconfirmed` and moves are still returned.
   - No lug history gives `unchecked`.
   - Measured spread under 3 A gives `unconfirmed`.
9. **`explain`:** the exact string for one move and one swap.
10. **Capture smoke test:** build `CircuitIn` from `capture.json` with a flat synthetic
    history. Check that legs and empty spaces (41-45, 47) come out as in §2.1, and that
    the five two-pole circuits (9/11, 10/12, 13/15, 14/16, 46/48) are all `ignored`.
11. **Privacy:** the response dict and `explain` output contain no id-like strings (fixture
    ids, hex, `ebus/`).

HA-layer tests (`tests/test_leg_balance_service.py`):

- The service is registered with `SupportsResponse.ONLY`.
- Picking by entry and by device works.
- A cloud-only entry gives "not supported yet".
- A stubbed `recorder_api.means` feeds the core.
- An unrecorded circuit is marked `low_coverage`.
- Debug-log text is free of names and ids.

## 9. Scope

**v1:** §3 L1-L13, local entries, the action only, single moves and swaps of single-pole
circuits, no rating rule, two-pole circuits ignored.

**Follow-ups:**

- **F2.** Cloud-only entries. They need the poles inferred from the number of spaces, and
  per-leg history from `current_l1/l2`.
- **F3.** A combined plan: the best set of 2-3 moves together, with a greedy pass.
- **F4.** Use 5-minute short-term statistics for a sharper "high" figure even when hourly
  history is long (v1 uses them only as the short-history fallback, L4).
- **F5.** A per-panel space-mapping override in options, if §5.5 finds panels the automatic
  check can't settle.
- **F7.** Load-aware hints (move a load to a cheaper time) instead of a space.

**Out of scope:**

- Any switching, relay or priority action.
- Two-pole circuits in any form: moving them, or counting a 120/240 V split per leg.
- Cross-panel moves.
- Utility-side or transformer balance.
- Three-phase services.
- Wire and breaker sizing, and any breaker-rating rule.
- A continuous sensor or Repairs issue. (The button and notification moved into scope in rev 4.)
- Any new recorded entity or statistic.
- Changing the recorder rounding decision.

## 10. Questions and the user's answers (rev 2)

- **Q1. Which leg is space 9 on?** The user expects L1, which matches L1's rule. Team-lead
  checked the dev statistics: **confirmed** (r = 0.99, §5.3). L9 stays for other panels.
- **Q2. Tandems?** SPAN allows them but reports a tandem as one circuit in one space. It is
  an ordinary single-pole circuit that moves as one unit. `shared_space` stays only for the
  case where the model really shows two circuits on one space.
- **Q3. Presentation?** The action only. No button or notification.
- **Q4. Threshold?** No answer. The defaults stay as named constants (`MIN_GAIN_A` 2 A,
  `MIN_GAIN_PCT` 10 %, `BALANCED_BELOW_A` 5 A).
- **Q5. 120/240 V loads?** Ignore two-pole breakers completely: out of the prediction, the
  metrics and the moves, listed as `ignored: two_pole`.
- **Q6. Rating rule?** None. Suggestions rest on measured usage; the only physical rule is
  that a single-pole circuit goes into a single-pole space.


---

## Rev 4 addendum: button and notification

User request: one "Analyze leg balance" button per local panel. A press runs the
analysis with default arguments and posts the result as a persistent notification. The
action stays as it is and still returns the dict.

### B1. Entity

| Item | Decision |
|---|---|
| Platform | Add `Platform.BUTTON` to `PLATFORMS`; new `button.py`. |
| Created for | Entries with `runtime.transport` set (local, and linked cloud entries). A cloud-only entry gets no button, so there is nothing to hide. |
| Unique id | `identity.leg_balance_uid(panel_key)` = `f"{panel_key}_analyze_leg_balance"`. Add it to `legacy.produced_uids` so the stale-cloud cleanup never offers it. Not cloud-shaped. |
| Category | `None`, so it shows under Controls on the device page. |
| Name | `_attr_has_entity_name`, `translation_key="analyze_leg_balance"`, name "Analyze leg balance" under `entity.button` in strings.json and en.json. |
| Icon | `icons.json` (new) with `mdi:scale-balance` for that key. Optional; if icons.json is skipped, set `_attr_icon`. |
| Default | Enabled by default. Adds no state history worth noting (one timestamp per press). |
| Available | Always; it reads recorder statistics only, not the live link. |
| Device | Same `DeviceInfo(identifiers={(DOMAIN, serial)})` as the panel device. |
| Admin | Any user may press it. It is read-only, as the action is. |

### B2. Shared code

- The button and the action both call `leg_balance.async_analyze(hass, entry)`. No
  analysis logic in `button.py`.
- New HA-side helper `leg_balance.async_notify(hass, entry) -> None`:
  1. Takes the per-entry lock (B4). If it's held, logs at debug and returns.
  2. Calls `async_analyze`; on `HomeAssistantError` (or any `Exception`, logged at debug
     as the type name only), uses `response=None`.
  3. Builds markdown with `lb.notification_markdown(response)`.
  4. Calls `persistent_notification.async_create(hass, message, title=..., notification_id=...)`.
  It never raises: the notification is the outcome, so the press doesn't also show an
  error toast.
- `button.async_press` only calls `async_notify`.

### B3. Notification

- **Id:** `f"{DOMAIN}_leg_balance_{entry.entry_id}"`. A new press replaces the old one.
  entry_id isn't a serial or host.
- **Title:** `f"Leg balance: {name}"`. `name` is the panel device's `name_by_user` when the
  user set one, else the fixed "SPAN panel". Never `entry.title` or the device's default
  name: the local default contains the host.
- **Text:** formatted in code, by a pure function in `span_client/leg_balance.py`:
  `notification_markdown(response: Mapping | None) -> str`. This fits the repo: the
  response text is already English from the pure module (as `commands.describe` is for
  logbook lines), and a persistent notification takes a literal string, not a
  translation key. Only the button name goes in strings.json.

Layout (each block is present only when it applies; NOTICE is always last):

| Case | Content |
|---|---|
| Header (non-error) | `**Window:** {periods_used} {hourly / 5-minute} periods over {span_hours} h (asked for {days} days).` Then the mapping verdict: `**Leg mapping:** {mapping}`, plus `mapping_note`. Then `**Imbalance:** typical {typical_a} A, high {high_a} A, {heavier} heavier.` |
| Balanced | `The legs are balanced (high imbalance under {BALANCED_BELOW_A} A). No moves suggested.` |
| Suggestions | A numbered list, one per suggestion: its `text` (circuit name, spaces before → after), then `high {high_a} → {high_after_a} A, typical {typical_a} → {typical_after_a} A`. |
| No useful move | `No move clears the minimum gain.` (not balanced, no suggestions) |
| Short history | `problem_note` for SHORT_HISTORY, shown above the results. |
| Not enough history / no layout | `problem_note` only, no metrics or list. |
| Ignored / not moved | One line: `Ignored: {n} two-pole circuits. Not moved: {m} circuits (protected, battery or solar, feed, low coverage).` Reasons listed are only those present, deduped. Counts only; names are in the action's response. |
| Error (`response=None`) | `Couldn't read or analyze this panel's recorded history. Check that the recorder is running, then try again.` (same wording as `leg_balance_read_failed`) |
| Always | A blank line, then `_{NOTICE}_`. |

Numbers are rounded to one decimal. The output contains circuit names and space numbers
only, never ids, serials or hosts.

### B4. Re-entrancy

A per-entry `asyncio.Lock` on the runtime data (`runtime.leg_balance_lock`, created at
setup). A second press while one is running is ignored with a debug log. The first
press's notification is the result. The action doesn't take the lock: concurrent calls
are read-only and harmless.

### B5. Tests

Pure (`tests/test_leg_balance.py`):
- `notification_markdown` for each case: balanced, suggestions (text, before → after),
  short history, not enough history, no layout, no useful move, ignored/not-moved summary
  (counts and deduped reasons), flipped mapping note, and error (`None`).
- NOTICE is present in every case, including the error.
- No digit string longer than 4 characters and no `.` host pattern in the output of the
  fixture-driven cases (guards against ids or hosts leaking in).

Repo checks:
- `leg_balance_uid` is in `produced_uids` and doesn't match the cloud uid regex.
- strings.json and en.json identical; `entity.button.analyze_leg_balance` present.
- `BUTTON` in `PLATFORMS`.

HA layer (**unverified** if the repo still can't import Home Assistant in tests; then
review only): a button only for entries with a transport; the press posts one notification
with the per-entry id and a title without the host; a second press replaces it; an error
still posts the notice; a press during a run is ignored.

### B6. README

Rewrite "Leg balance (local panels)":
1. **The button first:** "Press **Analyze leg balance** on the panel's device page (under
   Controls). The result appears in Notifications and replaces the previous one."
2. What it shows (in short), and the electrician notice.
3. **Advanced: the action.** The existing `span_ebus.analyze_leg_balance` example, for
   custom windows and thresholds or to use the full response in a script or card.

### B7. Release note

The release notes line becomes: "The new **Analyze leg balance** button (and the
`span_ebus.analyze_leg_balance` action) …". The button is **untested live**.
