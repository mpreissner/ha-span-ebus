# Options menu: Connection, Topology, Controls

Status: spec, local only (never committed). Base: dev @ 8160095 (0.3.0b1).
Author: architect-pr5. Scope: the options flow of local and linked entries only.
No new entities, no unique-id or statistics changes, no remove and re-add.

## 1. Why

Feedback from testing 0.3.0b1 on the dev instance:

1. A linked entry's options menu is titled "Cloud fallback" (`options.step.linked.title`).
   That menu covers the whole entry, so the title is wrong. The unlinked menu "Local panel"
   has the same problem in a milder form.
2. Controls showed only "Protected circuits". That is correct for the user's panel, but
   nothing says why the other two fields are missing.
3. Connection (`transport`) is fine as a concept. Keep it.
4. "Fed by another panel" (a single boolean, `fed_by_panel`) is a poor name and too coarse.
   Replace it with a Topology step that offers several choices.
5. "Remove cloud fallback" is a separate menu item. It should not be: choosing "Local only"
   under Connection should remove the fallback.

The rule behind all of these (memory: options-menu-naming-feedback): name steps after the
concept (Connection, Topology, Controls), and give each setting one place.

## 2. Today (dev 8160095)

`config_flow.py`, `SpanEbusOptionsFlow` (L1275):

| Entry | `async_step_init` routes to | Menu options |
|---|---|---|
| Cloud account (tokens, no `local_serial`) | `init`, "SPAN account" | `panels`, `link_local` |
| Local, no tokens | `local_panel` (L1302), "Local panel" | `cloud_add`, `controls`, `fed_by_panel` |
| Linked (`_is_linked`: tokens + `local_serial`) | `linked` (L1406), "Cloud fallback" | `transport`, `controls`, `fed_by_panel`, `cloud_remove` |

State that matters here:

- **`transport` (L1380)** sets `CONF_TRANSPORT_MODE` to `local_first` or `local_only`.
  It keeps the tokens and reloads only when the value changes.
- **`cloud_remove` (L1587)** works as follows:
  - It runs `cloud_removed_entry(data, feeds.keep_fed_answer(options), …)`, which drops
    `CLOUD_DATA_KEYS` and `CLOUD_OPTION_KEYS` (`panels`, `declined`, `feeds`).
  - In direction A (the entry has `cloud_stem`), it keeps `links` and stays at minor 2.
    In direction B, it drops the link and goes to minor 1.
  - It sets `local_only`, detaches the cloud and discards the token file.
  - In direction A it warns with `link_note.cloud_remove_fallback_only` when
    `legacy_repair.current(...).fallback_only` is not empty.
- **`cloud_add` chain (L1419 to `_cloud_add_confirm` L1564)** signs in, picks and matches the
  panel, writes the tokens and link, sets `local_first` and moves to minor 2.
- **`fed_by_panel` (L1310)** is a BooleanSelector whose default is `feeds.local_fed(options)`.
  It saves `CONF_FED_BY_PANEL` and reloads.
- **`feeds.local_fed`** (`span_client/feeds.py` L50) returns the saved `fed_by_panel` bool
  if there is one, else `cloud_fed(options)`. Its consumers are:
  - `local_entity.py:489`: the "Feed power" / "Feed … energy" translation keys;
  - `sensor.py:894/920/939`: the stand-in kept names.
- **`feeds_panel` attribute**: `topology.feeds_panel_attrs(hass, cloud_key, cloud_circuit)`
  looks only at the cloud `feeds` answers of loaded entries, keyed by cloud key and cloud
  circuit instance id.

### 2.1 Facts that change the brief

- **`local_only` with tokens does nothing with the tokens.**
  - `span_client/backfill.py` L94-102: `should_run` requires `mode == "local_first"`, so a
    `local_only` entry never gets a backfill runner.
  - `transport.py` never starts the cloud in that mode.
  - The register continuation (`transport.py` ~L613) requires `LOCAL_FIRST`.
  - `legacy_repair._local_only` (L62) already treats tokens with `local_only` the same as no
    tokens.

  So "local only but keep the account for history backfill" does not exist today. The user
  has also ruled it out (§4): there are exactly two states.
- **A linked entry's cloud `feeds` cannot say "fed".**
  - A linked entry's `panels` holds only its own cloud key (`_panel_options(labels,
    [link.cloud_key], {})` in `_cloud_add_confirm`; `multi_panel_upgrade` blocks direction A
    on accounts with several panels).
  - `feeds.feed_of` treats an upstream that is not in `panels` as own supply.
  - So `cloud_fed()` is False for every linked entry in practice. The only real answer to
    migrate is the saved `fed_by_panel` bool.
- **`feeds_panel` never works between two local entries today.** Each linked entry's answers
  cover only its own panel, and local-only entries have no answers at all.

## 3. Target menu

One menu for local and linked entries:

```
Panel options                 (step_id "panel"; was "local_panel" / "linked")
  Connection                  (step "transport", unchanged id)
  Topology                    (step "topology", replaces "fed_by_panel")
  Controls                    (step "controls")
