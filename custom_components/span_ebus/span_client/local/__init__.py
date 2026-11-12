"""Client for SPAN's official local API (firmware r202639 and later).

- `rest`: the v2 REST API over HTTPS: CA fetch, registration, status, the
  Homie schema, and credential management.
- `tls`: the per-panel CA, pinned in the config entry.
- `mqtt`: an asyncio-driven paho connection to the panel's eBus broker.
- `homie`: the Homie 5 device tree the panel publishes on that broker.

Not wired into the integration yet; the cloud path in the parent package is
still the only transport. When it is, local becomes the preferred transport
and the cloud path the fallback, which is on by default. A user who keeps the
panel off the internet can choose local-only.
"""
