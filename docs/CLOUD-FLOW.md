# SPAN Cloud — Auth & Live Data Flow

Recorded 2026-08-16 by decrypting the **live iOS SPAN app** through mitmproxy
(the iOS app is **not** certificate-pinned; a trusted CA is sufficient — no Frida
needed, unlike Android). These are empirical results captured from real traffic,
with all live tokens redacted.

This is the cloud data plane the HACS integration's **cloud backend** will target
until SPAN ships the local MAIN 40 API (~H2 2026), at which point the ebus/local
backend takes over.

## TL;DR — the whole path

```
cognito-idp-fips.us-west-2.amazonaws.com        → auth (Cognito SRP) → access token
app-api.prod.span-csp.com/graphql               → app config / 3rd-party keys only (NOT panel data)
mobilefrontend.prd.span.io                      → gRPC data plane (Bearer = Cognito access token)
    io.span.services.mobilefrontend.MobileFrontendService/
        ├─ GetSitesForUser            → site topology (siteId, panel resource, serial)
        ├─ AblyToken                  → signed Ably TokenRequest
        ├─ SearchResourcesForSerial   → (empty in capture)
        ├─ SubscribeAndGetTraits      → REQUIRED: registers the channel; + trait snapshot
        ├─ GetHistoryAggregation      → historical energy
        ├─ ListDispatches             → schedules / dispatches
        └─ SendMessages               → the ONLY write: trait commands, e.g. a breaker toggle
rest.ably.io/keys/v8kFxw.VMjbuw/requestToken    → exchange TokenRequest → real Ably token
rest.ably.io/comet/connect + /{conn}/recv       → LIVE realtime power stream (base64 protobuf)
```

> **`omni.prod.span-csp.com` is a red herring.** It appears only in static
> APK/DEX strings; the live app never contacts it. Earlier recon parked on the
> deprecated GraphQL `GetUserBuildings → buildings:null` path, which is why the
> Android emulator failed to load the home. The real data plane is gRPC at
> `mobilefrontend.prd.span.io`.

## 1. Auth — AWS Cognito SRP

| Field | Value |
|---|---|
| Host | `cognito-idp-fips.us-west-2.amazonaws.com` |
| User Pool | `us-west-2_xqz9y67ID` |
| App Client | `21vd907gimk5ctc0pop94l2lip` |
| Flow | SRP (`USER_SRP_AUTH` → `PASSWORD_VERIFIER`) |

Returns a Cognito **access token** (JWT). That token is the `Bearer` credential
for every `mobilefrontend.prd.span.io` gRPC call. No separate SPAN session token
is minted for the data plane.

**Which failures the user has to fix.** Cognito reports errors as JSON carrying a
`__type`, sometimes bare and sometimes qualified
(`com.amazon.coral.service#NotAuthorizedException`). Four of those mean the
credential itself is finished — `NotAuthorizedException`,
`UserNotFoundException`, `UserNotConfirmedException`,
`PasswordResetRequiredException` — and `cloud_auth` raises
`CloudCredentialsRejected` for them, as it does when the token store is missing
or holds no refresh token. Everything else (5xx, throttling, an unreachable host)
is a plain `CloudAuthError` and means *wait and retry*.

The split exists because the two need opposite handling. The integration turns
`CloudCredentialsRejected` into Home Assistant's reauth prompt — at setup by
raising `ConfigEntryAuthFailed`, and at runtime through the backend's
`on_auth_failed` callback into `entry.async_start_reauth`. A transient error must
*not* do that: a Cognito outage would otherwise ask every install for a password
that changes nothing. The backend latches the report so a retry loop hitting the
same rejection raises exactly one prompt, and it keeps retrying underneath, since
a revoked token and a revoked-then-restored one look identical until you try.

## 2. Data plane — gRPC over HTTP/2