```

The cloud-account menu (`init`, "SPAN account": Choose panels / Link the local panel) is
unchanged. That title describes the entry correctly, and its feed questions stay inside
Choose panels (`_FeedSteps`).

## 4. Connection

Fixed by the user (2026-11-14): exactly two states. There is no "local only but keep the
account" state.

| Value | Label | Meaning |
|---|---|---|
| `local_first` | "Local, with cloud fallback" | The entry holds the SPAN account (tokens). |
| `local_only` | "Local only" | The entry holds no SPAN account. Choosing it removes the tokens, as `cloud_remove` does today. |

The form is a list select of `transport_mode`, as today. The default is the entry's
effective state:
- `local_first` when the entry holds tokens and its mode is `local_first`;
- otherwise `local_only`. After §4.4 there is no entry with tokens and `local_only` left.

### 4.1 Backfill facts (what the wording may claim)

Checked against dev 8160095 so the strings stay true.

- **A restart and a reload are both recorded as gaps.**
  - The integrated energy sensors are `RestoreEntity`s. Each taken sample stores `last_at`
    in the restore extra data (`sensor.py` ~L778).
  - HA saves restore data when an entity is removed, which covers an integration reload as
    well as a shutdown.
  - On setup, `async_added_to_hass` sets `_down_since` from `last_at`, or from the
    recorder's last state if that total is larger (`sensor.py` ~L715-724).
  - The first taken sample calls `backfill.restart_gap(_down_since, at)` and records the gap
    (`sensor.py` L769-771, `span_client/backfill.py` L256).
  - A silence while running that is longer than `MAX_INTEGRATION_GAP_SECONDS` (= 300 s,
    `sensor.py` L96 → `coordinator.STALE_AFTER_SECONDS`) is also recorded (`sensor.py` L776).
    So is a lost local link that the cloud did not cover.
- **Only gaps of 5 minutes or more are kept.** `MIN_GAP_SECONDS = 300`
  (`span_client/backfill.py` L57-58: "Only gaps the integrator refused: anything shorter it
  bridged itself"). A reload that is back within seconds leaves no gap to fill.
- **Only three rows.** `ROW_FIELDS` covers site, solar and battery energy (`site_energy`,
  `pv_energy`, `battery_energy`). Circuit energy and the panel meter are never backfilled.
- **Only the last 7 days.** `MAX_AGE` = `cloud_history.MAX_BACKFILL_SECONDS`. Older starts
  are clipped (`restart_gap`).
- **Late.** Each gap is filled at least 75 minutes after it closes (`SETTLE`), with at most
  one request per 15 minutes and retries over about a day.
- **Single-panel sites only, with fallback only.** `should_run` requires `local_first`,
  tokens, a history target and a single-panel site (L94-102). Under `local_only`, backfill
  never runs, even when tokens are stored (§2.1).

So a reload does record its downtime across the unload and setup, but a normal reload is
back within seconds and stays under the threshold. Only a restart reliably makes a gap. The
wording therefore says "gaps of five minutes or more, such as after a Home Assistant
restart", doesn't mention reloads, names the three energy totals, says "single-panel sites",
and doesn't promise data for circuits.

### 4.2 Wording (exact English strings)

Step `transport`:
- `title`: "Connection" (unchanged)
- `description`: "Choose how Home Assistant reaches this panel."
- `data.transport_mode`: "Connection" (unchanged)
- `selector.transport_mode.options.local_first`: "Local, with cloud fallback: your SPAN
  account is used only when the local connection becomes unavailable, and to backfill
  missing site, solar and battery energy for gaps of five minutes or more, such as after a
  Home Assistant restart. Backfill covers the last 7 days on single-panel sites; circuit
  energy is not backfilled."
- `selector.transport_mode.options.local_only`: "Local only: your SPAN account is removed
  from Home Assistant for this panel, and the ability to backfill missing energy data is
  lost."

If labels that long render badly in the list, keep the labels "Local, with cloud fallback"
and "Local only", and put the text after each colon into the description as two short
paragraphs that begin with those names. The sentences stay the same either way.

Step `cloud_remove` (the confirm):
- `title`: "Use local only"
- `description`: "Home Assistant removes your SPAN account for this panel and runs it over
  the local connection only. The ability to backfill missing site, solar and battery energy,
  such as after a Home Assistant restart, is lost. Entities and their history
  stay.{warning}"
- `{warning}` is unchanged: `link_note.cloud_remove_fallback_only`, "{count} sensors that
  only your SPAN account provides stop updating. You can remove them afterwards." It is shown
  where it is shown today: direction A with fallback-only entities (O13).

Step `cloud_add`:
- `title`: "Sign in to your SPAN account" (was "Add cloud fallback")
- `description`: "Optional. If the local connection becomes unavailable, Home Assistant
  reads the panel through your SPAN account until it recovers. Controls stay local only.
  While the local connection works, Home Assistant only contacts your account to backfill
  missing solar, battery and site energy for gaps of five minutes or more, such as after a
  Home Assistant restart."

### 4.3 On submit

| Entry now | Chosen | Result |
|---|---|---|
| No tokens | Local only | No change, no reload (`async_create_entry` with the same options). |
| No tokens | With fallback | → `cloud_add`. The existing sign-in, pick and confirm chain runs unchanged and ends at `local_first`. |
| Tokens, `local_first` | With fallback | No change, no reload (as today). |
| Tokens, `local_first` | Local only | → `cloud_remove` confirm. The existing code runs unchanged, including the direction A warning. |

Four rows, which is every state that can exist after §4.4.

Removed:
- the `cloud_remove` and `cloud_add` menu options;
- the abort `cloud_remove_unsupported`, which is now unreachable. Remove it from both
  string files.

Not changed:
- The `cloud_remove` and `cloud_add` step ids and code. In direction A, `cloud_remove` still
  keeps the link (cloud-fallback-link spec §5.2), because the stand-ins' unique ids carry the
  cloud key. `keep_fed_answer` becomes `keep_topology` (§5.5).
- `CONF_TRANSPORT_MODE` stays in options, and `local_only` is still written on remove, so
  `legacy_repair._local_only`, `__init__._transport_mode` and `backfill.should_run` all
  keep working.
- The `transport` step can no longer write `local_only` while keeping the tokens, so no new
  entry can reach the legacy state.

### 4.4 Legacy entries: remove the dead account at setup (O4)

The legacy state is a local entry holding tokens with `transport_mode=local_only`. Only
0.3.0b1 entries that turned fallback off through the old `transport` step can be in it. Its
tokens are dead (§2.1), and the user decided that local only means no account. So setup
removes the account, and Connection then truthfully shows "Local only" with no account.
Choosing fallback runs the normal `cloud_add` sign-in.

**Removing the token file alone is not enough.** The account lives in two places:
- the token file (`_token_path`);
- `entry.data`: `CONF_TOKENS` plus the other `CLOUD_DATA_KEYS` (email, user id, device uuid,
  serial, primary panel), with the cloud `panels`/`declined`/`feeds` options.

`_is_linked`, the Connection default, the `build_cloud` gate and `should_run` all read
`CONF_TOKENS in entry.data`. Deleting only the file would leave an entry that looks linked
and still holds the account's email. So setup does what the `cloud_remove` confirm does,
without the form:

In `async_setup_entry`, in the `CONF_LOCAL_SERIAL` branch, before the existing
"no tokens → discard file" check and before the transport is built:

```python
if legacy_local_only(entry.data, entry.options):     # pure, in span_client/link.py
    data, options, minor = cloud_removed_entry(
        entry.data, feeds.keep_topology(entry.options),
        data_keys=CLOUD_DATA_KEYS, option_keys=CLOUD_OPTION_KEYS,
        cloud_stem_key=CONF_CLOUD_STEM,
    )
    options[CONF_TRANSPORT_MODE] = Mode.LOCAL_ONLY.value
    hass.config_entries.async_update_entry(entry, data=data, options=options,
                                           minor_version=minor)
    _LOGGER.info("Removed the unused SPAN account from a local-only panel entry")
