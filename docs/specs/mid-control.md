# Spec — main-relay / MID control (grid islanding) over the SPAN local + cloud APIs

**Status:** researched, **not implemented, never exercised.** No write to any MID
path has been sent to a panel. Per the standing constraint we do **not** test
against a live panel — least of all one without a battery, where the behavior of
these endpoints is unknown. This document records what the two API surfaces
expose so the control can be built confidently once a BESS is installed.

**Scope:** open / close the panel's main relay (island the home from the grid,
or reconnect it) — the Microgrid Interconnection Device — and report its state.
This is distinct from per-circuit relay control (see
[circuit-control.md](circuit-control.md)); the MID is the single upstream relay
that separates the whole home from the utility.

Sources: the official SPAN local-API docs repo
(`github.com/spanio/SPAN-API-Client-Docs`, release **r202633**, MAIN 32 public
beta) — `openapi-r202633.json`, `homie-r202633.json`, the MQTT reference docs —
plus the recovered cloud protobuf schema
([../reference/span-cloud-schema.txt](../reference/span-cloud-schema.txt)) and the
SPAN Home app bundle (Hermes; method in [../CLOUD-PROTO.md](../CLOUD-PROTO.md)).

## 1. Terminology

- **MID — Microgrid Interconnection Device.** The main relay. Open = islanded
  (home runs off battery/PV, disconnected from the utility); closed = grid-tied.
- **Islanding state** — which side of that relay the home is on: `ON_GRID` /
  `OFF_GRID` / `UNKNOWN`. A *position report*, not a command.
- **Relay state** — the relay contact itself: `OPEN` / `CLOSED` / `UNKNOWN`.

## 2. Bottom line — where a real trigger exists

| Control | Cloud API | Local REST (`/api/v1`) | Local MQTT / eBus (Homie) |
|---|---|---|---|
| **Main relay open/close** | ✅ command exists | ✅ `POST /api/v1/panel/grid` | ❌ read-only |
| Emergency reconnect | (via grid-state) | ✅ `POST /api/v1/panel/emergency-reconnect` | — |
| Circuit relays | ✅ trait 1/31 | ✅ `POST /api/v1/circuits/{id}` | ✅ `switch/relay/set` |
| Islanding hint on comms loss | — | — | ✅ `shed/asserted-islanding-state/set` |

The main relay **is** triggerable, and — importantly — **the local REST API can
do it without any cloud dependency.** The newer eBus/MQTT data model does *not*
expose it (islanding-state is published read-only); only the legacy `/api/v1`
REST surface and the cloud carry an actual write.

## 3. Local REST surface (the recommended path)

From `openapi-r202633.json`. This is the pre-existing SPAN local REST API
(the `/api/v1/panel` + `/api/v1/circuits/{id}` surface older community
integrations drive), carried forward alongside the new eBus model in r202633.

```
GET  /api/v1/panel/grid                 → RelayStateOut { relayState: string }   "Get Main Relay State"
POST /api/v1/panel/grid                   RelayStateIn  { relayState: RelayState } "Set Main Relay State"
POST /api/v1/panel/emergency-reconnect  → 200 (no body)                           "Run Panel Emergency Reconnect"
GET  /api/v1/islanding-state            → IslandingState { islanding_state: string }  (read-only)

RelayState = "UNKNOWN" | "OPEN" | "CLOSED"
```

- `POST /api/v1/panel/grid { "relayState": "OPEN" }` → island the home.
- `POST /api/v1/panel/grid { "relayState": "CLOSED" }` → reconnect to grid.
- `POST /api/v1/panel/emergency-reconnect` → explicit "close back to grid" action.
- Invalid input returns `422 HTTPValidationError`.

`PanelState` corroborates that this is the real relay: it carries
`mainRelayState: RelayState` next to `dsmGridState` / `dsmState` /
`currentRunConfig` and the per-branch relay states.

**Open question (needs the battery, not a test):** whether `POST
/api/v1/panel/grid` is *accepted* on a panel with no BESS/MID installed, or
rejected — the cloud equivalent is gated on MID hardware. The endpoint is
present regardless; the battery-less response is unverified and we will not
provoke it on the live panel.

## 4. Local eBus / MQTT model — read-only

From `homie-r202633.json` and the MQTT reference. The MID appears in the device
tree as a **grandchild** of the panel: panel → BESS (`energy.ebus.device.bess`,
proxied child) → MID (`<bess-id>-mid`, child of the BESS).

- `grid/islanding-state` — **READ-only** `ON_GRID` / `OFF_GRID` / `UNKNOWN`
  (relay position). Not settable.
