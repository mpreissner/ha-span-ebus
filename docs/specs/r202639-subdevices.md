# PR 4b: r202639 subdevices and remaining platforms

Status: draft for user review (Architect). Local only. Never commit, push or link.
Builds on: `docs/specs/r202639-model.md` (4a), which is merged. Branch: `feature/local-subdevices` from `dev`.

## 0. Scope

What 4b adds:

- **Subdevices.** Each BESS, PV, EVSE and MID becomes its own HA device, with `via_device` pointing to the panel. The remote CT gets its own device too. The downstream lugs stay on the panel device (see §3.6).
- **Platforms.** `binary_sensor`, `number` and `lock`. `button` is specified but deferred to PR 6 (§6.3).
- **Root properties 4a left unmapped.** `door/state`, `status/wifi`, `status/ethernet` and `status/time-zone`. Also shed-forecast, shed policy and asserted-islanding where described.
- **Circuit and lugs properties 4a left unmapped.**
  - `connection/backed-up`, `connection/count` and `info/locations` become attributes.
  - `connection/fed-by-device-{id,type,status}` and `connection/feeds-device-{id,type,status}` become the link graph. No entities.
- **Goal.** Parity with upstream SpanPanel/span wherever the r202639 API exposes the data. Every divergence is listed in §9.

What 4b does not change:

- **4a semantics.** Grid sign (+ = import), rounding plus write-on-change, the 4a translation names, `CONFIRM_WINDOW` = 10 s and the optimistic/confirm command pattern are all unchanged.
- **Fallback.** Every new row calls `transport.fallback_value(...)`, which still returns `NO_FALLBACK`. PR 5 fills it.
- **Deployment.** HA-only, minimum HA 2024.12.0. No new dependencies.

Ground truth: what the MAIN 40 capture actually publishes.

| Area | Capture state |
|---|---|
| Root | Publishes `door/state` = CLOSED, `status/ethernet` = true, `status/wifi` = false, `status/time-zone` and `pcs/active` = `pcs/enabled` = true. |
| Root capabilities not described | shed, shed-forecast, doe |
| Circuits and lugs | All 29 circuits and both lugs describe `connection/*` and `info/locations` but publish no values. |
| Device classes absent | No `bess`, `pv`, `evse` or `mid` device. The MID would occupy spaces 1–8 on a MAIN 40, and none is published. |

So the only parts of 4b that can be tested live on the user's panel are:

- the four root properties;
- `pcs/active` and `pcs/enabled` as binary sensors;
- the empty-unmapped invariant.

Everything else is tested with synthetic fixtures that say so in their names (§8).

Today `build_snapshot(capture)` reports these unmapped keys, and 4b must map every one:

```
circuit/lugs  connection/backed-up, connection/count,
              connection/fed-by-device-{id,status,type},
              connection/feeds-device-{id,status,type}
circuit       info/locations
root          door/state, status/ethernet, status/time-zone, status/wifi
```

## 1. "Grid" labelling (applies to everything below)

On the user's site, solar and battery sit between the utility meter and the panel. The panel's "grid" is therefore whatever feeds the panel's main lugs. For this user that is net house consumption after solar and battery, not the utility grid.

Rules:

1. **Keep SPAN's names.** Use "Grid power", "Grid state" and "Grid forming entity" for parity with upstream and SPAN's app. Each of these entities gets a translation-key description in `strings.json` ("power at the panel's main feed"). The README gains a paragraph: "Grid means what feeds this panel. If solar or storage is installed upstream of the panel, it is not your utility import."
2. **No derived utility figures.** Derive nothing that assumes panel grid = utility. That rules out "self-consumption", "solar-to-grid", `grid = feed − pv` and similar.
3. **No islanding claims without evidence.** `grid_islandable`, `grid_forming_entity` and the MID enums are created only when a MID (or BESS) device exists on this panel. They are never fabricated as "GRID" or "off". Upstream's panel-level `grid_islandable` would read "off" on the user's site, which is wrong: the site can island upstream. Creating nothing avoids that.
4. **Defer relabelling.** Relabelling lug-fed panels ("Feed power") belongs to the multi-panel topology work, not 4b. See Q5.

## 2. Model changes (pure, `span_client/local/model.py`)

### 2.1 Classes, scopes, uids