```

- `legacy_local_only(data, options)` is True when `CONF_TOKENS` and `CONF_LOCAL_SERIAL` are in
  data and the options' mode is `local_only`.
- The existing check right after it ("`CONF_TOKENS` not in data → `discard_tokens`") then
  deletes the file, and the unload deletes it again, as today.
- The log line carries no serial, email, entry title or id.
- There is no `async_detach_cloud`: under `local_only` the cloud was never started.
- It runs at most once per entry, because the condition is false afterwards. There is no
  version bump; minor goes to 1 in direction B and stays at 2 in direction A, as
  `cloud_removed_entry` decides.

**Risks:**
- **No confirm, so no warning.** The user never sees the fallback-only count that
  `cloud_remove` would show. Nothing changes at runtime, though: under `local_only` those
  rows already stopped updating, and `legacy_repair` already offers them for removal (it
  treats `local_only` as no tokens).
- **It can't be undone.** To use fallback again, the user signs in through `cloud_add`. This
  is the user's decision; the README and release notes should say so in one line ("0.3.0bN
  removes the stored SPAN account from panels set to Local only").
- **A write during setup.** `async_update_entry` inside `async_setup_entry` is allowed, and
  the integration registers no update listener, so it causes no reload loop. It must run
  before the transport and backfill are built, so they see the new data.
- **A wrong `local_only` reading.** An entry whose mode is invalid is treated as
  `local_first` by `_transport_mode`. `legacy_local_only` must use the same rule: only an
  exact `local_only` value triggers removal. A garbled option never deletes an account.
- **Downgrade to 0.3.0b1.** The entry is then a plain local entry (direction B) or a linked
  entry without an account (direction A), both states 0.3.0b1 already produces through
  `cloud_remove`.
- **The dev instance** may be in this state. The first restart on this branch removes its
  account; to keep fallback there, switch Connection back to fallback on 0.3.0b1 first.

## 5. Topology

**Topology is per panel, never site-wide.** Each `span_ebus` entry stores its own answer
in its own options, and it describes only what feeds that one panel. Saving Topology on one
panel never reads into or writes another panel's entry, and no answer is derived from
another panel's answer. The other panels' answers are used only for the loop check (§5.3)
and the read-only summary (§5.8). Cloud-account entries already answer per panel (the cloud
`feeds` map is keyed by panel); this keeps the same rule for local panels.

### 5.1 Choices

Step `topology`, field `supply`, list select with `translation_key="supply"`. The stored
values are the cloud feed vocabulary (`feeds.OWN_SUPPLY`, `LUGS`, `BRANCH`) plus a new
shared `feeds.BEHIND_DER = "behind_der"` (O15, O17):

| Value | Label | Meaning |
|---|---|---|
| `own_supply` | "Utility meter feeds this panel directly" | Today's own supply. |
| `behind_der` | "Utility meter, with solar or a battery between the meter and this panel" | DER sits between the utility meter and this panel, and no SPAN panel is upstream. The panel's "grid" is the house load after solar and battery. The user's site. |
| `lugs` | "Another panel's feed-through lugs" | `feeds.LUGS` |
| `branch` | "A breaker on another panel" | `feeds.BRANCH` |

For `lugs` and `branch`, a second step asks which panel; for `branch`, a third asks which
circuit, when that can be known (§5.3). Both are optional: the type alone sets the names.

The step description (exact text in §7) says the answer is for this panel only, explains
what each choice changes (§5.2), says "Saving reloads the panel." and ends with `{others}`
(§5.8).

### 5.2 What each choice changes

"Fed" means `feeds.local_fed(options)` is True. That function's result is the only input the
#63 names read, so the mapping is:

| Choice | Fed | #63 local names (grid_power, main_*_energy) | Linked stand-in names | Energy dashboard guidance | `feeds_panel` attribute |
|---|---|---|---|---|---|
| `own_supply` | no | "Grid power", "Grid consumed / returned energy" | "Grid …" | The panel's grid energy can be the grid source. | none |
| `behind_der` | yes | "Feed power", "Feed consumed / returned energy" | "Feed …" | Don't use the panel's feed energy as grid: it is the house load after solar and battery. Use the utility meter or the solar/battery system's grid meter for grid, and that system's sensors for solar and battery. | none |
| `lugs` | yes | "Feed …" | "Feed …" | Not grid. Use only the panel the utility meter feeds as the grid source. | none |
| `branch` | yes | "Feed …" | "Feed …" | Not grid. The upstream breaker already counts this panel; don't add both as devices. | Unchanged in this change: only the cloud answers set it (O8 deferred, §5.4). The follow-up gives the upstream panel's circuit `feeds_panel: <this panel's name>`. |

