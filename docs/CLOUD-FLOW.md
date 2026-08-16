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
        ├─ SubscribeAndGetTraits      → trait snapshot + subscription
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

Request protobuf: field 1 = client device UUID
`<device-uuid>` (per-install, from the mobile client).

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
- **The stream is occupancy-triggered.** Merely attaching to the channel makes
  SPAN's backend start publishing — frames arrive before any other call.
  `SubscribeAndGetTraits` is **not** required to start the stream; it only returns
  a one-shot 22 KB trait *snapshot* (same metric shapes as a live frame). The
  cloud backend skips it and reads the schema from the first content-bearing
  live frame instead.
- The stream interleaves lean **energy/interval** frames (empty of resources +
  site flows) between the **power** frames; the backend ignores the empties for
  schema purposes and only emits Readings for whatever a frame carries.

Live decode confirmed real values: site grid ≈ 4.0 kW, 29 circuits with per-circuit
power (e.g. instance 54 ≈ 660 W), `site_id=<siteId>`.

## 4. app-api GraphQL — what it's actually for

`app-api.prod.span-csp.com/graphql` is still live but only serves **app
configuration and third-party integration keys** (Customer.io, Zendesk, Segment).
It does **not** return panel/energy data. The deprecated `GetUserBuildings` query
returns `buildings: null`; ignore it.

## Implications for the cloud backend

1. **Auth**: Cognito SRP (user logs in; we never store the password) → cache the
   access token, refresh via Cognito refresh token.
2. **Bootstrap**: `GetSitesForUser` once → siteId + panel resource UUID.
3. **Realtime**: `AblyToken` → `rest.ably.io/.../requestToken` (accept **HTTP 201**)
   → subscribe `c:<userId>:<deviceUUID>` via **SSE `enveloped=true`**; decode
   base64-protobuf frames. Re-issue token on ~1h expiry. (See §3d — this whole
   path is now live-validated and implemented in `cloud_ably`/`backends.cloud`.)
4. **History**: `GetHistoryAggregation` for backfill / long-term stats.
5. ~~**Blocker**: recover the telemetry `.proto`~~ — **resolved.** Schema recovered
   from the APK (see `docs/CLOUD-PROTO.md`) and live-confirmed; the cloud backend
   decodes circuits + site flows end-to-end.

## Reproduce

The decrypted capture and per-flow dumps live in the session scratchpad
(`mitm/iphone_capture.mitm`, `mitm/decoded/*.txt`) and contain **live tokens** —
they are intentionally kept out of the repo and must never be committed.
