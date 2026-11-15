# SPAN Panel (eBus)

Live per-circuit data and control for a **SPAN Panel MAIN 40 (Gen3)** in Home
Assistant, over SPAN's official local API (firmware r202639 or later), with your
SPAN account as an optional cloud fallback.

## Setup

1. Install via HACS, then restart Home Assistant.
2. **Settings → Devices & Services → Add Integration → SPAN Panel (eBus)**.
3. Choose **A panel on my network** and enter the passphrase from the SPAN app
   (used once, never stored), then choose cloud fallback or local only. Or
   choose **My SPAN account, over the cloud** and sign in.

Already running the panel over the cloud? Open the entry's **Configure → Link
the local panel**. Your entities, history and dashboards are kept.

## What you get

- A device per panel with each circuit's power, current, energy and breaker
  switch, the panel's voltage, frequency, leg currents and overhead, and the
  site's grid, home, solar and battery flows, updated about once a second.
- Load-shed priority, protected circuits, and battery, solar and EV charger
  devices on local panels.
- An **Analyze leg balance** button that suggests breaker moves to even out the
  two legs.
- Cloud failover when the local link drops, and backfill of energy gaps from
  the cloud's history.

See the README for details.
