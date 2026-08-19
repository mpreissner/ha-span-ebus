# ha-span-ebus

Live per-circuit data from a **SPAN Panel MAIN 40 (Gen3)** in Home Assistant.

A HACS custom integration (`custom_components/span_ebus`) that runs inside Home
Assistant and streams live telemetry from SPAN's cloud (Cognito → gRPC → Ably
SSE). This is the path that works **today**, while the panel's local API is not
yet enabled. Live-validated end to end (see [docs/CLOUD-FLOW.md](docs/CLOUD-FLOW.md)).

When SPAN enables the local MAIN 40 API (~H2 2026), the integration will prefer
it: talk to the panel's own Electrification Bus (Homie 5 over MQTT) directly on
your LAN, and fall back to the cloud when local is unreachable or not yet
provisioned. That is a change of transport, not of deployment — the integration
still runs inside Home Assistant, still a HACS install, and your entities do not
change either way. The panel's broker is already healthy on :8883; the REST tier
that issues its credentials is still dormant (see
[docs/FINDINGS.md](docs/FINDINGS.md)).

Requires Home Assistant 2024.12.0 or newer.

## Install

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
- **the panel** — power, line frequency, per-leg voltage and per-leg current
  (L1/L2, for checking how evenly the legs are loaded), plus a combined current
  that is the *higher* of the two legs rather than their sum, i.e. busbar loading
  to compare against your main breaker;
- **the main feed** and the **site flows** SPAN reports (grid, home, …).

Circuit entity ids are keyed on SPAN's internal circuit identifier rather than its
label or panel space, so renaming a circuit in the SPAN app keeps its history.

There is nothing to configure beyond the sign-in: no YAML, no options, no
environment variables.

## Circuit control

Each breaker SPAN reports a switch for also gets a **switch entity** that turns
the circuit off and on, the same operation as the toggle on the app's breaker
screen. A circuit only gets one if the panel actually told us how to address its
relay, so nothing appears for the main feed, the panel's own metering, or a
circuit we could not resolve — better a missing switch than one that commands the
wrong breaker.

Turning a circuit **on** *releases* our own disconnect rather than forcing the
relay closed, so the panel's reasons for holding a circuit open (backup reserve,
load shed, a minimum reconnect time) still win. If the panel declines — an
always-on circuit, or one it reconnected too recently — the switch flips back
within a few seconds. Relay state is read from the panel roughly once a minute and
right after any change, so toggling a breaker in the SPAN app shows up here too.
Details: [docs/specs/circuit-control.md](docs/specs/circuit-control.md).

## How it gets your live data

SPAN's realtime telemetry arrives on an Ably channel named
`c:<userId>:<deviceUUID>`. The `deviceUUID` is a **client identifier we generate
ourselves** — SPAN issues nothing here; the `AblyToken` RPC echoes back whatever
we send. What actually starts the data flowing is the `SubscribeAndGetTraits`
RPC, which registers our channel as a subscriber for the panel's hardware
resources. The integration mints a UUID on first setup and reuses it for the life
of the config entry. See [docs/CLOUD-FLOW.md](docs/CLOUD-FLOW.md) §3.

## Layout

```
custom_components/span_ebus/
  __init__.py        config entry setup / unload
  config_flow.py     sign-in + re-auth flow
  coordinator.py     stream lifecycle, entity discovery, state
  sensor.py          circuit / panel / site-flow sensors
  switch.py          circuit relay switches
  span_client/       the panel data layer, and the only copy of it
    backend.py       CloudBackend: stream lifecycle, traits, commands
    cloud_*.py       Cognito auth, gRPC, protobuf, Ably SSE, telemetry
    models.py        normalized panel / node / property / reading types
    local/           staged for the local API; not imported yet
tests/               run against span_client, i.e. against what ships
docs/CLOUD-FLOW.md   the cloud path, end to end
docs/CLOUD-PROTO.md  protobuf shapes for the gRPC calls
docs/FINDINGS.md     empirical results from this panel
```

## References

- [SPAN API Client Docs](https://github.com/spanio/SPAN-API-Client-Docs) — spec `r202627` matches this panel
- [Griswoldlabs/span-panel-ha](https://github.com/Griswoldlabs/span-panel-ha) — the gRPC approach, now defunct
- [SpanPanel/span](https://github.com/SpanPanel/span) — Gen2 REST integration
