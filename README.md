# ha-span-ebus

Live per-circuit data from a **SPAN Panel MAIN 40 (Gen3)** in Home Assistant.

Two delivery paths share one normalized data model, so entities are identical
whichever is used:

1. **HACS custom integration** (`custom_components/span_ebus`) — runs inside HA
   and streams live telemetry from SPAN's cloud (Cognito → gRPC → Ably SSE).
   This is the path that works **today**, while the panel's local API is not yet
   enabled. Live-validated end to end (see [docs/CLOUD-FLOW.md](docs/CLOUD-FLOW.md)).
2. **`span-bridge` daemon** (`src/span_bridge`) — subscribes to the panel's own
   local MQTT broker (the SPAN "Electrification Bus", Homie 5) and republishes to
   your HA broker via MQTT Discovery. Ready for when SPAN enables the MAIN 40
   local API (~H2 2026); the broker is healthy but its credential-issuing REST
   tier is still dormant (see [docs/FINDINGS.md](docs/FINDINGS.md)).

## Install (HACS integration — recommended)

1. In HACS, add this repository as a **custom repository** (category:
   Integration), or install it if listed.
2. **Restart Home Assistant.**
3. **Settings → Devices & Services → Add Integration → “SPAN Panel (eBus)”.**
4. Sign in with your SPAN account email and password. That's it — nothing to
   look up or paste.

Your password is used **once**, locally, to compute the Cognito SRP proof and
obtain access tokens; it is never stored. Only the resulting tokens are kept
(auto-refreshing), and a re-auth prompt appears if they ever fully expire.

Setup takes up to a minute: it registers this install as a telemetry client and
waits for a real frame before finishing, rather than completing and leaving you
with zero entities. You then get one device per panel, updated at ~1–2 Hz, with:

- **each circuit** named as you named it in the SPAN app, reporting power and
  current (a 120 V branch does not meter its own voltage, so none is offered);
- **the panel** — power, current, per-leg voltage (L1/L2) and line frequency;
- **the main feed** and the **site flows** SPAN reports (grid, home, …).

Circuit entity ids are keyed on SPAN's internal circuit identifier rather than its
label or panel space, so renaming a circuit in the SPAN app keeps its history.

### How it gets your live data

SPAN's realtime telemetry arrives on an Ably channel named
`c:<userId>:<deviceUUID>`. The `deviceUUID` is a **client identifier we generate
ourselves** — SPAN issues nothing here; the `AblyToken` RPC echoes back whatever
we send. What actually starts the data flowing is the `SubscribeAndGetTraits`
RPC, which registers our channel as a subscriber for the panel's hardware
resources. The integration mints a UUID on first setup and reuses it for the life
of the entry (the daemon takes `SPAN_CLOUD_DEVICE_UUID`, or generates a per-run
one). See [docs/CLOUD-FLOW.md](docs/CLOUD-FLOW.md) §3.

---

## The `span-bridge` daemon (local MQTT path)

The rest of this README covers the daemon, which targets the panel's local
broker directly. It is the future local path; the cloud integration above is
what to use now.

### Architecture

```
                  ┌────────────────────────────────────┐
   SPAN panel ───►│ backends/ebus.py                   │
   mqtts:8883     │   TLS + panel CA, Homie 5 subscribe│
   (Homie 5)      └──────────────┬─────────────────────┘
                                 │  normalized models.Reading
                  ┌──────────────▼─────────────────────┐
                  │ homie.py     schema → PropertySpec │
                  │ discovery.py PropertySpec → HA cfg │
                  └──────────────┬─────────────────────┘
                                 │
                  ┌──────────────▼─────────────────────┐
   HA broker ◄────│ bridge.py    publish + retain      │
   (discovery)    └────────────────────────────────────┘
```

Backends are pluggable behind the `Backend` protocol in
`src/span_bridge/backends/__init__.py`:

| Backend | Status | Use |
|---|---|---|
| `ebus` | primary (local) | Official local MQTT. Target once the panel API is enabled. |
| `grpc_legacy` | stub | Gen3 gRPC on :50065. Dead on firmware ≥ 7.2.0 / r202627. |
| `cloud` | live | Cognito → gRPC → Ably SSE. Powers the HACS integration today. |

## Install

```bash
pip install -e .
cp .env.example .env    # then edit
```

Or with Docker:

```bash
docker compose up -d
```

## Configuration

All settings are environment variables (see `.env.example`). The essentials:

| Variable | Default | Meaning |
|---|---|---|
| `SPAN_HOST` | — | Panel IP or hostname |
| `SPAN_SERIAL` | auto | Panel serial; discovered via mDNS if unset |
| `SPAN_AUTH_FILE` | `~/.span-auth.json` | Credential cache (mode 0600) |
| `SPAN_CA_CERT_DIR` | `~/.span-ca-certs` | Cached panel CA certificates |
| `HA_MQTT_HOST` | — | Your Home Assistant broker |
| `HA_MQTT_PORT` | `1883` | |
| `HA_MQTT_USERNAME` | — | |
| `HA_MQTT_PASSWORD` | — | |
| `HA_DISCOVERY_PREFIX` | `homeassistant` | |
| `LOG_LEVEL` | `INFO` | |

## Panel-specific gotchas

Encoded in `homie.py`, but worth knowing:

- Circuit `active-power` is declared `kW` in the schema and reported in **watts**.
- PV `nameplate-capacity` is declared `kW` and reported in **watts**.
- Relay `CLOSED` means energized. A HA switch showing "on" maps to `CLOSED`.

## Layout

```
src/span_bridge/
  config.py      env-driven settings
  models.py      normalized panel/node/property/reading types
  auth.py        CA fetch, /api/v2/auth/register, credential cache
  homie.py       Homie 5 $description parser + unit-quirk compensation
  discovery.py   PropertySpec → HA MQTT Discovery payloads
  bridge.py      subscribe → translate → republish loop
  cli.py         probe / auth / discover / run
  backends/      ebus (primary), grpc_legacy, cloud
tools/probe_panel.sh    codified reconnaissance
docs/FINDINGS.md        empirical results from this panel
```

## References

- [SPAN API Client Docs](https://github.com/spanio/SPAN-API-Client-Docs) — spec `r202627` matches this panel
- [Griswoldlabs/span-panel-ha](https://github.com/Griswoldlabs/span-panel-ha) — the gRPC approach, now defunct
- [SpanPanel/span](https://github.com/SpanPanel/span) — Gen2 REST integration
