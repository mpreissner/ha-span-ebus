# Spec: local-first transport and parity with SpanPanel/span 2.2.x

**Status:** draft, 2026-11-12. Not implemented. Local only; do not commit or link it.
**Panel facts this rests on:** a live probe of a MAIN 40 on `spanos3/r202639/04` (hardwareVersion 3.0,
`proximityProven=false`, v1 REST returns 404), the official API changelog for r202639, the panel's own
`/api/openapi.json` and `/api/v2/homie/schema`, and SpanPanel/span at manifest 2.2.1.
**Scope:** move `span_ebus` to SPAN's official local API (v2 REST and eBus MQTT/Homie 5). Keep the cloud
path (Cognito, gRPC and Ably) as a fallback that the user can turn off. Reach feature parity with the
official integration, delivered in tiers.

## 1. Goals and non-goals

**Goals**

1. **Local first.** A panel on r202639 or later is read and controlled over the LAN, with no SPAN account
   needed.
2. **Cloud fallback.** If local is unreachable, an entry that also has a SPAN login carries on over the cloud,
   then hands back to local when local returns.
3. **Local-only is a choice.** The user can leave the cloud out entirely, both at setup and later in options.
   This follows the memory rule *local-only must be a user choice*.
4. **Nothing existing breaks.** A cloud-only entry upgrades in place and keeps its entity ids and history.
   That includes multi-site entries from PR #39 and the topology spec.
5. **Parity.** Match the official integration's entities, controls and the services that matter (section 9),
   without copying its code. Its licence is not ours to relicense. We reimplement from the API docs and the
   panel's schema.

**Non-goals**

- No daemon, container or environment-variable config. The integration is HA-only (memory).
- No custom sidebar dashboard or Adopted-tab UI in this effort; that is Tier 3 and deferred (section 9).
- No dependency on `span-panel-api`. The official integration pins `span-panel-api==3.8.0` and two schema
  packages with `==`. Installing both integrations in one HA would make pip resolve one pin against the
  other, and whichever lost would fail to load. Our client is small and we already own most of it.

## 2. Architecture

```
                 ┌──────────────────────── SpanCoordinator (per panel) ────────────────────────┐
config entry ──► │ TransportManager ── active: LocalTransport | CloudTransport                   │
                 │        │                                                                      │
                 │        ▼  normalized PanelSchema + Reading stream (r202639 vocabulary)        │
                 │ IdentityMap (transport node ids -> canonical entity keys)                     │
                 │        ▼                                                                      │
                 │ Model (devices, circuits, subdevices) -> snapshot every N s -> entities       │
                 └──────────────────────────────────────────────────────────────────────────────┘
```

- **One canonical vocabulary**, which is the local r202639 Homie model: node types and property ids as the
  panel publishes them. `LocalTransport` passes it through unchanged. `CloudTransport` translates into it:
  the existing `schema_from_frame` is extended so that, for example, `power` becomes `meter/active-power`
  and the relay becomes `switch/relay`. Entities never know which transport produced a value.
- **Model layer.** It replaces the flat `PropertySpec`-per-entity approach for anything with semantics:
  sign conventions, derived `sheddable`, solar role, remote CT, shared meters, and subdevices. Unknown
  properties still get the generic per-property treatment, which later becomes "adoption".
- **Snapshot debounce.** MQTT writes go into a dict, and the model rebuilds and dispatches at most every N
  seconds (option 0–15 s, as the official integration does). This is also our main lever on the recorder
  growth problem in the *recorder DB growth* memory. **The default is 1 s, matching the official integration
  (decided 2026-11-12).** Recorder growth is a separate decision that is still open; the options,
  such as rounding values before they are published, are in that memory.

### 2.1 Local client (`span_client/local/`, rewritten)

