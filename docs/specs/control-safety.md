# PR 6: control safety

**Status:** approved rev 1 (2026-11-14); all open questions decided as recommended.
**Branch:** `feature/control-safety` from dev 9f03792. Local spec; never commit or link.

## 0. Scope

What the panel lets us set, from the r202639 schema (`tests/fixtures/r202639/schema.json`):

| Class | Property | Values | Today |
|---|---|---|---|
| circuit | `switch/relay` | OPEN, CLOSED | switch (4a), hidden on storage/solar circuits (4b-1) |
| circuit | `load-shed/priority` | OFF_GRID, SOC_THRESHOLD, NEVER | select (4a) |
| distribution-enclosure | `shed/asserted-islanding-state` | NONE, ON_GRID, OFF_GRID | read-only attribute (4b-2) |
| evse | `config/user-max-charge-current` | integer A | read-only (held from 4b) |
| evse | `switch/lock-state` | UNLOCKED, LOCKED | read-only (held from 4b) |

`doe/*` is **not settable** in the schema, so DOE limits are out of scope and stay read-only.

PR 6 delivers:

1. Command outcomes and the `span_ebus_command` event, plus a logbook entry. These replace `PublishResult`.
2. Safety options, under a new "Controls" step in the options flow:
   - circuits whose relay may not be switched;
   - the per-circuit override for the storage/solar relay rule;
   - opting in to the islanding control.
