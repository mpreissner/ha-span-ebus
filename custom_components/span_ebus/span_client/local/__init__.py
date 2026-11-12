"""Client for SPAN's official local API (firmware r202639 and later).

- `rest`: the v2 REST API over HTTPS: CA fetch, registration, status, the
  Homie schema, and credential management.
- `tls`: the per-panel CA, pinned in the config entry.
- `mqtt`: an asyncio-driven paho connection to the panel's eBus broker.
- `homie`: the Homie 5 device tree the panel publishes on that broker.
- `health`: when to fail over to the cloud and when to come back.
- `discovery`: which panels found on the network the config flow offers.
- `onboard`: the steps the config flow runs to add a panel.

Local is the preferred transport and the cloud the fallback, which is on by
default. A user who keeps the panel off the internet can choose local-only.
"""
