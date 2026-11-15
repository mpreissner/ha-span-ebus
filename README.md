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
with zero entities. You then get one device per panel, updated as fast as the
panel reports — once or twice a second over ethernet, dropping to about once a
minute if it falls back to its cellular connection — with:

- **each circuit** named as you named it in the SPAN app, reporting power and
  current (a 120 V branch does not meter its own voltage, so none is offered);
- **the panel** — power, line frequency, per-leg voltage and per-leg current
  (L1/L2, for checking how evenly the legs are loaded), plus a combined current
  that is the *higher* of the two legs rather than their sum, i.e. busbar loading
  to compare against your main breaker;
- **panel overhead** — the main feed's power less the sum of every circuit, i.e.
  the steady draw of the panel's own relays and electronics (never below zero),
  with an energy total alongside so it can be tracked as a device on the Energy
  dashboard. SPAN does not meter this one, so its kWh are integrated from the
  power reading rather than read back from the panel;
- **the main feed** and the **site flows** SPAN reports (grid, home, …).

"Grid" means what feeds this panel: the power at its main feed, under SPAN's
own name for it. If solar or storage is installed upstream of the panel, it is
not your utility import.

Circuit entity ids are keyed on SPAN's internal circuit identifier rather than its
label or panel space, so renaming a circuit in the SPAN app keeps its history.

There is nothing to configure beyond the sign-in: no YAML and no environment
variables. A panel added over its local API has optional control options (see
below).

## Circuit control

Each breaker SPAN reports a switch for also gets a **switch entity** that turns
the circuit off and on, the same operation as the toggle on the app's breaker
screen. A circuit only gets one if the panel actually told us how to address its
relay, so nothing appears for the main feed, the panel's own metering, or a
circuit we could not resolve — better a missing switch than one that commands the
wrong breaker.

On a panel added over its local API, a circuit that feeds a battery or solar
inverter, or that the panel marks as storage, gets no switch: turning it off can
cut backup power or fault the inverter. A circuit that feeds a device the panel
has not identified yet is treated the same way. If the panel reports such a link
after the switch already exists (or the switch came from an earlier version),
the switch goes unavailable and refuses commands; it is not deleted, so your
customisations survive. The relay state is still shown as an attribute of the
circuit's power sensor.

### Control options (local panels)

**Settings → Devices & services → SPAN Panel (eBus) → Configure → Controls**
sets what Home Assistant may change on a panel added over its local API:

- **Protected circuits:** their switches stay unavailable and every command to
  them is refused. Their shed priority can still be changed.
- **Allow switching storage and solar circuits:** shown only while a circuit
  feeds a battery or solar inverter. A ticked circuit gets a working switch.
  Protecting a circuit wins over this.
- **Grid-state control:** shown only on a panel with a battery that accepts it.
  It adds a select that tells the panel the grid is up (`On grid`) or clears
  that (`Not asserted`). Like SPAN's own integration, it is unavailable while the
  battery reports healthy or the panel already sees the grid; an assertion in
  force can always be cleared.

Saving reloads the panel. Every command fires a `span_ebus_command` event with
its outcome (confirmed, not confirmed, superseded, unavailable, refused, not
sent or invalid), and the logbook shows it with who sent it.

To ask before the grid-state control changes anything, drive it from buttons
that confirm first, rather than the select's own dropdown:

```yaml
type: horizontal-stack
cards:
  - type: button
    name: Assert on grid
    icon: mdi:transmission-tower
    tap_action:
      action: perform-action
      perform_action: select.select_option
      target:
        entity_id: select.span_panel_grid_state
      data:
        option: on_grid
      confirmation:
        text: Tell the panel the grid is up?
  - type: button
    name: Clear
    icon: mdi:close-circle-outline
    tap_action:
      action: perform-action
      perform_action: select.select_option
      target:
        entity_id: select.span_panel_grid_state
      data:
        option: none
      confirmation:
        text: Clear what the panel is told about the grid?
```

Use your own entity id: it depends on the panel's name.

Turning a circuit **on** *releases* our own disconnect rather than forcing the
relay closed, so the panel's reasons for holding a circuit open (backup reserve,
load shed, a minimum reconnect time) still win. If the panel declines — an
always-on circuit, or one it reconnected too recently — the switch flips back
within a few seconds. Relay state is read from the panel roughly once a minute and
right after any change, so toggling a breaker in the SPAN app shows up here too.
Details: [docs/specs/circuit-control.md](docs/specs/circuit-control.md).

## Rotating a local panel's credentials

The `span_ebus.rotate_credentials` action asks a panel added over its local API
for a new passphrase and broker password. It stores the new password, waits
(about a minute at most) for the broker to accept it, then reloads the entry.
Only admin users can run it, and automations can't. Run it from
**Developer tools > Actions** with "Return response" on:

```yaml
action: span_ebus.rotate_credentials
data:
  config_entry_id: <your entry>   # or device_id; optional with one local panel
```

The response holds the new `hop_passphrase`, and whether the entry
`reconnected` and the password was `stored`. Keep the passphrase: it is shown
nowhere else. If `reconnected` is false, rotate again, or run the entry's
reauthentication with that passphrase.

The passphrase is not logged or stored by the integration, but two things
outside it can keep it:

- a script that saves the response in a variable keeps it in its trace;
- debug logging of `homeassistant.components.websocket_api` logs the response.

This action has not been tested against a real panel yet.

## Leg balance (local panels)

The `span_ebus.analyze_leg_balance` action reads up to 90 days of recorded
circuit power for a panel added over its local API, works out how unevenly the
two legs (L1 and L2) are loaded, and suggests up to three single-pole breaker
moves or swaps that would even them out. It changes nothing. Run it from
**Developer tools > Actions** with "Return response" on:

```yaml
action: span_ebus.analyze_leg_balance
data:
  config_entry_id: <your entry>   # or device_id; optional with one local panel
  days: 28                        # 7 to 90
```

The response gives the typical and high (95th percentile) difference between
the legs in amps, which leg is heavier, and each suggestion with the change it
would make. It also lists the circuits it left out (two-pole circuits, shared
spaces) and the ones it counted but never moves (protected circuits, battery or
solar, circuits feeding or fed by another panel).

Which spaces sit on which leg is worked out from the panel layout, then checked
against the panel's own lug currents. The response says whether that check
confirmed it; if it didn't, check the leg of each space by hand. Suggestions
need 72 hours of history; with less, the response shows the imbalance only.

These are suggestions only. Moving a breaker means working inside the panel:
have a licensed electrician check and do any move.

## Connection (local panels)

**Settings → Devices & services → SPAN Panel (eBus) → Configure → Connection**
chooses how Home Assistant reaches a panel added over its local API:

- **Local, with cloud fallback:** your SPAN account is used only when the local
  connection becomes unavailable, and to backfill missing site, solar and battery
  energy for gaps of five minutes or more, such as after a Home Assistant
  restart. Backfill covers the last 7 days on single-panel sites; circuit energy
  is not backfilled. Choosing it asks you to sign in to your SPAN account.
- **Local only:** your SPAN account is removed from Home Assistant for this
  panel, and the ability to backfill missing energy data is lost. Entities and
  their history stay.

This release removes the stored SPAN account from panels set to Local only; to
use fallback again, sign in under Connection.

## Several panels

A panel's "grid" readings are power and energy into the panel at its lugs. On a
site with solar or a battery between the utility meter and the panel, that is the
house's draw through the panel, not what the meter sees.

### Topology

**Configure → Topology** says what feeds a panel added over its local API. Each
panel answers for itself: saving it on one panel changes no other panel. Saving
reloads the panel. For a feed-through or breaker feed you can also pick the
upstream panel and, for a breaker, which breaker; panels from your SPAN account
and panels found on your network are listed even if Home Assistant doesn't run
them.

When anything other than the utility meter feeds the panel directly, its grid
readings are named "Feed power" and "Feed consumed/produced/net energy" ("Site
feed" and "Site feed energy" over the cloud). The entity ids and history stay as
they were. A panel linked to the cloud takes this from the feeds you answered
when adding the cloud panels until you answer Topology.

| What feeds this panel | Its readings | Energy dashboard |
|---|---|---|
| Utility meter feeds this panel directly | Grid | The panel's grid energy can be the grid source. |
| Utility meter, with solar or a battery between the meter and this panel | Feed | Not grid: it is the house load after solar and battery. Use the utility meter or the solar or battery system's grid meter as the grid source, and that system's sensors for solar and battery. |
| Another panel's feed-through lugs | Feed | Not grid. Use only the panel the utility meter feeds as the grid source. |
| A breaker on another panel | Feed | Not grid. The upstream breaker already counts this panel; don't add both as devices. |

**Solar or a battery upstream of the panel:** choose the second row. The panel
then reports what reaches it after the solar and battery system, so its feed
energy is your house load, not your utility import or export.

In "Individual devices", set a branch-fed panel's circuits' upstream device to
the circuit on the panel that feeds it.

A circuit that feeds another panel carries a `feeds_panel` attribute on its power
and energy sensors. It holds the name you gave that panel's device, or "SPAN"
and its model if you have not named it; never its serial or host. Local entries
without a cloud link do not get `feeds_panel`: the feed answers come from the
cloud entry.

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
  select.py          shed priority, grid-state control (local)
  number.py          EV charger current limit (local)
  lock.py            EV charger lock (local)
  logbook.py         logbook lines for control commands
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
