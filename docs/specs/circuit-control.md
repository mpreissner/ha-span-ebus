# Spec — circuit control (relay on/off) over the SPAN cloud

**Status:** implemented, pending a live write test on a benign circuit.
**Scope:** turn a detected breaker on or off from Home Assistant, and report the
relay's current state, using the same cloud path the mobile app uses.

## 1. What the app actually does

Recovered statically from the SPAN Home Android bundle (Hermes bytecode; see
[CLOUD-PROTO.md](../CLOUD-PROTO.md) for the method). The breaker-details screen's
toggle does exactly two things:

| tap | analytics event | request constant |
|---|---|---|
| off | `RELAY_TOGGLED_OFF` | `DISCONNECT_SWITCH_REQUEST` |
| on | `RELAY_TOGGLED_ON` | `RELEASE_DISCONNECT_SWITCH_REQUEST` |

Both constants are `SwitchLoadManagementTrait.SwitchLoadManagementCommandRequests`
values built with `controlSource: ControlFunctionSource.USER_COMMAND`.
`OVERRIDE_DISCONNECT_SWITCH_REQUEST` exists but is reached only from the
`BreakerSwitchOnOffWarning` sheet during a power outage / backup-priority hold —
**plain on/off never sends an override**, and neither do we.

There is no per-trait RPC. Every trait command goes out on one method:

```
POST https://mobilefrontend.prd.span.io
     /io.span.services.mobilefrontend.MobileFrontendService/SendMessages
```

`TraitService.sendCommandRequest` builds one `TraitMessage`, registers the
`request_id` in a local table, sends `SendMessagesRequest { 1: [msg] }`, and then
**waits for the answer on the Ably trait channel**, polling its own table every
`COMMAND_CHECK_INTERVAL_MS = 100` up to `DEFAULT_TIMEOUT_MS = 30000`. The HTTP
response itself is an ack — the recovered schema has no `SendMessagesResponse`.

## 2. Wire format

```
SendMessagesRequest { 1 msgs[] : TraitMessage }

TraitMessage {
  1 trait_metadata   : TraitMetadata    { 1 vendor_id, 2 product_id, 3 trait_id, 4 version }
  2 instance_metadata: InstanceMetadata { 1 resource_id{1 id}, 2 trait_instance_id{1 id} }
  14 command_request : CommandRequest {
       1 request_metadata: RequestMetadata { 2 resource_id{1 id},
                                             3 request_id{1 id},
                                             4 client_timeout_duration_msec }
       2 payload        : TraitCommandRequestPayload { 1 payload bytes }
     }
}
```

