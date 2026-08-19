# SPAN MAIN 40 (Gen3) — Reconnaissance Findings

Recorded 2026-08-16 against the live panel. These are empirical results, not
documentation claims. Re-run `tools/probe_panel.sh` to refresh.

## Panel identity

| Field | Value |
|---|---|
| Hostname | `span-<local-serial>.local` |
| IP | `10.100.6.21` |
| Serial | `<local-serial>` |
| Firmware | `spanos3/r202627/06` |
| Model | MAIN 40 (Gen3) |
| eth0 MAC | `00:14:2d:84:1d:0d` |
| wlan0 MAC | `cc:47:40:25:0d:e3` |

Discovered via mDNS: `dns-sd -L span-<local-serial> _span._tcp local`

## Port scan

| Port | State | What it actually is |
|---|---|---|
| 80 | open | nginx — `500` on `/`, **`502` on `/api/*`** |
| 443 | open | nginx — same behavior |
| 8883 | open | **MQTTS — the Electrification Bus broker** |
| 9001 | open | MQTT over WebSocket (TLS) |
| 50065 | closed | legacy Gen3 gRPC (`TraitHandlerService`) — gone |
| 50058 | open | gRPC server, reflection disabled, all known services `Unimplemented` |

## The gRPC path is dead — confirmed empirically

The community integration ([Griswoldlabs/span-panel-ha]) targets
`io.span.panel.protocols.traithandler.TraitHandlerService` on port 50065.

- Port 50065: `connection refused`.
- Port 50058: speaks h2c (HTTP/2 cleartext, `curl --http2-prior-knowledge` → 200),
  so a gRPC server **is** running. Reflection is disabled
  (`server does not support the reflection API`), and calling the known services
  from the upstream `span.protoset` returns a well-formed gRPC `Unimplemented`:

  ```
  TraitHandlerService/GetRevision   → Unimplemented
  TraitHandlerService/GetInstances  → Unimplemented
  WifiConfigService/GetWifiState    → Unimplemented
  ```

A well-formed `Unimplemented` (rather than a connection error) means the server
is healthy but no longer registers these services. Both services in the protoset
are gone, so this is a deliberate removal, not a rename of one endpoint. Without
reflection or a newer `.proto`, enumerating what 50058 *does* serve would require
brute-forcing service names. **Not worth pursuing** — see below.

[Griswoldlabs/span-panel-ha]: https://github.com/Griswoldlabs/span-panel-ha

## The official local API is already present on this panel

This is the important finding. The TLS certificate on 8883:

```
issuer  = C=US, ST=Local, L=LAN, O=SPAN.io, CN=<local-serial> CA
subject = C=US, ST=Local, L=LAN, O=SPAN.io, CN=span-<local-serial>.local
TLSv1.3, TLS_AES_256_GCM_SHA384
```