- Host: `mobilefrontend.prd.span.io` (resolves to the same ALB IPs as app-api)
- Service: `io.span.services.mobilefrontend.MobileFrontendService`
- Transport: **gRPC over HTTP/2**, `content-type: application/grpc`, `te: trailers`
- Client UA: `grpc-swift-nio/1.8.0`
- Auth: `authorization: Bearer <Cognito access token>`
- Path: `POST /io.span.services.mobilefrontend.MobileFrontendService/{Method}`
- Some calls carry Datadog RUM headers (`x-datadog-*`, `baggage`) — cosmetic, not required.

### GetSitesForUser — topology

Request: **empty** gRPC frame (`00 00 00 00 00`). Response protobuf decodes to the
account's site tree:

| Field | Value (this account) |
|---|---|
| siteId | `<siteId>` |
| Site name | `<site name — redacted>` |
| Location | `<location — redacted>` |
| Panel resource UUID | `<panel-resource-uuid>` |
| Panel **cloud** serial | `<cloud-serial>` |
| Model code | `1-02100-04` ("SPAN Panel MAIN 40") |
| Other resource UUIDs | `<resource-uuid-1>`, `<resource-uuid-2>` |
| **Hardware ids** | `<site-hardware-id>`, `<gateway-hardware-id>` (16-hex, at `1 → 3 → 1[] → 2`) |

> The **hardware ids** are the ones that matter for subscribing (§3e) — 16-hex
> short ids, a different namespace from the resource UUIDs in the same response.
> `SubscribeAndGetTraits` is rejected if you hand it resource UUIDs instead.

> **Two serials, one panel.** The cloud serial `<cloud-serial>` differs from the
> **local** mDNS serial `<local-serial>` in `FINDINGS.md`. This is not a conflict —
> they are separate identifier namespaces for the same physical MAIN 40. The
> cloud backend keys off `siteId` + panel resource UUID, not the local serial.

### SearchResourcesForSerial

Captured with an **empty** request and empty 200 response — the app appears to
call it opportunistically; not needed to bootstrap. Topology comes from
`GetSitesForUser`.

## 3. Ably realtime — the live power stream

### 3a. AblyToken RPC → signed TokenRequest

Request protobuf: field 1 = client device UUID `<device-uuid>`.

> **The device UUID is client-chosen, not SPAN-issued** (live-validated
> 2026-08-17). This RPC echoes whatever UUID you send straight into
> `channel_name`, `clientId`, and the token `capability`; there is no device
> registry to look it up in and no RPC that returns one. A freshly generated UUID
> gets a working token for a working channel — SPAN just won't publish to it until
> §3e registers it. This is why the HACS integration generates its own at setup
> instead of asking you to extract the mobile app's.

Response: a JSON **Ably TokenRequest** (embedded in the gRPC frame):

```json
{
  "keyName": "v8kFxw.VMjbuw",
  "nonce": "...",
  "mac": "...",
  "capability": "{\"c:<userId>:<deviceUUID>\":[\"subscribe\"]}",
  "clientId": "c:<userId>:<deviceUUID>",
  "timestamp": 1786904077118
}
```

- Ably app key id: `v8kFxw`; key name: `v8kFxw.VMjbuw`
- `userId` = `<userId>`
- Channel/clientId pattern: **`c:<userId>:<deviceUUID>`**
- Capability granted: `subscribe` only

### 3b. Exchange TokenRequest → real token

```
POST rest.ably.io/keys/v8kFxw.VMjbuw/requestToken
```

- Headers: `x-ably-version: 2`, `ably-agent: ably-js/1.2.50 reactnative`,
  `user-agent: homeowner/941 CFNetwork/... Darwin/...`
- Body: the TokenRequest JSON from 3a verbatim.
- Response `201`:

```json
{
  "token": "v8kFxw.Mg...",         // redacted
  "keyName": "v8kFxw.VMjbuw",
  "issued": 1786904077118,
  "expires": 1786907677118,        // ~1 hour TTL
  "capability": "{\"c:<userId>:<deviceUUID>\":[\"subscribe\"]}",
  "clientId": "c:<userId>:<deviceUUID>"
}
```