- **Class constants.** `BESS_CLASS = "energy.ebus.device.bess"`, `PV_CLASS = "energy.ebus.device.pv"`, `EVSE_CLASS = "energy.ebus.device.evse"`, `MID_CLASS = "energy.ebus.device.mid"`. Match them on the same `$type` rule 4a uses for lugs and circuits. The `$type` string is the exact suffix the schema dump shows; the Coder confirms it against the 4a classification helper.
- **Scopes.** `Scope` gains `BESS`, `PV`, `EVSE` and `MID`.
- **uid helper.** `subdevice_uid(K, cls_short, device_id, key) = f"{K}_{cls_short}/{device_id}/{key}"`, where `cls_short` is one of `bess`, `pv`, `evse` or `mid`. Derived keys use the `derived/` prefix, as in 4a.
  - Example (synthetic): `nt-0000-test_bess/bess-01/soc/soc`.
  - Every new uid contains at least two `/`. So none can match the cloud-collision regex `^[^_]+_[a-z]+(-\d+)?/[a-z_]+(_energy)?$`, which allows only one `/`. The test in §8 asserts this for every row.
- **Root uids.** New root rows use `panel_uid`, for example `…_distribution-enclosure/door/state`.
- **`PRESENTATION` keys.** `PRESENTATION` is keyed by `row.key`, so keys must stay unique across all of `ROWS`. Two subdevice classes share key names (`info/model`, `status/status`). Prefix the key with the class short name, giving `bess:info/model` and `evse:status/status`. Add a test that row keys are unique. The uid uses only the property part after the `:`.

### 2.2 Facts dataclasses (frozen)

```python
@dataclass(frozen=True)
class Link:            # who this subdevice hangs off
    via_kind: str      # "circuit" | "lugs"
    via_id: str        # Homie device id of the circuit/lugs
    status: str | None # OK | LOST | DEGRADED | None (unpublished)

@dataclass(frozen=True)
class BessFacts:  device_id, state, described: frozenset[str], values: Mapping[str, Any],
                  vendor, model, firmware, link: Link | None
class PvFacts:    same shape
class EvseFacts:  same shape, plus settable_limit: bool, settable_lock: bool
class MidFacts:   same shape
```

- `values` holds only described, non-PII keys, already coerced by the 4a coercers (int, float, enum, bool).
- `Snapshot` gains `bess`, `pv`, `evse` and `mid`, each a `Mapping[str, XFacts]` sorted by device id.
- `CircuitFacts` and `LugsFacts` gain:
  - `backed_up: str | None`
  - `unit_count: int | None`
  - `location: str | None` (this is `info/locations`, a room label, not geography)
  - `feeds: Link | None` and `fed_by: Link | None`
  - Each is set only once the value is published.

### 2.3 Link pass

`build_snapshot` becomes three passes, all pure:

1. **Collect.** Classify every device by `$type` (as now), adding the four subdevice classes.
2. **Resolve links.**
   - For each circuit with `connection/feeds-device-id` = X, where X is a known subdevice: X gets `Link("circuit", circuit_id, feeds-device-status)`.
   - For each lugs device with `fed-by-device-id` = X, where X is a known subdevice: X gets `Link("lugs", lugs_id, fed-by-device-status)`. This is a BESS upstream of the panel.
   - **Conflicts.** If two circuits claim the same device, the lowest circuit id wins and the conflict goes into `Unmapped` as a note. Never raise.
   - **Unknown targets.** An id that matches no device is kept only in that circuit's `feeds` link. It produces no entity and nothing is logged.
3. **Build facts.**

### 2.4 Read sets and PII

- **Read sets.** `_ROOT_READS` gains `door/state`, `status/ethernet`, `status/wifi`, `status/time-zone`, `shed/*`, `shed-forecast/*` and `shed/policy`. `_LUGS_READS` and `_CIRCUIT_READS` gain all of `connection/*` and `info/locations`.
- **New read sets.** `_BESS_READS`, `_PV_READS`, `_EVSE_READS` and `_MID_READS` cover the schema properties in §4, plus `info/*` minus PII.
- **`status/time-zone` joins `PII_KEYS`.** It reveals coarse location. It is read only to set the `Unmapped` invariant and the probe redaction, and is never exposed as an entity or attribute. HA already has the zone.
- **Subdevice PII.** `pii_keys(description)` returns `IDENTIFYING_KEYS` for the four subdevice classes, as well as for ROOT. Subdevice `info/serial-number` is never read into facts, the Store or attributes. `store_payload` already strips `IDENTIFYING_KEYS` from non-root devices; a test pins that.
- **`doe/export-limit` and `doe/import-limit`.** These stay unmapped (logged once at debug, key names only). They are not described on the user's panel, and their JSON contract is unknown. Re-evaluate them in PR 5 or with a capture.
- **The invariant.** After 4b, the expected `unmapped` set for the MAIN 40 fixture is empty. A test pins this.

