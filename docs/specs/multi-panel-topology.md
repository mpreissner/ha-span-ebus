# Spec — multi-panel support (picker, per-panel devices, feed topology)

**Status:** draft, 2026-11-02. Not implemented. Builds on PR #39 (one stream per
SPAN site), which is still open. The one-panel checks ran on 2026-11-02 and
confirmed the resource ids section 9 depends on. Sections 7 and 9 still rest on
behaviour nobody has observed: no two-panel site has been seen, so its response
shape is a guess, and the feed corrections have not been checked on real hardware.
**Scope:** find every panel on the account, let the user choose which belong to
this Home Assistant, give each chosen panel its own device and circuits whether or
not it shares a site, and ask how panels are fed. An account with one panel sees
no change.

## 1. Why

PR #39 treats a site as a panel: one stream, one device, one set of energy totals.
That covers panels on separate sites, the only multi-panel layout seen so far. It
leaves three gaps:

* **An account's panels may not all belong here.** A beach house and a home can
  share a login but not a Home Assistant.
* **A site may hold two panels.** `parse_sites` keeps the first panelboard, so the
  second would be missing, and its circuits would land on the first panel's device.
* **Some values cross a panel boundary.** What a downstream panel's feed looks like
  on the panel above depends on the wiring:

| Feed | The downstream panel's CTs match, upstream | What goes wrong if ignored |
|---|---|---|
| Own supply | Nothing | Nothing, provided no link is invented |
| Feed-through lugs | The upstream `feedthrough` reading | Upstream `panel_overhead` may include the downstream panel's whole load (unverified, section 7) |
| Branch circuit | One upstream circuit | That load appears twice in any two-panel total |

The rule throughout: **the panel is the unit the user sees, and the site is only
how the cloud delivers it.** Every panel is one device with its own circuits,
whichever way SPAN grouped them.

## 2. Why ask, rather than detect or infer

**Not a toggle.** Discovery already knows how many panels there are, and a toggle
could only be set wrongly.

**Not service size.** 200A and 400A do not determine the wiring. A 200A meter-main
can feed two independent panels, a 400A site can have a third panel fed from
either of the first two, and a separate building can have its own meter.
Service sizes also differ by country. Lugs, a branch breaker and a separate
supply mean the same thing everywhere, so the question is about those.

**Not detection first.** The schema has the right vocabulary
(`ElectricalConnectionPoint` separates `feeder_circuit_connection` from
`feed_through_lugs_connection`), but `SiteTopologyTrait` has never been decoded,
its trait id is unknown, and it is per site. With one panel per site it may not
link the two panels at all. The user's answer needs none of that. Detection
stays optional (section 10).

## 3. Setup flow

```
user step: email, password
  -> authenticate
  -> probe (unchanged: proves the channel carries telemetry)
  -> discover panels
       one panel, or discovery failed   -> create entry, done
       more than one panel              -> "panels" step
  -> "panels" step: tick the panels that belong here
       one ticked                       -> create entry
       more than one ticked             -> "feed" step
  -> "feed" step: one select per ticked panel
       every answer is own supply/lugs  -> create entry
       any answer is a branch           -> "feed_circuit" step per such panel
  -> create entry
```

* **Discovery failure does not block setup.** The entry is created with no panel
  list and no feed answers. Every panel the next start finds runs, and counts as
  own supply.
* **Reauth skips all of this.** It has no reason to touch topology, and
  `async_update_reload_and_abort` leaves `entry.options` alone.
* **The probe stays as it is.** On a multi-panel account it subscribes every
  panel on the entry's own UUID. That is harmless for a connectivity check,
  because the runtime streams use per-site channels and the serial it saves is
  not used for those panels.

### The `panels` step

A checklist of every panel on the account, all ticked by default, whichever site
each is on:

```
Which panels belong to this Home Assistant?
  [x] Home: MAIN 40 XC-AAAA
  [x] Home: MAIN 16 XC-BBBB
  [ ] Beach house: MAIN 40 XC-CCCC
```