Why `behind_der` uses the existing "Feed" names (decision O5): the panel's meter measures
what feeds this panel, which is not the utility grid. That is the confusion #63 fixed for
panels fed by another panel. Reusing the names adds no translation keys and does not change
entity ids.

None of the choices change entities, unique ids, statistics or the register continuation.

The cloud-account entries' "Site grid" / "Site feed" naming (`topology.cloud_panel_fed`)
still reads that entry's cloud `feeds`. Topology answers on local entries don't touch it.

### 5.3 Picking the upstream panel and circuit

- **Panel choices: every panel known for the site, not only the panels HA runs.** Gathered
  in this order, each giving a serial and, where known, a model:
  1. Every other `span_ebus` entry, loaded or not, from `async_entries(DOMAIN)` (not
     `topology._entries`, which is loaded only), including ignored ones:
     - a local entry's `local_serial`;
     - a cloud-account entry's `CONF_SERIAL`, and the serials of its loaded coordinators;
     - an ignored entry's `unique_id` (the serial `async_step_zeroconf` set).
  2. When this entry has tokens: the SPAN account's panels, from `discover_sites` in the
     executor. If `find_panel` finds this panel on one of the account's sites, list that
     site's panels; else list every panel on the account. Each `PanelInfo` gives `serial`
     and `model`. A failure skips this source with one debug line that names no panel
     ("Could not read the SPAN account's panels for the Topology step"); the form still
     opens.
  3. Panels found on the network over mDNS `_ebus._tcp` but not added: the in-progress
     `span_ebus` zeroconf flows (`async_progress_by_handler`), whose `context["unique_id"]`
     is the serial. HA creates those flows itself, and `async_step_zeroconf` has already
     probed the panel to get the serial.
     - Recommendation: don't browse or probe from the options flow. A browse candidate
       has only a host, and probing hosts from an options form is slow and touches
       devices the user didn't pick. A panel the browse finds but HA has no flow for
       (rare: `not_supported` or `cannot_connect`) is not offered; "Not listed" covers it.
  - **Dedupe** by serial, case-insensitively (`backend.same_serial`), keeping the first
    source's model when a later one has none. Exclude this panel by the same rule. A source
    entry with no known serial (a cloud panel known only by hardware id) is skipped, since
    the stored key is a serial (O7).
  - **Labels**, never a serial or host, in the form or in a log:
    1. the user's name for the HA device `(DOMAIN, serial)`, found in the device registry
       whether or not its entry is loaded (try the serial as each source spells it);
    2. else "SPAN <model>" (`feeds.panel_label`), from the device's model or `PanelInfo.model`;
    3. else "Panel 1", "Panel 2" and so on, numbered in a fixed order (sorted by serial
       internally, so the numbers don't shift between openings).

    Duplicate labels get " (2)", " (3)" and so on. The labelling is a pure helper in
    `feeds.py` (`upstream_choices(this_serial, candidates, names) -> dict[serial, label]`),
    so tests need no HA.
  - Add a choice "Not listed / not in Home Assistant", stored as no upstream.
- **An upstream HA doesn't run** is stored the same way, by its serial in `topology.upstream`
  (O7). Nothing else is stored, and nothing is looked up at runtime: whether this panel's
  names are "Grid" or "Feed" depends only on its own `supply`, never on the upstream.
  - **The upstream is added to HA later:** nothing is rewritten. Its label resolves to the
    new device's name the next time Topology opens. The breaker can then be picked by
    opening Topology again. The O8 follow-up finds it by the same serial.
  - **The upstream is removed from HA, or leaves the account:** the stored answer stays and
    the names don't change. When Topology opens and the stored serial matches no source,
    it is offered as "Panel not found now" and pre-selected, so saving without a change
    keeps it. That label is hard-coded English, like `commands.py`'s "Circuit not reported
    now", because dynamic selector options aren't translated.
- **Circuit choices** (branch only), from the first of these that works:
  1. a loaded local upstream's `transport.snapshot.circuits`;
  2. a loaded cloud upstream's coordinator circuits;
  3. when this entry has tokens and the upstream is a `PanelInfo` from source 2:
     `backend.panel_circuits` in the executor (the same read the cloud Choose panels flow
     makes).

  Each is labelled with `commands.circuit_choice_label` (name and spaces) and stored by its
  breaker spaces, as a sorted list of ints. Spaces are the key local and cloud already share
  (`match_circuits`), so the answer survives a cloud-to-local switch on either panel. If no
  circuits can be read (the upstream isn't loaded and isn't on this entry's account, or the
  read fails), the circuit step is skipped and the upstream panel is stored without
  `spaces`.
- **Reuse.** `_FeedSteps` holds the vocabulary (`OWN_SUPPLY`/`LUGS`/`BRANCH`) and the step
  shape (`feed` then `feed_circuit`, placeholders `panel` and `upstream`). It stays as it is
  for cloud-account entries. Topology reuses:
  - the constants;
  - `feeds.circuit_choices`-style labelling;
  - the `feed_circuit` form's strings, as a new `topology_circuit` step with the same
    placeholders.

  It does not reuse `_FeedSteps` itself. That mixin's state (`_sites`, `_labels`, cloud
  `find_panel` reads with `_access_token`) is for an account's site list, and a local-only
  entry has neither.