### 2.5 Row additions

- **New `when` predicates**, pure and taking `(snapshot, device_id)`:
  - `_has_mid` and `_has_bess_or_mid`
  - `_link_published` (`link is not None and link.status is not None`)
  - `_settable(key)`
  - `_described(key)` and `_published(key)`, as in 4a.
- **`row_device(snapshot, row, device_id) -> DeviceRef`.** A pure helper that `local_entity` maps to `DeviceInfo`:

```python
@dataclass(frozen=True)
class DeviceRef:
    kind: str           # "panel" | "bess" | "pv" | "evse" | "mid" | "remote_ct"
    device_id: str | None
    name_hint: str | None  # circuit label for remote CT / multi-EVSE disambiguation
    manufacturer: str | None
    model: str | None
    sw_version: str | None
```

- **`Presentation` gains `digits: int | None`.** It overrides `ROUNDING` by unit where set: SoE kWh = 2, battery % = 0, advertised current = 1, power stays 0. Write-on-change applies after rounding, as in 4a.

## 3. Device registry

All local devices follow the same pattern. `local_serial` is the 4a panel identifier.

| Device | identifiers | via_device | name | manufacturer / model / sw_version |
|---|---|---|---|---|
| Panel | `(DOMAIN, local_serial)` | none | as 4a | as 4a |
| BESS | `(DOMAIN, f"{K}_bess/{id}")` | `(DOMAIN, local_serial)` | "{panel} Battery" | vendor-name / info/model / firmware-version |
| PV | `(DOMAIN, f"{K}_pv/{id}")` | panel | "{panel} Solar" | vendor-name / product (or model) / none |
| EVSE | `(DOMAIN, f"{K}_evse/{id}")` | panel | "{panel} EV charger", plus " ({circuit label})" when there is more than one EVSE | vendor-name / model / firmware |
| MID | `(DOMAIN, f"{K}_mid/{id}")` | panel | "{panel} MID" | vendor-name / model / firmware; hw_version = hardware-version |
| Remote CT | `(DOMAIN, f"{K}_circuit/{id}")` | panel | circuit label, else "Remote CT {n}" | none |

- **No serials.** `serial_number` is never set on any subdevice. Same rule as the 4a panel decision: no private identifiers in the registry diagnostics dump.
- **3.1 Names.** Entities on subdevices use `has_entity_name=True` with suffix-only translation names ("Power", "State of charge", "Status"), so HA shows "{device} {suffix}".
- **3.2 Feeding circuits.** The feeding circuit's power and energy are **not** duplicated onto subdevices. Duplicating them would double recorder rows (see the recorder-growth decision). Instead, the main status or power entity of each subdevice carries an attribute `circuit: "<circuit label>"`.
- **3.3 Removed devices.** When a subdevice disappears from `$nodes`:
  - its entities go unavailable;
  - the device stays until the user deletes it;
  - `async_remove_config_entry_device` returns True for subdevices that are no longer present.
- **3.4 Remote CT.** 4a put remote-CT rows on the panel device. 4b moves them to their own device.
  - uids are unchanged, so history is kept.
  - On upgrade the entity registry entries move to the new device (`device_id` updated by HA on the next `async_add_entities`, because the device_info changed). Entity ids are not renamed. The Coder must verify this with a test, not assume it.
  - Synthetic only: the capture has no remote CT.
- **3.5 Multi-panel.** Linking a downstream panel's device to the upstream panel through the lugs is topology work (`multi-panel-topology.md`), not 4b.
- **3.6 Downstream lugs.** These stay on the panel device. They are a measurement point inside the enclosure, not separate hardware. Upstream does the same. A separate device would only make sense once the topology work identifies what the lugs feed.

## 4. Entities

Column legend:

- **Plat:** platform.
- **DC:** device class.
- **Cat:** entity category: D = diagnostic, C = config, blank = none.
- **En:** enabled by default.
- **Kind:** ValueKind.
- **Created when:** creation predicate, evaluated at setup and on description change, as 4a does.

All rows have `presence()` per 4a §4.4: unavailable when the device is absent, the link is down, or the value is unpublished, unless a fallback (still a stub) applies.

### 4.1 Panel (root), on the panel device

