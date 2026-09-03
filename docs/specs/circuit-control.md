# Spec — circuit control (relay on/off) over the SPAN cloud

**Status:** implemented and **confirmed end-to-end**. The envelope, the auth and
the requester were first proven against production by addressing trait instance
999 — an id no panel has — so nothing could move a relay. On 2026-09-03 a real
breaker was toggled from the SPAN app while capturing, which gave us both halves
of the exchange for real: the request the app sends (§6) and the *response* it
gets back (§7). Re-validated against SPAN's new React Native app; no change to
what we send is required.
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

Note a `resource_id` appears twice under different field numbers, and the two
name **different things**. `#1` of `InstanceMetadata` is the resource that owns
the trait instance (the panel). `#2` of `RequestMetadata` (whose `#1` is the
unset timestamp) is the *requester* — who is asking — and must be the caller's
own user id; see "Requester" below.

Trait ids: **`SwitchLoadManagementTrait` is 1/31**, and its `trait_instance_id` is
the *same* 30..56 id the telemetry frames and `CircuitBreakerTrait` (1/15) use, so
a circuit that reports power as instance 42 is switched as instance 42.
`trait_metadata` is echoed verbatim from the snapshot entry that declared the
instance (vendor 1, trait 31, version 1 — no `product_id`, which the snapshot
omits); `InstanceMetadata.resource_id` is the hardware id of the resource block
the entry came from, i.e. the panel, not the site.

### Requester — `RequestMetadata.resource_id`

Discovered from a live failure, not from the app bundle. Sending the panel's
hardware id here — the obvious reading, since the same value is correct in
`InstanceMetadata` — is rejected:

```
PERMISSION_DENIED (7): [Validation Error]: Requester <panel-hw-id>,
does not contain <userId>
```

`<userId>` is the `username` claim of the Cognito access token (**not** `sub`,
which is a different UUID) — the same `<userId>` in the Ably channel name
`c:<userId>:<deviceUUID>`. It never appears in any response body, so the server
resolved it from the bearer token. Read the message as: *the resource you named
does not contain the calling user.*

Which resource does contain the user? Established empirically against the live
service, aimed at trait instance 999 — an id this panel does not have, so no
breaker could move whatever the answer turned out to be:

| requester sent | result |
| --- | --- |
| panel hardware id | `PERMISSION_DENIED` |
| site hardware id | `PERMISSION_DENIED` |
| site resource UUID (dashed and stripped) | `PERMISSION_DENIED` |
| panel resource UUID (dashed and stripped) | `PERMISSION_DENIED` |
| panel device UUID | `PERMISSION_DENIED` |
| **the caller's user id** | **accepted** |

The same run also confirmed that `InstanceMetadata.resource_id` has no bearing
on the check: crossing the two ids changed which value the error quoted, and it
always quoted `RequestMetadata`'s.

So SPAN models the user as a resource, and a command is signed with it: the only
thing that "contains" the user is the user. `cloud_auth.user_id_from_token`
reads the claim, and `span_client/backend.py` calls it per command rather than
caching a configured value, so the requester always matches the token the same
request authenticates with.

### Responses (not consumed yet)

`SwitchLoadManagementCommandResponses { 1 disconnect | 2 release | 3 override |
4 get_switch_event }`, each carrying `disconnect_reasons`, `switch_state`, a
status enum, and a `ticket_number` that `GetSwitchEventRequest` can poll:

- `DisconnectStatus`: 1 DISCONNECTED, 2 ALREADY_DISCONNECTED, 3 ALWAYS_ON_CIRCUIT,
  4 MINIMUM_RECONNECT_TIME, 5 GRANTED_PENDING
- `ReleaseDisconnectStatus`: 1 RECONNECTED, 2 ALREADY_RECONNECTED,
  3 OTHER_DISCONNECT_REASONS, 4 MINIMUM_DISCONNECT_TIME, 5 GRANTED_PENDING