The site label comes from the head of the site entry (section 9), and the label
falls back to `<model> <serial>` without it. On our account that field is the
street line of the address rather than a name, so it appears only in this form and
the `feed` form, never in a device name, entity id or log. Ticking nothing is a
form error.

### The `feed` step

One form with one select per ticked panel, labelled as above, defaulting to own
supply:

```
What feeds this panel?
  Home: MAIN 40 XC-AAAA    [ Its own supply                    ]
  Home: MAIN 16 XC-BBBB    [ Feed-through lugs on MAIN 40 ...  ]
```

Each select offers its own supply, then lugs and branch for every other ticked
panel. With two panels the user changes one row. The same form handles three or
more. An unticked panel is not offered, so a panel fed by one counts as own supply.

Validation, reported as form errors:

* A panel cannot feed itself. The options make this impossible.
* Feeds cannot form a cycle, including two panels feeding each other.

Two panels may share one upstream panel. How a shared upstream reading is split
is not a question this spec has to answer, because the corrections in section 7
use the upstream reading as a whole.

### The `feed_circuit` step

Circuit names arrive only once a panel's stream is running, so this step probes
the site of the chosen upstream panel, and keeps only that panel's circuits:

```python
CloudBackend(path, site_device_uuid(entry_uuid, panel.site_id),
             user_id=user_id, site_id=panel.site_id,
             panel=panel).probe(timeout=PROBE_TIMEOUT)
```

The select lists `schema.circuits()` by name. `probe` returns the first
content-bearing schema, so the implementation must confirm that schema holds the
full circuit list. If it does not, the probe has to wait for one that does. A
timeout must not lose the user's other answers: save the answer without a circuit
and show it in options as incomplete.

### Options flow

The integration has no options flow today, and this adds one. It reuses the same
steps, because panels get added, wiring changes and a skipped question needs a
home. Saving reloads the entry.

Unticking a panel warns first, then removes its device and entities. Its saved
energy totals stay until the entry is removed, so ticking it again resumes them.

## 4. Stored data

Answers go in `entry.options`, keyed by panel:

```python
CONF_PANELS = "panels"
CONF_FEEDS = "feeds"

{"panels": ["<panel-a>", "<panel-b>", "<panel-c>"],
 "feeds": {
    "<panel-a>": {"type": "own_supply"},
    "<panel-b>": {"type": "lugs", "upstream": "<panel-a>"},
    "<panel-c>": {"type": "branch", "upstream": "<panel-a>", "circuit": "<circuit-id>"},
}}
```

A panel key is the panel's serial, which the device's unique ids already use. A
panel with no usable serial uses its hardware id (section 9).

* `panels` absent means nothing has been chosen yet. Every discovered panel runs,
  and the list is recorded on the first start where discovery works. That covers
  existing entries and a failed setup discovery.
* `panels` present means only those panels run.
* A panel with no `feeds` key counts as `own_supply`.
* A listed panel that is no longer on the account is ignored and logged. An
  `upstream` that has gone counts as `own_supply`.

Serials and site ids are private, so they stay out of logs above debug and out of
anything committed here.

## 5. A panel added later

An account that gains a panel has already finished setup, so nothing would ask.
That panel may not belong to this Home Assistant, so it stays off:

1. The new panel is not started, and is not added to `panels`.
2. A Repairs issue (`homeassistant.helpers.issue_registry`, not auto-fixable) says
   a panel was found and points to the options flow. It clears once the panel is
   ticked, or once the user declines it, which is stored as a `declined` list in
   `entry.options` so the notice does not return.

The same applies to a one-panel account, which never saw the picker.

## 6. Identity

### Which panel keeps the entry's identity

**This needs a change to PR #39's identity rule.** `multi_site` is computed as
`len(sites) > 1` on every setup. When a one-site account gains a panel, its
original panel flips to a derived device UUID and a site-scoped energy store. The
serial, and so the unique ids, should hold, but the store under the entry id
stops being read. `EnergyLedger.adopt` may rebuild totals from restored entities;
that has not been tested.

