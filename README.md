# span-bridge

Bridges a **SPAN Panel MAIN 40 (Gen3)** into Home Assistant by subscribing to the
panel's own local MQTT broker (the SPAN "Electrification Bus", Homie 5) and
republishing it to your Home Assistant broker using MQTT Discovery.

> **History:** this started as `ha-spancloud`, on the assumption that a MAIN 40
> has no local API and would need cloud scraping. That turned out to be wrong —
> see [docs/FINDINGS.md](docs/FINDINGS.md) — and the project was renamed to match
> what it actually does. Everything here is local: no cloud, no Android emulator.

## Why a bridge rather than pointing HA at the panel directly

Home Assistant's MQTT integration binds to one broker and does not speak Homie 5.
This daemon connects to the panel broker over TLS, translates the Homie topic
tree into HA Discovery configs, and republishes onto your existing broker. Your
entity IDs, history, and dashboards then live on infrastructure you control, and
the panel becomes a swappable data source.

## Status

**Blocked on one physical step.** All REST endpoints on the panel currently
return `502`; the API backend is not running. It is very likely gated behind
proof-of-proximity.

**Do this:** press the SPAN panel door switch **3 times**, then within 15 minutes:

```bash
./tools/probe_panel.sh 10.100.6.21
```

If `/api/v2/certificate/ca` returns `200` instead of `502`, the hypothesis holds
and the rest of the pipeline can be wired up:

```bash
span-bridge auth --host 10.100.6.21     # fetch CA + register, cache credentials
span-bridge discover                     # dump the panel's $description schema
span-bridge run                          # start the bridge
```

If it still returns `502` after the door-switch sequence, the alternative is to
read `hopPassphrase` out of the SPAN Home mobile app and pass it directly:

```bash
span-bridge auth --host 10.100.6.21 --passphrase <hopPassphrase>
```

## Architecture

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
| `ebus` | primary | Official local MQTT. Target this. |
| `grpc_legacy` | stub | Gen3 gRPC on :50065. Dead on firmware ≥ 7.2.0 / r202627. |
| `cloud` | stub | Last resort. Not implemented, and hopefully never needs to be. |

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
