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
        └─ ListDispatches             → schedules / dispatches
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

Token TTL is ~1 hour → the bridge must re-run AblyToken + requestToken on expiry.

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

The bridge does **not** replicate the app's comet long-poll. It subscribes with a
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
  verbatim (see `SUBSCRIBE_TRAITS` in `backends/cloud.py`). Vendor 1 is SPAN's
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

## 4. app-api GraphQL — what it's actually for

`app-api.prod.span-csp.com/graphql` is still live but only serves **app
configuration and third-party integration keys** (Customer.io, Zendesk, Segment).
It does **not** return panel/energy data. The deprecated `GetUserBuildings` query
returns `buildings: null`; ignore it.

## Implications for the cloud backend

1. **Auth**: Cognito SRP (user logs in; we never store the password) → cache the
   access token, refresh via Cognito refresh token.
2. **Bootstrap**: `GetSitesForUser` once → siteId, panel resource UUID, serial,
   and the **hardware ids** needed to subscribe.
3. **Realtime**: generate a device UUID locally (§3a) → `AblyToken` →
   `rest.ably.io/.../requestToken` (accept **HTTP 201**) → attach
   `c:<userId>:<deviceUUID>` via **SSE `enveloped=true`** → **`SubscribeAndGetTraits`**
   naming that channel (§3e, mandatory) → decode base64-protobuf frames. Re-issue
   token on ~1h expiry, and re-subscribe on every reconnect. (Live-validated and
   implemented in `cloud_ably`/`backends.cloud`.)
4. **History**: `GetHistoryAggregation` for backfill / long-term stats.
5. ~~**Blocker**: recover the telemetry `.proto`~~ — **resolved.** Schema recovered
   from the APK (see `docs/CLOUD-PROTO.md`) and live-confirmed; the cloud backend
   decodes circuits + site flows end-to-end.

## Reproduce

The decrypted capture and per-flow dumps live in the session scratchpad
(`mitm/iphone_capture.mitm`, `mitm/decoded/*.txt`) and contain **live tokens** —
they are intentionally kept out of the repo and must never be committed.