- `grid/grid-state` — always `UNKNOWN` on SPAN (no upstream utility sensor).
- `grid/grid-forming-entity` — `"GRID"` or the DER device id currently forming.
- The **only** writable islanding knob on MQTT is the panel's
  `shed/asserted-islanding-state` (`ebus/5/<panel-serial>/shed/asserted-islanding-state/set`,
  values `ON_GRID` / `OFF_GRID`). Docs: "accepted only during MID/BESS
  **communication loss**." This is a load-shed fallback hint, **not** a relay
  command — do not model it as MID control.

So on the eBus side there is no main-relay write. Anyone reasoning only from the
MQTT model would wrongly conclude the MID can't be triggered locally; the REST
surface (§3) is where the write lives.

## 5. Cloud surface (for parity / fallback only)

Recovered statically; **never captured live** and never exercised. Enum values
are only partially recovered. All go out on the one `SendMessages` method like
every other trait command (see circuit-control.md §1–2), signed by the caller's
user id.

- `io.span.traits.panel.PanelTrait.SetGridStateRequest { 1: grid_state }`
  — PanelCommandRequests #16 `set_grid_state_request`.
- `io.span.traits.panel.PanelTrait.SetMIDModeRequest { 1: mid_mode }`
  — PanelCommandRequests #17. `mid_mode` ∈ `MID_MODE_UNSPECIFIED` /
  `MID_MODE_GRID_DETECT` / `MID_MODE_NO_GRID_DETECT` (from the app bundle).
- `io.span.traits.site.SiteTrait.OverrideGridIslandingModeRequest
  { 1: grid_islanding_mode }` — SiteCommandRequests #8.
- `io.span.services.mobilefrontend.SetGridAndIslandingStateRequest
  { 1: resource_id, 2: grid_islanding_mode, 3: trigger_soe_based_loadshed }`.
- Supporting: `io.span.traits.der.DERBackupTrait { 1: der_backup_state,
  2: is_span_mid_used }`; `MicrogridInterconnectVoltageSample` (line/load/
  disconnect voltage telemetry).

We do **not** currently subscribe to the panel/site/backup command traits
(`SUBSCRIBE_TRAITS` in `span_client/backend.py` carries the 1/31 switch, not
these), so exposing the cloud path would require adding those.

## 6. Panel ↔ battery communications (context)

- **Transport:** the panel hosts a subnet on `eth1` — `10.42.1.0/24`, panel at
  `10.42.1.1`. Energy devices (e.g. a Powerwall) connect directly there; the
  panel NATs their traffic out to the vendor cloud. The panel is the hub.
- **Model:** the BESS is a **proxied child** device of the panel; the panel
  republishes battery telemetry under the eBus tree rather than the battery
  speaking Homie itself.
- **BESS-published properties:** `info` (vendor-name / model / serial /
  firmware), `meter/active-power`, `status/communication-state`, `soc`, `pcs`
  (power-control-system), `shed-forecast` (battery-time-remaining / load-shed
  forecast).
- **SoE / thresholds over REST:** `GET/POST /api/v1/storage/soe`
  (`BatteryStorage { soe: StateOfEnergy }`) and `GET/POST
  /api/v1/storage/nice-to-have-thresh` (low/high SoE bands).
- **Islanding coordination:** under normal operation the BESS/MID island
  autonomously; the panel's `shed/asserted-islanding-state` write is honored
  only when the panel has lost communication with the MID/BESS (§4).

## 7. Compatible battery systems

- Officially referenced: **Tesla (Powerwall), Enphase, FranklinWH, Lucid.**
- `vendor-name` in the BESS `info` node is a **free-form string**, not an enum —
  the data model doesn't hard-restrict the vendor field.
- r202633 proxies **at most one** BESS (also ≤1 PV, ≤1 EVSE).
- The cloud schema carried a `ModbusDiscoveryState` and a generic Modbus path,
  hinting SPAN onboards batteries over Modbus behind the scenes — i.e. the
  compatibility list is likely a *validated/supported* set rather than a hard
  protocol gate. **Inference only**; no unlisted system has been observed
  working and we have no way to test one without hardware.

## 8. If/when we implement

1. Prefer the **local REST** path (§3) — no cloud dependency, matches our
   local-first posture. Gate the entity on the panel actually reporting a MID /
   BESS present, and confirm the battery-less accept/reject behavior first.
2. Treat this as an outage-grade control: an accidental `OPEN` disconnects the
   home from the grid. Model it deliberately (confirmation / not a casual
   toggle), the way the app reserves `OVERRIDE_DISCONNECT_SWITCH_REQUEST` for
   its outage sheet.
3. Cloud path (§5) only as fallback, and only after adding the panel/site
   command traits to `SUBSCRIBE_TRAITS` and capturing a real exchange — its
   enums are not fully recovered.
4. Read-back of state: `GET /api/v1/panel/grid` (relay) and
   `GET /api/v1/islanding-state`, or the MQTT `grid/islanding-state` property.