Token TTL is ~1 hour → the client must re-run AblyToken + requestToken on expiry.
It does this by reconnecting rather than by watching the clock: every reattach in
`CloudBackend._run` re-enters `bootstrap()`, which mints a fresh token and a fresh
`SubscribeAndGetTraits`. `expires` is therefore recorded but not acted on — what
matters is that the stream reliably *ends* when it stops being useful, which is
what the two liveness guards in §3d exist to guarantee.

### 3c. Subscribe & receive — comet transport

The iOS app uses Ably's **comet** (HTTP long-poll) transport, not websockets:

```
rest.ably.io/comet/connect              → open connection, get {connId}
rest.ably.io/comet/{connId}/recv        → long-poll for messages
rest.ably.io/comet/{connId}/send        → outbound (subscribe/heartbeat)
```

Subscribed channel: `c:<userId>:<deviceUUID>`.

Telemetry arrives as Ably protocol messages (`action: 15`) carrying:

```json
{
  "name": "message",
  "encoding": "base64",
  "data": "<base64 protobuf>"
}
```

- ~1–2 frames/sec.
- `data` is base64 → **protobuf**. Frames are keyed by siteId
  (`<siteId>` observed inside decoded payloads).

### 3d. What the cloud backend actually uses — SSE, not comet (live-validated 2026-08-16)

The client does **not** replicate the app's comet long-poll. It subscribes with a
single long-lived Ably **SSE** GET, which is far simpler to consume:

```
GET https://realtime.ably.io/sse?channels=<channel>&access_token=<token>&enveloped=true&v=1.2
```

Two findings the live run pinned down, both easy to get wrong:

- **`enveloped=true` is mandatory.** With `enveloped=false` this app key's SSE
  endpoint delivered *no* telemetry (only `:keepalive` + an `id:` line, then
  idle). With `enveloped=true`, each SSE `message` event's `data` is a JSON Ably
  Message (`{action:0, name:"message", encoding:"base64", data:"<b64 protobuf>", …}`)
  and frames flow immediately at ~1–2/sec.
- **Attaching alone gets you nothing** — `SubscribeAndGetTraits` (§3e) is
  required. An earlier revision of this document claimed the stream was
  "occupancy-triggered", i.e. that merely attaching made SPAN start publishing.
  That was wrong: the original session had a live subscription already registered
  by the mobile app for the same channel, which made attaching *look* sufficient.
  A clean-room attach to a channel that has never been registered stays silent
  indefinitely. Corrected 2026-08-17.
- The stream interleaves lean **energy/interval** frames (empty of resources +
  site flows) between the **power** frames; the backend ignores the empties for
  schema purposes and only emits Readings for whatever a frame carries. The first
  few frames after a subscribe are often these empties, so allow ~30s before
  concluding a channel is dead.

Live decode confirmed real values: site grid ≈ 4.0 kW, 29 circuits with per-circuit
power (e.g. instance 54 ≈ 660 W), `site_id=<siteId>`.

**Liveness — the two ways this stream dies quietly** (added 2026-08-20, after a
stall that ran ~24h unnoticed). The reconnect loop only runs when `stream_frames`
returns or raises, and neither of these does so on its own:

1. *Dead socket, no FIN.* NAT eviction or an Ably node move leaves a half-open
   TCP that delivers nothing and never errors, so reading it blocks forever.
   Guard: `DEFAULT_STREAM_TIMEOUT` puts a 45s read budget on the connection. A
   healthy stream is never silent that long — the `:keepalive` comments alone
   reset the clock — so this only trips on a socket that is genuinely gone.
2. *Live socket, no publisher.* The connection is fine and keepalives keep
   arriving, but SPAN has stopped publishing (a lapsed registration, §3e). No
   timeout fires, because bytes are still flowing. Guard: `FRAME_SILENCE_SECONDS`
   — if no *decodable frame* arrives for 90s, the backend ends the stream itself
   and re-bootstraps. The stop-check is consulted per SSE *line* rather than per
   telemetry event, because on a quiet channel the keepalives are the only thing
   left to consult it on.