| Module | Job |
|---|---|
| `rest.py` | Async v2 REST on HA's aiohttp session. Calls: `certificate/ca`, `auth/register`, `auth/clients[/{name}]`, `auth/passphrase` (rotate), `dns/fqdn`, `status`, `homie/schema`. It maps errors to typed exceptions: 429 becomes `RateLimited(retry_after)`, 422 "dashboard password not available" becomes `PassphraseUnavailable`, 503 "serial not available" becomes `PanelNotReady`, 401/403 becomes `AuthFailed`, and a 504 on rotate becomes `OutcomeUnknown`. |
| `tls.py` | Builds an SSL context from the pinned CA PEM held in memory, with no file paths. It verifies the chain against the pinned CA only, with no hostname check: the panel certificate names only `span-<serial>.local`, and the user may enter an IP. Strict X.509 mode is cleared (the CA has no AKI) and partial chains are allowed. It also computes the SHA-256 fingerprint for diagnostics and logs. |
| `mqtt.py` | An async MQTTS client on `paho-mqtt` 2.x, which HA core already ships, driven by HA's event loop through `loop_read`/`loop_write`/`loop_misc` socket callbacks. It subscribes to `ebus/5/<serial>/#`, and also to `ebus/5/+/$description` and `ebus/5/+/$state` for child devices. Reconnect backoff is 1→60 s. Publishing to `<node>/<prop>/set` returns an outcome (section 8.3). |
| `homie.py` | Parses `$description` against the published schema and resolves the device tree from `$description` children. It caches the schema keyed on `deviceClassesSchemaHash`, in HA `Store`. |
| `discovery.py` | Matches zeroconf on `_ebus._tcp`, `_secure-mqtt._tcp`, `_span._tcp` and `_https._tcp` with TXT `v2_path=/api/v2`. |

All of the following go: `~/.span-auth.json`, `~/.span-ca-certs/`, the sync `requests` calls, and
proximity-first registration. Credentials live in the config entry (section 4).

**Library decision:** use `paho-mqtt` rather than `aiomqtt`. paho is already in HA's dependency set, so it
adds no new requirement. The cost is one adapter of about 120 lines. If that adapter proves fragile in
review, fall back to `aiomqtt`.

### 2.2 TransportManager

There are three modes per panel, set in options:

| Mode | When offered | Behaviour |
|---|---|---|
| `local_only` | The panel is registered locally | Cloud is never contacted. Losing local makes entities unavailable. |
| `local_first` (default when both are configured) | Local registered and a SPAN login present | Run local. Start cloud only when failing over. |
| `cloud_only` | No local registration (old firmware, or the user hasn't added it yet) | Today's behaviour. |

**Failover.** Local counts as down when any of these is true:
- the MQTT socket has been disconnected for 60 s or more;
- the panel's `$state` has been `lost` or `disconnected` for 60 s or more;
- no message has arrived for 120 s while `$state` reads `ready`. Retained values are not republished when
  unchanged, but power readings move all the time, so silence here means a stall.

When local is down, the manager starts the cloud stream and logs one warning. Local reconnect attempts carry
on in the background.

**Failback.** After local has been `ready` continuously for 5 minutes, switch back and stop the cloud stream.
The hysteresis is there to avoid flapping.

**No hot standby.** Cloud is never run alongside a healthy local connection. It costs SPAN quota and HA CPU,
and the two would race on control.

**State shown:** a diagnostic sensor `Transport` (`local`, `cloud`, `offline`) and a repair issue while on
fallback for more than 1 hour.

## 3. Values that depend on the transport

| Value | Local | Cloud fallback | Rule |
|---|---|---|---|
| Power, current, voltage, frequency | yes | yes (subset) | Values cloud lacks go unknown during fallback; they never read 0. |
| Energy, the panel's own `imported-energy`/`exported-energy` registers | yes | **no** | Energy sensors hold their last value and stay available during fallback. On failback the registers resume, so `total_increasing` sees the real delta. The cloud history aggregation is not mixed in, because two sources would step the statistics. |
| Relay, priority, EVSE, lock, GFE override | `/set` | relay only (circuit-control.md) | Controls the cloud cannot carry go unavailable during fallback. |
| BESS SoC, MID state, PCS, shed forecast, doors, network | yes | partial (traits in vic's CLOUD-TRAITS notes) | Same as the first row. |

Existing cloud-only entries keep the cloud-derived energy sensors they already have (`*_energy` unique ids).
Once local is linked (section 5), those sensors are re-pointed to the local registers. **Expect one step in
long-term statistics** at that point: the counters' bases differ. The changelog must state this, and the
**Decided 2026-11-12: keep and re-point the existing energy sensors**, and document the one-time step in
the changelog.

## 4. Config flow

```
user / zeroconf
  ├─ zeroconf: host from the announcement; abort if a panel with this local serial is already configured
  │            (update host if it moved and the pinned CA validates the new address)
  ├─ user: "How do you want to connect?"  [Local panel (recommended)] [SPAN account (cloud only)]
  │
  local:
  ├─ host step: IP / .local / FQDN  ─► GET /api/v2/status (serial, firmware, proximityProven)
  │       firmware < r202639 or v2 missing ─► explain, offer cloud
  ├─ fetch CA over :80 (plaintext, per the API), pin it, show its fingerprint
  ├─ auth step: passphrase (MAIN 40 and others; proximity offered only where the panel supports it)
  │       POST /api/v2/auth/register {name: "ha-span-ebus-<entry-short-id>", hopPassphrase}
  │       429 ─► "try again in N s" form error; 422/503 ─► specific messages
  ├─ connect test: MQTTS login, wait for $state=ready and $description (≤15 s)
  ├─ cloud step: "Also sign in to your SPAN account as a fallback?" [Yes] [No, stay local-only]
  │       yes ─► existing email/password step ─► link step (section 5)
  └─ options: snapshot interval, naming pattern
```

- **Entry data:** `host`, `local_serial`, `ca_pem`, `client_name`, `access_token`, `broker_username`,
  `broker_password`, plus the existing cloud keys when cloud is set up. `hopPassphrase` and
  `ebusBrokerPassword` may come back null; handle that and never store the passphrase.
- **unique_id:** a new local entry uses the local serial. An upgraded cloud entry keeps its existing
  `user_id` unique_id; the local serial is recorded per panel instead (section 5).
- **Multi-panel:** one entry per **SPAN account** stays as it is for cloud. Local registration is done
  **for all panels at once** (decided 2026-11-12). An account entry gets a "Link local panels" options
  step. It browses `_ebus._tcp` for about 5 s, adds any hosts the user enters by hand, reads
  `/api/v2/status` from each one, and proposes a local-to-cloud pairing for every panel (section 5). It
  asks for each panel's passphrase on one form, registers each panel, then links them all in one migration.
  A panel can be skipped, and so can a panel that fails, for example with a 429; the step can be rerun
  later to add it. A local-only user gets one entry per panel. Sharing one account across several
  entries is out of scope; the *multi-panel upgrade path* memory says remove and re-add.
- **Reauth** happens when the broker or REST rejects credentials, and asks for the passphrase again.
  **Reconfigure** changes the host and accepts the new host only if the pinned CA validates it.
- **Logging:** the passphrase, token, broker password and serials are never logged above debug, and the
  debug output is redacted too (follows PR #45).

## 5. Identity: linking local to an existing cloud entry

The cloud serial and the local serial are **different namespaces for the same panel** (`CLOUD-FLOW.md`,
"Two serials, one panel"). Cloud circuit nodes are `circuit-<trait instance id>`, while local circuit ids
are opaque strings. So nothing lines up by name.

**Link record**, stored in entry options per panel:

```
links[<cloud panel key>] = {
    local_serial,
    circuits: {local circuit id: cloud instance id},
    linked_at,
}
```

**Matching circuits.** Each local circuit has `info/spaces`; each cloud `CircuitInfo` has `spaces`. Match on
the identical space set, and they must agree on pole count. Circuits that match nothing are new: they get
local-keyed entities and are listed in the link summary. A remote CT (no spaces) never matches a cloud
circuit.

**Choosing the panel.** If the account has one cloud panel, it is the link target. With several, the link
step offers a select showing each panel's model and circuit count. It recommends the panel whose space map
best overlaps the local one, but never decides silently.

**Canonical unique ids.** These are the ids used for new entities, and for old ones once migrated:
- Panel-level: `<panel key>_<node type>/<property>`, for example `..._distribution-enclosure/meter/frequency`.
- Circuit-level: `<panel key>_circuit/<local circuit id>/<property>`.
- `<panel key>` is the **existing cloud key** for a linked panel and the local serial for a local-only one.
  That way an existing entry's device identifier never changes.

**Migration.** When a link is saved, `async_migrate_entries` rewrites each matched legacy unique id to its
canonical id. Examples:
- `<key>_circuit-<n>/power` becomes `<key>_circuit/<id>/meter/active-power`.
- The relay maps to `.../switch/relay`.

HA keeps the `entity_id`, the history and any user customisation across a unique-id change. Legacy entities
with no local equivalent stay as they are: for example `panel/overhead` and the site-level flows that local
`power-flows` replaces. They are restored as unavailable, and a repair lists them for the user to delete.
They are never deleted automatically.

**Cloud fallback after the link.** `CloudTransport` uses the same map in reverse (instance id to local
circuit id), so fallback readings land on the canonical entities.

## 6. Handling r202639 schema changes (model layer)

| Change | Handling |
|---|---|
| `always-on` replaced by `switch/relay-controllable` (inverted) | A circuit switch exists only when relay-controllable. `always_on` is kept as an attribute, set to `not relay-controllable`. |
| `never-backup` now expressed as `$settable=false` on `load-shed/priority` | The select is created only if settable. Otherwise the priority appears as an attribute only. |
| `sheddable` is derived | `relay-controllable AND priority != NEVER`. While priority is `UNKNOWN`, sheddable is None. |
| `feed` and `relative-position` replaced by `connection/*` | Solar circuits are those with `feeds-role == SOLAR`; their power and net energy are inverted. EVSE and BESS feed circuits are attached to their subdevice. |
| BESS `meter/active-power` is +discharging, and `power-flows/battery` is its negative | Both are surfaced with the sign documented. The README states "+discharging". |
| `grid-islandable` and `dominant-power-source` removed | Grid Forming Entity comes from `mid/grid/grid-forming-entity`, and is `GRID` when there is no MID. Grid Islandable is "a MID exists". The GFE override uses `shed/asserted-islanding-state`. |
| BESS `connected` replaced by `status/communication-state` | BESS Connected is `communication-state == OK`. |
| A remote CT is a circuit with no spaces and only a meter | Device "Remote CT n". Positive means import; it is not negated. It gets no switch or select. |
| No `breaker` capability when there is no main rating | Main Breaker Rating is not created. Current monitoring falls back to `lugs/connection/overcurrent-protection`. |
| `power-flows` shows only grid and site when there is no BESS or PV | Battery and PV entities are created only once published. |
| Retained values not republished when unchanged | The schema cache plus the last values kept in `Store`. Values are restored on startup, and never assumed to be 0. |
| Circuit `meter/shared-with-device-ids`, `switch/shared-with-device-ids` | Attributes, plus a group for current monitoring (Tier 3). |
| Downstream lugs figures unreliable (eBus maintainer) | Feedthrough entities are created **disabled by default**. Our own `panel/overhead` stays cloud-only. |

## 7. Subdevices

These are HA devices linked to the panel with `via_device`:
- **BESS**: SoC, SoE, nameplate, meter power, communication state, and identity.
- **MID**: grid state, islanding state, GFE.
- **EVSE**: status (enum), advertised current, lock (lock platform), charge-current limit (number, capped at
  `config/max-charge-current`), and binary sensors for charging, EV connected and panel link.
- **Solar**: PV power. Per-inverter devices are created from `pv` nodes and solar-role circuits.

Device identity is keyed on the subdevice's own serial where it publishes one, otherwise on its node id.

## 8. Control path

### 8.1 Settable properties

| Property | Entity |
|---|---|
| `circuit/switch/relay` | switch |
| `circuit/load-shed/priority` | select |
| `distribution-enclosure/shed/asserted-islanding-state` | button "GFE Override: Grid Connected" |
| `evse/switch/lock-state` | lock |
| `evse/config/user-max-charge-current` | number |

The GFE override button is available only while the panel is not on-grid and BESS communication is not OK.

### 8.2 Safety options

These are Tier 2 and live in options:
- **Who may operate the panel:** admins, everyone, or nobody. The default for new installs is admins. An
  upgraded install keeps everyone, which matches today's behaviour.
- **Allow control with no logged-in user**, meaning automations. On for upgraded installs, off for new ones.
- **Per-circuit relay debounce.** Default 2 s; 0 turns it off.

All of these are enforced in the transport's `send_command`, not in the entities. That keeps service calls
and websocket paths covered too.

### 8.3 Command outcome and record

| Outcome | Meaning |
|---|---|
| `confirmed` | The panel reported the new value within 10 s. |
| `accepted` | The broker acknowledged the publish but no state came back. |
| `unconfirmed` | Nothing came back within the deadline. |
| `failed` | The command was never handed to the broker. |
| `refused:<reason>` | Our own safety options refused it. |

Each command fires a `span_ebus_control_command` event and a logbook entry that names the user, or the
automation when there is no user.

## 9. Parity matrix (tiers)

**Tier 0: transport foundation.** This is required before anything ships.
- Async REST and MQTT client.
- Zeroconf discovery and the manual host step.
- Passphrase registration, with proximity where offered.
- CA pinning plus reauth and reconfigure.
- Credentials kept in the entry.
- Schema-hash cache.
- `$state` driving availability.
- The TransportManager with all three modes.
- Linking and migration (section 5).
- Diagnostics with redaction, and the CA fingerprint.

**Tier 1: entity and control parity.**
- **Panel:** current power, the main meter's produced/consumed/net energy, feedthrough figures (disabled by
  default), run config, GFE, main relay state, vendor cloud, software version, frequency, door, ethernet,
  wifi (SSID as an attribute), and panel status.
- **Diagnostics:** L1/L2 voltage, upstream and downstream currents, main breaker rating, busbar current, and
  upstream protection rating.
- **Power flows:** grid, site, battery, PV, and the `at_service_entrance` attribute.
- **Shed forecast:** time to priority shed and backup time remaining, plus their attributes.
- **PCS:** import limit, binding constraint, the attribute set, and PCS Active.
- **Circuits:**
  - Sensors: power, the produced/consumed/net energy group, current, and breaker rating.
  - Attributes: tabs, voltage, always_on, relay_state, relay_requester, shed_priority, is_sheddable,
    pcs_managed, pcs_priority, protection_functions, and shared-with.
  - Controls: switch and priority select.
- **Remote CTs.**
- **Subdevices:** BESS, MID, EVSE and Solar, as in section 7.
- **GFE override button.**
- **Options:** snapshot interval and energy dip compensation. Dip compensation defaults to on for new
  installs and off for upgrades.
- **Naming pattern:** friendly or circuit-number. Upgrades keep their current ids.

**Tier 2: security and operations.**
- The safety options and command record (section 8).
- A `rotate_credentials` service: admin only, returns the new passphrase in the response, and retries the
  broker for about 60 s.
- Repairs for a CA change, a schema hash that moves to something unparseable, and a long time on fallback.
- FQDN registration through `/api/v2/dns/fqdn` in reconfigure.
- Diagnostics including the masked device tree.

**Tier 3: extras.** Deferred, and each needs its own go/no-go.
- Current monitoring with threshold alerts, plus their services: set/clear circuit and mains thresholds,
  get_monitoring_status, set_global_monitoring, and test_notification.
- `export_circuit_manifest`.
- A websocket `panel_topology` API, which also lets span-card work against us.
- Adopting unknown devices and properties: generic entities, disabled by default. We partly have this
  already.
- The custom sidebar dashboard, graph horizons and favorites. **Recommend never.** span-card and HA's own
  dashboards cover these, and it is the largest maintenance surface in the official repository.

**Ours beyond parity** (kept): cloud fallback, multi-site account discovery and the panel picker, the
feed-topology question, panel overhead, cloud history backfill, and vic's trait work (backup priority and
PowerUp traits) as cloud-only extras.

## 10. Delivery plan (PRs to `dev`)

1. `feature/local-client`: the async REST, TLS, paho adapter, Homie parser and schema cache, with unit tests
   built from the probe fixtures (`status`, `schema`, `openapi`). **Redact the serial and host before
   committing the fixtures.** The panel is not touched yet.
2. `feature/local-config-flow`: zeroconf, host and auth steps, entry data, reauth and reconfigure. **Live
   test:** register against the user's panel. The user types the passphrase into the HA flow (or a local
   prompt), and it never passes through this chat or the logs.
