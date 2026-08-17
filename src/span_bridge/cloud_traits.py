"""Resolve telemetry instance ids to real circuit identities.

Telemetry frames identify a circuit only by `trait_instance_id`, an opaque
internal number. It is *not* a panel space: on a MAIN 40 the instance ids run
1, 2 and 30..56 while the panel has 40 spaces, so naming entities after them
produces confusing ids like `circuit-56` on a 40-space panel.

The real identities come from the ~22 KB snapshot `SubscribeAndGetTraits`
returns — the same payload the mobile app uses to draw the panel. Layout, by
wire field number (recovered from live payloads; there is no public `.proto`):

    1 resource[]
        1 { 1: hardware_id }
        2 trait_entry[]
            1 { 1: vendor_id, 3: trait_id }
            2 { 1: instance_id }
            3 { 1: metadata, 2: <trait value> }

The snapshot's pointer type, a `TraitRef`, is
`{ 1: { 1: vendor_id, 3: trait_id }, 2: { 1: instance_id } }`. Four traits
matter here:

    1/15  circuit, keyed by the *telemetry* instance id. Its position block is
          field #11 for two-wire circuits and #13 for three-wire ones —
          mirroring the telemetry oneof tags — and holds TraitRefs to the
          circuit's label and to the space(s) it occupies.
    1/16  circuit label  { 2: wire config, 3: breaker amps, 4: user label }
    1/17  panel space    { 3: displayed space number }
    1/31  switch/load management, keyed by the *same* instance id as 1/15:
          { 1: TraitRef back to 1/15, 2: config, 3: switch_state,
            5: last_disconnect_msec, 6: last_reconnect_msec }

`SwitchState` is `{ 0 UNSPECIFIED, 1 UNKNOWN, 2 OPEN, 3 CLOSED }`, so CLOSED
means energized. 1/31 is also the trait a relay command is addressed to, which is
why the entries' resource id and trait metadata are kept here rather than reduced
away — see `cloud_commands.SwitchTarget`.

Space numbers are the panel's own: a MAIN 40 reports 40 spaces numbered 9..48,
because spaces 1..8 are the main-breaker module that an MLO 48 swaps out for a
further branch module.

Trait ids are opaque and undocumented, so every accessor here is defensive: a
snapshot we cannot read degrades to unnamed circuits rather than breaking setup.
A circuit whose switch trait we cannot resolve simply gets no relay control.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .cloud_commands import SwitchTarget
from .cloud_pb import Message, ProtoError, parse

log = logging.getLogger(__name__)

VENDOR_SPAN = 1
TRAIT_CIRCUIT = 15
TRAIT_LABEL = 16
TRAIT_SPACE = 17
TRAIT_SWITCH = 31

# SwitchState. Only CLOSED means the circuit is energized; OPEN and the two
# not-a-state values are all "not energized", but only OPEN is *known* to be.
SWITCH_STATE_UNKNOWN = 1
SWITCH_STATE_OPEN = 2
SWITCH_STATE_CLOSED = 3

# Circuit position blocks, keyed by wire kind. These field numbers match the
# telemetry oneof tags (11 two_wire / 12 three_wire), which is how the two sides
# were tied together in the first place.
_POSITION_FIELDS = (11, 13)

# Guard against a pathological or hostile nesting depth while walking TraitRefs.
_MAX_DEPTH = 8


@dataclass(frozen=True)
class TraitEntry:
    """One trait instance as the snapshot declared it.

    The value is what every reader here wants, but `resource_id` and `metadata`
    are what a *command* needs — they address the instance — so they are carried
    alongside instead of being dropped.
    """

    resource_id: str | None
    metadata: tuple[int, int | None, int, int | None]
    value: Message


@dataclass(frozen=True)
class CircuitInfo:
    """What the trait snapshot knows about one telemetry instance."""

    instance_id: int
    label: str | None = None
    # Panel spaces the circuit occupies, as displayed on the panel (e.g. 9, 11).
    spaces: tuple[int, ...] = ()
    breaker_amps: int | None = None
    # Relay state from trait 1/31: True = CLOSED (energized), False = OPEN,
    # None = the panel reported UNKNOWN or we could not read the trait.
    relay_closed: bool | None = None
    # How to address this circuit's relay, when the snapshot told us. `None`
    # means no relay control is offered for this circuit.
    switch: SwitchTarget | None = None

    @property
    def display_name(self) -> str:
        """A human label, falling back to the instance id when unnamed."""
        return self.label or f"Circuit {self.instance_id}"


def parse_trait_snapshot(raw: bytes) -> dict[int, CircuitInfo]:
    """Map telemetry instance id -> CircuitInfo from a trait snapshot.

    Returns an empty mapping — never raises — if the snapshot is empty or its
    shape is unfamiliar; callers fall back to instance-id naming.
    """
    if not raw:
        return {}
    try:
        traits = _index_traits(raw)
    except (ProtoError, ValueError) as exc:
        log.debug("could not index the trait snapshot (%d bytes): %s", len(raw), exc)
        return {}

    circuits: dict[int, CircuitInfo] = {}
    for (vendor, trait, instance), entry in traits.items():
        if (vendor, trait) != (VENDOR_SPAN, TRAIT_CIRCUIT):
            continue
        info = _circuit_from(instance, entry.value, traits)
        if info is not None:
            circuits[instance] = info

    named = sum(1 for c in circuits.values() if c.label)
    switchable = sum(1 for c in circuits.values() if c.switch is not None)
    log.info(
        "trait snapshot resolved %d circuit(s), %d with labels, %d switchable",
        len(circuits),
        named,
        switchable,
    )
    return circuits


def _index_traits(raw: bytes) -> dict[tuple[int, int, int], TraitEntry]:
    """Flatten the snapshot into (vendor, trait, instance) -> TraitEntry."""
    out: dict[tuple[int, int, int], TraitEntry] = {}
    for resource in parse(raw).get_msgs(1):
        resource_ref = resource.get_msg(1)
        resource_id = resource_ref.get_str(1) if resource_ref is not None else None
        for entry in resource.get_msgs(2):
            key, instance, value = entry.get_msg(1), entry.get_msg(2), entry.get_msg(3)
            if key is None or value is None:
                continue
            body = value.get_bytes(2)
            if body is None:
                continue
            vendor, trait = key.get_int_opt(1), key.get_int_opt(3)
            if vendor is None or trait is None:
                continue
            ident = (instance.get_int_opt(1) or 0) if instance is not None else 0
            out[(vendor, trait, ident)] = TraitEntry(
                resource_id=resource_id,
                # product_id and version are absent for some traits; carry them
                # as None so a command echoes exactly what the snapshot said.
                metadata=(vendor, key.get_int_opt(2), trait, key.get_int_opt(4)),
                value=parse(body),
            )
    return out


def _circuit_from(
    instance: int,
    body: Message,
    traits: dict[tuple[int, int, int], TraitEntry],
) -> CircuitInfo | None:
    """Build a CircuitInfo by following trait 1/15's refs to its label and spaces."""
    relay_closed, switch = _switch_for(instance, traits)
    inner = body.get_msg(1)
    if inner is None:
        return None

    position: Message | None = None
    for no in _POSITION_FIELDS:
        position = _msg_or_none(inner, no)
        if position is not None:
            break
    if position is None:
        return CircuitInfo(instance_id=instance, relay_closed=relay_closed, switch=switch)

    refs = _trait_refs(position)
    label_ids = [i for vendor, trait, i in refs if (vendor, trait) == (VENDOR_SPAN, TRAIT_LABEL)]
    space_ids = [i for vendor, trait, i in refs if (vendor, trait) == (VENDOR_SPAN, TRAIT_SPACE)]

    label = amps = None
    if label_ids:
        label_body = _trait_inner(traits, TRAIT_LABEL, label_ids[0])
        if label_body is not None:
            label = label_body.get_str(4)
            amps = label_body.get_int_opt(3)

    spaces = []
    for space_id in space_ids:
        space_body = _trait_inner(traits, TRAIT_SPACE, space_id)
        number = space_body.get_int_opt(3) if space_body is not None else None
        if number is not None:
            spaces.append(number)

    return CircuitInfo(
        instance_id=instance,
        label=label or None,
        spaces=tuple(sorted(set(spaces))),
        breaker_amps=amps,
        relay_closed=relay_closed,
        switch=switch,
    )