| Key / uid suffix | Plat | DC | Unit | Cat | En | Kind | Created when | Value |
|---|---|---|---|---|---|---|---|---|
| `door/state` | binary_sensor | DOOR | – | D | yes | MEASUREMENT | described | on = OPEN; off = CLOSED; UNKNOWN or other → None |
| `status/ethernet` | binary_sensor | CONNECTIVITY | – | D | yes | MEASUREMENT | described | bool |
| `status/wifi` | binary_sensor | CONNECTIVITY | – | D | yes | MEASUREMENT | described | bool. The SSID stays excluded (4a). |
| `pcs/active` | binary_sensor | RUNNING | – | D | yes | MEASUREMENT | described | bool |
| `pcs/enabled` | binary_sensor | – | – | D | no | STATIC | described | bool |
| `shed-forecast/time-to-priority-shed` | sensor | DURATION | min | – | yes | MEASUREMENT | described | int. Attributes: `full_charge_time_to_priority_shed`, `confidence` (lower-cased), `soc_threshold_shed`, `soc_threshold_release` (from `shed/policy`, see below) |
| `shed-forecast/total-time-remaining` | sensor | DURATION | min | – | yes | MEASUREMENT | described | int. Attributes: `full_charge_total_time_remaining`, `confidence` |
| `derived/grid-islandable` | binary_sensor | POWER | – | D | yes | MEASUREMENT | `_has_bess_or_mid` | on iff a MID device exists |
| `mid:grid/grid-forming-entity`, uid `…_distribution-enclosure/derived/grid-forming-entity` | sensor | ENUM | – | D | yes | MEASUREMENT | `_has_mid` | MID value lower-cased. Options: battery, generator, grid, none, pv, unknown. Anything else → unknown, logged once. |

Notes:

- **`pcs/*` attributes.** `pcs/active` and `pcs/enabled` stay as attributes of the 4a `pcs_import_limit` entity too. No 4a behaviour changes.
- **`shed/policy`.** This is JSON `soc-priority.v1`. Parse it purely with `json.loads`. Accept only a dict whose `version` (or `$schema`) names `soc-priority.v1` and whose two thresholds are numbers 0–100. Anything else drops the attributes and logs once at debug, without the payload.
- **`shed/asserted-islanding-state`.** Exposed as a lower-cased attribute on `derived/grid-islandable` (none / on_grid / off_grid). **Its control is deferred to PR 6**, because it overrides grid-state detection during MID or BESS comm loss.
- **`dsm_state`.** Upstream's `dsm_state` (disabled) is a cloud and REST-era field. r202639's equivalent is MID `islanding-state` (§4.4), so there is no panel row.
- **Grid-forming entity placement.** This sits on the panel device for upstream parity, but only when a MID exists (§1 rule 3).
- **Time zone.** `status/time-zone` gets no row (it is PII, §2.4).

### 4.2 BESS device