The ticked count cannot decide this either, because it changes in the options
flow. Fix: record which panel owns the entry's identity.

```python
CONF_PRIMARY_PANEL = "primary_panel"   # entry.data, set once
```

* First setup with exactly one panel on the account: record it. Existing one-panel
  entries gain it on their first start after the upgrade.
* First setup with several panels: record `None`, and everything is scoped. The
  key's presence is the record. Leaving it unset would let an account that later
  shrinks to one panel name that panel primary and move it off its own store.
* A failed topology read records nothing and uses the legacy single stream.
* Reauth keeps the recorded value and the saved serial.
* `stream_identity` takes `primary_panel` in place of `multi_site`. The primary
  panel keeps the entry's energy store, and its site keeps the entry's device UUID,
  for good. Everything else is scoped.

### Scope follows the unit

| Thing | Belongs to | Key |
|---|---|---|
| Ably channel, device UUID, Ably connection | Site | `site_device_uuid(entry_uuid, site_id)` |
| HA device, circuits, energy store, history | Panel | Panel key |
| Site flows (`Frame.site_flows`, `site_instance_id`) | Site | Site id |

PR #39's `energy_store(hass, entry_id, site_id)` becomes
`energy_store(hass, entry_id, panel_key)`. Nothing has been released with the site
key, so this changes it with no migration, and only if done before release. The
contributor's test install is a remove and re-add.

## 7. What the feed answers change

The right-hand side of this table is provisional until the capture in section 8.

| Feed | Change |
|---|---|
| Own supply | None |
| Lugs | Decode `PanelInstant.feedthrough` (field 2) and add a feed-through power sensor to the upstream panel. Correct upstream `panel_overhead` only if `feed` proves to include the feed-through load |
| Branch | Add a `feeds_panel` attribute to that circuit's power and energy sensors, naming the downstream panel |

* `panel_overhead` on a branch-fed upstream panel needs no change: the feeding
  circuit is in the circuit sum, and its power is in the feed.
* The integration has no cross-panel total today, so double counting of a
  branch-fed panel happens in the user's own dashboards. The attribute makes that
  visible. An integration cannot set Home Assistant's Energy dashboard
  `included_in_stat`, so the user still sets that upstream device by hand.
* If the capture shows `panel_overhead` is wrong on lug-fed panels and the fix
  has to wait, hide it on upstream panels with a lug-fed child rather than
  reporting the downstream load as overhead.
* The feed answers apply the same way on one site or two. They never decide which
  device a circuit belongs to.

## 8. Captures needed

