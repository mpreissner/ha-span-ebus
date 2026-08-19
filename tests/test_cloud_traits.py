"""Tests for resolving telemetry instance ids to circuit identities.

The snapshot here is synthesized with the protobuf writer to mirror the layout
recovered from a live `SubscribeAndGetTraits` response (docs/CLOUD-PROTO.md).
Labels are placeholders — no captured payload or real circuit name is committed.
"""

from span_client import cloud_pb as pb
from span_client.cloud_commands import SwitchTarget
from span_client.cloud_traits import (
    SWITCH_STATE_CLOSED,
    SWITCH_STATE_OPEN,
    SWITCH_STATE_UNKNOWN,
    TRAIT_CIRCUIT,
    TRAIT_LABEL,
    TRAIT_SPACE,
    TRAIT_SWITCH,
    VENDOR_SPAN,
    CircuitInfo,
    parse_trait_snapshot,
)

HARDWARE_ID = "a1b2c3d4e5f60718"


def _trait_ref(trait: int, instance: int) -> bytes:
    """TraitRef { 1: {1: vendor, 3: trait}, 2: {1: instance} }."""
    key = pb.field_varint(1, VENDOR_SPAN) + pb.field_varint(3, trait)
    return pb.field_message(1, key) + pb.field_message(2, pb.field_varint(1, instance))


def _entry(trait: int, instance: int, value: bytes, version: int = 1) -> bytes:
    """One trait_entry: its key, its instance, and its value at 3 -> 2.

    The key carries no `product_id` (#2), matching real snapshots — which is why
    a command echoes the metadata back rather than reconstructing it.
    """
    key = (
        pb.field_varint(1, VENDOR_SPAN)
        + pb.field_varint(3, trait)
        + pb.field_varint(4, version)
    )
    return pb.field_message(
        2,
        pb.field_message(1, key)
        + pb.field_message(2, pb.field_varint(1, instance))
        + pb.field_message(3, pb.field_varint(1, 7) + pb.field_bytes(2, value)),
    )


def _circuit(position_field: int, label_id: int, space_ids: list[int]) -> bytes:
    """Trait 1/15's value: refs to the label and space(s), nested a level down.

    Real payloads bury the refs at a depth that varies by wire kind, so the
    fixture nests them too — that is what `_trait_refs`' walk exists for.
    """
    refs = b"".join(pb.field_message(2, _trait_ref(TRAIT_SPACE, s)) for s in space_ids)
    position = pb.field_message(1, _trait_ref(TRAIT_LABEL, label_id)) + refs
    return pb.field_message(1, pb.field_message(position_field, position))


def _label(name: str, amps: int, wire: int = 1) -> bytes:
    """Trait 1/16's value: { 1: { 2: wire, 3: amps, 4: label } }."""
    inner = pb.field_varint(2, wire) + pb.field_varint(3, amps) + pb.field_string(4, name)
    return pb.field_message(1, inner)


def _space(number: int) -> bytes:
    """Trait 1/17's value: { 1: { 3: displayed space number } }."""
    return pb.field_message(1, pb.field_varint(3, number))


def _switch(circuit_instance: int, state: int) -> bytes:
    """Trait 1/31's value: a back-ref to the circuit it switches, plus its state.

        { 1: { 1: TraitRef -> 1/15 same instance, 2: config, 3: switch_state } }
    """
    config = pb.field_varint(1, 2) + pb.field_varint(22, 32)
    inner = (
        pb.field_message(1, _trait_ref(TRAIT_CIRCUIT, circuit_instance))
        + pb.field_message(2, config)
        + pb.field_varint(3, state)
    )
    return pb.field_message(1, inner)


def build_snapshot() -> bytes:
    entries = (
        # a 120 V branch on one space, and a 240 V one straddling two
        _entry(TRAIT_CIRCUIT, 30, _circuit(11, label_id=101, space_ids=[9]))
        + _entry(TRAIT_LABEL, 101, _label("Branch A", 20))
        + _entry(TRAIT_SPACE, 9, _space(9))
        + _entry(TRAIT_SWITCH, 30, _switch(30, SWITCH_STATE_CLOSED))
        + _entry(TRAIT_CIRCUIT, 56, _circuit(13, label_id=102, space_ids=[47, 48]))
        + _entry(TRAIT_LABEL, 102, _label("Branch B", 40, wire=2))
        + _entry(TRAIT_SPACE, 47, _space(47))
        + _entry(TRAIT_SPACE, 48, _space(48))
        + _entry(TRAIT_SWITCH, 56, _switch(56, SWITCH_STATE_OPEN))
    )
    resource = pb.field_message(1, pb.field_string(1, HARDWARE_ID)) + entries
    return pb.field_message(1, resource)


