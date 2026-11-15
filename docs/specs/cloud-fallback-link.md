# PR 5: cloud fallback feeding, link migration and legacy repair

Status: final spec rev 4.5 (as built through 5d), 2026-11-14. Local only; never commit or push.

- Rev 2 applied the user's decisions on these points:
  - the energy-step repair;
  - the minor-version marker;
  - entity identity preserved in both directions (it added the local-only → local+cloud direction).
- Rev 3 applied the user's decisions on rev 2: overhead per panel in every mode (5o), site power flows
  re-pointed only after a capture, "Fix it" applies the energy correction itself, and the dip rule.
- Rev 4 applies the user's replacement of rev 3's decision 1, plus one new feature:
  - **No cloud polling for rows with no local equivalent.** Rev 3's catalogue, disabled
    registration and `CloudOnlyRowPoller` are gone. Every such row that has a local source is now
    fed locally (§5.3), and its enabled state is kept. The cloud is only ever the failover; a
    linked local-first entry holds no cloud connection while local is healthy.
  - **Integrated site energies.** Home, solar and battery energy have no local register. They are
    integrated from local power, like overhead energy (§8.2, in 5o).
  - **Automatic cloud gap backfill** for integrated energy rows on entries with cloud fallback
    (§8.3, new sub-PR 5d, after 5a).
  - Rev 3 decisions 2–4 (overhead, site flow re-pointing, energy-step auto-fix) stand unchanged.
- Rev 4.1 re-evaluates Q19 under local sourcing: feeds the feeder kind doesn't classify are now
  classified by value at link time, so only the directional site flows, generator, balance and
  residual stay fallback-only.
- Rev 4.2 applies the user's Q24 decision: site grid energy maps onto the lugs import register
  **only when the cloud site has exactly one panel**, in both directions (§8.4). A multi-panel site
  gets no re-point, no energy step, no fallback mapping and no backfill for it, and nothing sums
  panel imports locally. Whether a multi-panel site aggregates is a capture-gated item (Q28).
- Rev 4.3 (final) records the user's approval of every remaining recommendation (2026-11-13): Q18,
  Q19, Q21–Q23 and Q25–Q28 are decided as recommended, and R20 is handled by the release notes plus
  a note on the link confirm form (§9.3). No further changes without a new decision.