A per-device CA and server certificate, issued to this panel's serial. That is
the [SPAN API](https://github.com/spanio/SPAN-API-Client-Docs) Electrification
Bus architecture — MQTT for real-time state, REST for auth and certificate
issuance.

The published spec set includes `specs/r202627/`, which matches this panel's
firmware exactly. The spec's `mdns-services.json` declares `_secure-mqtt._tcp`
on 8883 and MQTT-WS on 9001 — **both of which are open here**.

SPAN's blog says the API is MAIN 32 only with MAIN 40 "expected in H2 2026."
The broker infrastructure is evidently already shipping on MAIN 40 firmware.

### What is still blocked

Every REST endpoint returns `502`:

```
502  /api/v2/certificate/ca
502  /api/v2/auth/register
502  /api/v2/status
502  /api/v1/panel
502  /api/v1/circuits
```

Note `502` on `/api/*` versus `500` elsewhere — nginx has an upstream configured
for `/api/` that is refusing connections. The REST backend exists but is not
running.

**Hypothesis (2026-08-16): DISPROVEN.** The door switch was pressed 3× and every
endpoint still returned `502`, on both HTTP and HTTPS, immediately and for 20
minutes afterwards. Also ruled out in the same session:

- **nginx virtual-host routing.** Requesting with `Host: span-<local-serial>.local`
  and via the mDNS name directly both still `502`.
- **A different port.** `nc` scan confirms only 80, 443, 8883, 9001, 50058 are
  open. The `_span._tcp` mDNS TXT record points the API at **port 80** — the
  one that is 502ing — and carries `version=spanos3/r202627/06 sn=<local-serial>`.

The reasoning that produced the hypothesis was wrong in a way worth recording:
proof-of-proximity is enforced *by* the REST service, so a panel gating on it
would have to be running that service in order to reject us — it would answer
`401`/`403`. A `502` is nginx reporting that nothing is listening upstream at
all. Proximity gating and a `502` are mutually exclusive explanations.

### The broker, by contrast, is fully healthy

Confirmed 2026-08-16 by connecting directly:

```
CONNACK: Not authorized
```

A well-formed MQTT-level rejection, not a transport failure — mosquitto is up,
serving TLS, and asking for credentials. So the two tiers fail independently,
and **the only missing piece is `ebusBrokerUsername`/`ebusBrokerPassword`**,
which are issued by the dead REST tier.

Two things this session established that change the code:

1. **The CA does not require REST.** The broker presents its full chain — leaf
   plus the per-device CA — so the CA can be read off the TLS handshake.
   Implemented as `tls.peer_chain_pem()` with `auth.fetch_ca_from_broker()` as
   an automatic fallback whenever REST returns 5xx. A recovered candidate is
   accepted only if a *verified* handshake then succeeds against it, which
   proves it signed what the broker actually presented; a self-signed impostor
   CA was confirmed to fail that check.

2. **OpenSSL 3 strict mode rejects SPAN's certificates.** The per-device CA
   omits the Authority Key Identifier extension, so a stock
   `create_default_context()` fails the handshake with `Missing Authority Key
   Identifier`. `VERIFY_X509_STRICT` must be cleared and
   `VERIFY_X509_PARTIAL_CHAIN` set. This would have broken the ebus backend on
   first contact regardless of the credential problem.

### Remaining avenues for credentials

Untested, roughly in order of cost:

- **Reboot the panel.** The REST tier may simply have crashed; certs were minted
  2026-08-14/15, so the unit is freshly commissioned.
- **A local-API toggle in the SPAN Home app.** If one exists, the service may be
  disabled rather than broken.
- **`hopPassphrase` from the app**, which still needs a live `/auth/register`.
- **Capturing the app's traffic** (mitmproxy + apk-mitm) to see whether it
  reaches the panel locally at all, or is currently cloud-only.

## The SPAN Home app is cloud-only on this panel (pcap, 2026-08-16)

Two router-side captures of the iPhone app — one pre-auth (`span.pcapng`), one
spanning a full login (`span2.pcapng`) — were analysed passively (SNI only, no
decryption). The result is unambiguous:

- **Zero packets to the panel** (`ip.addr==10.100.6.21`) in *either* capture.
  The app never contacts the panel on the LAN, during login or afterwards.
- The cloud surface it does use:

  | Host | Role |
  |---|---|
  | `cognito-idp-fips.us-west-2.amazonaws.com` | AWS Cognito — user auth |
  | `app-api.prod.span-csp.com` | SPAN backend API (data/control) |
  | `mobilefrontend.prd.span.io` | SPAN mobile backend-for-frontend |
  | `rest.ably.io`, `realtime.ably.io` | Ably — realtime pub/sub data transport |
  | launchdarkly / sentry / customer.io / branch.io / zendesk / gist.build | flags, crash, analytics, support |

Real-time panel data reaches the app over **Ably**, a cloud relay, not over the
local `:8883` broker. The local broker we proved healthy is dormant
infrastructure — nothing in the shipping app drives it. This matches SPAN's
statement that the MAIN 40 local API is "expected H2 2026": the broker binary is
present, but the service that issues credentials and the app code that would use
it locally are not live yet.

Consequence: **there is no working local data path on this panel today.** The
door switch, a reboot, and an app toggle cannot help, because the local REST
service that would honour them is not running. `tls.py` and the `ebus` backend
are correct but blocked on a server SPAN has not enabled.

## Auth flow (from official docs)

1. `GET /api/v2/certificate/ca` — fetch the panel's CA cert (no auth).
2. Obtain a `hopPassphrase`, either by:
   - **proof-of-proximity** — press the door switch 3×, 15-minute window; or
   - reading it from the SPAN Home mobile app.
3. `POST /api/v2/auth/register` — exchange for `accessToken` (REST) and
   `ebusBrokerPassword` (MQTT).
4. Connect to `mqtts://<panel>:8883` with those credentials and the panel CA.

Credentials are conventionally cached in `~/.span-auth.json` (mode 0600) and the
CA in `~/.span-ca-certs/<serial>.crt`.

## Data model (Homie 5, domain `ebus`)

```
ebus/5/<serial>/$state                        init|ready|disconnected|sleeping|lost
ebus/5/<serial>/$description                  full JSON schema for this panel
ebus/5/<serial>/<node>/<property>             value
ebus/5/<serial>/<node>/<property>/set         command (settable only)
```

Nodes: `core`, `lugs-upstream`, `lugs-downstream`, `power-flows`, `pcs`, `bess`,
`pv`, `evse`, plus one node per circuit keyed by circuit UUID.

Only three properties are writable:

| Node | Property | Values |
|---|---|---|
| core | `dominant-power-source` | `GRID`/`BATTERY`/`PV`/`GENERATOR`/`NONE`/`UNKNOWN` |
| circuit | `relay` | `OPEN`/`CLOSED` |
| circuit | `shed-priority` | `OFF_GRID`/`SOC_THRESHOLD`/`NEVER` |

### Firmware quirks to compensate for

- Circuit `active-power` is declared `kW` in the schema but **reported in watts**.
- PV `nameplate-capacity` is declared `kW` but **reported in watts**.
- Relay `CLOSED` = circuit energized (on); `OPEN` = de-energized (off).

Both unit quirks are compensated in the Homie schema parser,
`custom_components/span_ebus/span_client/local/homie.py`. That module is staged,
not wired up: nothing under `local/` is imported until SPAN enables the local
API.

## Conclusion

The panel ships the *infrastructure* for a first-party local MQTT API (broker
alive on :8883, per-device CA, spec matches firmware), but the service that
issues credentials is not running and the official app does not use the local
path at all — it is cloud-only via Cognito + Ably. So there are two viable
tracks:

1. **Cloud path (works today).** Replicate the app's cloud flow — Cognito
   auth → `app-api.prod.span-csp.com` → Ably subscription — to pull live data
   now. Requires a decrypting MITM of the app to learn the API endpoints, the
   Ably token-issuance call, and the channel naming. Cloud-dependent.
2. **Local path (blocked, ~H2 2026).** Keep the `ebus` backend ready and wait
   for SPAN to enable the MAIN 40 local API. Once live it becomes the preferred
   transport — LAN-direct, no cloud round trip — with the cloud path demoted to
   a fallback rather than removed, since a panel may be unreachable or not yet
   provisioned. No ETA under our control.

The earlier flat "do not build a cloud scraper" is retracted: the pcap evidence
shows the local button-press path is a dead end on this firmware, and the cloud
flow is the only route to data before SPAN ships the local API.

### Open question — what local auth will require, and what that costs the user

Transport selection is not purely internal, because the two paths may not need
the same things from the user. If **proof-of-proximity survives** (door switch,
or the passphrase from the app), a local-only install needs no SPAN account at
all — the user presses a button and never types a password. If SPAN instead
**requires a one-time cloud auth** to mint a local access token, then even
local-only setup starts with account credentials, and the config flow looks
much the same as it does today.

Either way, **local-only must be an offered choice**, not an inference. Most
users will want the cloud fallback and it should be the default, but someone
who has deliberately kept a panel off the internet should be able to say so and
have the integration honor it rather than quietly reaching out. Which of the
two auth shapes lands decides how much the config flow can drop when they do.

Not designed yet, and it cannot be until SPAN ships the API and we see which
authentication it accepts.