def _switch_for(
    instance: int,
    traits: dict[tuple[int, int, int], TraitEntry],
) -> tuple[bool | None, SwitchTarget | None]:
    """Relay state and command address from the 1/31 entry at `instance`.

    Returns `(None, None)` when there is no switch trait for the instance, or when
    the entry does not agree that it belongs to *this* circuit: its field #1 is a
    TraitRef back to the 1/15 instance it switches, and a command sent to the
    wrong instance would open somebody else's breaker. A disagreement is not
    something to guess through.
    """
    entry = traits.get((VENDOR_SPAN, TRAIT_SWITCH, instance))
    if entry is None or entry.resource_id is None:
        return None, None
    inner = _msg_or_none(entry.value, 1)
    if inner is None:
        return None, None

    back_ref = _trait_refs(inner)
    if (VENDOR_SPAN, TRAIT_CIRCUIT, instance) not in back_ref:
        log.debug(
            "switch trait instance %d does not point back at circuit %d; "
            "offering no relay control",
            instance,
            instance,
        )
        return None, None

    state = inner.get_int_opt(3)
    relay_closed: bool | None = None
    if state == SWITCH_STATE_CLOSED:
        relay_closed = True
    elif state == SWITCH_STATE_OPEN:
        relay_closed = False

    target = SwitchTarget(
        resource_id=entry.resource_id,
        instance_id=instance,
        metadata=entry.metadata,
    )
    return relay_closed, target


