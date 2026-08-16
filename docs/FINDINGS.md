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

**Hypothesis:** the REST API is gated behind proof-of-proximity. Per the docs,
pressing the panel door switch 3× opens a 15-minute authenticated window; that
action plausibly also starts the API service. This is the next thing to test,
and it requires physical access.

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

Both unit quirks are handled in `src/span_bridge/homie.py`.

## Conclusion

Do not build a cloud scraper, and do not build an Android emulator. The panel
exposes a first-party local MQTT API and the only missing piece is credentials,
which are gated behind a physical button press.
