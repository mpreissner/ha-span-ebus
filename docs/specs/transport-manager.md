# Transport manager (PR 3)

Local spec; never committed. Builds on `local-transport.md` §2.2, §4 and §10.

## Scope

- Entries with a local panel (`CONF_LOCAL_SERIAL`) hold a live broker link
  (`LocalMqtt` + `DeviceTree`) and a health tracker.
- Linked entries (local + cloud tokens) fail over to the cloud and back.
- `local_only` entries never touch the cloud.
- Cloud-only entries are unchanged.
- Out of scope: local entities (PR 4). Until then a linked entry shows only
  the Transport sensor while local is healthy, and cloud entities appear on
  failover. That's acceptable on unreleased dev.

## Health (HA-free: `span_client/local/health.py`)

`LocalHealth(clock)` takes three inputs: `connected(bool)`, `message()` and
`state(DeviceState)` for the root device.

`down(now)` is true when any of these holds:

- the socket has been disconnected for 60 s or more (counted from creation);
- the root has not been READY/ALERT for 60 s or more while connected (this
  widens §2.2's "lost/disconnected" to include init, sleeping, unknown and a
  missing state);
- the root is READY and nothing has arrived for 120 s.

`healthy_since` is when the link last became healthy (connected, root
READY/ALERT, not silent), or None.

`TransportSwitch(mode)` holds the mode and the active transport (LOCAL or
CLOUD). `evaluate(health, now)` returns START_CLOUD, STOP_CLOUD or None:

- LOCAL and `down` → CLOUD (start), unless the mode is `local_only`;
- CLOUD and healthy for 300 s or more → LOCAL (stop);
- `fallback_since` records the time of failover, for the 1-hour repair.

`cloud_only` means no local link at all (a cloud entry), so it never reaches
this code.

## HA wiring (`transport.py`)

`SpanTransportManager` belongs to an entry with a local serial:

- It owns `LocalMqtt` (entry credentials, `client_id=CONF_CLIENT_NAME`, port
  `CONF_MQTTS_PORT`) and feeds a `DeviceTree` and `LocalHealth`. The root is
  the local serial.
- It ticks every 5 s.
- Cloud coordinators are built lazily on the first failover, so an entry boots
  without internet; a cloud failure is logged and retried on a later tick.
- On failback, `coordinator.async_stop()` stops the cloud; on a later failover,
  `async_start()` restarts it.
- When `mqtt.auth_failed` is set, it starts reauth with
  `{**data, "local_reauth": True}` in the context data; `async_step_reauth`
  routes on that marker.
- Repair issue `transport_fallback_<entry>` appears after more than 1 h on the
  cloud, and is deleted on failback or unload.
- One warning is logged on failover and one info on failback.

## Runtime data

`SpanRuntime(coordinators, transport, hooks)`. `add_coordinator()` replays the
platform hooks so late coordinators get entities.

## Transport sensor

- Diagnostic enum sensor: `local`, `cloud` or `offline`.
- `cloud` only while the cloud stream is live.
- Device `{(DOMAIN, local_serial)}`, named after the entry title.
- Only on entries with a local panel.

## Options

Linked entries get a mode form: `local_first` (default) or `local_only`,
stored as `CONF_TRANSPORT_MODE` in options. A change reloads the entry.

## Add-panel discovery

`async_step_local`:

- Gathers candidates from in-progress zeroconf flows of this domain plus a
  short `_ebus._tcp.local.` browse on HA's shared zeroconf (best effort).
- Drops configured hosts and serials.
- Candidates → a select step `local_pick` with a "manual" option.
- None → `local_host` (the address form).
- The pure filter helper lives in `span_client/local/discovery.py`.