- **Loop check**, only where the panels involved have a stored answer: build
  `{serial (lower-cased): {"upstream": serial}}` from every other local entry's saved
  `topology`, plus cloud-account `feeds` answers whose key and upstream are serials. Add
  this panel's new answer and run `feeds.has_cycle`. On a loop, show the error `feed_cycle`
  (the existing string). A panel with no stored answer ends the chain, so an upstream HA
  doesn't run never causes a loop error.

### 5.4 Storage and consumers

New local option `CONF_TOPOLOGY = "topology"`, outside `CLOUD_OPTION_KEYS`, so it survives a
cloud remove:

```python
{"supply": "own_supply" | "behind_der" | "lugs" | "branch",
 "upstream": "<upstream panel's device serial>",   # lugs/branch, optional
 "spaces": [12, 14]}                               # branch, optional
```

The serial sits in options next to the serials already in data. It never goes in a log, an
exception, a placeholder or a fixture; tests use invented serials.

Pure helpers go in `span_client/feeds.py`, free of HA:
- `topology_of(options) -> dict`: the saved `topology` if it is valid; else derived (§5.5).
- `local_fed(options)`: `topology_of(options)["supply"] != "own_supply"`. The three existing
  consumers stay unchanged.

**O8 is deferred.** This change stores the local answers only. `topology.feeds_panel_attrs`
is unchanged and keeps reading the cloud `feeds` answers. `local_branch_answers` and the
local lookup are a follow-up (§10). The stored `upstream` and `spaces` are already the keys
that follow-up needs, so it needs no migration. What it will do: a circuit entity passes its
panel serial and spaces, the lookup checks the cloud answers first and then the local ones,
and the name comes from `panel_name(hass, …)`.

### 5.5 Migration (no remove and re-add)

No entry version or minor bump. Minor 1 and 2 mean unlinked and linked (`link.py` L304-309),
and `migration_ok` accepts any minor of major 1. Topology rewrites nothing at setup. The
only setup write in this spec is the legacy account removal in §4.4.
`topology_of` derives the answer when `topology` is absent:

1. If `fed_by_panel` is a bool: False gives `own_supply`. True gives the legacy marker
   `{"supply": "fed"}`, which `local_fed` treats as fed.
2. Otherwise, if `cloud_fed(options)` (practically never, §2.1): use that answer's type
   (behind_der/lugs/branch) with no upstream or spaces. Its upstream is a cloud key, which can't be
   mapped offline.
3. Otherwise `own_supply`.

The names an existing install shows therefore do not change on upgrade.

In the Topology form, a legacy `fed` answer has no default, so the user must pick lugs,
branch or behind_der. Saving writes `topology`. It also writes `fed_by_panel =
local_fed(new options)` as a mirror, so a downgrade to 0.3.0b1 keeps the names. The mirror
is never read when `topology` exists.

`keep_fed_answer` becomes `keep_topology(options)`. If `topology` is absent, it writes
`topology_of(options)` before `cloud_removed_entry` drops `feeds`, so the derived answer
survives the remove. It also keeps the `fed_by_panel` mirror.

### 5.6 Combinations (each panel answers for itself)