The app sets no `RequestMetadata.time_stamp` (#1) and generates `request_id` as a
UUID v4. `client_timeout_duration_msec` is 30000.

The payload bytes are a `SwitchLoadManagementCommandRequests`:

```
off: { 1: DisconnectSwitchRequest        { 1: DisconnectReason { 3: control_source } } }
on:  { 2: ReleaseDisconnectSwitchRequest { 1: DisconnectReason { 3: control_source } } }
```

`DisconnectReason` has three fields (`1 control_function_source`,
`2 manual_control_source`, `3 control_source`); the app populates **only #3**,
with `ControlFunctionSource.USER_COMMAND = 5`. So the entire payload for "off" is
six bytes, `0a 04 0a 02 18 05` — see `cloud_commands.py`.

Note the resource id appears twice under different field numbers: `#1` of
`InstanceMetadata`, `#2` of `RequestMetadata` (whose `#1` is the unset timestamp).

Trait ids: **`SwitchLoadManagementTrait` is 1/31**, and its `trait_instance_id` is
the *same* 30..56 id the telemetry frames and `CircuitBreakerTrait` (1/15) use, so
a circuit that reports power as instance 42 is switched as instance 42.
`trait_metadata` is echoed verbatim from the snapshot entry that declared the
instance (vendor 1, trait 31, version 1 — no `product_id`, which the snapshot
omits); `resource_id` is the hardware id of the resource block the entry came
from, i.e. the panel, not the site.

### Responses (not consumed yet)

`SwitchLoadManagementCommandResponses { 1 disconnect | 2 release | 3 override |
4 get_switch_event }`, each carrying `disconnect_reasons`, `switch_state`, a
status enum, and a `ticket_number` that `GetSwitchEventRequest` can poll:

- `DisconnectStatus`: 1 DISCONNECTED, 2 ALREADY_DISCONNECTED, 3 ALWAYS_ON_CIRCUIT,
  4 MINIMUM_RECONNECT_TIME, 5 GRANTED_PENDING
- `ReleaseDisconnectStatus`: 1 RECONNECTED, 2 ALREADY_RECONNECTED,
  3 OTHER_DISCONNECT_REASONS, 4 MINIMUM_DISCONNECT_TIME, 5 GRANTED_PENDING

These arrive on the Ably **trait** channel keyed by `request_id`. Our reader only
decodes telemetry push frames, so we do not see them. That is acceptable because
the same information is observable in the trait snapshot (§3) — but it is why a
rejected command (`ALWAYS_ON_CIRCUIT`, `MINIMUM_RECONNECT_TIME`) surfaces to the
user as "the state snapped back" rather than as an error. Decoding the trait
channel is future work.

## 3. Where relay state comes from

The trait snapshot from `SubscribeAndGetTraits` — already fetched on every
(re)connect for circuit labels — carries one 1/31 entry per breaker:

```
1 { 1 TraitRef -> 1/15 same instance
    2 config { … thresholds … }
    3 switch_state
    5 last_disconnect_msec { 1 utc }
    6 last_reconnect_msec  { 1 utc } }
```

`SwitchState { 0 UNSPECIFIED, 1 UNKNOWN, 2 OPEN, 3 CLOSED }`, recovered from the
bundle's enum construction. **CLOSED = energized**, which matches the `relay`
property the ebus/Homie path already publishes (`OPEN`/`CLOSED`), so the
normalized model needs no new vocabulary and `discovery.component_for` already
maps a settable `relay` to an HA switch with `payload_on = "CLOSED"`.

Telemetry frames never carry switch state, so state is refreshed by re-issuing
`SubscribeAndGetTraits` (a ~22 KB call) on a timer, `SWITCH_REFRESH_SECONDS = 60`,
and again a few seconds after we send a command so a rejection or a slow relay
converges instead of lying.

## 4. Implementation

| piece | file |
|---|---|
| payload + `SendMessagesRequest` builders | `src/span_bridge/cloud_commands.py` (new) |
| `SendMessages` RPC wrapper | `src/span_bridge/cloud_grpc.py` |
| `switch_state` + command addressing from the snapshot | `src/span_bridge/cloud_traits.py` |
| `relay` property, state readings, `send_command`, refresh timer | `src/span_bridge/backends/cloud.py` |
| HA switch entities | `custom_components/span_ebus/switch.py` (new) + `coordinator.py` |
| vendored mirror | `custom_components/span_ebus/span_client/` |

`cloud_traits.CircuitInfo` gains `relay_closed: bool | None` and
`switch: SwitchTarget | None` (resource id, instance id, trait metadata). That
required `_index_traits` to stop discarding the resource id and the entry's trait
metadata, which it previously reduced to `(vendor, trait, instance)`.

A circuit only advertises a `relay` property when the snapshot gave us a
`SwitchTarget` for it, so a panel whose snapshot we cannot read keeps working
read-only instead of exposing switches that would fail on use.

`send_command("circuit-42/relay", "OPEN"|"CLOSED")` — also accepting
`true/false/on/off/1/0` — builds and posts the message. It raises `GrpcError` on a
transport-level rejection, which the bridge logs and HA surfaces as a failed
service call.

## 5. Safety

Circuit control writes to a live electrical panel. Constraints held throughout:

- **Only the two user-command requests are ever sent.** No override, no
  set-backup-config, no shed policy.
- The feeder / main-feed node (`feed-*`) and the panel node get no relay
  property: `SwitchTarget`s come only from 1/31 entries, and the panel has none.
- The first live test must be a single, explicitly agreed-upon benign circuit
  (never a feeder or a critical load), toggled off and back on.
- No captured payload, resource id, serial, or real circuit label is committed;
  fixtures are synthesized with the protobuf writer.