| Key | Plat | DC | Unit | Cat | En | Kind | Value |
|---|---|---|---|---|---|---|---|
| `bess:meter/active-power` | sensor | POWER | W | – | yes | MEASUREMENT | raw sign, + = discharging (SPAN's convention; documented in the translation description). Attribute: `circuit`. |
| `bess:soc/soc` | sensor | BATTERY | % | – | yes | MEASUREMENT | digits 0 |
| `bess:soc/soe` | sensor | ENERGY_STORAGE | kWh | D | yes | MEASUREMENT | digits 2 |
| `bess:info/nameplate-capacity` | sensor | ENERGY_STORAGE | kWh | D | no | STATIC | |
| `bess:status/communication-state` | sensor | ENUM | – | D | no | MEASUREMENT | ok/degraded/lost/unknown |
| `bess:derived/connected` | binary_sensor | CONNECTIVITY | – | D | yes | MEASUREMENT | comm == OK when published; else link.status == OK; else None |
| `bess:info/vendor-name`, `info/model`, `info/firmware-version` | registry only | | | | | | not entities (upstream has vendor/model/firmware sensors; registry fields cover them) |

- **Row predicates.** Each row is created when its key is described on that BESS. `derived/connected` is created when either source is described.
- **Battery %.** Upstream also exposes a panel-level "battery %" sensor. 4a already has the panel battery SoC if power-flows publishes it. No duplicate is created.

### 4.3 PV device

| Key | Plat | DC | Unit | Cat | En | Kind | Value |
|---|---|---|---|---|---|---|---|
| `pv:info/nominal-power` | sensor | POWER | W | D | no | STATIC | nameplate |
| `pv:derived/panel-link` | binary_sensor | CONNECTIVITY | – | D | yes | MEASUREMENT | link.status == OK; created when `_link_published` described on the feeding circuit |

- **No PV power entity.** PV has no meter in r202639. PV power already exists on the panel as `power-flows/pv` (4a, "PV power"), and the feeding circuit has its own power.

### 4.4 MID device

| Key | Plat | DC | Unit | Cat | En | Kind | Value |
|---|---|---|---|---|---|---|---|
| `mid:grid/grid-state` | sensor | ENUM | – | D | yes | MEASUREMENT | up/down/degraded/unknown |
| `mid:grid/islanding-state` | sensor | ENUM | – | D | yes | MEASUREMENT | on_grid/off_grid/unknown |

- **Registry only.** `grid-forming-entity` is the panel row above. `info/*` goes to the registry.
- **Islanding state.** This diverges from upstream, which keeps islanding on the panel only. Here it sits on the MID device, because that is the device publishing it, and the panel has no copy in r202639.

### 4.5 EVSE device

| Key | Plat | DC | Unit | Cat | En | Kind | Value |
|---|---|---|---|---|---|---|---|
| `evse:status/status` | sensor | ENUM | – | – | yes | MEASUREMENT | lower-cased status. Attributes: `lock_state` (lower-cased), `circuit`. |
| `evse:derived/charging` | binary_sensor | BATTERY_CHARGING | – | – | yes | MEASUREMENT | status == CHARGING |
| `evse:derived/ev-connected` | binary_sensor | PLUG | – | – | yes | MEASUREMENT | status ∈ {PREPARING, CHARGING, SUSPENDED_EV, SUSPENDED_EVSE, FINISHING} |
| `evse:meter/advertised-current` | sensor | CURRENT | A | – | yes | MEASUREMENT | digits 1 |
| `evse:config/max-charge-current` | sensor | CURRENT | A | D | yes | STATIC | ceiling |
| `evse:derived/panel-link` | binary_sensor | CONNECTIVITY | – | D | yes | MEASUREMENT | as PV |
| `evse:config/user-max-charge-current` | number | CURRENT | A | C | yes | CONTROL | §6.1 |
| `evse:switch/lock-state` | lock | – | – | – | yes | CONTROL | §6.2 |

- **Status options.** The schema enum is AVAILABLE, PREPARING, CHARGING, UNAVAILABLE. The PLUG set includes upstream's OCPP states, which are harmless if never seen. The ENUM options list is the schema enum, lower-cased. An unknown value maps to None and is logged once.
- **Upstream's `evse_part_number`.** Not created: it is a cloud field, absent from r202639.

### 4.6 Circuits and lugs (attributes only)

On the existing 4a circuit power entity and the lugs power entities:

- `backed_up`: lower-cased (backed_up / not_backed_up / unknown).
- `unit_count`: int.
- `location`: from `info/locations` (a string, or a joined list if it is an array).

Only these three are exposed. The fed-by and feeds ids and types drive linking and the relay safety rule (§6.4). They are never shown. Attributes are added only when published, so the capture's attributes are unchanged and nothing new is written to the recorder on the user's panel. No new per-circuit entities, for recorder growth reasons.

`location` is user-entered room text. It is household-identifying only in the weak sense that circuit names already are, so it is treated like `info/name`: not in `IDENTIFYING_KEYS` and allowed in attributes, but excluded from logs.

## 5. Presence and fallback

- **No new presence logic.** 4b adds no presence logic beyond the predicates above. Subdevice rows use the device's own `$state` (ready/init/lost, as 4a), the transport link, and value publication.
- **Derived rows.** A derived row is unavailable when any of its inputs is unavailable, except where §4 gives an "else" source.
- **Fallback.** `fallback_value(scope, device_id, key)` is called with the new scopes and returns `NO_FALLBACK`. PR 5 decides which cloud fields map (BESS SoC is the obvious one).

## 6. Controls

All controls follow the 4a pattern:

- Publish to `…/set`.
- Show the target value optimistically.
- Confirm when the device echoes it within `CONFIRM_WINDOW` = 10 s.
- Otherwise revert and raise `HomeAssistantError` (translated).
- Controls are created only when the property is `$settable` in the description.
- No retries.

### 6.1 EVSE charge-current limit (number)

- **Bounds.**
  - min = the declared `$format` min, else 6 A (the J1772 floor; upstream uses 0).
  - max = min(declared max if any, published `config/max-charge-current`).
  - If min > max, then min = max.
  - Step 1, mode BOX.
- **Ceiling unknown.** While `config/max-charge-current` is unpublished, the entity is **unavailable**. Otherwise HA's default max of 100 A would accept unsafe input.
- **Above the ceiling.** A value above the current ceiling raises `ServiceValidationError` before anything is published. The ceiling is re-read at call time.
- **Shown value.** The published `config/user-max-charge-current` if any, else the ceiling (upstream's effective-limit semantics). Attribute `effective: true|false` shows which.
- **Integer payload.** The schema type is integer, so the payload is an integer string.
- **Safety note.** Lowering a limit is safe. Raising it is bounded by the EVSE's own ceiling, which the charger itself enforces, so this cannot exceed the circuit rating. See Q2.

### 6.2 EVSE lock

- Created when `switch/lock-state` is settable.
- `is_locked` = LOCKED.
- lock → publish `LOCKED`; unlock → publish `UNLOCKED`.
- Optimistic `is_locking` / `is_unlocking` during the confirm window.

### 6.3 GFE override button: deferred to PR 6

Upstream's `gfe_override`:

- is created only when a BESS exists;
- is available only when BESS comm != OK and the DSM state != ON_GRID;
- publishes `GRID` as the dominant power source.

The r202639 equivalent is writing `shed/asserted-islanding-state`. That tells the panel what the grid state is while it cannot see the MID or BESS. A wrong assertion during a real outage could mis-direct load shedding or backup behaviour, so this is safety-relevant. It also cannot be tested on the user's panel (no BESS or MID, and the capability is not described).

4b therefore ships no button platform. The `PLATFORMS` list stays without BUTTON. The spec for PR 6 should carry:

- the button, or a select for NONE/ON_GRID/OFF_GRID;
- availability gating as upstream;
- a confirmation option;
- an outcome event.

See Q3.

### 6.4 Relay switches on storage or solar feeding circuits

The PR 39 capture showed a battery breaker exposed as a relay switch. Switching off the circuit that feeds or charges a BESS (or that backfeeds PV) can cut backup power or trip inverter faults.

Proposal:

- In 4b the circuit `relay` switch row gains a `when` predicate: **not created** when the circuit's `feeds` link targets a BESS or PV device, or when its role (`connection/feeds-role`, if described) is STORAGE or GENERATION.
- The relay state stays visible as an attribute (4a's `relay_state`) on the power entity.

This changes 4a behaviour only for circuits that are linked. On the user's panel no circuit is linked (connection/* is unpublished), so nothing changes there. See Q4. Configurable override options belong to PR 6.

### 6.5 Deferred to PR 6 (summary)

- GFE override, and asserting or clearing islanding state.
- Control safety options: per-control confirmation, disabling switching for selected circuits, outcome events, and the override for §6.4.
- `rotate_credentials`.
- DOE import and export limits (doe/*) if they ever turn out to be settable.

## 7. `local_entity.py` and `__init__.py`

- **`PLATFORMS`.** Add `Platform.BINARY_SENSOR`, `Platform.NUMBER` and `Platform.LOCK`. The order is irrelevant.
- **New platform files.** `binary_sensor.py`, `number.py` and `lock.py` each call `async_setup_local` filtered by platform, as `select.py`, `sensor.py` and `switch.py` do in 4a. Cloud-mode entries create nothing on these platforms, so no new cloud entities.
- **Device info.** `device_info_for(ref: DeviceRef)` replaces the direct `panel_device_info` use in `SpanLocalEntity`. `panel_device_info` stays for the panel.
- **Translation keys.**
  - New `strings.json` / `translations/en.json` entries for every row in §4.
  - Enum `state` translations for each ENUM sensor.
  - Error strings for the number bound violation and the confirm timeout. The timeout string already exists from 4a; reuse it.
- **Attribute sanitising.** Attribute values pass through the existing sanitiser, so nothing from `PII_KEYS` is ever set.
- **Write-on-change.** Unchanged. Derived binary sensors compare the rounded or derived value.

## 8. Test plan

Fixture policy: the MAIN 40 capture is the only real fixture. Everything else extends `tests/test_local_model_synthetic.py`, whose module docstring already says "All data here is SYNTHETIC". New synthetic helpers and ids carry `synthetic` in their names:

- `synthetic_bess_tree()`;
- ids `bess-syn-01`, `pv-syn-01`, `evse-syn-01`, `evse-syn-02`, `mid-syn-01`;
- vendor `"Synthetic Vendor"`, firmware `"0.0.0-synthetic"`.

No serials anywhere. Synthetic `info/serial-number` values are `"SYNTHETIC-NOT-A-SERIAL"`, and they exist only so the tests can prove they are stripped.

**Model, capture fixture (`test_local_model.py`):**

1. `snapshot.unmapped == frozenset()` for MAIN 40.
2. Root binary values: door off, ethernet on, wifi off, pcs active on, pcs enabled on (disabled by default).
3. No subdevice, `grid_islandable`, grid-forming, shed or relay changes are created from the capture. The row set equals the 4a set plus the five root binary sensors.
4. All uids are unique, none matches the cloud regex, and all row keys are unique.
5. Every 4b row is grounded: it is either triggered by the fixture or listed in an explicit `SYNTHETIC_ONLY_ROWS` set, which a test checks against `ROWS`.
6. Circuit attributes are unchanged on the capture (connection/* is unpublished).
7. The Store round trip is unchanged. Time-zone is absent from `store_payload`.

**Model, synthetic (`test_local_model_synthetic.py`):**

8. BESS, PV, EVSE and MID facts are built. `$type` classification is covered.
9. Link pass:
   - a circuit feeding a BESS gives that BESS `via_kind = "circuit"`;
   - lugs fed by a BESS gives `via_kind = "lugs"`;
   - two circuits claiming the same device → lowest id wins, plus an unmapped note;
   - an unknown target → no entity.
10. Subdevice PII: the serial is never in facts, `values`, the Store or attributes.
11. `grid_islandable` and grid-forming are absent with no MID or BESS. With a BESS only, `grid_islandable` is off. With a MID, it is on. Grid-forming options and unknown mapping are covered.
12. Derived values:
    - EVSE charging and plug sets;
    - BESS connected via comm, then via link, then None;
    - door UNKNOWN → None.
13. `shed/policy` parsing: valid, wrong version, out-of-range, non-JSON → attributes absent.
14. Remote CT: the row device is `remote_ct`, the name hint is the circuit label or the "Remote CT n" fallback, and the uid is unchanged from 4a.
15. §6.4: no relay row for a circuit feeding a BESS or PV, or with role STORAGE. The relay row is present otherwise.
16. Presence: a subdevice `$state` of lost → unavailable; link down → unavailable; `fallback_value` called with the new scopes returns `NO_FALLBACK`.
17. Digits: SoE 2, SoC 0, advertised current 1. Write-on-change suppresses a 3.141 → 3.142 change for SoE.

**Entity and HA tests (`pytest-homeassistant-custom-component`):**

18. Device registry:
    - subdevices have `via_device_id` = the panel device;
    - no `serial_number`;
    - multi-EVSE names are disambiguated;
    - remote-CT entities move to the CT device without changing `entity_id`.
19. Number:
    - min/max from the declared format and the ceiling, including min > max clamping;
    - unavailable while the ceiling is unpublished;
    - a value above the ceiling raises `ServiceValidationError` with no publish;
    - confirm success;
    - confirm timeout reverts and raises.
20. Lock: lock and unlock publish the right payloads; optimistic states; timeout revert.
21. Not created when not `$settable`, for the number, the lock and the 4a controls (regression).
22. Cloud-mode entry: no binary_sensor, number or lock entities.

**Probe and diagnostics:**

23. The probe redaction list includes `status/time-zone` and subdevice `info/serial-number`.
24. Diagnostics dump: no serials or time-zone.

**Fixture hygiene:**

25. The committed `capture.json` contains the panel's time-zone string once. Replace it with `"Etc/UTC"`, and keep a test that the fixture's value is `Etc/UTC`. No history rewrite (Q6).

**Live (user's HA, after 4b-1):**

26. Door, Ethernet, Wi-Fi and PCS binary sensors appear on the panel device under Diagnostic. Opening the door flips the door sensor within one publish.
27. No new subdevices, no `grid_islandable` and no new circuit attributes.
28. Recorder: no new rows from circuits.

## 9. Divergences from upstream (deliberate)

| Upstream | 4b | Why |
|---|---|---|
| door: TAMPER | DOOR | It is a door; TAMPER implies an alarm. HA shows Open/Closed. |
| `grid_islandable` always on the panel | only with a MID or BESS | Avoids a false "off" when backup is upstream (§1) |
| `grid_forming_entity` defaults to GRID | only with a MID; never fabricated | same |
| islanding and GFE kept panel-only | MID enums on the MID device | r202639 publishes them on the MID |
| BESS serial sensor | none | private identifier rule |
| vendor/model/firmware sensors | registry fields only | avoids redundant entities and recorder rows |
| `evse_part_number`, `dsm_state` | none | not in r202639 |
| number min fallback 0 | 6 A | J1772 minimum; 0 A is not a valid pilot limit |
| `gfe_override` button | deferred to PR 6 | safety-relevant and untestable here |
| relay switch on every circuit | none on storage/solar feeders | PR 39 battery-breaker finding |

## 10. Split proposal

4b as a whole touches three platforms, a new model pass, registry changes and safety-adjacent controls. That is too big for one review. Proposed split:

- **4b-1: panel extras and binary_sensor.** Live-testable on the user's panel.
  - Root rows (door, Ethernet, Wi-Fi, PCS, shed-forecast and policy when described).
  - circuit/lugs `connection/*` and `info/locations` read into facts, attributes and links.
  - Time-zone PII and fixture redaction.
  - `binary_sensor` platform.
  - The empty-unmapped invariant.
  - §6.4 relay suppression. It needs links, which arrive here.
  - Tests 1–7, 13, 15, 23–28.
- **4b-2: subdevices, read only.** Synthetic only.
  - BESS, PV, MID and EVSE facts.
  - Remaining link resolution and `DeviceRef`.
  - Device registry.
  - Remote CT device.
  - `grid_islandable` and grid-forming.
  - All read-only subdevice entities.
  - Tests 8–12, 14, 16–18, 22.
- **4b-3: EVSE controls.** `number` and `lock` platforms. Tests 19–21.
- **PR 6:** GFE / asserted-islanding control, plus the items in §6.5.

Each part merges to `dev` independently. 4b-2 and 4b-3 cannot be verified on real hardware. Their PR descriptions must say so and ask for a capture from a site with the hardware (via the contributor's PR thread, per the contributor rule).

## 11. Privacy checklist

- New PII: `status/time-zone` and the subdevice serials. Both are added to the PII sets and the probe redaction, and are absent from the Store, attributes, logs and the registry.
- Logs carry key names and device class names only, never values for info/*, policy JSON or locations.
- Synthetic fixtures are labelled. The real fixture's time-zone is replaced (Q6).
- This spec contains no serials, host, site name, address, location, SSID or passphrase.

## Open questions for the user

1. **Split 4b into 4b-1 / 4b-2 / 4b-3?** (§10)
   *Recommendation:* yes. 4b-1 is fully live-testable on your panel. 4b-2 and 4b-3 rely on synthetic fixtures and should be reviewed as such.

2. **Ship the EVSE charge-current number and lock in 4b-3, or hold them for PR 6 with the other controls?**
   *Recommendation:* ship them in 4b-3. Neither can cut power to the house. The number is bounded by the charger's own published ceiling, and it is unavailable until that ceiling is known. They follow the existing 4a confirm pattern.

3. **GFE override / asserted islanding state.** This tells the panel the grid is up or down when it cannot see the MID or BESS. A wrong assertion during an outage could change backup and shedding behaviour.
   *Recommendation:* defer it to PR 6, with confirmation gating and an outcome event. For now expose it read-only as an attribute only. It is untestable on your panel.

4. **Suppress the relay switch on circuits that feed a battery or solar (or whose role is STORAGE)?** This changes 4a behaviour only for linked circuits. None on your panel are linked today.
   *Recommendation:* yes, suppress it in 4b-1, keep the relay state as an attribute, and offer a per-circuit opt-in override in PR 6.

5. **"Grid" naming.** Your solar and battery sit upstream, so the panel's "Grid power" is really house consumption.
   *Recommendation:* keep SPAN's "grid" names for parity. Add translation descriptions and a README note saying "grid = what feeds this panel". Create no islanding or grid-forming entities without a MID. Leave a "Feed" relabel to the multi-panel topology work. Alternative: rename to "Feed …" now; this breaks parity with upstream and SPAN's app.

6. **Time-zone in the committed capture fixture.** It is a coarse location hint and is already in git history once.
   *Recommendation:* replace it with `Etc/UTC` in the fixture in 4b-1 and add it to the probe redaction. Do not rewrite history; the exposure is region-level only.

## Decisions (user, 2026-11-12)

1. Split into 4b-1 (panel extras, binary_sensor, circuit links, relay rule), 4b-2 (device read-only entities, synthetic), 4b-3.
2. EV charger number and lock are HELD for PR 6. 4b-3 shrinks to nothing; EVSE ships read-only in 4b-2.
3. GFE override deferred to PR 6; current state as a read-only attribute only.
4. Relay rule: hide the relay switch on circuits feeding bess/pv or with role storage; keep relay state as an attribute; per-circuit override in PR 6. Ships in 4b-1.
5. Keep SPAN's "grid" names; add descriptions and a README note; no islanding entities without a MID or battery.
6. Replace the time zone in the committed capture fixture with Etc/UTC and add it to probe redaction; no history rewrite. Ships in 4b-1.