**Topology is re-resolved after a dead attach** (added 2026-08-21, after a stall
only a reload could clear). `bootstrap` caches the serial and hardware ids from
the first `GetSitesForUser` and never asks again, so every reattach re-registers
the *same* resource ids. Once those ids stop being the ones SPAN publishes for,
`SubscribeAndGetTraits` is still accepted, the channel still attaches, and it
stays silent — reattaching cannot fix it, and reloading the integration (a fresh
`CloudBackend` with empty caches) was the only thing that did. An attach that
delivered no telemetry now drops the cache so the next bootstrap re-reads it, and
a changed id set is logged at WARNING naming both sets.

**Reconnect policy** (added 2026-08-21, after 314 identical ERROR lines in 25h).
Ably resets a long-lived SSE socket periodically — `[Errno 104] Connection reset
by peer` off `stream_frames` — and the very next attach fixes it. So the retry is
graded by whether the attach that just ended ever *delivered a frame*:

- **Delivered telemetry** → routine drop. Retried after `reconnect_seconds` (5s)
  and logged at INFO, however it ended.
- **Delivered nothing** → counted. The wait doubles per consecutive dead attach
  up to `RECONNECT_BACKOFF_MAX_SECONDS` (300s), jittered ±20% so a cloud-wide
  outage doesn't bring every panel back in lockstep. Each attempt re-runs
  Cognito, `GetSitesForUser`, `AblyToken`, the token exchange and
  `SubscribeAndGetTraits`; at a fixed 5s that is twelve full re-auths a minute
  against a service already refusing us. One ERROR is logged on the
  `LOUD_AFTER_ATTEMPTS`th (3rd) consecutive dead attach and none after, so an
  outage reads as one loud line rather than several hundred.

Both liveness guards above end the stream by *returning* rather than raising, and
a watchdog trip is a dead attach like any other — a channel that attaches but
never publishes must back off too.

Downstream, `SpanCloudCoordinator.stream_is_live` gates entity availability:
after `STALE_AFTER_SECONDS` of silence the entities go unavailable instead of
advertising their last value. Without that, a stalled stream is indistinguishable
from a panel at constant load, and the recorder writes the flat line into history
as though it had been measured.

### 3e. SubscribeAndGetTraits — what actually starts the stream (live-validated 2026-08-17)

This is the registration call. It tells SPAN "publish these traits, for these
resources, to this channel", and returns a one-shot ~22 KB trait snapshot as a
bonus. Request shape, recovered from the iOS capture:

```
SubscribeAndGetTraitsRequest {
  1 subscriber_resource_id: ResourceId { 1 id = "c:<userId>:<deviceUUID>" }
  2 resources[]: ResourceIdWithTraitMetadata {
      1 resource_id: ResourceId { 1 id = "<hardware-id>" }   // 16-hex, from GetSitesForUser
      2 traits[]:    TraitMetadata { 1 vendor_id, 3 trait_id }
    }
}
```

Three things are easy to get wrong, and each fails differently:

- **The subscriber is the Ably channel name**, not a device or resource id. This
  is the whole trick — the channel we invented is what we register.
- **Resources are keyed by hardware id**, not resource UUID. The app subscribes
  both the site and the gateway hardware ids.
- **Traits must be enumerated.** The publisher sends only what a subscriber asked
  for, so the backend carries the app's exact 31 `(vendor_id, trait_id)` pairs
  verbatim (see `SUBSCRIBE_TRAITS` in `span_client/backend.py`). Vendor 1 is SPAN's
  trait namespace, 5 the Weave-derived common one.

Diagnostics, since the errors are opaque:

| Result | Means |
|---|---|
| `PERMISSION_DENIED (7)` | request parsed, but the subscriber/resource isn't yours |
| status `2` "Application error processing RPC" | malformed request shape |
| `NOT_FOUND (5)` | unknown resource id (e.g. a resource UUID where a hardware id belongs) |

Ordering: the app attaches its realtime stream **first**, then calls this. The
backend mirrors that (a ~2s delay after the SSE attach) rather than assume the
reverse works, and re-issues it on every reconnect. `MultiResourceSubscribe` and
`MultiSubscribe` also exist on the service but return `PERMISSION_DENIED` for
every subscriber shape tried — they are not this path.

