"""What happened to a control command, and the rules that may refuse one.

Free of Home Assistant. Every command an entity sends ends in exactly one
`CommandRecord`, fired as a `span_ebus_command` event once its outcome is
final. `SafetyRules` is what the transport checks before anything is
published, so the rules hold for every caller, not only entities.

Nothing here carries a device id, serial or topic into an event, a log line
or a message: records hold the control, the values and the outcome only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NamedTuple

from . import model
from .model import ControlOptions, Presence, Snapshot

EVENT = "span_ebus_command"


class Delivery(StrEnum):
    ACKED = "acked"  # the broker acknowledged the publish
    UNACKED = "unacked"  # the publish may or may not have reached the panel


class Outcome(StrEnum):
    CONFIRMED = "confirmed"  # the panel echoed the value within the window
    NOT_CONFIRMED = "not_confirmed"  # the window passed and the panel shows another value
    SUPERSEDED = "superseded"  # a newer command to the same entity replaced it first
    UNAVAILABLE = "unavailable"  # the entity went unavailable inside the window
    REFUSED = "refused"  # a safety rule or option stopped it; nothing was published
    NOT_SENT = "not_sent"  # the local link was down or the publish failed
    INVALID = "invalid"  # the value failed validation; nothing was published


class Control(StrEnum):
    RELAY = "relay"
    PRIORITY = "priority"
    ISLANDING = "islanding"
    EVSE_LIMIT = "evse_limit"
    EVSE_LOCK = "evse_lock"


class Refusal(StrEnum):
    PROTECTED = "protected"  # the user's protected-circuit option
    STORAGE_OR_SOLAR = "storage_or_solar"  # feeds a battery or PV, no override
    NOT_OPTED_IN = "not_opted_in"  # the islanding control is off in options
    GATED = "gated"  # an availability gate failed at call time
    OUT_OF_RANGE = "out_of_range"  # outside the bounds the device publishes now


@dataclass(frozen=True, slots=True)
class CommandRecord:
    control: Control
    requested: str  # the panel's value, as published (e.g. "OPEN", "16")
    previous: str | None  # what the entity showed before the command
    outcome: Outcome
    delivery: Delivery | None = None  # None unless something was published
    refusal: Refusal | None = None


EVENT_KEYS: frozenset[str] = frozenset(
    {"entity_id", "control", "requested", "previous", "outcome", "delivery", "refusal"}
)


def event_data(entity_id: str, record: CommandRecord) -> dict[str, Any]:
    """The event's payload: exactly EVENT_KEYS, plain strings or None."""
    return {
        "entity_id": entity_id,
        "control": str(record.control),
        "requested": record.requested,
        "previous": record.previous,
        "outcome": str(record.outcome),
        "delivery": None if record.delivery is None else str(record.delivery),
        "refusal": None if record.refusal is None else str(record.refusal),
    }


def describe(data: Mapping[str, Any]) -> str:
    """The logbook line: "OPEN requested: refused (protected)"."""
    outcome = str(data.get("outcome") or "unknown").replace("_", " ")
    refusal = data.get("refusal")
    if refusal:
        outcome = f"{outcome} ({str(refusal).replace('_', ' ')})"
    return f"{data.get('requested')} requested: {outcome}"


def as_text(value: Any) -> str | None:
    """A value as a record carries it."""
    return None if value is None else str(value)


# --- confirmation ----------------------------------------------------------------------


class Pending(NamedTuple):
    """A sent command waiting for the panel to echo it."""

    value: Any  # what was published, in the row's own spelling
    deadline: float  # monotonic time the window closes
    previous: str | None
    delivery: Delivery
    context: Any  # the service call's context, so HA records who sent it


def final_outcome(
    pending: Pending, presence: Presence, reported: Any, now: float, *, blocked: bool = False
) -> Outcome | None:
    """The outcome once it is final, else None (still waiting).

    `presence` and `reported` are what the panel reports now, before any
    safety rule hides the entity; `blocked` says whether one does. An echo
    confirms even when the echo itself blocks the control (clearing an
    islanding assertion once the grid is back does). Otherwise an entity gone
    unavailable ends the wait; a window that has passed does not confirm.
    """
    if presence is not Presence.UNAVAILABLE and reported == pending.value:
        return Outcome.CONFIRMED
    if presence is Presence.UNAVAILABLE or blocked:
        return Outcome.UNAVAILABLE
    if now >= pending.deadline:
        return Outcome.NOT_CONFIRMED
    return None


