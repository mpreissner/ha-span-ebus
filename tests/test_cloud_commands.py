"""Tests for the relay command builder.

These assert on exact bytes. That is deliberate: the message goes to a live
electrical panel, there is no public `.proto` to validate against, and a field
number silently drifting would mean commanding the wrong thing — most of these
assertions exist to pin the structure recovered in
docs/specs/circuit-control.md rather than to exercise logic.
"""

from span_client import cloud_pb as pb
from span_client.cloud_commands import (
    CONTROL_SOURCE_USER_COMMAND,
    DEFAULT_TIMEOUT_MS,
    TRAIT_SWITCH_LOAD_MANAGEMENT,
    SwitchTarget,
    build_send_messages,
    build_switch_request,
    build_trait_message,
    disconnect_payload,
    release_payload,
    switch_payload,
)

TARGET = SwitchTarget(
    resource_id="a1b2c3d4e5f60718",
    instance_id=42,
    metadata=(1, None, TRAIT_SWITCH_LOAD_MANAGEMENT, 1),
)
REQUEST_ID = "00000000-0000-4000-8000-000000000000"
# The caller's SPAN user id — not the panel, which merely owns the trait.
REQUESTER = "00112233445566778899aabbccddeeff"


def test_off_is_a_disconnect_request_with_a_user_control_source():
    payload = pb.parse(disconnect_payload())
    request = payload.get_msg(1)
    assert request is not None, "off must go out as field #1, DisconnectSwitchRequest"
    assert payload.get_msg(2) is None

    reason = request.get_msg(1)
    # Only control_source (#3) is populated; the app leaves the other two unset,
    # and a disconnect attributed to load management would not be ours to release.
    assert reason.get_uint(3) == CONTROL_SOURCE_USER_COMMAND
    assert reason.get_int_opt(1) is None
    assert reason.get_int_opt(2) is None


def test_on_is_a_release_not_a_force_close():
    # Releasing our own disconnect leaves the panel's reasons (backup reserve, a
    # minimum reconnect time) in charge. An override would overrule them, and the
    # app only ever sends one from its outage warning sheet.
    payload = pb.parse(release_payload())
    assert payload.get_msg(2) is not None
    assert payload.get_msg(1) is None
    assert payload.get_msg(3) is None, "we must never build an override request"


def test_switch_payload_maps_closed_to_release_and_open_to_disconnect():
    assert switch_payload(closed=True) == release_payload()
    assert switch_payload(closed=False) == disconnect_payload()


def test_the_off_payload_is_the_expected_six_bytes():
    # 0a 04 (request #1, 4 bytes) 0a 02 (reason #1, 2 bytes) 18 05 (source #3 = 5).
    assert disconnect_payload().hex() == "0a040a0218" + f"{CONTROL_SOURCE_USER_COMMAND:02x}"


def test_the_requester_is_the_user_not_the_panel_that_owns_the_trait():
    # The live failure this pins: naming the panel (or the site) as the requester
    # earns `PERMISSION_DENIED [Validation Error]: Requester <id>, does not
    # contain <userId>`. Only the caller's own user id passes, so the message
    # carries two different resource ids and they must not be crossed.
    msg = pb.parse(build_trait_message(TARGET, disconnect_payload(), requester_id=REQUESTER))
    assert msg.get_msg(2).get_msg(1).get_str(1) == TARGET.resource_id
    assert msg.get_msg(14).get_msg(1).get_msg(2).get_str(1) == REQUESTER
    assert TARGET.resource_id != REQUESTER


def test_trait_message_addresses_the_instance_three_times_over():
    msg = pb.parse(
        build_trait_message(
            TARGET, disconnect_payload(), requester_id=REQUESTER, request_id=REQUEST_ID
        )
    )

    metadata = msg.get_msg(1)
    assert metadata.get_uint(1) == 1  # vendor
    assert metadata.get_int_opt(2) is None  # product_id: absent in the snapshot
    assert metadata.get_uint(3) == TRAIT_SWITCH_LOAD_MANAGEMENT
    assert metadata.get_uint(4) == 1  # version

    instance = msg.get_msg(2)
    assert instance.get_msg(1).get_str(1) == TARGET.resource_id
    assert instance.get_msg(2).get_uint(1) == TARGET.instance_id

    # The command block names the requester and the request id the app would
    # match its Ably response on.
    command = msg.get_msg(14)
    request_metadata = command.get_msg(1)
    assert request_metadata.get_msg(2).get_str(1) == REQUESTER
    assert request_metadata.get_msg(3).get_str(1) == REQUEST_ID
    assert request_metadata.get_uint(4) == DEFAULT_TIMEOUT_MS
    # The app sends no timestamp, so neither do we.
    assert request_metadata.get_msg(1) is None

    assert command.get_msg(2).get_bytes(1) == disconnect_payload()


def test_product_id_is_included_when_the_snapshot_declares_one():
    # Entry metadata omits product_id today, but internal TraitRefs carry it at
    # #4=... so the field is real; echoing whatever we were given means a server
    # that starts requiring it needs no code change.
    target = SwitchTarget(resource_id="hw", instance_id=7, metadata=(1, 4, 31, 1))
    metadata = pb.parse(
        build_trait_message(target, disconnect_payload(), requester_id=REQUESTER)
    ).get_msg(1)
    assert metadata.get_uint(2) == 4


def test_request_ids_are_unique_per_call():
    first = build_trait_message(TARGET, disconnect_payload(), requester_id=REQUESTER)
    second = build_trait_message(TARGET, disconnect_payload(), requester_id=REQUESTER)
    assert first != second, "a retry must be distinguishable from a duplicate"


def test_send_messages_wraps_each_message_in_field_one():
    request = pb.parse(build_send_messages(b"\x08\x01", b"\x08\x02"))
    assert list(request.values(1)) == [b"\x08\x01", b"\x08\x02"]


def test_build_switch_request_is_one_message_ready_to_post():
    request = pb.parse(
        build_switch_request(TARGET, closed=False, requester_id=REQUESTER, request_id=REQUEST_ID)
    )
    messages = request.get_msgs(1)
    assert len(messages) == 1
    payload = messages[0].get_msg(14).get_msg(2).get_bytes(1)
    assert payload == disconnect_payload()