Each row is a site; each panel's choice is made on its own entry and doesn't depend on the
others. "Names" are the #63 local names and the linked stand-ins, which follow `local_fed`.
A cloud-account entry running the same panels gives each panel the same answer through its
own feed question, and gets "Site grid" or "Site feed" the same way (O15 adds `behind_der`
there).

| Site | Panel | Its choice (upstream) | Its names | Grid source in the Energy dashboard |
|---|---|---|---|---|
| Two independent panels, both on the utility | A | `own_supply` | Grid | A's grid energy |
| | B | `own_supply` | Grid | and B's grid energy (two services: add both) |
| 400 A service: main panel, plus a critical-loads panel behind solar or a battery | A | `own_supply` | Grid | A's grid energy covers A's share only. With a split service, take grid from the utility meter or the solar/battery system's grid meter |
| | B | `behind_der` | Feed | Never B's feed energy; B's loads are already counted by the solar/battery system |
| One panel fed from another's lugs | A | `own_supply` | Grid | A's grid energy |
| | B | `lugs` (A) | Feed | Not B: A already counts it |
| One panel fed from another's breaker | A | `own_supply` | Grid | A's grid energy |
| | B | `branch` (A, spaces) | Feed | Not B: A's breaker already counts it |
| Chain | A | `own_supply` | Grid | A's grid energy only |
| | B | `branch` (A) | Feed | Not B |
| | C | `lugs` (B) | Feed | Not C |
| Upstream panel behind DER, second panel on its lugs | A | `behind_der` | Feed | The utility meter or the solar/battery system's grid meter |
| | B | `lugs` (A) | Feed | Not B |
| Fed panel that is itself behind DER (solar or a battery between A's breaker and B) | A | `own_supply` | Grid | A's grid energy |
| | B | `branch` (A) | Feed | Not B |
| Upstream not in HA | B | `lugs` or `branch` (serial of a panel HA doesn't run, or "Not listed") | Feed | The utility meter, or the upstream panel outside HA |

Notes:
- A panel can't be both `behind_der` and fed by another panel, and doesn't need to be:
  `behind_der` means DER between the utility meter and this panel with no SPAN panel
  upstream. A fed panel with DER on its feed picks `lugs` or `branch`, which already gives
  the Feed names.
- Changing B's answer never changes A's names, and the other way round. Removing A from HA
  leaves B's answer and names as they are (§5.3).

### 5.7 Cloud-account feed vocabulary (O15)

The cloud Choose panels feed question (`_FeedSteps`, `feeds.py`) already answers per panel
with `own_supply`, `lugs` and `branch`; Topology stores the same values (O17). The
differences that stay: a cloud answer's upstream is a cloud panel key and its circuit is a
cloud circuit number, while a local answer's upstream is a serial and its circuit is
breaker spaces.

`behind_der` is cheap to add to the cloud question, so add it in this PR:
- `feeds.BEHIND_DER = "behind_der"`.
- `feed_of`: keep a `behind_der` answer as it is (it has no upstream to check); today it
  would fall back to `own_supply`.
- `is_fed`: `behind_der`, `lugs` and `branch` are fed, so `cloud_panel_fed` gives "Site
  feed" and `cloud_fed` reports a linked panel as fed.
- `form_value`: an answer with no upstream returns its type (`own_supply` or `behind_der`).
- `from_form`: a bare `own_supply` or `behind_der` value returns `{"type": value}`.
- `feed_choices`: add `behind_der` after `own_supply`, labelled "Its own supply, with solar
  or a battery before the panel". The cloud labels are hard-coded English today, so this
  adds no translation key.
- `has_cycle`, `clean_feeds`, `branch_fed` and `branch_circuits` need no change.
- §5.5 step 2: a linked panel's cloud `behind_der` answer derives `behind_der`.
- A downgrade to 0.3.0b1 reads `behind_der` as `own_supply`, so those panels show grid
  names again. Nothing breaks.

### 5.8 Other panels' answers (read-only, O16)

The Topology description ends with `{others}`: one line per other `span_ebus` panel that
has a saved answer, using the §5.3 labels and no serial. Empty when there are none.

```
Other panels:
- Garage: fed from this panel's breaker
- Shop: fed from Garage's feed-through lugs
- Barn: fed from the utility meter, with solar or a battery before it
```

- Phrases: `own_supply` "fed from the utility meter"; `behind_der` "fed from the utility
  meter, with solar or a battery before it"; `lugs` "fed from <upstream>'s feed-through
  lugs"; `branch` "fed from a breaker on <upstream>"; an upstream that is this panel reads
  "this panel"; an unknown or unset upstream reads "another panel".
- Sources: other local entries' saved `topology` (not derived answers), and a cloud-account
  entry's `feeds` answers for panels whose key is a serial.
- The text is built by a pure helper in `feeds.py` and is hard-coded English, like
  `feed_choices`, because a placeholder isn't translated.
- It is information only; nothing in the form is pre-filled from it (§5 intro).

## 6. Controls hints

The Controls description gains `{hidden}`, filled only for fields the form leaves out. Each
line is a `link_note` option fetched with `_note()`, because placeholders aren't translated:

- `controls_no_override`: "No circuit on this panel feeds a battery or solar inverter, so
  there are none to allow switching."
- `controls_no_islanding`: "Grid-state control appears only on panels with a battery whose
  grid state the panel lets Home Assistant set."

Built from `ControlsForm.override` (empty) and `ControlsForm.show_islanding` (False). Nothing
else changes; saving still writes hidden fields as off (PR 6 deviation 5).

## 7. Strings (strings.json and translations/en.json identical)

- Remove `options.step.local_panel`, `options.step.linked`, `options.step.fed_by_panel`, and
  the abort `cloud_remove_unsupported`.
- Add `options.step.panel`: title "Panel options", menu_options `transport` "Connection",
  `topology` "Topology", `controls` "Controls".
- Add:
  - `options.step.topology`: title "Topology", data `supply` "What feeds this panel", and
    this description:

    "Choose what feeds this panel. This answer is for this panel only: each SPAN panel
    answers for itself, and saving here doesn't change any other panel.\n\n- **Utility
    meter feeds this panel directly:** its sensors are named Grid, and its grid energy can
    be the Energy dashboard's grid source.\n- **Solar or a battery between the meter and
    this panel:** its sensors are named Feed. Don't use them as the grid source; use the
    utility meter or your solar or battery system's grid meter.\n- **Another panel's
    feed-through lugs** or **a breaker on another panel:** its sensors are named Feed.
    Don't use them as the grid source; the upstream panel already counts this
    one.\n\nSaving reloads the panel.{others}"
  - `topology_panel`: data `upstream` "Which panel", description "The panel that feeds
    this one. Panels from your SPAN account and panels found on your network are listed
    too, even if Home Assistant doesn't run them.";
  - `topology_circuit`: data `circuit` "Which breaker", placeholders `panel` and `upstream`;
  - the selector `supply` with the four labels.
- Change (exact text in §4.2):
  - `transport` description and both `transport_mode` selector labels;
  - `cloud_remove` title and description (account removed, backfill lost, `{warning}` kept);
  - `cloud_add` title and description;
  - `controls.description` gets `{hidden}`.
- Add `link_note` options `controls_no_override` and `controls_no_islanding`.
- README:
  - replace the "Fed by another panel" text in the several-panels section with a Topology
    subsection carrying the §5.2 Energy dashboard table;
  - add a note for solar or battery upstream of the panel;
  - describe the two Connection states in the §4.2 words, including the five-minute,
    three-row and 7-day limits of backfill;
  - note that this release removes the stored SPAN account from panels set to Local only.

## 8. Tests (no HA needed for the pure parts)

- `feeds.topology_of`, `local_fed`, `keep_topology`:
  - every legacy combination: no keys; `fed_by_panel` True or False; cloud answers;
    `topology` present but invalid;
  - the mirror written on save.
- `feeds_panel_attrs` is unchanged: the existing tests keep passing, and a saved local
  `branch` topology adds no attribute (O8 deferred).
- Connection routing table (§4.3), all four rows. Use the existing flow-test harness if one
  exists (`test_link.py` / `test_topology_labels.py` patterns). Otherwise make the
  routing a pure function `connection_action(has_tokens, mode, chosen) -> {noop, add,
  remove, enable}` and test that.
- Connection default: `local_first` only for tokens with `local_first`.
- `legacy_local_only`: True only for local serial + tokens + exact `local_only`; False for
  `local_first`, a missing or invalid mode, no tokens, and cloud-account entries.
- The §4.4 setup path, with the existing setup harness if there is one: a legacy entry
  in direction A and in direction B ends with no `CLOUD_DATA_KEYS`, no cloud option keys,
  mode `local_only`, the right minor version, a kept `topology`/`fed_by_panel` answer and no
  token file. The log record has no serial or email (use `caplog` with invented values).
  A second setup changes nothing.
- Connection wording guard: the `local_first` label contains "five minutes" and the
  `local_only` label says the account is removed. This catches an edit that drops the
  qualifiers. A second guard asserts `backfill.MIN_GAP_SECONDS == 300` and the `ROW_FIELDS`
  keys, so a change there fails until the strings are revisited.
- Topology upstream choices (`upstream_choices`):
  - every source is listed: unloaded and ignored entries, account panels, zeroconf flows;
  - dedupe is case-insensitive, and this panel is excluded in any case;
  - label fallback: device name, then "SPAN <model>", then "Panel N", with stable numbers;
    duplicates get " (2)";
  - no label contains a serial or host (assert against the invented values);
  - a stored upstream that matches no source shows as "Panel not found now" and is the
    default;
  - a branch upstream without readable circuits saves `upstream` without `spaces`.
- Loop check: a chain through saved answers errors; an upstream with no saved answer
  doesn't; serials compared case-insensitively.
- An account-read failure still opens the form, and its log record names no panel.
- Cloud `behind_der` (O15): `feed_of`, `is_fed`, `form_value`, `from_form` round-trip,
  `feed_choices` order, `has_cycle` and `clean_feeds` with a `behind_der` answer, and
  `cloud_panel_fed` true for it.
- `{others}` text: phrases per type, "this panel", "another panel", empty when no other
  panel has a saved answer, no serial, derived answers left out.
- Per-panel rule: saving Topology on one entry leaves every other entry's options
  unchanged.
- Controls hints: which notes appear for each `ControlsForm`.
- Strings parity: the existing check that `strings.json` equals `en.json`, and that every
  step id or menu option in the flow has a string.
- The existing `test_topology_labels.py` keeps passing through `local_fed`.

## 9. Decisions (recommendation first)

| # | Decision | Recommendation | Risk |
|---|---|---|---|
| O1 | Menu title | One menu, `panel`, "Panel options", for both local and linked entries. Keep "SPAN account" for cloud-account entries. | Low |
| O2 | Order | Connection, Topology, Controls. | Low |
| O3 | Connection states | **Fixed by the user:** two. With cloud fallback keeps the account; Local only removes it, as `cloud_remove` does. It also matches the code: under `local_only`, backfill, fallback and continuation are all off (§2.1). | Low |
| O4 | Legacy entries with tokens and `local_only` | **Changed per team-lead:** remove the dead account at setup (§4.4) by running the same `cloud_removed_entry` rewrite as the confirm, then deleting the file. Log only that it was removed. Connection shows "Local only" with no account, and fallback goes through the normal `cloud_add` sign-in. The re-enable row is dropped. Deleting only the file is not enough, because `CONF_TOKENS` and the email stay in `entry.data`. | Medium: it can't be undone and shows no warning, though nothing changes at runtime. Only an exact `local_only` triggers it. The dev instance loses its account on the first restart if it is in this state. |
| O5 | Names for `behind_der` | **Accepted:** reuse the #63 "Feed …" names ("Feed power", "Feed consumed / returned energy"), so no new translation keys. | Medium: the user sees their "Grid power" become "Feed power" once they choose it. That is intended and matches their site. |
| O6 | Topology storage | New local option `topology` outside `CLOUD_OPTION_KEYS`. The cloud `feeds` stays cloud-only and is never written from Topology. | Low |
| O7 | Upstream and circuit keys | Upstream by device serial (HA's identifier for both local and cloud panels), also for panels HA doesn't run (§5.3). Circuit by breaker spaces. | Medium: serials in options. They are already in data, never logged. |
| O8 | `feeds_panel` from local answers | **Deferred per team-lead.** Topology stores `upstream` and `spaces`; `feeds_panel_attrs` keeps reading cloud answers only. The local read is a follow-up (§10). | Low: the `branch` choice gives no attribute between local entries until then, which is the same as today. |
| O9 | Migration | Derive on read, with no version bump and no setup write. A legacy `fed_by_panel=True` shows as a required choice. Saving writes `topology` plus a `fed_by_panel` mirror for downgrade. | Low |
| O10 | Reusing `_FeedSteps` | Reuse the constants, the circuit labelling and the strings shape, not the mixin (it is tied to a cloud token and site list). `_FeedSteps` stays unchanged for cloud-account entries. | Low |
| O11 | Step ids | Keep `transport`, `controls` and `cloud_remove` (the confirm) to limit churn. Add `panel`, `topology`, `topology_panel`, `topology_circuit`. Drop `local_panel`, `linked` and `fed_by_panel`. | Low: step ids aren't stored. |
| O12 | Controls hints | Add the two `{hidden}` notes. | Low |
| O13 | Remove confirm warnings | The account-removed and backfill-lost text is always shown (§4.2). The fallback-only `{warning}` stays where it is today: direction A with fallback-only entities. Direction B is unchanged and out of scope. | Low |
| O14 | Backfill wording | State the real limits: gaps of five minutes or more, site, solar and battery energy only, the last 7 days, single-panel sites; no circuits. The example is a restart only. A reload records its downtime too, but a normal one stays under 5 minutes (§4.1). Guard it with the §8 tests. | Low |
| O15 | `behind_der` in the cloud feed question | Yes, in this PR (§5.7): a few lines in `feeds.py` and one hard-coded label, no new strings. Cloud-account entries then get "Site feed" for it. | Low: a downgrade shows grid names for those panels again. |
| O16 | Read-only summary of the other panels' answers | Yes (§5.8): one `{others}` placeholder from a pure helper, saved answers only, same labels, no serials. It helps the user answer consistently without making the answer site-wide. | Low: hard-coded English, like the cloud feed labels. |
| O17 | Stored supply values | Use the cloud vocabulary: `own_supply` (was `utility` in the draft), `behind_der`, `lugs`, `branch`. Nothing is stored yet, so this costs nothing and gives one vocabulary in `feeds.py`. | Low |
| O18 | Upstream sources | All entries (loaded, unloaded, ignored), the account's panels when this entry has tokens, and in-progress zeroconf flows. No mDNS browse or probe from the options flow; "Not listed" covers the rest (§5.3). | Low: a panel on the network that HA made no flow for isn't offered. |

## 10. Out of scope

- Cloud-account entries' Choose panels and feed questions, except the `behind_der` addition (O15, §5.7).
- New entities.
- Backfill without fallback.
- The `feeds_panel` attribute from local Topology answers (O8): `local_branch_answers`, the
  local lookup in `topology.feeds_panel_attrs`, and passing panel serial and spaces from
  `local_entity` and the cloud circuit sensors. The stored keys already fit it.
- Browsing and probing mDNS `_ebus._tcp` hosts from the options flow (O18).
- A site-wide Topology answer, or deriving one panel's answer from another's (§5).
- Live tests (memory: no-live-control-tests, dev-instance-is-prod).