### 3f. The trait snapshot — circuit identity (live-validated 2026-08-17)

The ~22 KB response to §3e is not a bonus to discard: it is the **only** source of
circuit identity. Telemetry frames name a circuit solely by `trait_instance_id`,
an internal number with no relation to a panel space — on a MAIN 40 the instance
ids observed were `1, 2, 30…56` for a panel with 40 spaces, which is why entities
were being created as `circuit-56` on a 40-space panel.

Snapshot layout, by wire field number:

```
1 resource[]
    1 { 1: hardware_id }
    2 trait_entry[]
        1 { 1: vendor_id, 3: trait_id }
        2 { 1: instance_id }
        3 { 1: metadata, 2: <trait value, packed> }
```

Its pointer type is a `TraitRef`:
`{ 1: { 1: vendor_id, 3: trait_id }, 2: { 1: instance_id } }`. Three of the 31
subscribed traits carry the identity:

| trait | value | holds |
|---|---|---|
| `1/15` circuit | keyed by the **telemetry** instance id; position block at #11 (two-wire) or #13 (three-wire) | TraitRefs to the label and space(s) |
| `1/16` circuit label | `{ 2: wire config, 3: breaker amps, 4: user label }` | the user's own name, e.g. "Branch A", and the breaker rating |
| `1/17` panel space | `{ 3: displayed space number }` | the number printed on the panel |

The refs are not at fixed offsets — depth varies by wire kind — so
`cloud_traits._trait_refs` walks the position subtree and filters by trait id.

**Space numbering**: a MAIN 40 reports its 40 spaces as **9…48**. Spaces 1–8 are
the main-breaker module that an MLO 48 swaps for a further branch module, so both
panels share one numbering scheme. Live snapshot: 27 circuits, every space in
9…48, breaker ratings 15–45 A.

Entity **node ids stay keyed on the instance id** (`circuit-30`) so renaming a
circuit in the SPAN app does not orphan its history; the label rides along as the
node's display name. Duplicate labels — two circuits can both be "Outlets" — are
qualified with their panel spaces. A non-panel instance *absent* from a non-empty
snapshot is the main feed (instance 2 here; its power tracks the panel total to
within one sampling window), published as `feed-<id>`.

### 3g. The same snapshot carries relay state — and the command address

Trait **1/31** (`SwitchLoadManagementTrait`) has one entry per breaker, keyed by
the *same* instance id as 1/15 and the telemetry:

```
1 { 1 TraitRef -> 1/15, same instance   <- the circuit this switch belongs to
    2 config { … thresholds … }
    3 switch_state                       <- 1 UNKNOWN, 2 OPEN, 3 CLOSED
    5 last_disconnect_msec { 1 utc }
    6 last_reconnect_msec  { 1 utc } }
```

**CLOSED = energized.** Live snapshot: 27 circuits, 27 switchable, all CLOSED. The
back-ref is checked against the id we looked the entry up under; a disagreement
yields no relay control rather than a possibly-wrong target (27/27 agreed).

The entry also supplies most of the command address: its resource's
`hardware_id` (the panel — the resource that *owns* the trait), the instance id,
and the trait metadata (vendor 1, trait 31, version 1 — the snapshot omits
`product_id`, so it is echoed back as absent).

The one piece the snapshot does **not** supply is the *requester* — a second
`resource_id` in the same message, naming who is asking. That one is the
caller's own **user id**, read from the access token (§4).

## 4. Circuit control — the one write path

Toggling a breaker is a `SendMessages` POST carrying a `TraitMessage` addressed to
that 1/31 instance; wire format and payload bytes are in
[CLOUD-PROTO.md § The command surface](CLOUD-PROTO.md#the-command-surface--sendmessages),
the whole design in [specs/circuit-control.md](specs/circuit-control.md).

Three consequences for the flow:

- **A write is signed with the user id, not with a panel or site id.** The
  message carries a *requester* `resource_id` alongside the panel it targets,
  and the server checks that the named resource contains the calling user — so
  the only value that passes is the user itself. Anything else gets
  `PERMISSION_DENIED [Validation Error]: Requester <id>, does not contain
  <userId>`, where `<userId>` is the access token's `username` claim (the same
  id in the Ably channel name). Panel, site, and their UUIDs were all tried
  against the live service and refused.