3. The islanding control (upstream's GFE override), gated as upstream.
4. The EVSE charge-current number and the EVSE lock, as specified in r202639-subdevices §6.1–6.2.

Not in PR 6:
- `rotate_credentials` (Q5);
- DOE writes;
- the cloud-only relay switch (Q6).

## 1. Command outcomes (replaces `PublishResult`)

### 1.1 Pure types (`span_client/local/commands.py`, new; no HA imports)

```python
class Delivery(StrEnum):
    ACKED = "acked"        # broker acknowledged the publish
    UNACKED = "unacked"    # OutcomeUnknown: may or may not have reached the panel

class Outcome(StrEnum):
    CONFIRMED = "confirmed"          # the panel echoed the value within CONFIRM_WINDOW
    NOT_CONFIRMED = "not_confirmed"  # the window passed, and the panel shows another value
    SUPERSEDED = "superseded"        # a newer command to the same entity replaced it first
    UNAVAILABLE = "unavailable"      # the entity went unavailable inside the window
    REFUSED = "refused"              # a safety rule or option stopped it; nothing was published
    NOT_SENT = "not_sent"            # local link down or publish failed; nothing reached the broker
    INVALID = "invalid"              # the value failed validation; nothing was published

class Control(StrEnum):
    RELAY = "relay"
    PRIORITY = "priority"
    ISLANDING = "islanding"
    EVSE_LIMIT = "evse_limit"
    EVSE_LOCK = "evse_lock"

class Refusal(StrEnum):
    PROTECTED = "protected"            # the user's protected-circuit option
    STORAGE_OR_SOLAR = "storage_or_solar"  # model.relay_blocked, no override
    NOT_OPTED_IN = "not_opted_in"      # islanding control is off in options
    GATED = "gated"                    # an availability gate failed at call time (islanding, EVSE ceiling)

@dataclass(frozen=True)
class CommandRecord:
    control: Control
    requested: str          # the panel's value, as published (e.g. "OPEN", "16")
    previous: str | None    # what the entity showed before the command
    outcome: Outcome
    delivery: Delivery | None   # None unless something was published
    refusal: Refusal | None

def event_data(entity_id: str, record: CommandRecord) -> dict[str, Any]: ...
```

`event_data` returns exactly these keys:
- `entity_id`, `control`, `requested`, `previous`, `outcome`;
- `delivery` and `refusal` (either may be None).

It **never** includes a device id, serial, topic, host or circuit uid. A test asserts the key set.

### 1.2 Transport

- `async_send_command(device_id, key, value) -> Delivery`. Behaviour is otherwise unchanged.
- Before encoding, a new guard runs: `self.safety.check(device_id, key)`. It raises `ControlRefused(refusal)`, a `HomeAssistantError` subclass with a translated message (§2.4). The safety rules therefore hold for any future caller, such as a service, and not just for entities (r202639-model §4.5 put them here).
- Nothing is retried.

### 1.3 Entity (`local_entity.SpanLocalEntity._async_command`)

Each command produces **one** record, fired when its outcome is final:

| Path | Outcome | Fired | Raises |
|---|---|---|---|
| `ControlRefused` | REFUSED | immediately | yes, re-raised |
| `ServiceValidationError` | INVALID | immediately | yes |
| `HomeAssistantError` from send | NOT_SENT | immediately | yes |
| sent, echoed in window | CONFIRMED | on echo | no |
| sent, window passed | NOT_CONFIRMED | at deadline | no (call already returned) |
| sent, newer command first | SUPERSEDED | when the newer one is sent | no |
| sent, entity goes unavailable | UNAVAILABLE | on that snapshot | no |

- **NOT_CONFIRMED** also logs a warning naming only the control and outcome. Today this is a debug log.
- **The event:** `hass.bus.async_fire("span_ebus_command", event_data(...), context=self._command_context)`. The context comes from the service call (`self._context` at call time), so HA records who issued the command.
- **`_pending`** becomes `(value, deadline, previous, delivery, context)`. Everything else about the confirm logic stays as it is.
- **Order of checks:** the existing `self._blocked(...)` check in `_async_command` moves into the transport guard. The entity still goes unavailable while blocked (presence), but the refusal and its event come from one place.

### 1.4 Logbook (`logbook.py`, new)

`async_describe_events` for `span_ebus_command`. It produces "{requested} requested: {outcome}" for the entity, for example "OPEN requested: confirmed" or "OPEN requested: refused (protected)". There is no other command record: the event plus the logbook is the record (Q4).

## 2. Safety options

### 2.1 Storage (`entry.options`)

| Key | Type | Default |
|---|---|---|
| `protected_circuits` | list of circuit device ids | `[]` |
| `relay_override` | list of circuit device ids | `[]` |
| `islanding_control` | bool | `False` |

- These are only for local entries, linked or not.
- Device ids live in local entry storage and never appear in logs or events.
- Saving the step reloads the entry, as the other options steps do.

### 2.2 Options flow

- **Local, unlinked:** `init` today jumps straight to `cloud_add`. It becomes a menu of `cloud_add` and `controls`.
- **Linked:** the `linked` menu gains `controls`.
- **Cloud-only:** unchanged.

The `controls` step is a form:
- **`protected_circuits`:** a multi-select of every circuit with a relay, labelled with the circuit name and tab numbers from `transport.snapshot`.
- **`relay_override`:** shown only when at least one circuit is currently `feeds_storage_or_solar`. The options are those circuits only. The description warns that switching them can cut backup power or trip an inverter.
- **`islanding_control`:** shown only when the panel describes `shed/asserted-islanding-state` as settable **and** a BESS exists. The description says this is an outage-grade control.
- **Stale ids:** if a saved id names a circuit that is gone, it is dropped silently on the next save. At runtime it is simply ignored.
- **No snapshot yet** (the panel is unreachable): the step aborts with `controls_unavailable` and the message "Connect to the panel first".

### 2.3 Effects

**`protected_circuits`:**
- The relay switch is still created, but it is **unavailable**.
- Commands are refused with `PROTECTED`.
- The relay state stays visible as `relay_state` on the circuit power sensor, which is the same treatment as the 4b-1 storage rule.
- The priority select is **not** affected (Q2).

**`relay_override`:**
- A listed circuit gets its relay switch even when `feeds_storage_or_solar` holds.
- The row's `when` predicate and `relay_blocked` both consult the override.
- `protected_circuits` wins over `relay_override`.

**`islanding_control`:** see §3. While it is off, no entity is created.

### 2.4 Translated messages (strings.json and en.json, identical)

| Key | Message |
|---|---|
| `exceptions.control_protected` | "Switching this circuit is turned off in the integration's options" |
| `exceptions.control_storage_or_solar` | "This circuit feeds a battery or solar inverter; allow it in the integration's options to switch it" (replaces the hard-coded English in `_async_command`) |
| `exceptions.control_not_opted_in` | |
| `exceptions.control_gated` | |

There are also options-step strings and the `controls_unavailable` abort.

## 3. Islanding control (GFE override)

- **Platform:** a select on the panel device, `translation_key="asserted_islanding"` (Q1). The options are `none` and `on_grid`. **OFF_GRID is not offered**: asserting that the grid is down when it is not is the dangerous direction, and upstream only ever asserts that the grid is present.
- **Created when:**
  - `islanding_control` is on;
  - the property is described and settable;
  - a BESS device exists in the snapshot.
- **Available when:**
  - the BESS communication state != OK;
  - **and** the panel's grid/DSM state != ON_GRID. Use the same r202639 source upstream SpanPanel/span uses for its `gfe_override` availability; the coder confirms it from upstream's source and cites the file and line in the PR.
  - Otherwise the entity is unavailable, and a call made in a race is refused with `GATED`.
- **Shown value:** the published `asserted-islanding-state`, lower-cased. OFF_GRID shows as `None`, because it is not an option.
- **Confirmation:** the opt-in is the confirmation (Q3). The README also shows how to add a dashboard `confirmation:` to the card.
- **Untestable** on the user's panel, which has no BESS or MID. It is tested with synthetic snapshots only.

## 4. EVSE number and lock

These follow r202639-subdevices §6.1–6.2 unchanged:
- The bounds are the declared min (else 6 A) up to min(declared max, `config/max-charge-current`).
- The number is unavailable while the ceiling is unknown.
- A value above the ceiling raises `ServiceValidationError`, outcome INVALID.
- The payload is an integer string.
- The lock publishes LOCKED / UNLOCKED, with optimistic locking and unlocking during the confirm window.

Both emit command events. `PLATFORMS` gains `NUMBER` and `LOCK`. Neither can be tested live: synthetic fixtures only.

## 5. Tests (pure)

- **`commands.py`:**
  - the `event_data` key set, with no id-shaped values: a test feeds a record whose previous and requested values contain a device id and serial pattern, and asserts they pass through only as given;
  - outcome selection, through a pure `final_outcome(pending, presence, shown_value, now)` helper extracted from `_confirm`.
- **Safety:** a pure `SafetyRules.from_options(options, snapshot).check(device_id, key) -> Refusal | None` covering:
  - a protected circuit;
  - a storage circuit with and without the override;
  - protected beating the override;
  - islanding not opted in, and gated;
  - a stale id;
  - keys that are never guarded (priority, EVSE).
- **Model:**
  - `relay_blocked` and the row `when` predicate with an override set;
  - the islanding row's created and available predicates (BESS present or absent, comm OK or not, DSM on grid or not).
- **EVSE:** bounds, the unknown ceiling, a value above the ceiling, and the integer payload.
- **Options flow helpers:** the controls schema builder, with each field shown or hidden by the snapshot, and labels carrying no device id.
- **Strings:** `cmp strings.json translations/en.json`, plus every new translation key existing.
- **Existing AST test:** no `@callback` on `@property`.

The usual checks apply:
- pytest;
- ruff check and ruff format --check;
- the strings cmp;
- no specs, mempalace or entities.json in the diff.

The reviewer imports the changed modules in ha-span-dev.

## 6. Live testing on ha-span-dev (production: every write needs the user's go-ahead)

1. **Writes nothing:**
   - mark one circuit protected;
   - confirm its switch goes unavailable;
   - call `switch.turn_off` from developer tools;
   - expect a refusal, an event with outcome `refused`, a logbook line, and **no publish**: the broker log shows no `/set`.
2. **Needs explicit approval of one benign circuit:**
   - toggle that circuit off and back on;
   - expect two `confirmed` events and two logbook lines.
3. **Priority select:** a write, so ask first. It is optional.
4. **Not testable live:** islanding, EVSE, and the storage override (no circuit is linked on this panel).

## 7. Privacy

- Events, logbook text, logs and exceptions carry entity ids, control names, values and outcomes only.
- Option labels use circuit names, which are user labels, so they stay in HA's own storage and UI only.
- No fixture adds real names.

## 8. Release notes (0.3.0b1)

- New: a `span_ebus_command` event and logbook entries for every control command, with its outcome (confirmed, not confirmed, refused and so on).
- New: options to protect circuits from switching, and to allow switching on battery or solar circuits.
- New, untested live: the islanding control (opt-in, battery systems only), and the EV charger current limit and lock.

## Open questions for the user

1. **Islanding control shape.** Should it be a select (`none` / `on_grid`) or upstream's single "GFE override" button (asserts ON_GRID only)?
   *Recommendation:* the select. It can also clear the assertion (NONE), which the button cannot. OFF_GRID is never offered.
2. **Protected circuits and the priority select.** Should protecting a circuit also lock its shed priority?
   *Recommendation:* no. Priority changes don't cut power, and they are how you set up backup.
3. **What "confirmation" means.** HA can't show a confirm dialog from an integration. The choices are:
   - (a) opt-in options, as in this spec, plus README dashboard `confirmation:` snippets;
   - (b) an "Arm controls" button that must be pressed within 60 s before a guarded command runs.

   *Recommendation:* (a). (b) adds a platform and friction for little gain, since automations would have to press the button too.
4. **Command record.** Is the event plus logbook enough, or do you also want an in-memory list of the last 50 commands in diagnostics?
   *Recommendation:* the event plus logbook now. Add the list with the diagnostics work, which doesn't exist yet.
5. **`rotate_credentials`.** local-transport.md plans it as its own `feature/rotate-credentials` PR: an admin-only service that returns the new passphrase in its response.
   *Recommendation:* split it out of PR 6. It touches the broker credentials and passphrase handling, which is a different risk from controls.
6. **Cloud-only relay switch.** Do protected circuits and events also apply to legacy cloud-only entries?
   *Recommendation:* no. Those entries have no local snapshot to build the options from, and they are the legacy path.

## Decisions (user, 2026-11-14)

All as recommended:
1. Islanding is a select (`none` / `on_grid`); OFF_GRID is never offered.
2. Protecting a circuit doesn't lock its priority.
3. Confirmation means the opt-in options plus README snippets for dashboard `confirmation:`. There is no arm button.
4. The record is the event plus the logbook; the list of recent commands waits for diagnostics.
5. `rotate_credentials` gets its own PR.
6. Cloud-only relay switches are unchanged.
