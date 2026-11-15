# ha-span-ebus

Live per-circuit data and control for a **SPAN Panel MAIN 40 (Gen3)** in Home
Assistant.

A HACS custom integration (`custom_components/span_ebus`) that runs inside Home
Assistant. It talks to the panel directly over your network, using SPAN's
official local API (Homie 5 over MQTT, panel firmware r202639 or later), and can
use your SPAN account over the cloud as a fallback. Panels on older firmware, or
that you'd rather not register locally, can still be added over the cloud alone.

Requires Home Assistant 2024.12.0 or newer.

## Install

1. In HACS, add this repository as a **custom repository** (category:
   Integration), or install it if listed.
2. **Restart Home Assistant.**
3. **Settings → Devices & Services → Add Integration → "SPAN Panel (eBus)"**,
   then choose how to add it.

### A panel on my network (recommended)

Panels found on your network are listed; otherwise enter the panel's address.
Enter the panel's passphrase from the SPAN app. It is used once to register Home
Assistant with the panel and is never stored or logged.

You're then asked whether to sign in to your SPAN account as a **cloud
fallback** (recommended) or stay **local only**. You can change this later under
Connection (see below).

### My SPAN account, over the cloud

Sign in with your SPAN account email and password. Your password is used
**once** to obtain access tokens and is never stored. Only the tokens are kept;
they refresh automatically, and a re-auth prompt appears if they ever fully
expire. Setup takes up to a minute: it waits for the first live reading before
finishing, so a problem shows up as an error rather than an entry with no
entities. With several panels on the account, you pick which ones this Home
Assistant runs.

### What you get

One device per panel, updated about once a second:

- **each circuit**, named as in the SPAN app, with power, current and energy, and
  a breaker **switch** and **shed priority** where the panel allows control (local);
- **the panel**: power, line frequency, per-leg voltage and per-leg current
  (L1/L2), plus a combined current that is the *higher* of the two legs, i.e.
  busbar loading to compare against your main breaker;
- **panel overhead**: the main feed's power less the sum of every circuit, i.e.
  the steady draw of the panel's own electronics, with an energy total;
- **the main feed** and the **site flows**: grid, home, solar and battery power
  and energy;
- on a local panel, **status sensors** (door, Ethernet, Wi-Fi, power control),
  the shed forecast where published, and **battery, solar, EV charger and
  grid-isolation devices** linked to the panel (untested live);
- a diagnostic **Transport** sensor showing `local`, `cloud` or `offline`.

Readings are rounded and written only when they change, to keep the recorder
database small.

