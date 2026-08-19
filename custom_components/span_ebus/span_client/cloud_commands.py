"""Build the trait command that opens or closes a circuit's relay.

SPAN has no per-trait RPC. Every trait command — a breaker toggle included — goes
out on one method, `MobileFrontendService/SendMessages`, as a `TraitMessage`
addressed to a trait instance. The mobile app's breaker screen sends exactly two
of them (recovered from the app bundle; see docs/specs/circuit-control.md):

    off -> SwitchLoadManagementCommandRequests { 1: DisconnectSwitchRequest }
    on  -> SwitchLoadManagementCommandRequests { 2: ReleaseDisconnectSwitchRequest }

both carrying `DisconnectReason { 3: control_source = USER_COMMAND }`. "On" is a
*release* of our own disconnect rather than a force-close: the panel may hold a
circuit open for its own reasons (backup reserve, load shed, a minimum
reconnect time), and releasing leaves those in charge. The app's third request,
`override_disconnect_switch_request`, is reached only from its power-outage
warning sheet, so it is deliberately not built here.

The HTTP reply to `SendMessages` is an ack — the recovered schema has no
`SendMessagesResponse`. The app matches the real answer by `request_id` on its
Ably *trait* channel; we instead re-read the trait snapshot, which reports
`switch_state` directly (`cloud_traits`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from .cloud_pb import field_message, field_string, field_varint

# SwitchLoadManagementTrait. Its instances share the ids CircuitBreakerTrait
# (1/15) and the telemetry frames use, so the circuit that reports power as
# instance 42 is switched as instance 42.
TRAIT_SWITCH_LOAD_MANAGEMENT = 31

# ControlFunctionSource.USER_COMMAND — "a person asked for this", as opposed to
# the panel's own load management. It is what the app sends and it is what makes
# the disconnect ours to release later.
CONTROL_SOURCE_USER_COMMAND = 5

# SwitchLoadManagementCommandRequests oneof tags.
_REQUEST_DISCONNECT = 1
_REQUEST_RELEASE = 2

# The app's DEFAULT_TIMEOUT_MS: how long it tells the panel it will wait.
DEFAULT_TIMEOUT_MS = 30_000


@dataclass(frozen=True)
class SwitchTarget:
    """Everything needed to address one breaker's switch trait.

    `resource_id` is the resource that *owns* the trait instance — the panel.
    It is not who is asking: that is the caller's user id, which
    `build_trait_message` takes separately as `requester_id`.

    `metadata` is the trait's `(vendor_id, product_id, trait_id, version)` as the
    snapshot declared it, echoed back verbatim rather than reconstructed — the
    snapshot omits `product_id` for this trait, and inventing one would be a
    guess about a field the server is authoritative on.
    """

    resource_id: str
    instance_id: int
    metadata: tuple[int, int | None, int, int | None]

    @property
    def trait_id(self) -> int:
        return self.metadata[2]


def disconnect_payload(control_source: int = CONTROL_SOURCE_USER_COMMAND) -> bytes:
    """`SwitchLoadManagementCommandRequests` asking for the relay to open."""
    return field_message(_REQUEST_DISCONNECT, _disconnect_reason(control_source))


def release_payload(control_source: int = CONTROL_SOURCE_USER_COMMAND) -> bytes:
    """`SwitchLoadManagementCommandRequests` releasing our disconnect."""
    return field_message(_REQUEST_RELEASE, _disconnect_reason(control_source))


def switch_payload(closed: bool) -> bytes:
    """The payload that leaves the relay `closed` (i.e. energized) or open."""
    return release_payload() if closed else disconnect_payload()


def _disconnect_reason(control_source: int) -> bytes:
    """`{ 1: DisconnectReason { 3: control_source } }`.

    Field #1 of both request messages is the reason; the app populates only the
    reason's field #3, leaving `control_function_source` and
    `manual_control_source` unset.
    """
    return field_message(1, field_varint(3, control_source))


def build_trait_message(
    target: SwitchTarget,
    payload: bytes,
    *,
    requester_id: str,
    request_id: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> bytes:
    """One `TraitMessage` carrying `payload` as a command to `target`.

        TraitMessage {
          1  trait_metadata    { 1 vendor, 2 product, 3 trait, 4 version }
          2  instance_metadata { 1 resource_id{1 id}, 2 trait_instance_id{1 id} }
          14 command_request   { 1 request_metadata { 2 resource_id{1 id},
                                                      3 request_id{1 id},
                                                      4 client_timeout_msec },
                                 2 payload { 1 bytes } }
        }

    The two `resource_id`s are **not** the same resource, which is the trap here:

    - `InstanceMetadata`'s (#1) is the resource the trait instance lives on — the
      panel's hardware id.
    - `RequestMetadata`'s (#2) is the *requester*: who is asking. SPAN's user ids
      are resource ids too, and the server checks that the named resource
      contains the caller, so the only value that passes is the caller's own
      **user id** (the access token's `username` claim, and the `<userId>` in the
      Ably channel `c:<userId>:<deviceUUID>`).

    Naming a panel or a site here is rejected with `PERMISSION_DENIED
    [Validation Error]: Requester <id>, does not contain <userId>` — established
    against the live service by sending each candidate at a nonexistent trait
    instance, where only the user id was accepted.

    `request_id` is a UUID the response is keyed on; we generate one per call so
    a retry is distinguishable from a duplicate.
    """
    vendor, product, trait, version = target.metadata

    metadata = field_varint(1, vendor)
    if product is not None:
        metadata += field_varint(2, product)
    metadata += field_varint(3, trait)
    if version is not None:
        metadata += field_varint(4, version)

    instance = field_message(1, field_string(1, target.resource_id)) + field_message(
        2, field_varint(1, target.instance_id)
    )

    # #1 of RequestMetadata is the timestamp the app never sets; the requester
    # goes at #2.
    request_metadata = (
        field_message(2, field_string(1, requester_id))
        + field_message(3, field_string(1, request_id or str(uuid.uuid4())))
        + field_varint(4, timeout_ms)
    )
    command = field_message(1, request_metadata) + field_message(2, field_message(1, payload))

    return field_message(1, metadata) + field_message(2, instance) + field_message(14, command)


def build_send_messages(*messages: bytes) -> bytes:
    """`SendMessagesRequest { 1: msgs[] }` — the envelope the RPC takes."""
    return b"".join(field_message(1, msg) for msg in messages)


def build_switch_request(
    target: SwitchTarget,
    closed: bool,
    *,
    requester_id: str,
    request_id: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> bytes:
    """A ready-to-post `SendMessages` body that opens or closes one relay."""
    return build_send_messages(
        build_trait_message(
            target,
            switch_payload(closed),
            requester_id=requester_id,
            request_id=request_id,
            timeout_ms=timeout_ms,
        )
    )
