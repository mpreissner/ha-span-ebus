"""Tests for resolving telemetry instance ids to circuit identities.

The snapshot here is synthesized with the protobuf writer to mirror the layout
recovered from a live `SubscribeAndGetTraits` response (docs/CLOUD-PROTO.md).
Labels are placeholders — no captured payload or real circuit name is committed.
"""

from span_bridge import cloud_pb as pb
from span_bridge.cloud_traits import (
    TRAIT_CIRCUIT,
    TRAIT_LABEL,
    TRAIT_SPACE,
    VENDOR_SPAN,
    CircuitInfo,
    parse_trait_snapshot,
)


def _trait_ref(trait: int, instance: int) -> bytes:
    """TraitRef { 1: {1: vendor, 3: trait}, 2: {1: instance} }."""
    key = pb.field_varint(1, VENDOR_SPAN) + pb.field_varint(3, trait)
    return pb.field_message(1, key) + pb.field_message(2, pb.field_varint(1, instance))


def _entry(trait: int, instance: int, value: bytes) -> bytes:
    """One trait_entry: its key, its instance, and its value at 3 -> 2."""
    key = pb.field_varint(1, VENDOR_SPAN) + pb.field_varint(3, trait)
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


def build_snapshot() -> bytes:
    entries = (
        # a 120 V branch on one space, and a 240 V one straddling two
        _entry(TRAIT_CIRCUIT, 30, _circuit(11, label_id=101, space_ids=[9]))
        + _entry(TRAIT_LABEL, 101, _label("Branch A", 20))
        + _entry(TRAIT_SPACE, 9, _space(9))
        + _entry(TRAIT_CIRCUIT, 56, _circuit(13, label_id=102, space_ids=[47, 48]))
        + _entry(TRAIT_LABEL, 102, _label("Branch B", 40, wire=2))
        + _entry(TRAIT_SPACE, 47, _space(47))
        + _entry(TRAIT_SPACE, 48, _space(48))
    )
    resource = pb.field_message(1, pb.field_string(1, "a1b2c3d4e5f60718")) + entries
    return pb.field_message(1, resource)


def test_resolves_labels_amps_and_spaces():
    circuits = parse_trait_snapshot(build_snapshot())
    assert set(circuits) == {30, 56}

    branch = circuits[30]
    assert branch == CircuitInfo(
        instance_id=30, label="Branch A", spaces=(9,), breaker_amps=20
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


def test_unfamiliar_or_empty_snapshots_degrade_quietly():
    # Trait ids are undocumented, so a shape change must cost us labels, not setup.
    assert parse_trait_snapshot(b"") == {}
    assert parse_trait_snapshot(pb.field_string(9, "not a snapshot")) == {}
    assert parse_trait_snapshot(b"\xff\xff\xff") == {}