def _trait_inner(
    traits: dict[tuple[int, int, int], TraitEntry], trait: int, instance: int
) -> Message | None:
    """The payload of trait `VENDOR_SPAN/trait` at `instance`, one level in."""
    entry = traits.get((VENDOR_SPAN, trait, instance))
    return _msg_or_none(entry.value, 1) if entry is not None else None


def _trait_refs(msg: Message, depth: int = 0) -> list[tuple[int, int, int]]:
    """Every TraitRef under `msg`, depth-first, as (vendor, trait, instance).

    The refs we want are not at fixed offsets — a circuit's position block nests
    them a variable number of levels deep, and differently for two- and
    three-wire circuits — so we walk the whole subtree and filter by trait id.
    """
    found: list[tuple[int, int, int]] = []
    if depth > _MAX_DEPTH:
        return found

    key, instance = _msg_or_none(msg, 1), _msg_or_none(msg, 2)
    if key is not None and instance is not None:
        vendor, trait, ident = (
            key.get_int_opt(1),
            key.get_int_opt(3),
            instance.get_int_opt(1),
        )
        if vendor is not None and trait is not None and ident is not None:
            found.append((vendor, trait, ident))

    for no in sorted(msg.fields):
        for value in msg.values(no):
            if not isinstance(value, bytes) or not value:
                continue
            try:
                found.extend(_trait_refs(parse(value), depth + 1))
            except (ProtoError, ValueError):
                continue
    return found


def _msg_or_none(msg: Message | None, no: int) -> Message | None:
    """`get_msg` that yields None instead of raising on a non-message field.

    Walking a heuristic layout means probing field numbers that sometimes hold a
    varint; that is a miss, not a malformed message.
    """
    if msg is None:
        return None
    try:
        return msg.get_msg(no)
    except (ProtoError, ValueError):
        return None
