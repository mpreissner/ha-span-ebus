# Topology-aware grid labels

Status: draft rev 1, 2026-11-14. Local only: never commit or push.

This spec follows multi-panel-topology.md §7 and the PR 39 capture findings. It covers:
- how a panel fed by another panel names its "grid" entities;
- whether a panel on its own supply keeps the word "grid";
- the rest of §7;
- where the battery breaker fix goes.

## 1. Facts this rests on

- **What "grid" measures.** A panel's "grid" flow is power into the panel at its lugs.
  - On a lug-fed panel (MAIN 16 in the PR 39 capture), that is the upstream panel's feedthrough.
  - On a branch-fed panel, it is the upstream circuit.
  - Every panel reports its own feed as `grid`.
- **The user's own site.** Solar and the battery sit between the meter and the panel, so "grid" there is
  house consumption through the panel, not the utility meter.
- **The cloud's site grid energy** is measured at the same point, energy into the panel.
- **Unique ids never change.** Only names and translation keys may.
  - A name change keeps the registry entity_id, and so the statistic_id and the history.
  - Only new installs get entity_ids made from the new names.
  - A name the user has set in the registry still wins.
- **The Energy dashboard.** An integration can't keep a sensor out of HA's grid picker, and can't set
  `included_in_stat`.
  - The picker offers any `energy` sensor with `total`/`total_increasing`.
  - The only levers are the name, the device_class/state_class, and entity_category.
  - Dropping state_class would end the long-term statistics, so it's ruled out.
- **The feed answers have no effect at runtime yet.**
  - They live in `entry.options["feeds"]` (`const.py:36`), keyed by the cloud panel key.
  - `CONF_FEEDS` is in `CLOUD_OPTION_KEYS` (`const.py:55`), so removing the cloud fallback drops it.
  - A local-only entry never has one.
- **The current names**
  - **Local**, by translation key (`span_client/local/model.py:1385-1442`, strings.json L444-490):
    - `grid_power` "Grid power";
    - `main_consumed_energy`, `main_produced_energy` and `main_net_energy` ("Main meter … energy");
    - `upstream_current_a/b`.
  - **Cloud**, built from node names (`sensor.py:186-187`, `:245-249`):
    - "Site grid" and "Site grid energy";
    - "Main feed <property>" for every feed node (`backend.py:319-320`).

## 2. Fed panels: proposal

### 2.1 Where "fed" comes from

A new helper, `feeds.is_fed(feeds, key, panels) -> bool`, is true when `feed_of(...)["type"]` is `LUGS` or
`BRANCH`.

| Entry | Source of the answer |
|---|---|
| Cloud entry | `entry.options["feeds"]` for each panel key, the answer the user already gave. |
| Local entry, linked | The cloud answer for the linked panel's cloud key (`link.cloud_key`). |
| Local entry, not linked | A new local option, `fed_by_panel` (bool, default off), in the "local_panel" options menu that PR 6 added. |

- The `fed_by_panel` option is outside `CLOUD_OPTION_KEYS`, so it survives removing the cloud.
- A linked entry shows the same field, prefilled from the cloud answer. The local option wins once saved.
  (**T2**)

The answer is read when the platform sets up. Changing it reloads the entry: options flows already save
through `async_update_entry`, and the coder must check whether the "local_panel" step reloads.

### 2.2 Names on a fed panel

Local: new translation keys, chosen in `model.translation_key(row)` from the flag. The row key and unique
id stay as they are.

| Row (unique id unchanged) | Own supply (today) | Fed panel (new key → name) |
|---|---|---|
| `grid_power` | Grid power | `feed_power` → "Feed power" |
| `main_consumed_energy` | Main meter consumed energy | `feed_consumed_energy` → "Feed consumed energy" |
| `main_produced_energy` | Main meter produced energy | `feed_produced_energy` → "Feed produced energy" |
| `main_net_energy` | Main meter net energy | `feed_net_energy` → "Feed net energy" |
| `upstream_current_a/b` | Upstream current L1/L2 | Unchanged: it's already neutral |
| `site_power` | Site power | Unchanged |

Cloud: the node name override comes from the same flag, in `sensor.py` at construction.

| Today | Fed panel |
|---|---|
| "Site grid" | "Site feed" |
| "Site grid energy" | "Site feed energy" |

- The cloud unique ids (`{serial}_site/grid`, `{serial}_site/grid_energy`) are unchanged.
- The stand-ins follow the local translation key (`standin.py:61`), so they need nothing extra.

Everything else stays as it is: device_class, state_class and entity_category. Statistics carry on
unbroken.

### 2.3 Keeping fed panels out of the grid totals

- The integration has no cross-panel grid total, so there's nothing to exclude in code.
- Dashboard picker: the "Feed …" names are the signal. README gets a short "Several panels" section:
  - Use only the topmost panel's grid energy, or better the utility meter, as the grid source.
  - In "Individual devices", set the upstream device of a branch-fed panel's circuits.
- **T3, optional:** a repair, raised when the energy prefs list a fed panel's feed energy as a grid source.
  - It isn't fixable by us: it explains, and the user edits the dashboard.
  - It reads `energy` prefs through the existing `recorder_api` style wrapper.
  - Recommended: defer to a later PR.

## 3. Own supply: keep "grid"?