3. `feature/transport-manager`: the three modes, failover and failback, and the Transport diagnostic.
4. `feature/r202639-model`: the model layer, the new platforms (binary_sensor, select, number, button,
   lock) and the subdevices. Tier 1 entities.
5. `feature/local-link-migration`: link step, circuit matching, unique-id migration and the legacy repair.
   Test on a copy of the user's `.storage` before running it live.
6. `feature/control-safety` and `feature/rotate-credentials`: Tier 2.
7. Version bump to `0.3.0b1` on `dev` (docs and version only), README rewrite, then release `dev` to `main`.

Each PR goes through the Reviewer pass before merge.

## 11. Open questions

1. **Does `distribution-enclosure/info/serial-number` equal the cloud serial?** `CLOUD-FLOW.md` says the
   mDNS serial differs. If the Homie value matches the cloud one, linking becomes automatic. To be checked
   on the first live MQTT session.
2. ~~Energy at link time~~: **decided**, keep and re-point (section 3).
3. ~~Default snapshot interval~~: **decided**, 1 s (section 2).
4. **Does the MAIN 40 on r202639 offer proximity at all?** The schema says `hopPassphrase` is "non-MAIN32
   only" and the probe shows `proximityProven=false`. We assume passphrase-only and offer proximity only if
   registration without a passphrase succeeds.
5. ~~Linking several panels~~: **decided**, all at once (section 4).
6. **Cloud mode for firmware older than r202639:** keep `cloud_only` indefinitely, or deprecate it once
   SPAN finishes the rollout. Recommend keeping it; it costs nothing extra.

## Add-panel discovery (requested 2026-11-12, for the transport-manager PR)

When adding a panel locally, discover first and ask for an address only as
a fallback:

- `async_step_local` lists `_ebus._tcp` panels already seen by HA zeroconf
  (in-progress discovery flows, or a short browse via the shared zeroconf
  instance), excluding panels already configured, and lets the user pick one.
- A "Enter the address myself" choice is always offered; when nothing is
  found the flow goes straight to the host form.
- Picking a discovered panel continues exactly as a manual host would
  (probe, pin CA, passphrase).