This repo is public: a capture must be reduced to numbers and shapes before
anything is pasted, with every serial, site id, hardware id and address replaced.
`tools/capture_panels.py` does this (PR #46, merged to `dev` at 8b4b30e).

On our panel (2026-11-02, about 3 minutes, five load steps) the stream held on one
subscribe and redaction held. `feedthrough` read zero, as expected with nothing on
the lugs. The schema names mislead: `PanelInstant` field 3 (`total_branch`) tracks
`feeder`, while field 4 (`balance`) tracks the circuit sum, as does the `home` flow
exactly. Judge the topology by measured matches, not by those names. Rows also
disagreed by up to 800 W with the load on, which looks like a cycling load, so the
tool now prints each median's range and asks for a steady load.

**From our own one-panel account: done 2026-11-02.** Telemetry was read for 75
seconds on a throwaway channel, with no commands sent.

| Check | Result |
|---|---|
| Frame `resources` keys against member ids | One key, the kind 5 id. It held all 31 instances in every one of the 78 frames with a block |
| Trait snapshot resource ids | Two: the kind 5 id (167 entries, all 29 `1/15` circuits) and the kind 4 id (19 entries, no circuits) |
| Site id | Identical to the kind 4 member's hardware id. Every frame's site header carries it, and it never appears as a block in `resources` |
| Full `GetSitesForUser` dump | Boards list their components, and those components carry the hardware ids, which links a board to its kind 5 id (section 9) |
| `GetResourcesForHardware` with serial and model code | Permitted. Returns the same members and board as the site's group |
| `SearchResourcesForSerial` with serial only | Permitted. Same content, wrapped in `found_hardware` with a type field. An earlier capture's empty result does not hold here |

The kind 5 inference stands, on one panel. 5 of the 83 frames carried no block and
no flows, only the site header, so splitting has to tolerate those.

**From the two-panel account already known** (MAIN 40 lugs feeding a MAIN 16), run
while switching a large MAIN 16 load on and off, recording for each panel:

| Value | Answers |
|---|---|
| MAIN 40 `feeder`, `feedthrough`, `total_branch`, `feed-*` node power | Does `feed` include the MAIN 16's load? What is the sign of `feedthrough`? |
| MAIN 16 `feeder` | Does it track the MAIN 40 `feedthrough`? |
| Site flows on both panels | Does the MAIN 40 `grid` flow include the MAIN 16? Does the MAIN 16 report a `grid` flow, and is it the same power counted twice? |
| Both panels' energy totals over a day | Do totals match the SPAN app per panel? |
| Raw trait snapshot, redacted | Is `SiteTopologyTrait` present, and does it link two sites? |

Nothing goes to that contributor until there is a published build to test against.

**From a two-panel site, if one turns up.** The site response, frame `resources`
keys and trait resource ids, redacted. This confirms or replaces section 9. A
branch-fed install would likewise answer section 8's questions for that case. None
is known of yet, so both behaviours may ship unconfirmed. The branch case only adds
an attribute, so getting it wrong costs little. The two-panel-site case is guarded
by section 9's fallback.

## 9. Two panels on one site

### What is known

```
1 sites[] {
    1 { 1 site_id  2 label  5 address { 4 ?  6 ?  7 ?  9 same text as label } }
    3 groups[] {
        1 members[] { 1 kind  2 hardware_id  3 site_id  6 site_id  8 component_uuid }
        2 panelboards[] {
            1 uuid
            4 { 3 serial  5 model_code }
            5 name
            8 components[] {
                1 uuid
                4 { 3 serial  5 model_code }
                5 name
                7 { 1 hardware_id }        only on components with a member
            }
        }
    }
}
```

Member kinds seen: 4 site, 5 gateway, 6 bess. All of that comes from one-panel
sites. Fields 3 and 2 of the group are repeated in the schema's shape, which
`parse_sites` already reflects.

Confirmed on our panel (section 8):

* **The kind 5 id is the resource id** for circuits, in frames and in traits.
* **The board links to it directly.** Of the board's nine components, two carry a
  hardware id, and those are the kind 4 and kind 5 ids. Each member's field 8 also
  equals its component's uuid. So a board's kind 5 id can be read from the board
  itself, rather than inferred from sharing a group.
* **The site id is a piece of hardware.** It is the id of a component inside the
  board (model 1-02351-06), and it owns a real resource with its own metering trait
  (`1/26`). Site flow history is already requested against it, by
  `energy_targets`. So the kind 4 block is not an unknown block.
* **Board field 5 is a name**, and on our panel it is not the literal `Panelboard`
  earlier notes recorded. No code depends on that text. Whether it is user-set is
  unknown, because the report redacts it. Look at it on our own panel when building
  the picker, and use it in the label if it reads well.
* **The site label is not a name here.** Field 2 matches the street line of the
  address block. It may be a default the app lets users change.

The opposite of the two-panel layout has been seen: two panels, two sites. So SPAN
may never put two on one site. The schema allows it (`SiteTopologyTrait` is a tree
that can hold more than one `span_panel_node`), and the SPAN app's devices tab
suggests it. Against that, a site id being one panel's own component suggests each
panel may anchor its own site. If a two-panel site exists, its second panel probably
has its own kind 4 component, so **a second kind 4 member must not read as a second
site**. The site id comes only from the head of the site entry.

### Guessed response shapes

**Unverified. No such response has been seen.** If boards keep their component
lists as ours does, shapes A and B both pair directly, and the differences below
stop mattering.

**A. One group per panel.**

```
3 { 1 [ {4 site}, {5 gateway-1} ]   2 [ board XC-AAAA ] }
3 { 1 [ {5 gateway-2} ]             2 [ board XC-BBBB ] }
```

**B. One group, repeated members and boards.** Two of each, linked only through
each board's components.

```
3 { 1 [ {4 site}, {5 gateway-1}, {5 gateway-2} ]   2 [ board XC-AAAA, board XC-BBBB ] }
```

**C. One gateway, two boards.** One subscribable id. The panels may share a resource
block, or be told apart by some id we have not seen.

```
3 { 1 [ {4 site}, {5 gateway-1} ]   2 [ board XC-AAAA, board XC-BBBB ] }
```

### Parsing

`SiteInfo` gains `name` and `panels: tuple[PanelInfo, ...]`:

```python
@dataclass(frozen=True)
class PanelInfo:
    site_id: str
    serial: str | None
    model_code: str | None
    hardware_id: str | None    # the frame and trait resource id, once paired

    @property
    def key(self) -> str: ...  # serial, else hardware_id, else site_id

    def answers_to(self, key: str | None) -> bool: ...  # key, serial or hardware_id
```

`SiteInfo.lead`, the first panel with a serial, names the site's single stream until
step 4 splits it. A site with no board gets one synthesised panel, so `panels` is
never empty. `primary_panel_for` counts panels, not sites, and `stream_identity`
checks every panel on the site, because a new board can sort ahead of the primary.

`answers_to` keeps a primary recorded by hardware id when a serial appears later.
Site id is deliberately not an alias: two boards on a site would both match. So a
panel with neither serial nor gateway stops matching a recorded site id; it has no
circuit stream, so nothing moves. A panel that is not the primary and has no serial
still changes store key when a serial appears; step 5 settles that through the
recorded `panels` list. Unpaired boards are step 4's: they have no hardware id, so
the site id fallback must not key two of them.

`parse_sites` reads member kinds and collects every board and every kind 5 member.
`hardware_ids` stays the full list used for subscribing. Pairing, first match wins:

1. One board and one kind 5 member on the site: they pair. This is every site seen
   so far.
2. A board's components (`8[].7.1`) hold exactly one kind 5 member's id: they pair.
   This link is confirmed on our panel, and handles shapes A and B.
3. Each group holds exactly one board and one kind 5 member: pair within the group
   (shape A without component ids).
4. `GetResourcesForHardware(serial, model_code)` for each board still unpaired, when
   the result holds that board and exactly one kind 5 member. The call works on our
   account, and its response has the group's fields, so rules 1 and 2 can be reused
   on it. If it returns the whole site, it settles nothing. It runs only when rules
   1 to 3 leave a board unpaired, so no one-panel site ever calls it, and a failed
   call counts as no answer.
5. Otherwise, the pairing is unknown. **Response order is never used to pair.** A
   wrong guess swaps two panels' circuits while looking correct.

### Routing

Frames and snapshots are already grouped by resource: `Frame.resources` is
`{resourceId: samples}`, and `parse_trait_snapshot` walks `1 resource[]`. The
collisions come later, where `parse_trait_snapshot`'s result and `_index_traits` key
on instance id alone. Rather than scope each of those, split first:

```python
split_frame(frame, panels) -> dict[panel_key, Frame]
split_snapshot(raw, panels) -> dict[panel_key, bytes]
```

* One stream per site, as PR #39 has it. Each panel gets its own state after the
  split, its own `PanelSchema` and its own coordinator, fed by the site's stream.
* Everything after the split sees one panel, so `_node_for`, `feed-<id>`
  classification, instance-keyed node ids, `panel_overhead` and `schema.circuits()`
  stay as they are. Two panels can both have an instance 42, because they are never
  in the same schema.
* Circuit control already works: `SwitchTarget.resource_id` comes from the block
  that declared the circuit.
* Energy history already targets by resource id.
* The site id's resource and `Frame.site_flows` are the site's, not a panel's. They
  go where "Site flows on a shared site" says, and are never treated as an unknown
  block. The site's snapshot block holds no circuits, so no circuit depends on it.
* Other blocks belonging to no ticked panel are dropped, with one debug log per
  block id. This covers an unticked panel on the same site.
* A frame may hold any mix of blocks, or none (section 8). Whether two panels share a
  frame is unknown, and the split works either way.

**A site with one panel skips the split entirely** and takes every block, as today.
That path must stay unchanged, so that a wrong assumption here cannot touch an
existing install, including the kind 5 inference.

### When pairing fails

Blocks are still separable without knowing which board each is. What is missing is
the name, so nothing is mixed up:

* **Several blocks hold circuits (shape B, pairing unknown).** Each block gets a
  device named `SPAN panel <n>`, keyed on its hardware id, with the right
  circuits and no serial or model. The picker has to list kind 5 members rather than
  boards here, and uses the same names.
* **One block (shape C).** Instance ids are unique within a block, so nothing
  collides. The site stays one device with every circuit, as today, and setup logs
  a warning with the board count, asking for a capture.

A later pairing that gives such a panel a serial must not change its key. Its
hardware id stays its key, and only the device name and model update.

### Site flows on a shared site

Flows (grid, solar, battery) and their energy belong to the site, and splitting them
would be invented. On a site with more than one panel they go on a small site device,
created only for such sites. A site that holds the primary panel keeps them on that
panel's device, because moving them would orphan their history.

## 10. Detection, later and optional

Once the question exists, detection can only pre-fill it or question it:

* Pre-fill from `SiteTopologyTrait`, if a capture shows it links panels.
* Flag a mismatch, for example lugs chosen but the upstream `feedthrough` stays at
  zero while the downstream panel draws load.
* Suggest the circuit for a branch feed by correlating each upstream circuit with
  the downstream `feeder` over time.

A lightly loaded subpanel gives no clear result, so detection never overrides an
answer and never changes values by itself.

## 11. Implementation order

1. Done: the one-panel checks in section 8 confirmed the kind 5 link.
2. Done: merged as c3fc9e3. Change PR #39 before release: panel-keyed energy store and `CONF_PRIMARY_PANEL`
   (section 6). Merge it. This protects existing installs regardless of the rest.
3. Done: merged as 3f32a38. `PanelInfo`, `parse_sites` and pairing, with a
   site's single panel taking the unchanged path.
4. Done: merged as 38b43c6. `split_frame`, `split_snapshot`, per-panel
   coordinators and the site device. As built, the split takes
   `(frame, routes, unrouted)`: `SiteRoutes` holds the block-to-device map and
   `unrouted` keeps the drop log to one line per id. One `SiteStream` owns the
   connection and feeds a `SitePanelBackend` per device; each child keeps a merged
   frame so a panel's schema is built from everything seen for it, and re-emits
   when that grows. Open: a panel's key moves from hardware id to serial if it
   pairs later, until the stored list (step 5) pins it; an unpaired primary on a
   shared site does not match its route, so flows go to the site device; a BESS
   block on a shared site is dropped as unknown.
5. Done: merged as f8fd9d2: the `panels` step, stored list and Repairs
   issue, including translations in `strings.json` and `translations/`. A
   panels-only options flow moved here from step 6, since the Repairs issue has to
   point somewhere. As built, `panel_plan` reads the stored answers and pins
   recorded keys, `site_routes` takes `chosen`, and names index the site's
   gateways so unticking one panel doesn't rename another. A shared site stays
   split with one panel ticked, so the other's blocks drop. Open: a panel stored
   by serial whose pairing fails on one start shows as an unpaired gateway, so it
   doesn't start and raises Repairs until the next start; on an unsplit site, a new
   board sorting ahead of the lead reads as a new panel; a primary whose device id
   came from the entry serial and differs from its key keeps its device when
   unticked; if no chosen panel is on the account nothing runs, with a warning.
6. Done: merged as 4486e9c: the `feed` and `feed_circuit` steps, added to the
   options flow. Change from section 3: circuit names come from the
   `SubscribeAndGetTraits` snapshot (`panel_circuits`), not from a stream probe, since
   that call answers directly with the full list. Not yet run against the real cloud;
   check it in step 7. Field labels are the picker's labels, as Home Assistant can't
   translate dynamic field names, and option labels are English only.
7. Done: published v0.2.0b1 from `dev` at df27dbd (below), v0.1.16 stays Latest, and
   installed on our panel.
   `panel_circuits` works against the real cloud: 29 circuits on the MAIN 40, all
   named and in space order (a MAIN 40's spaces run 9-48), with a throwaway device id,
   so the real one on a running stream is unchecked. One panel unchanged, and reauth
   works, and the HACS steps are recorded below.
8. Ask the contributor to install it and run the two-panel capture (section 8). Tool
   merged; asked on #39 on 2026-11-02, waiting on the capture and a day of energy
   totals.
9. Section 7 corrections, once the two-panel capture supports them.

### Getting a build to testers

HACS installs from releases or the default branch, so `dev` is not on offer. A
GitHub pre-release needs nothing from `main`, and v0.1.16 stays Latest, so nobody
is offered it unless they ask:

```
manifest.json version -> 0.2.0b1      (direct commit to dev)
gh release create v0.2.0b1 --target dev --prerelease
```

Testers choose Redownload in HACS, expand "Need a different version?", pick the tag
from the Release drop-down, then restart from the Repairs notice HACS raises. There
is no beta switch in that dialog (checked on our own install, v0.2.0b1). Installing
`dev` as a branch is not an option: it has no fixed version, so a report could not
be tied to a commit.

* Later betas are offered automatically only if the integration's hidden
  Pre-release switch is on in HACS. Otherwise testers redownload each one.
* Release notes must say a multi-panel account has to remove and re-add the
  integration, and that going back to v0.1.16 means doing so again, without the
  per-panel energy totals.
* This counts as a release for section 6: the panel-keyed store and
  `CONF_PRIMARY_PANEL` must be in the first pre-release.

## 12. Tests

Home Assistant is not a test dependency here, so the logic stays in plain,
importable functions, as `stream_identity` already does:

* `parse_sites`: one panel on one site, panels on separate sites, and synthetic
  shapes A, B and C, labelled in the tests as guesses.
* Pairing: by a board's component ids, with members listed in the opposite order to
  the boards; never by order, including when counts match; lookup result used when it
  names one kind 5 member, ignored when it returns the whole site; no lookup call
  when the site response already pairs. Fixtures use invented ids only.
* `parse_sites`: a second kind 4 member does not make a second site.
* `split_frame` and `split_snapshot`: the same instance id on two panels stays
  apart; an unticked panel's and an unknown block are dropped; the site's block and
  flows are kept for the site; a frame with no block is harmless; a one-panel site
  passes through unchanged.
* The existing single-panel test suite passes untouched.
* Reading stored answers: `panels` absent and present, missing feed key, departed
  panel, departed upstream.
* Validating answers: self-feed, two-panel cycle, longer cycle, valid chains, empty
  selection.
* `stream_identity`: the primary keeps the entry's identity when a second panel
  appears on its own site or another, a newcomer is scoped, and a `None` primary
  scopes everything.
* Overhead correction, once section 7 is settled.

The flow steps themselves need a manual run on a real or faked two-panel account.
Manual checks: a one-panel setup shows no new step, reauth keeps the answers, and
unticking removes the device while re-ticking resumes its energy totals.

## 13. Out of scope

* Cross-panel totals or a whole-home sensor.
* Automatic detection as a source of truth.
* A battery or other resource block on a site with more than one panel, beyond
  being dropped by the split. Nothing has been seen to say where it belongs.
* Two Home Assistant entries sharing one panel. Each entry is one login, and ticking
  the same panel in two is the user's choice to avoid.
* Migrating multi-panel entries. They remove and re-add the integration.
* Local-only operation. Discovery, the panel keys and the circuit probe all use the
  cloud, so the local-only option will need its own answer for multi-panel accounts.