def test_resolves_labels_amps_and_spaces():
    circuits = parse_trait_snapshot(build_snapshot())
    assert set(circuits) == {30, 56}

    branch = circuits[30]
    assert branch == CircuitInfo(
        instance_id=30,
        label="Branch A",
        spaces=(9,),
        breaker_amps=20,
        relay_closed=True,
        switch=SwitchTarget(
            resource_id=HARDWARE_ID, instance_id=30, metadata=(1, None, 31, 1)
        ),
    )

    # A three-wire circuit's position block sits at #13 rather than #11, and it
    # occupies two spaces.
    main = circuits[56]
    assert main.label == "Branch B"
    assert main.spaces == (47, 48)
    assert main.breaker_amps == 40


def test_space_numbers_are_panel_spaces_not_instance_ids():
    # The point of the whole module: instance 56 lives in space 47/48 on a
    # 40-space panel, so entities must not be named after the instance id.
    circuits = parse_trait_snapshot(build_snapshot())
    spaces = {s for c in circuits.values() for s in c.spaces}
    assert max(spaces) <= 48
    assert max(circuits) > max(spaces)


def test_display_name_falls_back_to_the_instance_id():
    # A circuit whose label trait never arrived still needs a name.
    orphan = pb.field_message(
        1,
        pb.field_message(1, pb.field_string(1, "hw"))
        + _entry(TRAIT_CIRCUIT, 42, _circuit(11, label_id=999, space_ids=[])),
    )
    circuits = parse_trait_snapshot(orphan)
    assert circuits[42].label is None
    assert circuits[42].display_name == "Circuit 42"


def test_relay_state_and_command_address_come_from_the_switch_trait():
    circuits = parse_trait_snapshot(build_snapshot())
    # CLOSED means energized, so the boolean must not be read as "disconnected".
    assert circuits[30].relay_closed is True
    assert circuits[56].relay_closed is False
    # The command address is the panel resource plus the same instance id the
    # telemetry uses — no extra RPC needed to switch a circuit we can meter.
    assert circuits[56].switch == SwitchTarget(
        resource_id=HARDWARE_ID, instance_id=56, metadata=(1, None, 31, 1)
    )


def test_unknown_switch_state_is_not_reported_as_open():
    # UNKNOWN is a state the panel really sends. Collapsing it to False would
    # show a live breaker as off.
    entries = _entry(TRAIT_CIRCUIT, 30, _circuit(11, 101, [9])) + _entry(
        TRAIT_SWITCH, 30, _switch(30, SWITCH_STATE_UNKNOWN)
    )
    raw = pb.field_message(
        1, pb.field_message(1, pb.field_string(1, HARDWARE_ID)) + entries
    )
    info = parse_trait_snapshot(raw)[30]
    assert info.relay_closed is None
    # The circuit is still addressable, so the control exists and reads unknown.
    assert info.switch is not None


def test_a_switch_trait_pointing_elsewhere_yields_no_control():
    # The 1/31 entry names the circuit it switches. If that disagrees with the
    # instance we looked it up under, the mapping is not one we understand — and
    # acting on it would open some other breaker.
    entries = _entry(TRAIT_CIRCUIT, 30, _circuit(11, 101, [9])) + _entry(
        TRAIT_SWITCH, 30, _switch(31, SWITCH_STATE_CLOSED)
    )
    raw = pb.field_message(
        1, pb.field_message(1, pb.field_string(1, HARDWARE_ID)) + entries
    )
    info = parse_trait_snapshot(raw)[30]
    assert info.switch is None
    assert info.relay_closed is None


def test_a_circuit_without_a_switch_trait_stays_read_only():
    circuits = parse_trait_snapshot(
        pb.field_message(
            1,
            pb.field_message(1, pb.field_string(1, HARDWARE_ID))
            + _entry(TRAIT_CIRCUIT, 42, _circuit(11, 101, [9])),
        )
    )
    assert circuits[42].switch is None


def test_unfamiliar_or_empty_snapshots_degrade_quietly():
    # Trait ids are undocumented, so a shape change must cost us labels, not setup.
    assert parse_trait_snapshot(b"") == {}
    assert parse_trait_snapshot(pb.field_string(9, "not a snapshot")) == {}
    assert parse_trait_snapshot(b"\xff\xff\xff") == {}