| Option | |
|---|---|
| A ★ | Keep "Grid power" and "Main meter … energy". This is parity with our released names and with the official integration's wording. It changes nothing for existing users or their dashboards. README notes that on sites with solar or a battery upstream of the panel, "grid" means power into the panel. |
| B | Rename to "Panel feed power" and similar everywhere. More accurate on upstream-solar sites like the user's, but it renames entities on every install, and the panel's own app says "grid". |

**Recommend A.** The user's site is the main case B would help, and the README note covers it.

## 4. The rest of §7

### 4.1 Feedthrough power sensor on the upstream panel

| Path | Status | Work |
|---|---|---|
| Local | Done: `feedthrough_power`, `feedthrough_imported_energy` and `feedthrough_exported_energy`, plus `feedthrough_current_a/b`. | Nothing. |
| Cloud | The feedthrough instance is decoded and classified (feeder kind 4, `cloud_traits.py`), but `_node_names` calls every feed node "Main feed". An upstream panel with a feedthrough therefore shows two "Main feed power" sensors. | Name feed nodes by feeder kind: kind 3 stays "Main feed", kind 4 becomes "Feedthrough", and anything unknown is "Feed {instance}". |

- The cloud fix needs `_node_names` to receive the feeder kinds `schema_from_frame` already holds.
- No new cloud feedthrough energy sensor (**T5**). The cloud is the fallback, and the local registers
  already meter it.
- Existing entity_ids keep their old `…_main_feed_power_2` form. Only the friendly names change.

### 4.2 Overhead correction

- Done on both paths. `overhead.py` computes `max(0, feed − Σcircuits − feedthrough)`.
  - The cloud chooses by feeder kind (`backend.py` `is_overhead_feed`/`panel_overhead`).
  - The local path uses `local_overhead_w` (`model.py:1870`).
- It doesn't depend on the feed answers, and there's nothing left to do.
- A test pins the PR 39 numbers: the upstream panel's overhead excludes the MAIN 16 load. Confirm the test
  exists in `tests/test_overhead.py`, and add it if not.

### 4.3 `feeds_panel` attribute

- When a panel's answer is `BRANCH` with a `circuit`, the upstream panel's sensors for that circuit get
  `feeds_panel: <name of the downstream panel's device>`. This covers the power sensor and the energy
  sensor, on the cloud and, through the link, on local.
- It uses the device name from the registry, which is the user's own label. Never a serial or key.
- Lug-fed panels need no attribute. The feedthrough sensors already name it.
- The answer can be stale or absent, so the attribute simply isn't set. It's display only.

## 5. Battery breaker fix

★ **Separate PR, before this one.** It lives in `cloud_traits` and doesn't involve naming:
- read field 12 for a `1/15` kind-3 entry's spaces;
- name it "Battery";
- with shed policy 1 (always on), give it no relay switch.

The contributor's capture already drives its tests. It also touches which relay switches exist, the PR 6
control-safety area, so a small, separate review is safer. (**T6**)

## 6. Tests (no HA)

- **`feeds.is_fed`:**
  - own supply, lugs and branch;
  - an upstream that has gone (falls back to own supply);
  - a self-reference.
- **`model.translation_key`:**
  - each fed row gets its `feed_*` key, and the own-supply rows are unchanged;
  - unique-id construction is identical in both cases (assert equality for every row).
- **Cloud naming:**
  - the fed flag turns "Site grid" into "Site feed";
  - `_node_names` with the PR 39 two-panel fixture gives one "Main feed" and one "Feedthrough", no
    duplicates.
- **`feeds_panel`:**
  - set only on the chosen circuit;
  - the value is the device name only;
  - absent with no answer.
- **Options:** `fed_by_panel` isn't removed when the cloud is detached, and it's prefilled from the cloud
  answer when linked.
- **strings.json** and en.json stay in step: the new keys `feed_power` and `feed_*_energy`, and the
  option label.

## 7. Decisions

Recommended options are marked ★. Risky ones are flagged.

- **T1. Fed-panel names**
  - ★ "Feed power" and "Feed consumed/produced/net energy" (local), and "Site feed" and "Site feed energy"
    (cloud), through new translation keys and a cloud name override.
  - **RISKY (low):** the friendly names change on existing fed-panel installs. Entity ids and statistics
    don't. Only the contributor's two-panel install is affected today.
- **T2. Where a local-only entry learns it's fed:** ★ a new `fed_by_panel` local option, kept when the
  cloud is detached; on a linked entry it's prefilled from the cloud answer. The alternative is to apply
  this to cloud-linked entries only.
- **T3. Repair when a fed panel's feed energy is a grid source:** ★ defer.
- **T4. Own supply:** ★ A, keep "Grid" with a README note.
- **T5. Cloud feedthrough**
  - ★ Rename only ("Feedthrough"), with no new cloud energy sensor.
  - **RISKY (low):** the friendly name changes for anyone whose upstream panel has a feedthrough. The
    entity_id keeps the `main_feed_…_2` form.
- **T6. Battery breaker fix:** ★ a separate small PR, ahead of this one.
- **T7. Overhead:** ★ no change, just confirm the PR 39 test exists.
- **T8. `feeds_panel` value:** ★ the downstream device's registry name, which is the user's own label.
  The alternative is the entry title.
- **T9. Live testing:** ★ none, by the no-live-tests rule. Naming is fully unit-testable. **Not risky.**