- Rev 4.4 (2026-11-14) records the deviations approved during 5b (merged as PR #57). They are listed
  in §6.1 and override §4.1, §5.1 and §6 where they differ.
- Rev 4.5 (2026-11-14) records the deviations approved during 5d. They are listed in §8.3.1 and
  override §4.1 and §8.3 where they differ.

Builds on:
- `transport-manager.md` (PR 3);
- `r202639-model.md` (4a);
- `r202639-subdevices.md` (4b);
- `multi-panel-topology.md` (master §5 link record; §7–8 feedthrough);
- `local-transport.md`.

Notation:

| Symbol | Meaning |
|---|---|
| **S** | Local serial, `entry.data[CONF_LOCAL_SERIAL]` |
| **K** | Cloud panel key, `PanelInfo.key` (today `coordinator.serial`) |
| **n** | Cloud circuit instance id |
| **C** | Local circuit id |
| **U** | Upstream lugs id |
| **D** | Downstream (feedthrough) lugs id |
| **P** | Distribution-enclosure id |

## 1. Goals

1. **Cloud readings during an outage.** While a linked local-first entry shows `Transport.CLOUD`,
   local MEASUREMENT entities show cloud readings instead of `unknown`.
   `transport.fallback_value(scope, device_id, key)` stops returning `NO_FALLBACK` for every row it
   can map.
2. **A durable link record.** It ties each local circuit id to the cloud circuit instance id. It is
   built by a deterministic matcher: identical space sets, equal poles, never a remote CT.
3. **Entity identity is preserved in both directions. This is a hard requirement.**
   - **Cloud-only → cloud+local.** The user's existing cloud entities are the entities from then on.
     entity_id and statistic_id don't change, and Energy dashboard selections keep working
     untouched.
   - **Local-only → local+cloud.** The existing local entities are fed by the cloud during an
     outage. No entity_id changes.
4. **The cloud is only the failover.** A linked local-first entry holds no cloud connection while
   local is healthy. Cloud rows with no local row to re-point onto are fed from local values
   (§5.3); nothing is polled for them. The only cloud calls outside a failover are the bounded gap
   backfill requests of goal 10.
5. **Panel overhead per panel, in every mode.** It is calculated locally for cloud, local and linked
   entries alike, by one function.
6. **Both directions are opt-in options steps.** Local-only stays a user choice and never touches
   the cloud.
7. **"Fix it" corrects the one-time energy jump itself.** The user never opens Developer tools, and
   the fix is never applied twice.
8. **A minor-version marker on linked entries.** No entry gets a major bump.
9. **A legacy repair.** It retires cloud-keyed entities only when the user confirms. Nothing is
   auto-deleted.
10. **Gaps in integrated energy are filled from SPAN's history**, automatically, on entries with
    cloud fallback only, at most once per gap, with no double counting (§8.3).

## 2. Non-goals

- Commanding anything over the cloud. Relay and priority control stay LOCAL-only.
- Fallback values for BESS, PV, EVSE and MID. The cloud carries no SoC, SoE or inverter data.
- Mixing cloud energy into local energy registers. Local ENERGY rows never take a fallback, with one
  exception: the lugs import register on a single-panel site continues by cloud *delta* during an
  outage, never by the cloud total (§8.4).
- Summing several panels' lugs imports locally to stand in for a site grid total. In a lug-fed
  (feedthrough) topology that double counts, and without a capture aggregation can't be told apart
  from topology (Q28).
- Automatic multi-panel upgrades. A cloud account with several panels uses remove and re-add.
- Rounding or write-on-change for plain cloud entities on unlinked cloud entries. That stays
  unchanged; the overhead change in 5o doesn't alter it.
- Polling the cloud for any row while local is healthy (rev 3's poller is withdrawn).
- Feeding unmatched circuits from the cloud. They are a matcher problem only (Q8).
- Backfill on local_only entries, on unlinked cloud entries (their ledger already catches up), or
  for metered registers (grid and lugs counters, circuit `exported-energy`). Repairing the hourly
  shape of a register's growth across an outage is out of scope.
- EVSE controls (PR 6).

## 3. Split

| PR | Content | Risk | Depends on |
|---|---|---|---|
| **5o** | **Local derived rows, in every mode (§8.1, §8.2).** <ul><li>The pure `overhead.py` function and its integrator.</li><li>Local `derived/overhead` and `derived/overhead-energy` rows on the panel device, for local-only and linked entries.</li><li>**New in rev 4:** local integrated `derived/site-energy`, `derived/pv-energy` and `derived/battery-energy` rows, created only when the source `power-flows/*` row is published.</li><li>`EnergyAccumulator.add` reports a rejected gap (needed by 5d).</li><li>The cloud `panel_overhead` moves onto the same function and subtracts the feedthrough when the feeder kind classifies it.</li><li>Cloud overhead uids are unchanged.</li></ul> | Low: new local entities; the cloud value changes only on panels where it was `None` (feedthrough) | PR 3, 4a |
| **5a** | <ul><li>`link.py` (record, matcher) and `fallback.py` (pure mapper).</li><li>`fallback_value` wiring and the cloud → coalescer refresh hook.</li><li>Relay shown read-only on CLOUD.</li><li>Linked entries stop creating the cloud coordinator's own entities.</li><li>**The "Add cloud fallback" options step for local-only entries (direction B, §5.2).** It writes the link record with the site history target and creates no new entities.</li><li>The minor-version marker and `async_migrate_entry` (§7).</li></ul> | Low: no unique-id rewrites, no new entities | PR 3, 4a, 5o |
| **5b** | <ul><li>**The "Link local panel" options step for cloud-account entries (direction A, §5.1).**</li><li>Unique-id re-pointing, including overhead onto the 5o rows and, on a single-panel site only, site grid energy onto the lugs register (§8.4).</li><li>Device merge.</li><li>**`standin.py`: cloud-keyed rows fed from local values (§5.3)**, enabled state kept.</li><li>Fallback-only rows for cloud rows with no local source (§5.3).</li><li>**The automatic `energy_step` fix (§9.2).**</li></ul> | High: registry and statistics | 5a |
| **5c** | <ul><li>The legacy repair (`legacy_entities`) for cloud-keyed duplicates from pre-5a linked entries.</li><li>Unmatched-circuit cloud entities from direction A.</li><li>Fallback-only rows, when the user wants them gone, and on local_only.</li></ul> | Medium: deletes on confirm | 5a (5b for direction-A rows) |
| **5d** | **Cloud gap backfill (§8.3).** `backfill.py`: gap detection, history fetch, statistics adjustment, its store and its tests. | Medium: writes statistics | 5a (5o for the integrated rows) |

Why this order:
- **5o goes first.** It doesn't depend on linking, it helps every user (local-only included), and it
  gives 5b local targets to re-point the cloud overhead pair onto and to source the site energy
  stand-ins from.
- **Direction B lands in 5a.** It writes no unique ids: it only adds tokens and a link record to an
  entry whose uids are already canonical.
- **Direction A gets its own review**, because it rewrites the registry.
- **5d comes after 5a** because it needs the link record's history target and the stored tokens.
  It doesn't need 5b; on direction-A entries it covers the stand-in energies once 5b lands.

## 4. Data flow (fallback)

```
SpanCloudCoordinator.data: dict["node/prop", Reading]  (push, ~2 Hz; relay every 60 s)
        │  coordinator.async_add_listener(_on_cloud_update)   [registered in _start_cloud]
        ▼
SpanTransportManager._on_cloud_update ──► if shown is CLOUD: self._coalescer.mark()
        ▼
SpanLocalEntity._recompute()
   (MEASUREMENT, or CONTROL with source switch/relay) and shown is CLOUD
        └─► transport.fallback_value(row.scope, device_id, row.source)
                └─► fallback.resolve(self.link, cloud_data, scope, device_id, key)
        └─► _present(value)  (options filter + ROUNDING, same as local values)
```

Rules:

- **When `fallback_value` returns a value.** It returns `NO_FALLBACK` unless all of these hold:
  - the mode is LOCAL_FIRST;
  - `self._cloud` is not None;
  - `shown is Transport.CLOUD`;
  - `cloud_live`;
  - the key maps.

  It never starts the cloud and never awaits.
- **Missing readings.** A mapped key with no reading returns `None` (presence → UNKNOWN). An
  unmapped key returns `NO_FALLBACK`.
- **Removal and failback.** The listener is removed in `_stop_cloud`. Failback needs nothing else:
  the next snapshot recompute sees `shown is LOCAL`.
- **Recorder load.** The coalescer, rounding and write-on-change keep the recorder quiet at the
  cloud tick rate.
- **Stand-ins and integrated rows** (§5.3, §8.2) read the *shown* value of their source local row.
  So they follow failover automatically: on CLOUD they see the fallback value; they need no cloud
  code of their own.

### 4.1 API signatures

```python
# link.py (new, pure; 5a)
@dataclass(frozen=True, slots=True)
class PanelLink:
    cloud_key: str                      # K at link time
    local_serial: str                   # S
    circuits: Mapping[str, int]         # C -> n
    feeds: Mapping[int, str]            # cloud feed n -> "U" | "D", value-classified (§5.3)
    unmatched_local: tuple[str, ...]
    unmatched_cloud: tuple[int, ...]
    site_history: HistoryTarget | None  # resource/instance/node of the site, for 5d
    site_panel_count: int               # panels on the cloud site of K (§8.4); refreshed per cloud start

    @property
    def single_panel_site(self) -> bool:   # gates every grid-energy mapping (§8.4)
        return self.site_panel_count == 1
    linked_at: str                      # ISO-8601 UTC

def match_circuits(local: Mapping[str, model.CircuitFacts], cloud: Iterable[CircuitInfo]
                   ) -> tuple[dict[str, int], tuple[str, ...], tuple[int, ...]]
    """frozenset(spaces) equality AND equal poles; remote CTs never match; a space set seen
    twice on either side matches nothing."""
def link_from_options(options: Mapping[str, Any]) -> PanelLink | None
def link_to_options(options: Mapping[str, Any], link: PanelLink) -> dict[str, Any]

# fallback.py (new, pure; 5a)
def resolve(link: PanelLink | None, data: Mapping[str, Reading],
            scope: model.Scope, device_id: str, key: str) -> Any   # value | None | NO_FALLBACK
FALLBACK_KEYS: Mapping[tuple[model.Scope, str], FallbackSource]    # the §8 table
class RegisterContinuation:   # one per lugs import entity on a single-panel site (§8.4)
    def local(self, value: float | None) -> float | None    # local register sample -> shown
    def cloud(self, value: float | None) -> float | None    # cloud grid_energy sample -> shown
    def cloud_started(self) -> None                         # re-seeds the cloud mark

# overhead.py (new, pure; 5o)
def overhead_w(feed_w: float | None, circuits_w: Iterable[float | None],
               feedthrough_w: float | None, has_feedthrough: bool) -> float | None
    """max(0, feed - Σcircuits - feedthrough); None if any required input is None."""

# energy.py (5o: one change)
class EnergyAccumulator:
    def add(self, power_w: float, timestamp: float) -> float | None
        """As today; returns the length in seconds of a gap it refused to bridge, else None."""

# standin.py (new; 5b)
@dataclass(frozen=True, slots=True)
class StandIn:
    uid_suffix: str          # e.g. "site/home"; uid = f"{K}_{uid_suffix}"
    scope: model.Scope
    source_row: str          # the local Row key it reads, e.g. "site_power"
    transform: Callable[[float], float]   # identity, or the confirmed sign flip (§5.3)
STANDINS: tuple[StandIn, ...]             # the §5.3 table
FALLBACK_ONLY: frozenset[str]             # uid suffixes with no local source (§5.3)
def standins_for(registry_uids: Iterable[str], cloud_key: str) -> tuple[StandIn, ...]

# backfill.py (new; 5d)
class GapBackfill:            # one per linked local-first entry (§8.3)
    async def async_setup(self) -> None
    async def async_stop(self) -> None
    def record_gap(self, row_key: str, start: datetime, end: datetime) -> None   # @callback
    def kick(self, reason: Literal["startup", "reconnect", "failback", "gap"]) -> None
def allocate(buckets: Sequence[Bucket], gap_start: datetime, gap_end: datetime
             ) -> list[tuple[datetime, float]]   # pure: prorated kWh per UTC hour start

# identity.py (new, pure; 5b)
def rewrite_plan(registry_entries: Iterable[RegistryEntryView], link: PanelLink,
                 local_rows: Iterable[tuple[Row, str]]) -> RewritePlan
@dataclass(frozen=True)
class RewritePlan:
    repoint: tuple[tuple[str, str, str], ...]   # (entity_id, old uid, new uid)
    standin: tuple[str, ...]                     # entity_ids kept on K uids, fed locally
    fallback_only: tuple[str, ...]               # entity_ids with no local source
    unmatched: tuple[str, ...]                   # entity_ids of unmatched cloud circuits
    energy: tuple[str, ...]                      # entity_ids of re-pointed register energy sensors
                                                 # (K_site/grid_energy only if link.single_panel_site)

# energy_step.py (new; 5b)
class EnergyStepTracker:      # one per upgraded entry while its store exists (§9.2)
    async def async_setup(self) -> None
    async def async_apply(self) -> ApplyResult    # called by the repair flow
def expected_step(cloud_last: float, first_local: float) -> float   # pure, kWh

# recorder_api.py (new; 5b, reused by 5d) — the only module that touches the recorder
async def last_short_term(hass, statistic_id, types) -> ...
async def sums(hass, statistic_id, start, end, period: Literal["5minute", "hour"]) -> ...
def adjust(hass, statistic_id, start: datetime, kwh: float) -> None

# transport.py
class SpanTransportManager:
    link: PanelLink | None
    def fallback_value(self, scope: model.Scope, device_id: str, key: str) -> Any
    async def _async_refresh_link(self) -> None     # re-match circuits on each cloud start
```

`panel_key` stays **S** for every entry. The PR 3 comment "PR 5 may point it at a cloud key" is
resolved the other way; §5.4 explains why.

### 4.2 Link record storage

- **Where it lives:** `entry.options["links"][K]` (master §5), with these fields:
  - `local_serial`;
  - `circuits{C: n}`;
  - `feeds{n: U|D}` (value-classified feeds, §5.3);
  - `unmatched_local`;
  - `unmatched_cloud`;
  - `site_history` (resource, instance and node of the site, for the 5d backfill);
  - `site_panel_count` (panels on K's cloud site, `len(site_choices(site))` from `find_panel`; §8.4);
  - `linked_at`.
- **Removed in rev 4:** `cloud_only[]` and the per-row `history_targets{}`. Stand-ins and
  fallback-only rows are derived at setup from the registry (uids with the `{K}_` prefix), so
  nothing about them is stored.
- **Reloads:** link-only option changes don't reload the entry.
- **Diagnostics** redact these fields:
  - the keys;
  - the serials;
  - `site_history` (resource, instance and node ids).
- **`_async_refresh_link`** re-matches only the circuits, and refreshes `site_history` and
  `site_panel_count` if the cloud returns different ones. A changed panel count switches only the
  §8.4 fallback mapping; it never re-points or un-points anything after the upgrade.

## 5. Entity identity in both directions

### 5.1 Direction A: cloud-only → cloud+local (5b)

**Decision: re-point the cloud unique ids onto the canonical local unique ids, prefixed with S.**

Why this keeps everything:
- The registry keeps the entity_id when a unique id changes (`er.async_update_entity(entity_id,
  new_unique_id=...)`).
- The recorder keys long-term statistics by statistic_id, which for a sensor is its entity_id.
- The Energy dashboard stores `stat_energy_from`, `stat_energy_to` and `stat_consumption` as
  statistic_ids.
- So the dashboard, history, long-term statistics, cost sensors, automations, area and name all
  survive.
- Unit and state_class are identical on both sides for every re-pointed row (W/measurement,
  A/measurement, V, Hz, kWh/total_increasing). So the recorder raises no "unit changed" or "state
  class changed" statistics issue. This is tested.
- RestoreSensor restores by entity_id, so an integrated row re-pointed or stood in keeps its total.

Why not keep cloud uids as the canonical key for those rows:
- Every local entity would need a per-entry uid override table forever, and two uid schemes would
  coexist on one entry type.
- Direction B produces local uids. Re-pointing makes A and B converge on one registry shape, so later
  PRs only ever reason about one scheme. Stand-ins (§5.3) are the bounded exception, waiting on a
  capture.
- With the S prefix, an older release that already understands linked entries (PR 3+) computes the
  same uids. That is what makes the minor-version marker safe (§7).

Before → after. entity_id and statistic_id stay the same in every row.

| Cloud entity (before uid) | After uid | Fed from | Notes |
|---|---|---|---|
| `K_circuit-<n>/power` | `S_circuit/C/meter/active-power` | local row | W, + consuming on both sides (R2) |
| `K_circuit-<n>/power_energy` | `S_circuit/C/meter/exported-energy` | local register | One-time step, corrected by §9.2 |
| `K_circuit-<n>/current` | `S_circuit/C/meter/current` | local row | |
| `K_circuit-<n>/relay` | `S_circuit/C/switch/relay` | local row | |
| `K_panel/voltage_l1`, `_l2` | `S_distribution-enclosure/P/meter/voltage-a`, `-b` | local row | |
| `K_panel/frequency` | `S_distribution-enclosure/P/meter/frequency` | local row | |
| `K_panel/current` | `S_distribution-enclosure/P/meter/busbar-current` | local row | Q10 |
| `K_panel/current_l1`, `_l2` | `S_lugs/U/meter/current-a`, `-b` | local row | |
| `K_feed-<n>/power` (main feed: feeder kind 3, the single feed, or `link.feeds[n] = U`) | `S_lugs/U/meter/active-power` | local row | The feed, not `panel/power` (the feed is sampled with the circuits) |
| `K_feed-<n>/power` (feedthrough: feeder kind 4, or `link.feeds[n] = D`) | `S_lugs/D/meter/active-power` | local row | Feeds the kind doesn't classify are classified at link time by value (§5.3); only a feed that fails both stays fallback-only |
| `K_site/grid_energy`, **single-panel site** | `S_lugs/U/meter/imported-energy` | local register | One-time step, corrected by §9.2; cloud-delta fallback during an outage (§8.4, Q24) |
| `K_site/grid_energy`, **multi-panel site** | **unchanged: fallback-only** (§5.3) | cloud ledger, only while CLOUD | No re-point, no energy step, no backfill, no local sum (§8.4, Q28) |
| `K_panel/overhead` | `S_distribution-enclosure/P/derived/overhead` | 5o row | |
| `K_panel/overhead_energy` | `S_distribution-enclosure/P/derived/overhead-energy` | 5o integrated row | Continues from the restored total, so there is no step |
| `K_transport` | uid of the local transport sensor | local | no state_class |
| `K_site/grid`, `home`, `solar`, `battery` | **unchanged: a stand-in** (§5.3) | `power-flows/grid`, `site`, `pv`, `battery` | Re-pointed onto those uids only once a capture confirms (Q12) |
| `K_site/home_energy`, `solar_energy`, `battery_energy` | **unchanged: a stand-in** | 5o integrated `derived/site-energy`, `pv-energy`, `battery-energy` | Continues from the restored total, so there is no step; gaps backfilled (5d) |
| `K_site/voltage_l1`, `_l2`, `K_site/frequency` | **unchanged: a stand-in** | `meter/voltage-a`, `-b`, `meter/frequency` | Same value as the re-pointed panel rows |
| `K_panel/power` | **unchanged: a stand-in** | `lugs/U/meter/active-power` | Same value as the re-pointed main feed |
| Directional flows, generator, balance, residual, their energies; a feed neither the kind nor the value check classifies | **unchanged: fallback-only** (§5.3) | cloud, only while CLOUD | Q19 |
| `K_circuit-<n>/*` of unmatched circuits | **unchanged: no feed**, unavailable | — | Listed by the legacy repair (Q8) |

### 5.2 Direction B: local-only → local+cloud (5a)

This adds the options step **"Add cloud fallback"** to a local-only entry. It replaces today's
`not_cloud_account` abort for local entries. The transport step stays for linked entries.

1. Explain that the step is optional and what it does. The user can cancel.
2. Cloud sign-in, reusing the account steps of the cloud add flow.
3. Run `find_panel(sites, S)`. It aborts with:
   - `panel_not_on_account` (already exists);
   - `panel_on_cloud_entry` when another entry on this install already runs this panel through the
     cloud. The user should run "Link local panel" on that entry or remove it, so the panel never
     has two entries.
4. **One-time cloud retrieval.**
   - Fetch the trait snapshot (circuit list, labels, feeder kinds) and the site history target, and
     record the site's panel count from the `find_panel` result (§8.4).
   - Match the circuits against the current local snapshot.
   - The confirm form shows `{matched}` of `{total}` circuits and the `{unmatched}` count.
5. On confirm:
   - write the tokens to `data`;
   - write `links` and `CONF_TRANSPORT_MODE = local_first` to `options`;
   - set `minor_version = 2`;
   - reload.

Before → after:

| Entity | Before uid | After uid | entity_id | statistic_id |
|---|---|---|---|---|
| every local entity (including the 5o derived rows) | `S_<row uid>` | `S_<row uid>` (unchanged) | same | same |
| cloud-keyed rows of any kind | none | none created | — | — |

No registry row changes and no entity is created.
- **During an outage**, matched MEASUREMENT rows and the relay state show cloud values instead of
  `unknown`, and the 5o integrated rows keep integrating from them.
- **Gaps** in the 5o integrated energies are backfilled from the cloud (5d).
- **Grid energy.** Only on a single-panel site does the lugs import entity
  (`main_consumed_energy`) continue by cloud delta during an outage (§8.4). On a multi-panel site
  it holds, as every local register does today.
- **The local add flow's** existing `cloud_fallback` menu becomes the same code path.

**"Remove cloud fallback"** (Q17) sits in the transport step of a linked entry.
- **Direction B entries:** it drops the tokens and links, sets the mode to local_only and
  `minor_version` back to 1, and reloads. Nothing else changes.
- **Direction A entries:** it drops the tokens, keeps `links[K]` (no secrets; stand-ins need K for
  their uids), sets local_only and keeps `minor_version = 2` (the re-pointed uids stay). Stand-ins
  keep updating, because they are fed locally. Fallback-only rows go unavailable and the legacy
  repair (5c) offers to remove them; the warning names their count.

### 5.3 Rows with no local row to re-point onto: fed locally

These rows replace rev 3's catalogue and poller. **Nothing in this section talks to the cloud.**

There are three kinds. They are derived at setup from the registry: every entity of this entry
whose uid starts with `{K}_` and that wasn't re-pointed.

**1. Stand-ins (`standin.py`).** A cloud-keyed entity that keeps its uid `{K}_<suffix>` and shows the
shown value of a local row, through a fixed transform. It exists only where a registry entry
already has that uid (direction A). It is never created new, and direction B never has any.

| Stand-in uid suffix | Local source row (facts value) | Transform | Status |
|---|---|---|---|
| `site/grid` | `power-flows/grid` (`grid_power`) | identity | **Sign is specified below** |
| `site/home` | `power-flows/site` (`site_power`) | identity | Values to confirm with a capture (Q12) |
| `site/solar` | `power-flows/pv` (`pv_power`) | `PV_TO_CLOUD_SIGN` (Q21) | Sign unconfirmed |
| `site/battery` | `power-flows/battery` (`battery_power`) | `BATTERY_TO_CLOUD_SIGN` (Q21) | Sign unconfirmed |
| `site/voltage_l1`, `_l2` | `meter/voltage-a`, `-b` | identity | |
| `site/frequency` | `meter/frequency` | identity | |
| `panel/power` | `lugs/U/meter/active-power` (`current_power`) | identity (+ into the panel) | |
| `site/home_energy` | 5o `derived/site-energy` | identity (kWh total) | Integrated (§8.2) |
| `site/solar_energy` | 5o `derived/pv-energy` | identity | Integrated |
| `site/battery_energy` | 5o `derived/battery-energy` | identity | Integrated |

**Grid sign, explicitly.**
- The panel publishes the raw Homie property `power-flows/grid` **negative while importing**. The
  upstream lugs show the same moment as positive (`model.py:41`).
- The facts layer applies `GRID_FLOW_SIGN = -1`, so `PowerFlows.grid` (and the `grid_power` row) is
  **+ import, − export**.
- Cloud `site/grid` is **+ import, − export** too.
- So the stand-in transform on the facts value is the **identity**. Relative to the raw Homie value
  it is a negation, which the facts layer already did. A fixture test pins both: a raw −1200 W
  publication shows +1200 W on `site/grid`.
- On the user's own site (solar and battery upstream of the panel) "import" means "flowing into the
  panel", not utility import. That's equally true of the cloud value today, so nothing changes for
  them.

**Values and timing.**
- A stand-in's state is its source row's presented value (rounding, write-on-change), so it follows
  the §10 presence of that row, including the cloud fallback during an outage.
- If its source row isn't published on this panel (for example `power-flows/pv` on a panel with no
  PV), the stand-in is unavailable. Its identity and history stay.
- **Duplicate local rows are suppressed.** While a stand-in stands for a local row, that local row's
  own entity is not created (for example no new `S_…/power-flows/site` next to `K_site/home`). Two
  exceptions show the same value twice by design: `K_site/voltage_*`/`frequency` (the panel voltage
  rows already took `K_panel/voltage_*`) and `K_panel/power` (the lugs row already took the main
  feed). Each pair is one source of truth with two entity_ids the user already had.
- **Energies.** Stand-in energies read the 5o integrated row's total. In direction A that row starts
  from the stand-in's restored state (the cloud ledger total it showed last), so there is no step
  and no energy-step entry. The integrated row's own `S_` entity is suppressed as a duplicate.

**Once a capture confirms a site flow (Q12)**, a later release re-points that stand-in onto the
local uid, the same way §5.1 does, and the suppressed duplicate becomes that entity. No user action.

**2. Fallback-only rows.** A cloud entity with no local source at all.

| Uid suffixes | Why there's no local source |
|---|---|
| `site/grid_to_home`, `grid_to_battery`, `solar_to_home`, `solar_to_battery`, `solar_to_grid`, `battery_to_home`, `battery_to_grid` and their `_energy` rows | The panel publishes only the four net flows; directional splits can't be derived from them |
| `site/generator`, `balance`, `residual` | No local property |
| `site/grid_energy` on a **multi-panel site** (rev 4.2) | The site total doesn't map onto one panel's lugs, and panel imports aren't summed locally (§8.4, Q28) |
| `feed-<n>/power` that neither the feeder kind nor the link-time value check classifies | Can't tell U from D (R12); expected to be empty in practice |

**Link-time feed classification** (rev 4.1). When a panel has several feeds and trait `1/21` doesn't
classify them, the link step classifies each feed by value. It reads the cloud `feed-<n>/power`
readings and the local `lugs/U` and `lugs/D` `active-power` over the same 30 s window (the link step
already holds both snapshots), and assigns a feed to U or D when its mean is within
`max(50 W, 10 %)` of that lugs row and outside that band of the other. Ambiguous (both, or neither,
or U and D within 100 W of each other, e.g. a downstream panel idling) → unclassified, retried at
every cloud start like the circuit re-match. The result is stored as `link.feeds{n: "U"|"D"}`, and
the feeder kind, when present, always wins. Sign: compared on magnitudes, because the feed sign is
normalised by the same transform as §8.

- **Power rows** are UNAVAILABLE except while `shown` is CLOUD, when they show the fallback
  coordinator's reading.
- **Energy rows** hold their total outside CLOUD. During CLOUD the fallback coordinator's ledger
  adds energy as it does today. The ledger mark for these keys is re-seeded at each cloud start
  (`adopt` semantics: the first bucket only sets the mark), so a fallback never dumps the whole
  local period into one hour.
- **5d doesn't backfill them.** That would mean fetching continuously while local is healthy.
- The legacy repair lists them so the user can retire them (Q19).

**3. Unmatched circuits.** `K_circuit-<n>/*` for circuits the matcher didn't pair. They get no feed in
any transport (not even during an outage) and are UNAVAILABLE. The local circuit they correspond to
already has its own new entities. The legacy repair lists them; fixing the match is a matcher
problem (Q8).

**Enabled state.** Stand-ins, fallback-only rows and unmatched rows all keep the user's current
enabled or disabled state. Nothing is registered disabled, because nothing new is registered. This
replaces rev 3's Q14 and is decided.

**local_only.** Stand-ins and integrated rows keep updating (they're local). Fallback-only rows go
unavailable and are listed by the legacy repair. There is no cloud client and no network call.

### 5.4 Device registry

The cloud device `(DOMAIN, K)` becomes the panel device.
- When K ≠ S, the upgrade adds `(DOMAIN, S)` with `merge_identifiers`, so the device_id, area and
  name survive.
- Stand-ins and fallback-only rows sit on the panel device.
- Subdevices are local-only and new.

## 6. Upgrade steps (direction A, "Link local panel", 5b)

1. **Pre-checks.**
   - The entry is a cloud account.
   - There is exactly one panel in `options["panels"]`; otherwise abort `multi_panel_upgrade`.
   - No other entry has S; otherwise abort `already_configured`.
2. **Find the panel.**
   - Browse `_ebus._tcp` for about 5 s, accept a manual host, and read `/api/v2/status`.
   - The panel must answer to K through `PanelInfo.answers_to`; otherwise abort
     `panel_not_on_account`.
3. **Register.** Ask for the passphrase and register, using the local add flow code. On failure,
   re-show the form.
4. **Fetch and plan.**
   - Fetch one local snapshot, the cloud trait snapshot and the site history target, and take the
     site's panel count from the `find_panel` result.
   - Match the circuits and build the `RewritePlan`. `K_site/grid_energy` goes to `repoint` and
     `energy` only when the site has exactly one panel; otherwise it goes to `fallback_only` (§8.4).
5. **Confirm form.** It shows:
   - `{matched}` of `{total}` circuits;
   - the counts of stand-ins, fallback-only rows and unmatched-circuit entities.
6. **On confirm, in order,** keeping the reverse plan in memory:
   1. **Collision check.** If any new uid already exists, abort `uid_collision` and write nothing.
   2. **Re-point** every row in `repoint`.
   3. **Merge the device** (§5.4).
   4. **Write the energy-step store (§9.2).** For each entity in `plan.energy` (circuit energies, and
      `K_site/grid_energy` on a single-panel site only), record:
      - `cloud_last`: its numeric hass state now, in kWh;
      - `t_repoint`: UTC now.

      If the state isn't numeric, use `cloud_last` = the recorder's last short-term `state`
      (`get_last_short_term_statistics(hass, 1, statistic_id, True, {"state"})`). If neither
      exists, record the entity as skipped with reason `no_prior_reading`.
   5. **Update the entry** with `async_update_entry`:
      - `data`: host, S and credentials;
      - `options`: `links` (with `site_history`) and mode local_first;
      - `minor_version=2`.
   6. **Reload.**

   On an exception in steps i to iii, apply the reverse plan and abort `migration_failed`. The error
   is logged without serials or hosts. On an exception in step iv, delete the store and continue.
   The jump then stays uncorrected; this is logged as a count only.

### 6.1 As built in 5b (rev 4.4, approved deviations)

- **Local targets.** The re-point target is `model.panel_uid` plus the row uid. The `P/` segment in
  the §5.1 table is a slip.
- **Rows with no local target.** An uncovered `K_` uid, feed current included, becomes
  `fallback_only`. A relay with no local switch becomes `unmatched`. Any other missing target becomes
  `fallback_only`.
- **Collisions** are checked per (domain, uid). The first uid in sorted order wins, and the log
  carries a count only.
- **The account entry keeps unique_id = K.** The duplicate check matches on unique_id or
  `CONF_LOCAL_SERIAL`, through the pure helper `backend.serial_taken`.
- **The rewrite prefix** is the running coordinator's `.serial`, stored as `CONF_CLOUD_STEM`, in
  place of `entity_prefix`. Linking aborts with:
  - `link_not_running` when no coordinator is running or the stem is the `span-cloud` placeholder;
  - `link_nothing_matched` when the plan matches zero rows.
- **Serial mismatch** (replaces step 2's `panel_not_on_account` and the removed
  `options.error.wrong_panel`). A panel that doesn't answer to K may still be linked when the
  circuit check (`circuits_verified`) passes. `link.serial_check(answers, matched, local_total)`
  decides, and a failure aborts `panel_circuits_mismatch`. This is required because the user's cloud
  serial differs from the local one.
- **Sibling guard.** If the panel at the address answers to another panel on the account
  (`backend.sibling_answers`), the link aborts `link_other_panel`. Overlapping space numbers could
  otherwise pass the circuit check.
- **Commit.** One try/undo covers every registry, device and energy-step store write, with the entry
  update last. Any failure, cancellation included, runs the undo. A cancellation is re-raised after
  the undo, and ordinary errors abort `migration_failed`. This replaces the step-iv "delete the store
  and continue" rule.
- **Energy step.** An echo within `MIN_STEP_KWH` of `cloud_last` stays WAITING_FIRST. At the 24 h
  time-out an echo-only record settles NONE. Dismissing the issue deletes the record.
- **Repair title** uses the panel device name, not the account name.
- **Setup (5a review #2).** When a picked cloud panel fails the check, the local entry is kept and a
  local-only choice is offered.

## 7. Minor-version marker and downgrade

**The marker.**
- The config flow stays `VERSION = 1`, `MINOR_VERSION = 1`. New entries of every kind are created as
  1.1.
- Linking sets `minor_version = 2` via `async_update_entry`. That covers direction A, direction B
  and the local add flow with cloud fallback.
- The marker means "this entry has a link record and S-prefixed re-pointed uids".
- Local-only and cloud-only entries stay 1.1. "Remove cloud fallback" returns a direction-B entry to
  1.1; a direction-A entry stays 1.2 (§5.2).

**The new `async_migrate_entry(hass, entry)`:**
- **`entry.version > 1`:** log "entry from a newer major version" (no identifiers) and return False.
  HA marks the entry as a migration error and touches nothing.
- **`entry.version == 1`** (minor 1 or 2): return True with no change. Minor 2 needs no data
  reshaping. A minor above 2 from a future release also returns True, following HA's rule that minor
  versions are backward compatible.
- **Verify during implementation** against the HA 2024.12 `ConfigEntry.async_migrate` source. When
  (version, minor) differs from the handler's, it calls `async_migrate_entry`; without one, a
  same-major difference loads the entry.

**What an older release does with a 1.2 entry:**
- **PR 3/4 releases** have no `async_migrate_entry`, so HA loads the entry.
  - It is a linked entry (S plus tokens), which they already support.
  - Local uids are S-prefixed and match, so every re-pointed entity and its history keep working.
    The 5o derived rows are unknown to them and show unavailable; so do stand-ins.
  - On failover those releases rebuild the cloud coordinator, which re-adds cloud-keyed entities.
    Stand-in and fallback-only uids collide and are skipped by the registry; the others get suffixed
    entity_ids (the pre-5a behaviour).
  - Re-upgrading cleans this up through the legacy repair. No data is lost.
- **Releases before PR 3** ignore `CONF_LOCAL_SERIAL` and run the entry as a cloud account.
  - Its cloud entities come back under new suffixed entity_ids.
  - This is unsupported. The 5b release notes say "do not downgrade below <PR 3 release> after
    linking".
  - Q16 offers the hard-refuse alternative.

## 8. Fallback mapping table

Cloud values are already W, A, V or Hz after decoding. `Cn` = `link.circuits[device_id]`.

| Scope | Local row source | Cloud source | Transform | Notes |
|---|---|---|---|---|
| CIRCUIT | `meter/active-power` | `circuit-<Cn>/power` | as-is (+ consuming on both sides) | R2 |
| CIRCUIT | `meter/current` | `circuit-<Cn>/current` | as-is | |
| CIRCUIT | `switch/relay` (CONTROL) | `circuit-<Cn>/relay` | CLOSED → on, OPEN → off, else None | read-only, §10 |
| CIRCUIT | energies, net | — | — | never fed |
| CIRCUIT | `load-shed/priority` | — | — | no cloud field |
| PANEL | `meter/voltage-a` / `-b` | `panel/voltage_l1` / `_l2` | as-is | else `site/voltage_l1` / `_l2` |
| PANEL | `meter/frequency` | `panel/frequency` | as-is | else `site/frequency` |
| PANEL | `meter/busbar-current` | `panel/current` | as-is (larger leg) | Q10 |
| PANEL | `power-flows/grid` | `site/grid` | as-is: both + import, − export (§5.3) | |
| PANEL | `power-flows/site` | `site/home` | as-is | |
| PANEL | `power-flows/pv` | `site/solar` | inverse of `PV_TO_CLOUD_SIGN` | Unmapped until confirmed (Q21) |
| PANEL | `power-flows/battery` | `site/battery` | inverse of `BATTERY_TO_CLOUD_SIGN` | Unmapped until confirmed (Q21) |
| PANEL | `derived/overhead`, `derived/overhead-energy`, `derived/site-energy`, `pv-energy`, `battery-energy` | — (not fallback keys) | §8.1/§8.2 compute them from whatever values are shown, local or fallback | 5o |
| UPSTREAM | `meter/active-power` | `feed-<n>/power` of feeder kind 3, or the single feed | to local sign (+ import) | Else `panel/power`; Q11 |
| UPSTREAM | `meter/current-a` / `-b` | `panel/current_l1` / `_l2` | as-is | |
| UPSTREAM | `meter/imported-energy` | `site/grid_energy` | cloud **delta** on the last local value, never the total (§8.4) | Single-panel site only (`link.single_panel_site`); multi-panel → unmapped |
| UPSTREAM | other energies, net, ratings | — | — | |
| DOWNSTREAM | `meter/active-power` | `feed-<n>/power` of feeder kind 4 | to local sign (§8.1) | Unmapped unless classified |
| DOWNSTREAM | others | — | — | |
| PANEL | status, door, pcs, shed-forecast, derived islanding, `$state` | — | — | UNKNOWN on cloud |
| BESS/PV/EVSE/MID | all | — | — | UNKNOWN on cloud |

- **Unmatched circuits** return NO_FALLBACK for every row.
- **Unconfirmed signs.** A row whose sign is unconfirmed at merge time ships unmapped as a fallback.
  Its stand-in (direction A) still shows the local value through the same pinned constant; with no
  hardware to test, the constant follows the official SpanPanel/span integration's convention, and
  the release notes flag it (Q21).

### 8.1 Panel overhead, per panel, in every mode (5o)

**What it is.** The power the panel's own electronics and busbar take: what enters through the
upstream lugs, minus what leaves through circuits and through the feedthrough lugs to a downstream
panel.

**Formula** (pure `overhead.overhead_w`), per panel:

```
overhead_W = max(0, F − Σ P_c − D)
  F   = feed power into this panel (upstream lugs active-power; + into the panel)
  P_c = each circuit's active power on this panel (+ consuming)
  D   = power leaving through this panel's feedthrough lugs to a downstream panel (+ leaving);
        0 if the panel has no downstream lugs
→ None unless F, every circuit, and D (when the panel has downstream lugs) all have a value.
```

Why D is subtracted:
- The PR 39 two-panel capture showed that the upstream panel's feed includes the downstream panel's
  load.
- Without the subtraction, the upstream overhead would include the whole downstream panel.
- With it, each panel's overhead counts only that panel, and overheads add up across a site.
- **Downstream panels.** A downstream panel's F is its own upstream lugs. That is fed from the
  upstream panel's feedthrough, but it is still measured at its own lugs. It also subtracts its own
  D if it has one.

**Sign of D.**
- Positive means leaving towards the downstream panel. The local `lugs/D/meter/active-power` sign is
  taken from the PR 39 capture fixture.
- The test asserts that the upstream panel's overhead comes out ≥ 0 and close to the expected small
  value on that fixture.
- If the fixture shows the opposite sign, the transform negates it. This is decided in the test, not
  at runtime.

**Inputs per mode:**

| Mode | F | P_c | D |
|---|---|---|---|
| Local (local-only, or linked with `shown` LOCAL) | `lugs/U/meter/active-power` | `circuit/C/meter/active-power` | `lugs/D/meter/active-power` (the `feedthrough_power` row) |
| Linked with `shown` CLOUD | the fallback value of the same rows (§8) | fallback | fallback; if D is unmapped on a feedthrough panel → None |
| Cloud entry | `feed-<n>/power` of feeder kind 3 (main breaker), else the single non-circuit instance | `circuit-<n>/power` | `feed-<n>/power` of feeder kind 4 (feed-through lugs) |

**Cloud classification.**
- Feeds are classified by trait `1/21` feeder kind. Its decoding comes from the contributor's
  CLOUD-TRAITS notes and is not yet confirmed by a capture (Q20).
- With one non-circuit instance and no kind, it is the feed and D = 0. That is today's behaviour.
- With two or more instances and no kinds, the result is None. That is today's behaviour too.
- So the cloud value changes only on feedthrough panels, where it was always None before.

**Energy.**
- `derived/overhead-energy` integrates `overhead_W` with the existing `EnergyAccumulator`:
  - trapezoidal;
  - positive part only;
  - gaps longer than `MAX_INTEGRATION_GAP_SECONDS` (300 s) are not bridged;
  - None inputs break the run.
- It is a RestoreSensor that restores its total and writes on change at ENERGY_PRECISION 3.
- On the local path, dt comes from the snapshot's monotonic receive time. It integrates on each
  coalesced recompute, not on each MQTT message.
- On a linked entry the energy integrates straight across failover and failback. The gap rule
  covers OFFLINE. There's no reset, because the accumulator belongs to the entity, not to the
  transport.
- **Overhead gaps are not backfilled.** The cloud has no history for the main feed, so there's no
  source for them (Q22).
- The cloud entry's `SpanPanelOverheadEnergySensor` keeps its own accumulator, unchanged.

**Entities and uids:**

| Entry | Power uid | Energy uid | Default |
|---|---|---|---|
| Cloud entry (unchanged) | `{K}_panel/overhead` | `{K}_panel/overhead_energy` | as today |
| Local-only and linked (new in 5o) | `S_distribution-enclosure/P/derived/overhead` | `S_distribution-enclosure/P/derived/overhead-energy` | enabled |
| Direction A | `K_panel/overhead` re-pointed to the local uid (§5.1) | `K_panel/overhead_energy` re-pointed; it keeps its restored total, so there's no step | as before |

- Both entities sit on the panel (distribution enclosure) device.
- The power entity is W/measurement with the local power rounding. The energy entity is
  kWh/total_increasing.
- Names: "Panel overhead" and "Panel overhead energy" (translation keys, no `description` keys).
- MLO 48 shares the enclosure with MAIN 40. Each panel device gets its own pair, keyed by its own P.

### 8.2 Integrated site energies (5o)

Cloud home, solar and battery energy come from SPAN's history. Locally there's no register for them,
so 5o integrates them from the local power flows, exactly like overhead energy.

| Local row (uid suffix on `S_distribution-enclosure/P/`) | Integrates | Matches cloud history field | Created when |
|---|---|---|---|
| `derived/site-energy` | positive part of `power-flows/site` | `home_import` | `power-flows/site` is published |
| `derived/pv-energy` | positive part of `PV_TO_CLOUD_SIGN · power-flows/pv` (production) | `solar_to_grid + solar_to_home + solar_to_battery` | `power-flows/pv` is published |
| `derived/battery-energy` | positive part of `BATTERY_TO_CLOUD_SIGN · power-flows/battery` | `battery_import` (direction to confirm, Q21) | `power-flows/battery` is published |

- Same integrator rules as §8.1: trapezoidal, gaps over 300 s not bridged, None breaks the run,
  RestoreSensor, ENERGY_PRECISION 3, write on change, kWh/total_increasing, enabled by default.
- They integrate the *shown* source value, so they continue across failover on linked entries.
- **Grid energy is not integrated.** It has registers: `lugs/U/meter/imported-energy` and
  `exported-energy` (the existing `main_consumed_energy` and `main_produced_energy` rows). How the
  cloud's site grid energy relates to them is §8.4.
- **Restore with a recorder cross-check** (also applied to overhead energy, because 5d depends on
  it). RestoreSensor data is saved every 15 minutes and at stop, so after a crash it can be older
  than what the recorder already recorded. At setup each integrated row reads the entity's last
  recorded state. If that is numeric and greater than the restored total, the row adopts it, and
  its `last_at` (§8.3) becomes that state's `last_reported` (falling back to `last_updated` on HA
  versions without it). This removes today's small backwards dip after a crash, and it stops 5d
  from backfilling energy the statistics already hold.
- Names: "Site energy", "Solar energy", "Battery energy" (translation keys `site_energy`,
  `pv_energy`, `battery_energy`). They follow the naming of the official SpanPanel/span integration
  where it has an equivalent.
- In direction A each is suppressed while a stand-in (`K_site/home_energy` etc.) stands for it, and
  it starts from the stand-in's restored state.

### 8.3 Cloud gap backfill (5d)

**Scope.**
- **Entries:** linked entries with mode LOCAL_FIRST, tokens and a `site_history` target. Never
  local_only: `GapBackfill` isn't constructed there, and a test asserts that no client is built.
- **Rows:** the integrated rows that have a cloud history field, which are `derived/site-energy`,
  `pv-energy` and `battery-energy`, or their direction-A stand-ins.
- **Not covered:**
  - overhead energy, which has no cloud source (Q22);
  - registers (grid and lugs counters, circuit `exported-energy`), which count through an outage by
    themselves. That includes the lugs import register on a single-panel site, whose outage
    continuation (§8.4) is a fallback, not a backfill;
  - `K_site/grid_energy` on a multi-panel site, in any form: it is fallback-only and gets no 5d row
    (§8.4);
  - fallback-only rows (§5.3).

**What a gap is.** An interval of at least `MAX_INTEGRATION_GAP_SECONDS` (300 s) that the row's
integrator didn't integrate. It arises in three ways:

| Cause | Detected by | Gap |
|---|---|---|
| HA was down | setup: restored `last_at` vs now | `[last_at, first sample after setup)` |
| Local and cloud both down past 300 s (OFFLINE), or the source row stopped publishing | `EnergyAccumulator.add` returns a refused gap | `[previous sample, this sample)` |
| The integrator was reset (None input for over 300 s, a source row that came back) | same | same |

- Each integrated row persists `last_at`, the UTC wall-clock time of its last integrated sample, in
  its RestoreSensor extra data (`extra_restore_state_data`) together with the total. That keeps the
  pair consistent, and §8.2's recorder cross-check corrects both after a crash.
- The accumulator runs on monotonic time. The entity keeps the wall-clock time of each sample next
  to it, so a gap is always recorded in UTC.
- Gaps under 300 s are never recorded. During CLOUD the rows integrate fallback values, so a normal
  failover is not a gap.
- Gaps are stored in `helpers.storage.Store` key `span_ebus.<entry_id>.backfill`, version 1, written
  with `async_delay_save` (10 s) and removed in `async_remove_entry`:

```json
{"gaps": [{"id": "<row key>|<start ISO>", "row": "site_energy",
           "start": "<UTC ISO>", "end": "<UTC ISO>",
           "status": "pending", "attempts": 0, "next_try": "<UTC ISO>",
           "hours": null, "sum_before": null, "reason": null}]}
```

- Overlapping gaps of the same row are merged. Gaps that start more than 7 days ago
  (`MAX_BACKFILL_SECONDS`) are clipped to 7 days; the clipped part is dropped as `expired`.

**When it fetches.**
- **Triggers:** setup (2 minutes after `EVENT_HOMEASSISTANT_STARTED`), local reconnect, failback
  (`STOP_CLOUD`), and a newly recorded gap. Each trigger only calls `kick()`; the scheduler decides.
- **Settle delay.** A gap is fetched no earlier than its end plus 75 minutes. SPAN's buckets fill in
  late, and the ledger notes buckets can be revisited; 75 minutes also guarantees every hour the gap
  touches has been compiled by the recorder.
- **Rate limit.** At most one history request per entry per 15 minutes. One request covers every
  pending gap that fits one window, for all rows (the site target returns every field at once).
- **Retries.** A failed or empty attempt sets `next_try` at +15 min, +1 h, +6 h and +24 h. After the
  4th failure the gap is `failed`. A gap still pending when it reaches 7 days old is `expired`.
- **While shown is CLOUD** nothing is fetched; failback kicks it.

**What it fetches.**
- `cloud_history` with the stored `site_history` target and the site's time zone, which comes from
  the trait snapshot at link time. Like the other targets, the time zone is stored but never logged.
- **Resolution:** QUARTER_HOURLY when the gap's padded window is 24 h or less, else HOURLY. The
  window is aligned with `align_start` and padded to the resolution's minimum window (7200 s).
- **Fields:** `home_import`, `solar_to_grid`, `solar_to_home`, `solar_to_battery` and
  `battery_import`, mapped to rows through `SITE_FLOW_ENERGY`.
- **Client:** if a fallback coordinator is running, its client; otherwise a short-lived client built
  from the stored tokens. It refreshes and saves tokens the way the coordinator does, and it is
  closed after the request.
- **Auth failure** counts as a failed attempt. It doesn't raise reauth from here; the failover path
  raises reauth when it actually needs the cloud (Q25).

**Allocation (pure `allocate`).**
- Each bucket's kWh is prorated by the fraction of the bucket that lies inside `[start, end)`.
  Buckets wholly inside the gap count whole. The two edge buckets count only their in-gap fraction.
- The integrator covered `[…, start]` and `[end, …]` itself, and the backfill covers exactly
  `[start, end)`. So the intervals are disjoint and nothing is counted twice. The proration error is
  at most one bucket edge each side, assuming even power within the bucket.
- The prorated kWh is summed per UTC hour start: `hours = [(h, kWh_h), …]`. Hours with 0 kWh are
  dropped.
- **No data.** A bucket that is missing, null, or all-zero across every field while local power
  showed draw on both sides counts as no data. The attempt fails and is retried.

**Writing: statistics adjustments only; the entity state is never changed.**
- For each `(h, kWh_h)` in order, call `async_adjust_statistics(statistic_id, h, +kWh_h, "kWh")`
  through `recorder_api.adjust`. Each call adds `kWh_h` to every short-term and hourly row from `h`
  onwards. So after all of them, the row at hour `h` has risen by `cum_h` (the gap energy up to and
  including `h`), and every row after the gap has risen by the whole gap.
- **Why adjustments, not a state step.** Adding the energy to the integrator's total would land it
  all in the hour of the backfill, which defeats the point of filling the gap hours.
- **Why not `async_import_statistics`.**
  - It writes rows without shifting later sums, so it would need an adjust after it anyway.
  - It overwrites rows the recorder compiled.
  - For source `recorder` its validation varies across 2024.12 → current.
  - One API family (the §9.2 adjust, with its verified idempotency) is enough.
- **The entity state stays in step with statistics.** For a total_increasing sensor the recorder
  never compares the absolute state with the sum. It adds state *deltas*, and later compiles seed
  from the last (adjusted) row. The state keeps counting exactly as before, each future delta lands
  correctly, and the difference between state and sum is a constant offset, as it already is for
  any sensor whose statistics began after its counter. Nothing re-derives the sum from the state,
  so nothing drifts.
- **Hours where HA was down.** On startup the recorder's `CompileMissingStatisticsTask` compiles the
  missed periods from the last recorded state, giving flat rows, and the adjustments fill them.
  **Verify on the 2024.12 pin.** If a row is missing anyway, its energy lands in the next existing
  row: never lost, never doubled. That's tested.

**Idempotency, across restarts.** This reuses §9.2.5's rule: queue only after a DB read proves the
change isn't there.
1. Before queueing, read the hourly sums for every `h` in `hours` plus the last row before the
   first. Store them as `sum_before[h]`, set `status = queued`, and save the store
   (`async_save`, not delayed).
2. Queue all adjustments in one synchronous callback, with no await between them, so the recorder
   receives them as one contiguous run.
3. **Verify** by polling the same rows every 1 s for up to 30 s. The expected sums are
   `sum_before[h] + cum_h`. When they match, set `applied`.
4. **Partial application** (the recorder stopped mid-run) shows as a prefix: the first `j`
   adjustments took effect, so `row_h − sum_before[h] = cum_min(h, j)`. Verification finds `j` and
   re-queues only adjustments `j+1…n`, after re-reading. This is the only re-queue, and it's proven
   by the DB first.
5. **At setup**, each `queued` gap is re-verified the same way: `applied`, back to `ready` (nothing
   took effect), the remaining suffix, or `changed_since` for any other pattern, which is never
   touched again.
6. **`changed_since`** also covers a user who adjusted the statistic by hand between the read and
   the queue.

**No double counting with the energy-step repair.**
- The two sets of rows are disjoint by construction. §9.2 covers re-pointed *registers* (circuit
  energy, and `K_site/grid_energy` on a single-panel site); 5d covers *integrated* rows, which never
  get an energy-step entry.
- As a guard, 5d skips any statistic_id that is in a live `energy_step` store until that store is
  deleted.

**Cloud unavailable or no data.**
- The gap is left as it is and nothing is written.
- One debug log line carries the gap count, the attempt number and the status, never ids, serials,
  targets, hosts or the time zone.
- Retries are bounded as above. A `failed` or `expired` gap stays in the store for 7 days (so
  diagnostics can show counts), then it's pruned.

### 8.3.1 As built in 5d (rev 4.5, approved deviations)

- **Gating:** `should_run(mode, *, has_tokens, has_target, single_panel_site)`. The runner is never
  built on local_only, and never on a multi-panel site (for any row). The cloud's site history is
  site-wide, and how it splits across panels is unknown.
- **Gaps keyed by target:** each gap belongs to the entity's unique_id, not the row, because a linked
  row can have a local row and a kept stand-in with separate statistics. The entity_id is resolved
  through the entity registry at write time. There is no register hook. Queued gaps are re-verified
  on every run, after the recorder queue drains (`recorder_api.async_block_till_done`, 60 s timeout;
  on timeout the gap stays queued and the runner retries soon).
- **Retries:** the first attempt plus retries at 15 min, 1 h, 6 h and 24 h. A gap is FAILED on its
  5th failed attempt.
- **Partial write:** a partial prefix after the 30 s verify stays QUEUED and is never re-written
  without drain-then-classify (`settle_queued(..., drained=True)`).
- **One gap per statistic per batch** (`ready_to_write`): the oldest READY gap per target, and none
  while that target has a QUEUED gap.
- **`allocate(buckets, gap_start, gap_end, components)`.** A bucket with count==0 is null (no data).
  Hours of `MIN_HOUR_KWH` (2 × sum tolerance) or less are dropped, so at most one prefix fits. The
  snapshot includes the first row after the last hour.
- **No data:** missing, null or zero-count buckets always mean no data. All-zero values mean no data
  only when `draw` is true. Draw is true when the last sample before the gap and the first one after
  it are both above 0 W. `last_w` is saved in the restore data; when it's missing after a restart,
  draw is false.
- **HA-down gap:** [restored last_at, first sample), clamped to the last MAX_AGE (`restart_gap`) and
  dropped only when its length is zero or negative.
- **Time zone:** `hass.config.time_zone or "UTC"`, never logged, nothing new stored.
- **Cloud client:** short-lived, from the token file (written from entry.data only when missing).
  Refreshes stay in the file, matching the coordinator. `cloud_auth.token_lock(path)` (a per-path
  RLock) serializes load, refresh and save across all callers, and save and discard take it too. No
  save happens after a refresh if the file is gone. A stop flag is checked inside the lock.
  CloudCredentialsRejected counts as a failed attempt, with no reauth.
- **Deferrals** (no attempt counted): the transport's `cloud_in_use`, the recorder not loaded, and an
  entity tracked by energy_step. An entity that isn't recorded or has no sum gives up at once
  (FAILED no_statistics).
- **Known, pre-existing:** a cloud start rewrites the token file from entry.data. If the cloud ever
  rotates refresh tokens, a refresh saved only to the file is lost on restart. This isn't new in 5d.

### 8.4 Site grid energy (rev 4.2; Q24 decided)

**What the cloud measures.** `K_site/grid_energy` counts the energy flowing *into the panel*, which is
the same point as `lugs/U/meter/imported-energy`. On a site with solar or a battery upstream of the
panel that's house draw, not utility import, on both sides alike.

**The gate.** Every grid-energy mapping depends on `link.single_panel_site`, which means K's cloud
site has exactly one panel (`site_panel_count`, from `find_panel` at link time and refreshed at each
cloud start). When a site holds several panels, the site total doesn't map onto one panel's lugs.

| | Single-panel site | Multi-panel site |
|---|---|---|
| Direction A (link) | `K_site/grid_energy` is re-pointed onto `S_lugs/U/meter/imported-energy` and gets an energy-step entry (§9.2) | **Left alone.** It keeps its K uid as a fallback-only energy row (§5.3): it grows only during CLOUD, from the ledger re-seeded at each cloud start. No re-point, no energy step. The legacy repair lists it |
| Direction B (add cloud) | The lugs import entity continues by cloud delta during CLOUD (below) | No mapping. The register holds during CLOUD, as every local register does |
| 5d backfill | Never (it's a register) | Never |
| Local sum of panel imports | Never | Never: it double counts in a lug-fed topology, and aggregation can't be told apart from topology without a capture (Q28) |

- A panel count that changes after linking only switches the direction-B style fallback mapping on
  or off. It never re-points, un-points or steps anything after the upgrade.

**Outage continuation** (`fallback.RegisterContinuation`, single-panel site only). The cloud total and
the panel register count from different origins, so the cloud total is never shown. Only its growth
is added on top of the last local value:
- **LOCAL:** shown = the local register. The continuation remembers `local_last` and the highest
  value shown, `high`.
- **Cloud start:** the cloud mark is unset. The first cloud reading only sets it (the `adopt`
  semantics of §5.3) and adds nothing.
- **CLOUD:** shown = `local_last + max(0, cloud − mark)`. If the cloud value goes backwards (a ledger
  re-seed), the mark is re-seeded at that value and the shown value holds.
- **No local value since setup** (HA restarted during the outage): no continuation. The row behaves
  as a register does today (§10).
- **Failback:** shown = `max(local, high)`. It holds at `high` until the register passes it, so the
  state never decreases and `total_increasing` sees neither a dip nor a reset. Over the outage the
  total growth is the register's own; the cloud only spreads it across the outage hours. If the cloud
  undercounted, the remainder lands when the register resumes.
- **A real register reset** (the local value is below `0.9 × local_last`, e.g. a replaced panel)
  passes straight through and clears `high`, so the hold can't freeze the row indefinitely.
- Presented with ENERGY_PRECISION and write on change. Nothing is persisted: a restart drops the
  continuation, which is the "no local value since setup" case.

## 9. Repairs

### 9.1 Legacy repair (5c)

- **Issue:** `legacy_entities_<entry_id>`, translation_key `legacy_entities`, `is_fixable=True`,
  `severity=WARNING`.
- **When it is raised:** at setup, when the registry holds entities of this entry whose uid matches a
  legacy cloud pattern and that are neither produced by a current local row nor a stand-in. That
  covers:
  - cloud-keyed duplicates from pre-5a linked entries;
  - unmatched-circuit cloud entities from direction A (Q8);
  - fallback-only rows (Q19), including `K_site/grid_energy` on a multi-panel site (§8.4). They
    still work during an outage, so the text says "only updated while the local connection is down"
    and the user chooses;
  - on local_only, fallback-only rows (they never update there).
- **Stand-ins are never listed.** They're fed and current.
- **Fix flow:**
  - A confirm step lists counts and up to 20 entity_ids, never unique ids.
  - Submit runs `er.async_remove` on each one and deletes the issue.
  - Statistics stay in the recorder.

### 9.2 Energy step: automatic correction (5b)

**Why there's a jump.**
- A re-pointed register energy entity (circuit energy, and `K_site/grid_energy` on a single-panel
  site) changes source. It
  goes from the cloud ledger total to a panel register (circuit `exported-energy`, lugs
  `imported-energy`), which counts from when the panel was installed.
- The recorder's `total_increasing` compiler (`sensor/recorder.py`) then sees one step from
  `cloud_last` to `first_local`:
  - **Reset** if `first_local < 0.9 × cloud_last`: the sum adds the whole new value, so the step is
    `first_local`.
  - **Otherwise** the sum adds `first_local − cloud_last`. If that is negative it is a "dip": HA
    warns and the sum goes down.

```python
def expected_step(cloud_last, first_local):          # kWh, pure
    return first_local if first_local < 0.9 * cloud_last else first_local - cloud_last
```

(Rev 2 treated every drop as a reset, which was wrong for drops of 10% or less.)

**The correction.** Subtract `step` from the statistic's sum, from the 5-minute bucket that holds
the first local state onwards. Integrated rows (overhead and site energies, and their stand-ins) are
not affected: they continue from their restored totals, so they never get a store entry.

#### 9.2.1 HA API used (verified on HA 2026.9; re-verify on the 2024.12 test pin)

- **The adjust call.** `get_instance(hass).async_adjust_statistics(statistic_id, start_time,
  sum_adjustment, adjustment_unit)`.
  - It is a `@callback` instance method on `Recorder`, not a module function. It has existed since
    about 2022.10 and backs the Developer tools "adjust sum" dialog.
  - It queues an `AdjustStatisticsTask` and returns None. It gives no result and no completion
    signal.
- **What the task does** (`statistics.adjust_statistics`):
  - It converts `adjustment_unit` into the statistic's unit.
  - It adds the adjustment to `sum` for every short-term row with `start_ts ≥ start_time`, and every
    hourly row with `start_ts ≥ start_time.replace(minute=0)`.
  - If the statistic has no metadata, it silently does nothing.
  - **It is not idempotent:** running it twice subtracts twice.
- **Later compiles** seed from the last (now adjusted) row, so the correction carries forward. An
  hour that isn't compiled yet is compiled from adjusted short-term rows.
- **The recorder queue** serialises the adjust against compiles, so there is no race with the
  5-minute compile.
- **Reads** run on the recorder executor via `get_instance(hass).async_add_executor_job`:
  - `statistics_during_period(hass, start, end, {statistic_id}, "5minute", {"energy": "kWh"},
    {"sum"})`;
  - `get_last_short_term_statistics(hass, 1, statistic_id, True, {"state"})`;
  - `get_metadata(hass, statistic_ids={statistic_id})`.
- **Recorded check:** `recorder.is_entity_recorded(hass, entity_id)`, imported from the
  `homeassistant.components.recorder` package (it is re-exported there in 2026.9).
- **Manifest:** add `"after_dependencies": ["recorder"]`. Recorder imports stay lazy, inside
  `recorder_api.py`, which 5d reuses.
- **Units.** Every read requests kWh explicitly, and the adjust passes `adjustment_unit="kWh"`. The
  recorder converts if the statistic's unit is Wh or MWh, so the integration does no unit math.

#### 9.2.2 Stored record

`helpers.storage.Store` key `span_ebus.<entry_id>.energy_step`, version 1:
- It is written in upgrade step 6.iv, before the reload.
- It is deleted when finished, and removed in `async_remove_entry`.
- It replaces rev 2's `entry.data[CONF_ENERGY_STEP]`. Progress writes would otherwise churn the
  entry.

```json
{"t_repoint": "<UTC ISO>", "issue_raised": false,
 "entities": {"sensor.kitchen_energy": {
    "cloud_last": 1234.567, "first_local": null, "first_at": null,
    "start": null, "step": null, "sum_before": null,
    "status": "waiting_first", "reason": null}}}
```

Statuses:

| Status | Meaning |
|---|---|
| `waiting_first` | No local numeric state seen yet |
| `waiting_stats` | Step known; the bucket hasn't been compiled yet |
| `ready` | Can be applied |
| `queued` | Adjust queued; not yet verified |
| `applied` | Done |
| `none` | `\|step\| < 0.001 kWh`; nothing to do and not listed |
| `skipped` | Can't be applied; `reason` says why |

#### 9.2.3 Computing amount and start

- **First local value.**
  - `EnergyStepTracker` subscribes with `async_track_state_change_event` to the entities still in
    `waiting_first`.
  - The first new state that is numeric with unit kWh (or convertible) sets `first_local` = float
    state in kWh and `first_at` = `state.last_updated` in UTC.
  - Unavailable or unknown states in between are ignored, and so does the compiler ignore them.
- **Step and start.**
  - `step = expected_step(cloud_last, first_local)`.
  - If `|step| < 0.001` the status is `none`.
  - Otherwise `start` = `first_at` floored to the 5-minute boundary (UTC). That is the short-term
    bucket B that compiles the step.
  - The hourly correction starts at `start.replace(minute=0)`, which the recorder computes itself.
  - The status becomes `waiting_stats`.
- **Rounding.** `first_local` is the presented (rounded) value, which is exactly what the recorder
  saw. `cloud_last` is the last state the recorder saw from the cloud entity.

#### 9.2.4 Gate: the notice appears only after the first local statistic exists

The tracker checks at setup and every 5 minutes while any entity is `waiting_*`.

For each `waiting_stats` entity, it runs these pre-checks:
1. `"recorder" in hass.config.components`. If not, skip with `recorder_not_loaded`.
2. `is_entity_recorded`. If not, skip with `excluded_from_recorder`.
3. The metadata exists and has `has_sum`. If not, skip with `no_statistics`.

It then reads rows from `[start − 1 h, start + 5 min)`:
- **If the row for B exists:**
  - Let `prev` be the last row before B.
  - **Sanity check.** If `prev` exists and `sum_B − sum_prev < step − 0.01`, skip with `unexpected`.
    The recorded change doesn't contain the step, so applying it would corrupt the sum. A dip
    (negative step) passes naturally.
  - Otherwise store `sum_before = sum_B` (kWh) and set the status to `ready`.

**Timeout.** 24 h after `t_repoint`, entities still `waiting_*` are skipped with
`no_statistics_recorded`.

**Raising the issue.**
- **Issue:** `energy_step_<entry_id>`, translation_key `energy_step`, `is_fixable=True`,
  `severity=WARNING`, `is_persistent=False`.
- It is raised once no entity is `waiting_*` and at least one is `ready` or `skipped`. Before that,
  nothing is shown.
- It is recreated at each setup from the store while the store exists.
- If every entity is `none`, the store is deleted and nothing is raised.

#### 9.2.5 Applying it once: idempotency

**`async_apply()`** holds a per-entry `asyncio.Lock`, so a double-clicked or retried flow
serialises. For each `ready` entity:
1. Re-read `sum_B` (kWh).
2. Compare it with the stored values:
   - **`|sum_B − sum_before| ≤ 1e-6`:** not applied yet. Set the status to `queued`, save the store,
     then call `async_adjust_statistics(statistic_id, start, -step, "kWh")`.
   - **`|sum_B − (sum_before − step)| ≤ 1e-6`:** already applied, by an earlier run whose status
     write was lost. Set the status to `applied` without queueing.
   - **Anything else:** skip with `changed_since`. The user or something else has adjusted it; it is
     never touched.
3. **Verify.** Poll `sum_B` every 1 s, for up to 30 s, until it equals `sum_before − step`. Then set
   the status to `applied`. If it isn't seen in time, the status stays `queued`.

**At the next setup**, each `queued` entity is re-verified from the DB and never re-queued:
- `sum_before − step` → `applied`;
- `sum_before` → back to `ready`. The task was lost, for example because HA stopped first, so the
  issue is raised again;
- anything else → `changed_since`.

So an adjustment is queued only after a DB read proves it isn't there. A queued task is never
re-queued in the same run. Across a restart, the recorder has either run the task or lost it, and
the DB says which. **No path applies it twice.**

**Ignore.**
- If the user picks HA's "Ignore" (`dismissed_version` set on the issue), the tracker deletes the
  store at the next check and stops. "Ignore" means "keep the jump".
- The legacy acknowledge-only behaviour from rev 2 is removed.

#### 9.2.6 Repair flow

- **Step `confirm`** (no fields). It shows:
  - a plain explanation;
  - `{entities}`: the `ready` list as `- sensor.kitchen_energy: 1234.567 kWh`;
  - `{skipped}`: the skipped list with plain reasons, or "none".

  Submit calls `async_apply()`.
- **Step `result`** (no fields). It shows `{applied}`, `{skipped_count}` and `{skipped}` (each with
  its reason). Submit finishes the flow:
  - When every entity is terminal (`applied`, `skipped` or `none`), the store and the issue are
    deleted. The notice never returns.
  - If some are still `queued`, the issue is deleted and the store kept. The next setup verifies
    them silently and re-raises only if a task was lost.
- **What the text contains.** Lists show entity_ids and kWh only, never unique ids. The hour is
  shown in HA's configured time zone and never logged. Logs carry counts and status names only.


### 9.3 strings.json additions

These are titles and descriptions under `issues`. There are no entity `description` keys.

```json
"issues": {
  "legacy_entities": {
    "title": "{title} has old cloud entities",
    "fix_flow": {"step": {"confirm": {
      "title": "Remove old cloud entities",
      "description": "{count} entities from the earlier cloud setup of {title} are not updated from the panel. Some of them still update while the local connection is down.\n\n{entities}\n\nTheir history stays available in Statistics. Select Submit to remove them."
    }}}
  },
  "energy_step": {
    "title": "{title}: fix a one-time jump in energy history",
    "fix_flow": {
      "step": {
        "confirm": {
          "title": "Fix the energy jump",
          "description": "{title} now reads circuit and grid energy from the panel's own meters instead of your SPAN account. Those meters count from when the panel was installed, so the Energy dashboard recorded one false jump around {hour} for these sensors:\n\n{entities}\n\nSelect Submit and Home Assistant removes that jump from their history. Nothing else changes. Choose Ignore if you want to keep it.\n\nCan't be fixed automatically:\n{skipped}"
        },
        "result": {
          "title": "Energy history fixed",
          "description": "Fixed {applied} sensors. The Energy dashboard updates within a few minutes.\n\nNot changed ({skipped_count}):\n{skipped}"
        }
      }
    }
  }
}
```

The `{skipped}` placeholder is built in code from these reason texts. They live as translated
strings under `issues.energy_step.reasons`, not as entity descriptions:

| Reason | Text |
|---|---|
| `excluded_from_recorder` | "not recorded" |
| `recorder_not_loaded` | "recorder not running" |
| `no_statistics` / `no_statistics_recorded` | "no statistics recorded" |
| `no_prior_reading` | "no earlier reading" |
| `unexpected` | "history doesn't show the jump" |
| `changed_since` | "already adjusted by hand" |

5d adds no issue and no strings. Backfill is silent; its counts appear only in diagnostics.

Options-flow strings:
- **`cloud_add` (5a):**
  - title "Add cloud fallback";
  - description: "Optional. If the local connection fails, Home Assistant reads the panel through your SPAN account until it recovers. Controls stay local only. While the local connection works, Home Assistant only contacts your account to fill gaps in solar, battery and site energy after an outage.";
  - confirm `cloud_add_confirm` with `{matched}`, `{total}` and `{unmatched}`;
  - abort `panel_on_cloud_entry`.
- **`link_local` (5b):**
  - title "Link the local panel";
  - fields `host` and `passphrase`;
  - `link_confirm` with `{matched}`, `{total}`, `{standins}` ("kept, now fed by the panel"),
    `{fallback_only}` ("only updated while the local connection is down") and `{unmatched}`
    ("no longer updated");
  - `link_confirm` also takes `{grid_energy_note}` (R20, Q28): empty on a single-panel site;
    otherwise "Site grid energy is one of the sensors only updated while the local connection is
    down, because this site has several panels. If your Energy dashboard uses it, switch to the
    panel's imported energy sensor.";
  - aborts:
    - `multi_panel_upgrade` ("This account has several panels. Remove the entry and add each panel locally instead.");
    - `uid_collision`;
    - `migration_failed`.
- **`cloud_remove` (menu option "Remove cloud fallback")**, with the warning
  `cloud_remove_fallback_only` on direction-A entries: "{count} sensors that only your SPAN account
  provides stop updating. You can remove them afterwards."
- **5o:** entity translation keys `overhead` ("Panel overhead"), `overhead_energy` ("Panel overhead
  energy"), `site_energy` ("Site energy"), `pv_energy` ("Solar energy") and `battery_energy`
  ("Battery energy").

Rev 3's `{cloud_only}`, `{unpollable}` and `local_only_cloud_rows` strings are removed.

## 10. State matrix changes

| Kind | LOCAL up | LOCAL down (shown LOCAL) | CLOUD, mapped value | CLOUD, mapped, no reading | CLOUD, unmapped | OFFLINE |
|---|---|---|---|---|---|---|
| MEASUREMENT | VALUE | UNAVAILABLE | VALUE | UNKNOWN | UNKNOWN | UNAVAILABLE |
| ENERGY | VALUE if value | VALUE if value | VALUE if value (local register) | same | same | UNAVAILABLE |
| Lugs import register, single-panel site (rev 4.2) | VALUE (held at `high` after failback until the register passes it) | VALUE if value | VALUE (`local_last` + cloud delta, §8.4); no local value since setup → as ENERGY | VALUE (held) | — | UNAVAILABLE |
| STATIC | VALUE | VALUE if value else UNAVAILABLE | VALUE if value else UNKNOWN | same | same | UNAVAILABLE |
| CONTROL relay | VALUE | UNAVAILABLE | **VALUE, read-only** | **UNAVAILABLE** | UNAVAILABLE | UNAVAILABLE |
| CONTROL priority | VALUE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE |
| Derived overhead power (5o) | VALUE when all inputs exist, else UNKNOWN | UNAVAILABLE | VALUE from the fallback inputs, else UNKNOWN | UNKNOWN | UNKNOWN | UNAVAILABLE |
| Integrated energy (overhead, site, pv, battery; 5o) | VALUE (integrating) | VALUE (held; gap recorded after 300 s) | VALUE (integrating fallback inputs) | VALUE (held) | VALUE (held) | VALUE (held; gap recorded) |
| Stand-in (5b) | as its source row | as its source row | as its source row | as its source row | as its source row | as its source row |
| Fallback-only power (5b) | UNAVAILABLE | UNAVAILABLE | VALUE from the coordinator | UNKNOWN | — | UNAVAILABLE |
| Fallback-only energy (5b) | VALUE (held) | VALUE (held) | VALUE (ledger, re-seeded at cloud start) | VALUE (held) | — | VALUE (held) |
| Unmatched circuit (5b) | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE |
| Any of the above, local_only | stand-ins and integrated rows as LOCAL; fallback-only power UNAVAILABLE, energy held | | — | — | — | |

- "Held" means the restored or last total, shown as the state (the integrated rows are
  RestoreSensors, so the Energy dashboard sees no unavailable gap and no reset).
- `presence()` gains a `control_fallback_has_value` input.
- Relay commands on CLOUD raise `local_unavailable`, as they do today. An UNKNOWN relay shows as
  UNAVAILABLE.
- `relay_blocked` (4b) still hides the relay on BESS and PV feeders.

## 11. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | K ≠ S (Q1) | Match through `answers_to`; uids use S; device identifiers merged |
| R2 | Sign conventions (circuit power, grid, feed and feedthrough power) | Grid sign pinned explicitly (§5.3) with a raw-Homie fixture test; fixture tests against existing cloud entity values and the PR 39 capture |
| R3 | Energy jump on upgrade; the automatic adjust is not idempotent and gives no completion signal | Exact step math including the dip rule; gate on the compiled bucket; DB-read proof before every queue; `queued` re-verified, never re-queued; sanity check; skip with reason |
| R4 | Partial registry rewrite | Collision pre-check, reverse plan, entry written last |
| R5 | Matcher mis-pairs after a breaker move | Exact match; ambiguity means no match; re-match on cloud start |
| R6 | No BESS/PV/EVSE/MID hardware; MLO untested | No fallback mapping for those scopes; site pv/battery stand-ins follow SpanPanel/span's convention and are flagged in release notes (R14) |
| R7 | Cloud tick rate | Coalescer, rounding, write-on-change |
| R8 | Local-only touching the cloud | Guards in `fallback_value`; `GapBackfill` not constructed on local_only, and it re-checks the mode before every request; a test asserts no cloud constructor and no HTTP call in local_only, including with stand-ins, fallback-only rows and stored gaps present |
| R9 | Identifier leaks | Repairs list entity_ids; logs use counts; `site_history` and the site time zone redacted and never logged; fixtures use `nt-0000-test` |
| R10 | *Retired in rev 4* (was the cloud-only poll cost; there is no poller) | — |
| R11 | Downgrade below PR 3 after linking | Release-note warning; Q16 alternative |
| R12 | Feeder-kind decoding (trait 1/21) is unconfirmed | Link-time value classification of feeds (§5.3) covers missing kinds; the kind wins when present; a feed both methods leave ambiguous is fallback-only and re-tried at each cloud start |
| R13 | Recorder API drift between 2024.12 and current | Tests run on the 2024.12 pin with the real recorder fixture; one wrapper module (`recorder_api.py`) owns every recorder call for 5b and 5d |
| R14 | PV and battery flow signs and the battery energy direction are unconfirmed (no hardware) | Constants `PV_TO_CLOUD_SIGN` and `BATTERY_TO_CLOUD_SIGN` follow SpanPanel/span and are pinned by fixtures; fallback mapping stays off until confirmed; release notes flag the stand-ins and integrated rows (Q21) |
| R15 | A statistics row is missing for a gap hour (HA down, missing-period compile didn't run) | The adjust lands the energy in the next existing row: never lost, never doubled; tested by deleting a row in the fixture |
| R16 | SPAN revises buckets after 5d fetched them | Settle delay of 75 min after the gap end; fetch once, accept later revisions (Q26) |
| R17 | RestoreSensor data is up to 15 min stale after a crash, so the restored total and `last_at` lag the recorder | Recorder cross-check at setup (§8.2) adopts the larger recorded state and its time; tested with a crash fixture |
| R18 | Duplicate values: `K_site/voltage_*`/`frequency` next to the re-pointed panel rows, and `K_panel/power` next to the main feed | By design (identity); both read one source; listed in release notes (Q27) |
| R19 | Backfill cost and cloud rate limits | Only after a real gap ≥ 300 s; one request per entry per 15 min covering all rows; at most 4 attempts per gap; nothing while CLOUD; 7-day cap. A healthy local entry makes zero cloud requests |
| R20 | Site grid energy on a multi-panel site (rev 4.2): a direction-A `K_site/grid_energy` that the Energy dashboard uses stops growing while local is healthy, because it becomes fallback-only | *(Decided.)* No local sum (it can double count); release notes; the `{grid_energy_note}` on the link confirm form, with it counted in `{fallback_only}` (§9.3); the legacy repair lists it so the user can pick the local lugs import entity instead |

## 12. Test plan

### Unit tests (pure)

- **Matcher:**
  - exact match;
  - pole mismatch;
  - remote CT;
  - duplicate space set;
  - MAIN 40 numbering 9–48;
  - empty list.
- **`resolve`:** parametrised over `FALLBACK_KEYS`, plus:
  - unmapped → NO_FALLBACK;
  - mapped but missing → None;
  - relay OPEN, CLOSED and UNKNOWN;
  - voltage falls back to `site/*`;
  - feedthrough mapped only with feeder kind 4;
  - grid: cloud +1200 → local `grid_power` +1200 (identity on facts values);
  - UPSTREAM `meter/imported-energy` maps only when `link.single_panel_site`; NO_FALLBACK otherwise.
- **`RegisterContinuation`:**
  - the first cloud reading adds nothing; later readings add their delta to `local_last`;
  - a cloud value going backwards re-seeds the mark and the shown value holds;
  - failback with the register below `high` holds at `high`, then follows the register once past it;
    the shown sequence never decreases;
  - failback with the register above `high` steps up to it;
  - a register below `0.9 × local_last` passes through and clears `high`;
  - no local value since construction → `cloud()` returns None.
- **`overhead_w`:**
  - no feedthrough;
  - with a feedthrough (subtracted);
  - floored at 0;
  - None if the feed, any circuit, or a required D is None;
  - D ignored when `has_feedthrough` is False;
  - PR 39 two-panel fixture: the upstream overhead is ≥ 0 and small, and the downstream overhead is
    computed from its own lugs.
- **Cloud classification:**
  - one unclassified feed → as today;
  - two unclassified → None;
  - kinds 3 and 4 → the feedthrough is subtracted.
- **`EnergyAccumulator.add`:** returns None on normal samples, returns the gap length (and doesn't
  integrate) for a gap over `max_gap_seconds`, and returns None at exactly the limit.
- **Stand-in table:**
  - every `STANDINS` entry names an existing local row;
  - grid: a raw Homie `power-flows/grid` of −1200 W yields +1200 W on `site/grid`;
  - pv and battery transforms equal the pinned constants;
  - `standins_for` returns only uids present in the registry input.
- **`rewrite_plan`:**
  - every §5.1 row maps to exactly one of `repoint`, `standin`, `fallback_only` or `unmatched`;
  - circuit energies land in `energy`; integrated rows never do;
  - `K_site/grid_energy` lands in `repoint` and `energy` when `site_panel_count == 1`, and in
    `fallback_only` (not `energy`) when it is 2 or more;
  - feeds unclassified by kind are value-classified to U/D (fixtures: two clearly separated feeds; U and D within 100 W → unclassified → `fallback_only`; the kind overrides the value check);
  - the K ≠ S case;
  - no planned uid collides with a current local uid (a property test over all rows).
- **`allocate`:**
  - buckets wholly inside the gap count whole;
  - the two edge buckets are prorated by overlap;
  - a gap inside one bucket gets only its fraction;
  - summed per UTC hour, across a DST change in the site time zone;
  - zero-kWh hours dropped;
  - quarter-hourly and hourly inputs give the same hourly totals for even power.
- **Gap merge and expiry:** overlapping gaps of one row merge; a gap starting over 7 days ago is
  clipped; the retry schedule is +15 min, +1 h, +6 h, +24 h, then `failed`.
- **Prefix detection:** for `cum` and observed deltas, find `j` for j = 0…n, and flag any other
  pattern as `changed_since`.
- **`expected_step`:**
  - a jump up;
  - equal values;
  - a dip within 10% (negative step);
  - exactly 0.9× (a dip, not a reset);
  - a reset below 0.9× (step = `first_local`);
  - a zero `cloud_last`.
- **Start bucket:** `first_at` at :00, :04:59.9 and :05 → the right 5-minute floor, in UTC across a
  DST change.
- **Presence matrix** additions: derived overhead, integrated energies, stand-ins, fallback-only rows
  and unmatched circuits.

### HA tests (pytest-homeassistant-custom-component, HA 2024.12.0)

- **Fallback:**
  - CLOUD shows rounded cloud values;
  - energy registers are unchanged;
  - priority is unavailable;
  - the relay is read-only (commands raise `local_unavailable`);
  - recompute happens on cloud ticks only while CLOUD;
  - failback.
- **No cloud while local is healthy:** a linked entry with LOCAL up for a simulated day makes no
  cloud connection and no HTTP request (no gaps recorded).
- **local_only:**
  - no cloud coordinator, no `GapBackfill` and no network call, even with stand-ins, fallback-only
    rows and a stored backfill gap in the fixture;
  - `fallback_value` is NO_FALLBACK;
  - stand-ins and integrated rows keep updating from local values;
  - fallback-only power rows are unavailable and energy rows hold.
- **5o derived rows:**
  - a local-only entry gets overhead power and energy, plus site, pv and battery energy only where
    the source flow is published;
  - values follow the formula on snapshot updates;
  - energy integrates and restores after a restart;
  - a gap longer than 300 s is not bridged;
  - **crash restore:** restore data 15 min older than the last recorded state → the row adopts the
    recorded total and time, and shows no backwards dip;
  - on a linked entry the energies continue across failover and failback with no drop;
  - the cloud entry's overhead uids and values are unchanged on a single-feed panel.
- **Direction B:**
  - registry rows that existed before are unchanged (uids and entity_ids);
  - no entity is created;
  - minor_version is 2;
  - "Remove cloud fallback" restores 1.1 and leaves the local entities untouched;
  - **grid energy, single-panel site:** during CLOUD the lugs import entity grows by the cloud delta
    from its last local value, and after failback it never decreases;
  - **grid energy, multi-panel site:** the lugs import entity holds during CLOUD (no mapping), and 5d
    records nothing for it;
  - the panel count refreshed at a cloud start switches only the mapping;
  - the `panel_on_cloud_entry` abort.
- **Direction A:**
  - Starting from a registry and recorder fixture of cloud entities, with an Energy prefs fixture
    that references `stat_energy_from` (`K_site/grid_energy`, `K_site/solar_energy`) and
    `stat_consumption` (circuit energy):
    - after the upgrade, every entity_id is unchanged;
    - every statistic_id is unchanged, and the dashboard prefs resolve to entities that have states;
    - no recorder statistics issues are raised.
  - **Enabled state** of every stand-in, fallback-only and unmatched row is unchanged; nothing new
    is registered.
  - **Stand-ins** show local values (grid sign checked), follow failover, and suppress the duplicate
    local entity; the site energy stand-ins continue from their restored totals with no jump and no
    energy-step entry.
  - **`K_site/grid_energy`, single-panel site:** re-pointed onto the lugs register, with an
    energy-step entry.
  - **`K_site/grid_energy`, multi-panel site** (fixture: the entry's panel shares its site with a
    second panel): uid unchanged, no energy-step entry, classed fallback-only, listed by the legacy
    repair, grows only during CLOUD, and no gap is recorded for it by 5d.
  - **No local sum:** on a two-panel site fixture no entity's value equals or is derived from the sum
    of both panels' lugs imports.
  - **Fallback-only rows:** power updates only while CLOUD; energy grows only during CLOUD and the
    first cloud bucket after a long local period adds nothing (re-seeded mark); both are listed by
    the legacy repair.
  - **Unmatched circuits** stay unavailable in every transport and are listed by the legacy repair.
  - **"Remove cloud fallback"** on a direction-A entry keeps `links[K]` without tokens, stays 1.2,
    and stand-ins keep updating.
  - **Aborts and rollback:**
    - the `uid_collision` abort writes nothing;
    - an exception injected mid-plan rolls back;
    - a multi-panel account aborts.
- **Energy step**, with the real recorder fixture and `async_wait_recording_done`:
  - **Not raised early:** not before the first local state, and not before bucket B is compiled.
    It is raised after both.
  - **Correct amounts:** for a jump up, a reset and a dip:
    - the listed amounts;
    - after the fix, the short-term **and** hourly sums from B onward equal the pre-upgrade
      trajectory plus the real growth;
    - the rows before B are untouched.
  - **Applies exactly once:**
    - Submit twice, two concurrent flows, and a retried flow after a simulated status-write failure
      each leave one adjustment.
    - A restart with a `queued` status re-verifies and doesn't re-queue.
    - A lost task (an adjust patched to drop) returns to `ready` and is raised again.
  - **Changes made by hand:** an adjustment made before Submit → `changed_since`, untouched.
  - **Skipped and reported:**
    - an entity excluded from the recorder;
    - the recorder not loaded;
    - no metadata;
    - the 24 h timeout;
    - the sanity-check failure.
  - **Units:** statistic metadata in Wh → the correction is right in Wh.
  - **Ignore** → the store is deleted at the next check and nothing is applied.
  - **Clean up:** all `none` → nothing is raised and the store is deleted.
  - **Survives a restart:** the issue is recreated from the store while it is pending.
- **Gap backfill (5d)**, with the real recorder fixture and a mocked history client:
  - **Gap detection:**
    - HA down for 3 h (restored `last_at`) → one gap per integrated row;
    - local and cloud both down for 10 min (OFFLINE) → a gap; for 4 min → none;
    - a normal failover (CLOUD for 1 h) → no gap;
    - a crash with stale restore data → the gap starts at the recorded time, not the restored one.
  - **Fetch and merge:**
    - the request is made only after the settle delay, uses the stored target and time zone, and
      picks QUARTER_HOURLY for a 3 h gap and HOURLY for a 2-day gap;
    - after it, the hourly and short-term sums for the gap hours rise by the prorated cloud kWh, and
      every later row by the whole gap;
    - the entity state is unchanged, and the next local delta lands normally on top.
  - **No double count:**
    - at the edges: integrated energy before and after the gap plus the backfill equals the cloud
      total over the whole window, within one bucket's proration;
    - a restart with a gap `queued` re-verifies and doesn't re-queue;
    - a partial run (adjust patched to drop after the 2nd of 5) re-queues only adjustments 3–5;
    - a hand adjustment between read and verify → `changed_since`, untouched;
    - a statistic_id in a live `energy_step` store is skipped;
    - a missing statistics row for a gap hour → the energy lands in the next row, once.
  - **Cloud down:** the history call raising, returning nothing, or an auth error → nothing is
    written, one debug line with counts only, retries follow the schedule, then `failed`; no reauth
    issue is raised.
  - **Rate limit:** gaps on three rows and three triggers within 15 min → one request.
  - **While CLOUD:** no request; failback kicks it.
  - **Expiry:** a gap older than 7 days at setup → `expired`, no request.
- **Minor-version marker:**
  - `async_migrate_entry` returns True for 1.1, 1.2 and 1.3, and False for 2.x;
  - loading a 1.2 entry with the migrate function patched out (simulating PR 3/4) works.
- **Legacy repair:** the count is correct, stand-ins are never listed, and the fix removes exactly
  the listed entities.
- **Privacy:** log records, issue placeholders, the backfill store's debug output and diagnostics
  contain no fixture serial, host, passphrase, history target id or time zone.

## 13. Open questions (with recommendations)

1. **Is K equal to S on r202639?** Don't assume it in code: match through `answers_to` and use S for
   uids. Confirm with the capture.
2. **Relay over the cloud?** Yes, read-only.
3. **BESS SoC?** No mapping. The cloud has none.
4. **Duplicate cloud entities on linked entries?** Stop creating them in 5a, and retire existing ones
   only through the confirmed repair in 5c.
5. **Energy step?** "Fix it" applies the recorder adjustment itself, once, after the first local
   statistic exists (§9.2). "Ignore" keeps the jump. *(Decided.)* Rev 4 adds `K_site/grid_energy`
   to its scope on single-panel sites only (Q24).
6. **Round cloud values?** Yes, through `_present`.
7. **Multi-panel accounts?** Abort and point to remove and re-add.
8. **Unmatched circuits?** *(Rewritten for rev 4.)* No cloud feed in either direction, ever.
   - Direction A: their cloud entities stay, unavailable, and the legacy repair lists them.
   - Direction B: nothing is created; the local circuit has its own entities and simply gets no
     fallback value during an outage.
   - The fix is a better match (re-run on each cloud start), not a cloud feed.
9. **When to rebuild the link map?** Re-match circuits on every cloud start. Write options only when
   they change, and don't reload.
10. **Busbar current from `panel/current`?** Map it. Drop it to unmapped if the capture differs by
    more than 5%.
11. **Upstream power source?** `feed-<n>/power` (feeder kind 3, or the single feed), falling back to
    `panel/power`.
12. **`site/*` power entities?** *(Decided; rev 4 changes only the interim.)*
    - Re-point grid, home, solar and battery onto `power-flows/*` only once a capture shows matching
      values and signs.
    - Until then they keep their cloud uid as stand-ins fed from `power-flows/*` locally (§5.3).
13. **Entry versioning?** A minor-version marker on linked entries only (§7). *(Decided.)*
14. **Enabled state on upgrade.** *(Decided in rev 4.)* Every row is fed locally where a source
    exists and keeps the user's current enabled state. Nothing is registered disabled and nothing is
    polled.
15. **Panel overhead** *(decided).* Per panel, local calculation, every mode, in 5o (§8.1). It
    subtracts the feedthrough. Cloud uids are kept; local and linked entries use `derived/overhead*`;
    direction A re-points.
16. **Hard refuse on downgrade.** Not by default.
    - The alternative is to put major version 2 on linked entries only. Older releases would then
      refuse to load them, as a migration error with the data untouched, instead of running them
      degraded.
    - Revisit only if a user reports a broken downgrade below PR 3.
17. **"Remove cloud fallback."** In 5a for direction B (back to 1.1, nothing else changes). For
    direction A (5b) it keeps `links[K]` without tokens and stays 1.2, because stand-ins need K.
    Stand-ins keep working; fallback-only rows go to the legacy repair.
18. **Integrated site energies on every entry, in 5o.** *(Decided 2026-11-13: yes.)* `derived/site-energy`,
    `pv-energy` and `battery-energy` are created on local-only entries too (where the flow is
    published), enabled, because the official integration offers the equivalents and the panel has
    no register for them. This grows 5o from "overhead" to "derived rows". Alternative: create them
    only on linked entries in 5b. Not recommended: local-only users would lack solar and battery
    energy for the Energy dashboard.
19. **What's left without a local source** (re-evaluated in rev 4.1 under local sourcing). *(Decided
    2026-11-13: keep them as fallback-only rows.)*
    - Now local: `panel/power` (stand-in ← lugs U active power), `site/voltage_*` and `frequency`
      (stand-ins ← panel meter rows), the main feed and feedthrough feeds (re-pointed onto lugs U/D;
      unclassified feeds are value-classified at link time, §5.3).
    - Unmatched-circuit rows (including current) get no feed by decision (Q8), so they aren't in this
      set either.
    - **Still left:** the directional site flows (`grid_to_home`, `solar_to_grid`, …), `generator`,
      `balance`, `residual` and their energies. The panel publishes only the four net flows, so the
      splits can't be derived locally. Plus, in theory, a feed both classifiers leave ambiguous.
    - Recommended: keep them (identity), update them only during a fallback, list them in the
      legacy repair. They're rarely used (most dashboards use net flows). Alternative: derive the
      splits locally from net flows with a fixed priority (solar → home first, etc.); not
      recommended, because it would silently show numbers SPAN's own split doesn't match.
20. **Feeder-kind decoding.** Cloud overhead on feedthrough panels and the cloud mapping of `lugs/D`
    depend on trait `1/21` (3 = main breaker, 4 = feed-through lugs), decoded only from the
    contributor's notes. 5o ships with the fallback to today's behaviour. Confirm with a two-panel
    cloud capture if one ever becomes available; don't block on it.
21. **PV and battery signs, and the battery energy direction.** *(Decided 2026-11-13, as
    recommended.)* There's no hardware and no
    capture. Recommended: take `PV_TO_CLOUD_SIGN` and `BATTERY_TO_CLOUD_SIGN` from SpanPanel/span's
    handling of the same `power-flows` properties, pin them in fixtures, and keep the cloud *fallback*
    mapping for those two rows off until a user confirms. `derived/battery-energy` integrates the
    direction that matches cloud `battery_import`; which physical direction that is (charge or
    discharge) also follows SpanPanel/span. Release notes flag all of it.
22. **Overhead energy gaps aren't backfilled.** *(Decided 2026-11-13: accepted.)* The cloud has no history for the main feed or
    `panel/power`, so there's no source. Recommended: accept it. Alternative: reconstruct overhead
    from the site and circuit histories; not recommended (it compounds proration errors on a small
    number).
23. **Backfill write method.** *(Decided 2026-11-13: statistics adjustments only.)* Recommended: statistics adjustments only, entity state
    unchanged (§8.3). Alternative: bump the integrator's total by the gap energy as well, so the
    state equals what a gapless integrator would show. Not recommended: the recorder would count
    the bump as new growth in the current hour, so the adjust would then have to subtract it there,
    doubling the number of writes that must be exactly-once. The cost of the recommendation is a
    constant offset between state and sum, which no HA feature compares.
24. **Site grid energy and the lugs import register.** *(Decided 2026-11-13, §8.4.)* The cloud
    `site/grid_energy` measures energy into the panel, the same point as the lugs import register.
    - Single-panel site: direction A re-points it onto the register with the §9.2 step; direction B
      continues the register by cloud delta during an outage.
    - Multi-panel site: no re-point, no energy step, no fallback mapping, no 5d backfill. In
      direction A it stays as a fallback-only row.
    - Never summed locally across panels.
25. **No reauth from backfill.** *(Decided 2026-11-13, as recommended.)* Recommended: a backfill auth failure counts as a failed
    attempt and raises nothing. A user with an expired cloud login and healthy local shouldn't be
    nagged for an optional fill. The failover path still raises reauth when the cloud is really
    needed. Alternative: raise reauth after the 4th auth failure.
26. **Fetch once, or refresh revised buckets?** *(Decided 2026-11-13: fetch once.)* Recommended: fetch once after a 75-minute
    settle delay and accept later revisions. Re-fetching to apply deltas would need a second
    exactly-once write per gap for a small gain.
27. **Duplicate values in direction A.** *(Decided 2026-11-13: keep both.)* `K_site/voltage_*`/`frequency` and `K_panel/power`
    stay as stand-ins that show the same value as the re-pointed panel rows and main feed.
    Recommended: keep both (identity). Alternative: list them in the legacy repair as "duplicates"
    so the user can retire them; reasonable, but rev 4 doesn't, because they're current and correct.
28. **Do multi-panel sites aggregate grid energy?** *(Decided 2026-11-13: capture-gated, like Q12;
    R20 handled as recommended.)*
    Unknown whether a multi-panel site's `site/grid_energy` is the sum of its panels, the most
    upstream panel's feed, or something else. In a lug-fed topology the upstream feed already
    contains the downstream panel, so a local sum would double count.
    - Until a two-panel cloud capture with both panels' lugs registers shows what it is, rev 4.2
      applies Q24's multi-panel rules and sums nothing.
    - Once confirmed, a later release can map it (for example onto the most upstream panel's lugs
      register) the same way §5.1 does.
    - Consequence to accept now (R20): on a direction-A entry whose site has several panels, an
      Energy dashboard grid source of `K_site/grid_energy` stops growing while local is healthy.
      Decided: say so in the release notes and on the link confirm form (`{grid_energy_note}`, §9.3,
      counted in `{fallback_only}`); the user can switch the dashboard to the lugs import entity. This is reachable only when the
      entry carries one panel of a multi-panel site (accounts with several configured panels abort
      `multi_panel_upgrade`).