"Grid" means what feeds this panel: the power at its main feed. If solar or
storage is installed upstream of the panel, it is not your utility import (see
[Topology](#topology)).

Circuit entity ids are keyed on SPAN's internal circuit identifier rather than its
label or panel space, so renaming a circuit in the SPAN app keeps its history.

There is no YAML and no environment variables. Optional settings are under
**Configure** (see below).

## Moving a cloud entry to the local API

If you already run the panel over the cloud, don't add it again: open the cloud
entry's **Configure → Link the local panel**, and enter the panel's address and
passphrase. The cloud account stays as the fallback.

Linking keeps your entities, entity ids, history, statistics and dashboards:

- each entity that has a local reading moves onto it (circuit power, current,
  energy and switches; panel voltage, frequency, current and overhead; feed
  power and energy);
- site totals (grid, home, solar, battery, voltage, frequency and their energy)
  are kept and fed from the panel;
- SPAN's directional site flows (grid to home, solar to grid, battery to grid
  and the like), generator, balance and residual have no local reading: they
  update only while the cloud is carrying the panel;
- a circuit the panel doesn't report stops updating.

The confirmation screen counts each group before anything changes. Afterwards,
two repairs may appear: one fixes the one-time jump between the last cloud
energy reading and the first local one, and one offers to remove old cloud
entities that no longer update (their statistics stay).

**Energy dashboard:** entries keep working, except any that use the directional
site flows above (for example *solar to grid energy* as return to grid). Swap
those for the panel's feed energy sensors. On a panel with something other than
the utility meter feeding it, the grid sensors are renamed (see
[Topology](#topology)); their ids don't change.

It works the other way round too: a panel added on your network can sign in to
your SPAN account as a fallback under **Connection**, and Home Assistant matches
it to the account's panel without creating new entities. That direction, and
picking the account's panel by hand when the serials differ, are untested live.

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

Press **Analyze leg balance** on the panel's device page (under Controls). The
result appears in Notifications and replaces the previous one.

It reads up to 28 days of recorded circuit power for a panel added over its
local API and changes nothing. The notification shows:

- how much history it used;
- whether the leg of each space was checked against the panel's own lug
  currents: confirmed, flipped (the legs are the other way round, and the
  suggestions allow for it), unconfirmed or unchecked. Unless it says
  confirmed, check the leg of each space by hand;
- the typical and high (95th percentile) difference between the legs in amps,
  and which leg is heavier;
- up to three single-pole breaker moves or swaps, each with the high and
  typical difference before and after;
- how many circuits it left out (two-pole circuits, shared spaces) and how many
  it counted but never moves (protected circuits, battery or solar, circuits
  feeding or fed by another panel, circuits measured by a remote CT, and
  circuits with too little history).

With under about 4 hours of recorded data there's no result at all. Suggestions
need 72 hours of history; with less, it shows the imbalance only.

These are suggestions only. Moving a breaker means working inside the panel:
have a licensed electrician check and do any move.

### Advanced: the action

The `span_ebus.analyze_leg_balance` action runs the same analysis and returns
the full result. Use it for a different window or thresholds, or to use the
result in a script or card. Run it from **Developer tools > Actions** with
"Return response" on:

```yaml
action: span_ebus.analyze_leg_balance
data:
  config_entry_id: <your entry>   # or device_id; optional with one local panel
  days: 28                        # 7 to 90
```

The response holds the same fields as the notification, plus each suggestion's
circuit and spaces, the empty spaces, and the reason each left-out circuit was
left out. The mapping check is reported as `confirmed`, `flipped`,
`unconfirmed` or `unchecked`.

## Connection (local panels)

**Settings → Devices & services → SPAN Panel (eBus) → Configure → Connection**
chooses how Home Assistant reaches a panel added over its local API:

- **Local, with cloud fallback** (the default): your SPAN account is used for two
  things only.
  - **Failover:** if the local link is down for a minute (or silent for two),
    readings switch to the cloud, and switch back after five minutes of a
    healthy local link. Controls are unavailable while on the cloud, and a
    repair appears if it stays on the cloud for over an hour.
  - **Backfill:** gaps of five minutes or more in the home, solar and battery
    energy totals, from the last 7 days (such as after a Home Assistant restart),
    are filled from the cloud's history. This works on single-panel sites;
    circuit energy is not backfilled.

  Choosing it asks you to sign in to your SPAN account.
- **Local only:** your SPAN account is removed from Home Assistant for this
  panel. There is no failover and no backfill. Entities and their history stay.

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

**Locally**, Home Assistant registers with the panel once using its passphrase,
pins the panel's certificate authority, and subscribes to the panel's
Electrification Bus (Homie 5 over MQTT on port 8883). A panel that moves to a new
address is followed when it is rediscovered, and **Reconfigure** changes the
address by hand.

**Over the cloud**, SPAN's realtime telemetry arrives on an Ably channel named
`c:<userId>:<deviceUUID>`. The `deviceUUID` is a **client identifier we generate
ourselves**; what starts the data flowing is the `SubscribeAndGetTraits` RPC,
which registers our channel as a subscriber for the panel's hardware resources.
See [docs/CLOUD-FLOW.md](docs/CLOUD-FLOW.md) §3.

## Layout

```
custom_components/span_ebus/
  __init__.py        config entry setup / unload
  config_flow.py     setup, linking, options and re-auth flows
  coordinator.py     stream lifecycle, entity discovery, state
  transport.py       local-first transport with cloud failover
  backfill.py        cloud history backfill for energy gaps
  sensor.py, switch.py, select.py, number.py, lock.py, binary_sensor.py,
  button.py          entity platforms
  repairs.py, legacy_repair.py   energy-jump and stale-entity repairs
  leg_balance.py, services.py    leg balance and credential rotation
  logbook.py         logbook lines for control commands
  span_client/       the panel data layer (no Home Assistant imports)
    backend.py       CloudBackend: stream lifecycle, traits, commands
    cloud_*.py       Cognito auth, gRPC, protobuf, Ably SSE, telemetry, history
    local/           local API: onboarding, TLS, MQTT, Homie, commands
    identity.py, link.py, standin.py, legacy.py   cloud-to-local linking
    models.py        normalized panel / node / property / reading types
tests/               run against span_client, i.e. against what ships
docs/CLOUD-FLOW.md   the cloud path, end to end
docs/CLOUD-PROTO.md  protobuf shapes for the gRPC calls
docs/FINDINGS.md     empirical results from this panel
```

## References

- [SPAN API Client Docs](https://github.com/spanio/SPAN-API-Client-Docs) — the local API, spec `r202639`
- [Griswoldlabs/span-panel-ha](https://github.com/Griswoldlabs/span-panel-ha) — the gRPC approach, now defunct
- [SpanPanel/span](https://github.com/SpanPanel/span) — Gen2 REST integration