- **The reply is an ack, not an answer.** The app matches the real
  `SwitchLoadManagementCommandResponses` by `request_id` on the Ably *trait*
  channel, which we do not read. So a panel-side policy refusal
  (`ALWAYS_ON_CIRCUIT`, `MINIMUM_RECONNECT_TIME`) surfaces as the state reverting,
  not as an error.
- **State needs its own refresh.** Telemetry never carries `switch_state`, so
  `SubscribeAndGetTraits` is re-issued on a timer (`SWITCH_REFRESH_SECONDS = 60`)
  and again a few seconds after a command. That is also how a breaker toggled in
  the SPAN app reaches Home Assistant.

## 5. app-api GraphQL — what it's actually for

`app-api.prod.span-csp.com/graphql` is still live but only serves **app
configuration and third-party integration keys** (Customer.io, Zendesk, Segment).
It does **not** return panel/energy data. The deprecated `GetUserBuildings` query
returns `buildings: null`; ignore it.

## Implications for the cloud backend

1. **Auth**: Cognito SRP (user logs in; we never store the password) → cache the
   access token, refresh via Cognito refresh token. A refusal that a new password
   would fix (§1) starts HA's reauth flow instead of retrying forever.
2. **Bootstrap**: `GetSitesForUser` once → siteId, panel resource UUID, serial,
   and the **hardware ids** needed to subscribe.
3. **Realtime**: generate a device UUID locally (§3a) → `AblyToken` →
   `rest.ably.io/.../requestToken` (accept **HTTP 201**) → attach
   `c:<userId>:<deviceUUID>` via **SSE `enveloped=true`** → **`SubscribeAndGetTraits`**
   naming that channel (§3e, mandatory) → decode base64-protobuf frames. Re-issue
   token on ~1h expiry, and re-subscribe on every reconnect. (Live-validated and
   implemented in `cloud_ably`/`backends.cloud`.)
   **Keep the subscribe response** — it is what names the circuits (§3f). The
   schema is held back a couple of seconds waiting for it, since HA fixes entity
   names at creation.
4. **Control**: `SendMessages` with a 1/31 `TraitMessage` to open/close a relay
   (§4) — targeting the panel, signed with the **user id** from the token;
   state comes from re-reading the snapshot, not from the reply.
5. **History**: `GetHistoryAggregation` for backfill / long-term stats.
   Until that is wired, **energy is integrated locally** — the realtime channel
   carries instantaneous power only, and HA's Energy dashboard measures in kWh,
   so each watt property gets a trapezoidal Riemann sum alongside it
   (`energy.EnergyAccumulator`). It sums only the positive part, so a signed flow
   like site `grid` cannot run a `total_increasing` sensor backwards, and it
   refuses to bridge a gap longer than the coordinator's staleness threshold
   rather than invent energy for a window with no readings in it. The panel does
   report real lifetime accumulators — `EnergyAccumulators`, 10⁻⁴ Wh, see
   `CLOUD-PROTO.md` — inside the lean `EnergyMetric` frames the stream
   interleaves (§3d); decoding those would replace the derivation with measured
   totals, but needs a captured energy frame to pin the layout, which we do not
   have.
6. ~~**Blocker**: recover the telemetry `.proto`~~ — **resolved.** Schema recovered
   from the APK (see `docs/CLOUD-PROTO.md`) and live-confirmed; the cloud backend
   decodes circuits + site flows end-to-end.

## Reproduce

The decrypted capture and per-flow dumps live in the session scratchpad
(`mitm/iphone_capture.mitm`, `mitm/decoded/*.txt`) and contain **live tokens** —
they are intentionally kept out of the repo and must never be committed.