Recovered statically, this section assumed responses arrived on a separate Ably
"trait" channel we do not subscribe to. **That was wrong** — see §7: they arrive
on `c:<userId>:<deviceUUID>`, the same channel our telemetry reader is already
attached to, keyed by `request_id`. We still do not *decode* them, so a rejected
command (`ALWAYS_ON_CIRCUIT`, `MINIMUM_RECONNECT_TIME`) still surfaces to the user
as "the state snapped back" rather than as an error — but the cost of fixing that
is now a decoder branch, not a second subscription.

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
normalized model needs no new vocabulary and `switch.py` turns a settable
`relay` into an HA switch entity whose "on" is `CLOSED`.

Telemetry frames never carry switch state, so state is refreshed by re-issuing
`SubscribeAndGetTraits` (a ~22 KB call) on a timer, `SWITCH_REFRESH_SECONDS = 60`,
and again a few seconds after we send a command so a rejection or a slow relay
converges instead of lying.

## 4. Implementation

Paths are relative to `custom_components/span_ebus/`, which is the whole of
the code — there is no second copy.

| piece | file |
|---|---|
| payload + `SendMessagesRequest` builders | `span_client/cloud_commands.py` (new) |
| `SendMessages` RPC wrapper | `span_client/cloud_grpc.py` |
| `switch_state` + command addressing from the snapshot | `span_client/cloud_traits.py` |
| `relay` property, state readings, `send_command`, refresh timer | `span_client/backend.py` |
| HA switch entities | `switch.py` (new) + `coordinator.py` |

`cloud_traits.CircuitInfo` gains `relay_closed: bool | None` and
`switch: SwitchTarget | None` (resource id, instance id, trait metadata). That
required `_index_traits` to stop discarding the resource id and the entry's trait
metadata, which it previously reduced to `(vendor, trait, instance)`.

A circuit only advertises a `relay` property when the snapshot gave us a
`SwitchTarget` for it, so a panel whose snapshot we cannot read keeps working
read-only instead of exposing switches that would fail on use.

`send_command("circuit-42/relay", "OPEN"|"CLOSED")` — also accepting
`true/false/on/off/1/0` — builds and posts the message. It raises `GrpcError` on a
transport-level rejection, which the client logs and HA surfaces as a failed
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

## 6. Live request, captured (2026-09-03)

The 2026-09 SPAN app toggling a real breaker, decoded from the wire. This is the
first capture of an actual relay command; everything before it was reconstructed
from the app bundle and probed at instance 999. The shape in §2 holds exactly —
`TraitMessage{1 trait_metadata, 2 instance_metadata, 14 command_request}`, 174
bytes on the wire, `SendMessages` answering with a 5-byte empty frame (a bare
gRPC length prefix and nothing else), which settles that the HTTP reply really is
an ack with no `SendMessagesResponse` behind it.

```
#1  trait_metadata    { 1 vendor=1, 3 trait=31 }          ← 1/31, no product_id, NO VERSION
#2  instance_metadata { 1 resource{1 "<panel-hardware-id>"}, 2 instance{1 33} }
#14 command_request {
      1 request_metadata { 2 resource{1 "c:<userId>:<deviceUUID>"}   ← requester
                           3 request_id{1 "<uuid-v4>"}
                           4 client_timeout_duration_msec = 43000 }
      2 payload { 1 <6 bytes> }                            ← off: 0a 04 0a 02 18 05
    }
```

Three cosmetic deltas from what we build, all confirmed immaterial by the probe
below:

| field | we send | new app sends |
|---|---|---|
| `TraitMetadata.version` (#4) | `1` | omitted |
| `client_timeout_duration_msec` | `30000` | `43000` |
| requester `resource_id` | bare `<userId>` | `c:<userId>:<deviceUUID>` |

The third one is the interesting one, because it explains the error message. §2
records the rejection as *"Requester X, does not contain `<userId>`"*, and reads
it as "the resource you named does not contain the calling user". The app's form
passes that check for the most literal reason available: the channel name
**contains the user id as a substring**. A bare user id contains itself, so both
forms pass. That is a containment test, not an identity test.

### The re-run probe (2026-09-03)

Same instance-999 technique, so no relay could move, now with negative controls —
without them a run where everything passes proves only that the check is gone.

| | requester | version / timeout | result |
|---|---|---|---|
| **A** | bare `<userId>` — *what we send today* | 1 / 30000 | `grpc-status=0` **accepted** |
| **B** | `c:<userId>:<deviceUUID>` — *what the new app sends* | — / 43000 | `grpc-status=0` **accepted** |
| **C** | bare `<userId>` | — / 43000 | `grpc-status=0` **accepted** |
| **D** | panel hardware id — *negative control* | — / 43000 | `grpc-status=7` PERMISSION_DENIED |
| **E** | the token's `sub` claim — *negative control* | — / 43000 | `grpc-status=7` PERMISSION_DENIED |

Both controls were refused with the identical message shape recorded in August,
so the check is still live and still discriminating; it simply accepts both
forms, and neither the version field nor the timeout value affects acceptance.
**`cloud_commands.py` needs no change.** E is worth keeping as a control
specifically because `sub` is the plausible wrong answer — it is a real claim in
the same token, and it fails.

> **Methodology note, or you will misread your own results.** On **rejection**
> the server sends a trailers-only response, putting `grpc-status` in the
> *initial* headers where httpx can see it. On **success** the status arrives in
> genuine HTTP/2 trailers, which httpx does not surface at all — so a passing
> probe looks like an uninformative HTTP 200 with an empty body, exactly like a
> probe that did nothing. The first run of this was read wrong for that reason.
> Route probes through the local proxy (mitmproxy records trailers) or use a raw
> h2 client. Failures need neither.

## 7. Live response, captured (2026-09-03)

The reply to each command arrives as another `TraitMessage` **on the telemetry
channel** — `c:<userId>:<deviceUUID>`, the one our reader is already attached to
— correcting the assumption in §2 that it needed a separate trait channel. It is
distinguished from a request by the field number: **`#15 command_response`**
where the request carried `#14 command_request`. `request_metadata` is echoed
verbatim, so `request_id` is the correlation key, as the app's own table implies.

```
#1  trait_metadata    { 1 vendor=1, 3 trait=31 }
#2  instance_metadata { 1 resource{1 "<panel-hardware-id>"}, 2 instance{1 33} }
#15 command_response {
      1 request_metadata { … echoed: requester, request_id, timeout … }
      4 payload { 1 SwitchLoadManagementCommandResponses }
    }
```

The payload is the `SwitchLoadManagementCommandResponses` from §2, and the two
observed variants line up with its recovered field numbering:

```
off → 1 disconnect { 1 disconnect_reasons{3 control_source=5}
                     2 switch_state=3 (CLOSED)
                     3 status=5 (GRANTED_PENDING)
                     4 ticket_number }

on  → 2 release   { 2 switch_state=2 (OPEN)
                    3 status=5 (GRANTED_PENDING)
                    4 ticket_number }
```

Read that as: *request granted, relay has not moved yet* — `switch_state` is the
state at the moment of the reply, which is still the pre-command one, and
`GRANTED_PENDING` is the status in both enums' slot 5. `disconnect_reasons` is
present on the disconnect and absent on the release, which is what "release the
disconnect" should mean. It also confirms that a command is genuinely
asynchronous: the ack, the response, and the relay actually moving are three
separate events, which is why §3's snapshot re-read after a command exists.

Caveats worth carrying: this is **one sample of each direction**, from a single
successful toggle. The enum readings come from the bundle-recovered numbering,
not from having seen a rejection — no `ALWAYS_ON_CIRCUIT` or
`MINIMUM_RECONNECT_TIME` response has ever been captured, and the field
assignment `2 = switch_state, 3 = status` is inference from exactly these two
frames. Do not build error handling on it without capturing a real refusal.

**Why we still do not consume it.** State comes from re-reading the snapshot
(§3), which is a coarser but strictly more trustworthy source: it reports where
the relay *is*, not where it was promised to go. Decoding `#15` would buy a
faster and more specific failure message, and is now cheap enough to be worth
doing — but it should wait for a capture of a rejected command, because a
decoder written against two GRANTED_PENDING samples would be guessing at the
exact cases it exists to report.

## 8. Safety — as exercised

The 2026-09 live toggle followed §5: a single benign circuit, agreed in advance,
switched off and back on from SPAN's own app rather than by our code, with the
capture running. The instance-999 probes carry no risk by construction — the
panel has no instance 999, so the relay layer is never reached whatever the
requester check decides. No captured payload, resource id, serial, or real
circuit label is committed; every id above is a placeholder.