# --- safety rules ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SafetyRules:
    """Whether a command may be published, from the entry's options and the
    latest snapshot. `check` with no value is the availability check: what an
    entity uses to go unavailable.
    """

    options: ControlOptions
    snapshot: Snapshot

    @classmethod
    def from_options(cls, options: Mapping[str, Any] | None, snapshot: Snapshot) -> SafetyRules:
        return cls(ControlOptions.from_options(options), snapshot)

    def check(self, device_id: str, key: str, value: Any = None) -> Refusal | None:
        if key == model.RELAY_KEY:
            return self._relay(device_id)
        if key == model.ISLANDING_KEY:
            return self._islanding(value)
        if key == model.EVSE_LIMIT_KEY:
            return self._evse_limit(device_id, value)
        return None

    def _relay(self, device_id: str) -> Refusal | None:
        # Protection is checked even for a circuit the snapshot does not hold;
        # the storage-or-solar rule needs the circuit's link, so an unknown
        # circuit passes it.
        if device_id in self.options.protected:
            return Refusal.PROTECTED
        if model.relay_blocked(self.snapshot, device_id, self.options.relay_override):
            return Refusal.STORAGE_OR_SOLAR
        return None

    def _islanding(self, value: Any) -> Refusal | None:
        if not self.options.islanding:
            return Refusal.NOT_OPTED_IN
        requested = None if value is None else str(value).upper()
        if requested is not None and requested not in model.ISLANDING_CHOICES:
            return Refusal.GATED  # OFF_GRID is never asserted from here
        if model.islanding_gated(self.snapshot.panel, requested):
            return Refusal.GATED
        return None

    def _evse_limit(self, device_id: str, value: Any) -> Refusal | None:
        facts = self.snapshot.evse.get(device_id)
        if facts is None or model.evse_limit_bounds(facts) is None:
            return Refusal.GATED  # the ceiling is unknown
        if value is None:
            return None
        if model.evse_limit_problem(facts, value) is not None:
            return Refusal.OUT_OF_RANGE
        return None


# --- the options form ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ControlsForm:
    """What the options step offers, built from the snapshot.

    Choices map a circuit's device id (the stored value) to its label (the
    circuit's name and spaces, never an id). Defaults are the saved options,
    filtered to the current choices. A protected circuit the snapshot does
    not hold right now stays protected (offered under a placeholder label);
    one dropped only when the panel reports it without a relay switch.
    """

    protected: dict[str, str]
    override: dict[str, str]  # empty: the field is not shown
    show_islanding: bool
    protected_default: list[str]
    override_default: list[str]
    islanding_default: bool


def circuit_choice_label(circuit: model.CircuitFacts) -> str:
    spaces = "/".join(str(space) for space in circuit.spaces or ())
    if circuit.name and spaces:
        return f"{circuit.name} ({spaces})"
    return model.circuit_label(circuit)


def _kept(saved: Iterable[str], choices: Mapping[str, str]) -> list[str]:
    return [item for item in saved if item in choices]


def controls_form(snapshot: Snapshot, options: Mapping[str, Any] | None) -> ControlsForm:
    current = ControlOptions.from_options(options)
    switchable = [c for c in snapshot.circuits.values() if c.has_switch]
    protected = {c.device_id: circuit_choice_label(c) for c in switchable}
    absent = sorted(i for i in current.protected if i not in snapshot.circuits)
    for number, device_id in enumerate(absent, start=1):
        protected[device_id] = f"Circuit not reported now ({number})"
    override = {
        c.device_id: circuit_choice_label(c) for c in switchable if model.feeds_storage_or_solar(c)
    }
    panel = snapshot.panel
    show_islanding = panel is not None and panel.islanding_settable and panel.has_bess
    return ControlsForm(
        protected=protected,
        override=override,
        show_islanding=show_islanding,
        protected_default=_kept(sorted(current.protected), protected),
        override_default=_kept(sorted(current.relay_override), override),
        islanding_default=current.islanding,
    )


def controls_options(
    form: ControlsForm, options: Mapping[str, Any], user_input: Mapping[str, Any]
) -> dict[str, Any]:
    """The entry's new options: what was ticked, limited to what was offered.
    A field the form did not show is saved off.
    """
    protected = _kept(user_input.get(model.PROTECTED_CIRCUITS) or (), form.protected)
    override = _kept(user_input.get(model.RELAY_OVERRIDE) or (), form.override)
    islanding = form.show_islanding and user_input.get(model.ISLANDING_CONTROL) is True
    return {
        **options,
        model.PROTECTED_CIRCUITS: protected,
        model.RELAY_OVERRIDE: override,
        model.ISLANDING_CONTROL: islanding,
    }
