"""What feeds each panel: the user's answers, keyed by panel.

Plain functions, so the rules can be tested without Home Assistant. Nothing here
decides which device a circuit belongs to, on one site or two.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from .backend import same_serial
from .cloud_traits import CircuitInfo
from .link import (
    LOCAL_ONLY,
    TRANSPORT_MODE,
    cloud_removed_entry,
    has_account,
    legacy_local_only,
    link_from_options,
    save_options,
)

OWN_SUPPLY = "own_supply"
# Solar or a battery sits between the utility meter and this panel, and no
# SPAN panel is upstream: what the panel meters is the house load after them.
BEHIND_DER = "behind_der"
LUGS = "lugs"
BRANCH = "branch"
FED_TYPES = (BEHIND_DER, LUGS, BRANCH)

# The entry options these rules read: the panels ticked and their answers (the
# same keys as the integration's const), and a local panel's own say on whether
# another panel feeds it. That one is not a cloud option, so it outlives the cloud.
PANELS = "panels"
FEEDS = "feeds"
FED_BY_PANEL = "fed_by_panel"
# A local panel's own Topology answer; like FED_BY_PANEL it is not a cloud option.
TOPOLOGY = "topology"

# The Topology answer's supply values, and the marker a legacy "fed by another
# panel" tick derives to: fed, but without saying how.
SUPPLIES = (OWN_SUPPLY, BEHIND_DER, LUGS, BRANCH)
LEGACY_FED = "fed"

Feed = dict[str, Any]


def feed_of(feeds: Mapping[str, Feed], key: str, panels: Iterable[str]) -> Feed:
    """A panel's feed. No answer, or an upstream that has gone, means its own supply."""
    feed = feeds.get(key)
    if feed is not None and feed.get("type") == BEHIND_DER:
        return {"type": BEHIND_DER}  # nothing upstream to check
    if feed is None or feed.get("upstream") == key or feed.get("upstream") not in set(panels):
        return {"type": OWN_SUPPLY}
    return dict(feed)


def is_fed(feeds: Mapping[str, Feed], key: str, panels: Iterable[str]) -> bool:
    """Whether what the panel meters is not the utility grid: another panel
    feeds it, through its lugs or a branch circuit, or solar or a battery sits
    before it.
    """
    return feed_of(feeds, key, panels)["type"] in FED_TYPES


def cloud_fed(options: Mapping[str, Any]) -> bool:
    """What the cloud answers say about a linked local panel; False if unlinked."""
    link = link_from_options(options)
    if link is None:
        return False
    return is_fed(options.get(FEEDS) or {}, link.cloud_key, options.get(PANELS) or ())


def saved_topology(value: Any) -> dict[str, Any] | None:
    """A stored Topology answer, cleaned; None if it isn't one.

    Only lugs and branch keep an upstream, and only branch keeps spaces (with
    an upstream). The legacy marker is accepted so a kept answer reads back.
    """
    if not isinstance(value, Mapping):
        return None
    supply = value.get("supply")
    if supply not in (*SUPPLIES, LEGACY_FED):
        return None
    out: dict[str, Any] = {"supply": supply}
    upstream = value.get("upstream")
    if supply in (LUGS, BRANCH) and isinstance(upstream, str) and upstream:
        out["upstream"] = upstream
        spaces = value.get("spaces")
        valid = supply == BRANCH and isinstance(spaces, list | tuple) and bool(spaces)
        if valid and all(isinstance(s, int) and not isinstance(s, bool) for s in spaces):
            out["spaces"] = sorted(spaces)
    return out


def topology_of(options: Mapping[str, Any]) -> dict[str, Any]:
    """A local panel's Topology answer: saved, else derived from what older
    versions stored.

    A saved "fed by another panel" tick derives to the legacy marker (fed, not
    saying how); else a linked panel's cloud answer gives its type, without the
    upstream (a cloud key, which can't be mapped offline); else its own supply.
    """
    saved = saved_topology(options.get(TOPOLOGY))
    if saved is not None:
        return saved
    fed = options.get(FED_BY_PANEL)
    if isinstance(fed, bool):
        return {"supply": LEGACY_FED if fed else OWN_SUPPLY}
    link = link_from_options(options)
    if link is not None:
        feed = feed_of(options.get(FEEDS) or {}, link.cloud_key, options.get(PANELS) or ())
        if feed["type"] in FED_TYPES:
            return {"supply": feed["type"]}
    return {"supply": OWN_SUPPLY}


def local_fed(options: Mapping[str, Any]) -> bool:
    """Whether what a local panel meters is not the utility grid."""
    return topology_of(options)["supply"] != OWN_SUPPLY


def with_topology(options: Mapping[str, Any], topology: Mapping[str, Any]) -> dict[str, Any]:
    """Options with a new Topology answer, and the old fed tick as a mirror so a
    downgrade keeps the names. The mirror is never read while the answer exists.
    """
    out = {**options, TOPOLOGY: dict(topology)}
    out[FED_BY_PANEL] = local_fed(out)
    return out


def panel_label(name_by_user: str | None, model: str | None) -> str | None:
    """How a panel is named in another's attributes: the user's own name for
    its device, else "SPAN <model>", else nothing.

    Never the device's default name: a cloud device's carries the serial and a
    local one's the host.
    """
    if name_by_user:
        return name_by_user
    if model:
        return f"SPAN {model}"
    return None


def panel_identifiers(serial: str, linked: Iterable[tuple[str | None, str | None]]) -> list[str]:
    """The device identifiers a panel may be shown under, best first.

    The serial in each spelling (the panel reports lowercase, SPAN's cloud
    uppercase), then the stem of any cloud device a local entry with this
    serial was merged into. `linked` holds (local serial, stem) per entry.
    """
    stripped = serial.strip()
    spellings = [serial, stripped, stripped.lower(), stripped.upper()]
    spellings += [str(stem) for local, stem in linked if stem and same_serial(local, serial)]
    return list(dict.fromkeys(item for item in spellings if item))


def keep_topology(options: Mapping[str, Any]) -> dict[str, Any]:
    """Options with the Topology answer saved, for before the cloud answers go.

    Until the user saves their own, a linked panel's answer may come from the
    cloud's; removing the cloud drops those, so the answer it gave is kept,
    with the fed mirror.
    """
    if saved_topology(options.get(TOPOLOGY)) is not None:
        out = dict(options)
        out[FED_BY_PANEL] = local_fed(out)
        return out
    return with_topology(options, topology_of(options))


def branch_circuits(
    feeds: Mapping[str, Feed], upstream: str, panels: Iterable[str]
) -> dict[str, str]:
    """Circuit (cloud instance id, as text) -> the panel it feeds, on `upstream`.

    Only answers that still hold and name a circuit count.
    """
    keys = list(panels)
    out: dict[str, str] = {}
    for key in keys:
        feed = feed_of(feeds, key, keys)
        circuit = feed.get("circuit")
        if feed["type"] == BRANCH and feed["upstream"] == upstream and circuit is not None:
            out[str(circuit)] = key
    return out


def feeds_panel(
    circuits: Mapping[str, str], circuit: str | int | None, names: Mapping[str, str | None]
) -> str | None:
    """The name of the panel a circuit feeds, or None.

    `circuits` is `branch_circuits`; `names` maps a panel key to its device's
    name. Without a name there is no answer: never a key or serial in its place.
    """
    if circuit is None:
        return None
    key = circuits.get(str(circuit))
    if key is None:
        return None
    return names.get(key) or None


def clean_feeds(feeds: Mapping[str, Feed], panels: Iterable[str]) -> dict[str, Feed]:
    """The answers that still hold for these panels."""
    keys = list(panels)
    return {key: feed_of(feeds, key, keys) for key in keys}


def has_cycle(feeds: Mapping[str, Feed]) -> bool:
    """True if following the feeds upstream from any panel comes back around."""
    for start in feeds:
        seen = {start}
        upstream = feeds[start].get("upstream")
        while upstream in feeds:
            if upstream in seen:
                return True
            seen.add(upstream)
            upstream = feeds[upstream].get("upstream")
    return False


def form_value(feed: Feed) -> str:
    if "upstream" in feed:
        return f"{feed['type']}:{feed['upstream']}"
    return BEHIND_DER if feed.get("type") == BEHIND_DER else OWN_SUPPLY


def from_form(value: str, prior: Feed | None = None) -> Feed:
    """Read a select's value, keeping a circuit already chosen on the same upstream."""
    if value in (OWN_SUPPLY, BEHIND_DER):
        return {"type": value}
    feed_type, _, upstream = value.partition(":")
    feed: Feed = {"type": feed_type, "upstream": upstream}
    if feed_type == BRANCH and prior and prior.get("upstream") == upstream:
        circuit = prior.get("circuit")
        if circuit is not None:
            feed["circuit"] = circuit
    return feed


def feed_choices(key: str, labels: Mapping[str, str], current: Feed) -> dict[str, str]:
    """Select value -> label: its own supply, then each other panel's lugs and branches."""
    choices = {
        OWN_SUPPLY: "Its own supply",
        BEHIND_DER: "Its own supply, with solar or a battery before the panel",
    }
    for upstream, label in labels.items():
        if upstream == key:
            continue
        choices[f"{LUGS}:{upstream}"] = f"Feed-through lugs on {label}"
        unfinished = current.get("upstream") == upstream and "circuit" not in current
        note = " (circuit not chosen)" if current.get("type") == BRANCH and unfinished else ""
        choices[f"{BRANCH}:{upstream}"] = f"Branch circuit on {label}{note}"
    return choices


def branch_fed(feeds: Mapping[str, Feed]) -> list[str]:
    return [key for key, feed in feeds.items() if feed["type"] == BRANCH]


def circuit_choices(circuits: Mapping[int, CircuitInfo]) -> dict[str, str]:
    """Select value -> label, in panel order. Labels need not be unique; spaces are."""

    def place(info: CircuitInfo) -> tuple[int, int]:
        return (min(info.spaces, default=0), info.instance_id)

    return {
        str(info.instance_id): cloud_circuit_label(info)
        for info in sorted(circuits.values(), key=place)
    }


# --- a local panel's Topology step ---------------------------------------------------
#
# A local answer names its upstream by the panel's device serial and its circuit
# by breaker spaces. Serials are option values only: never a label, a log line or
# a placeholder. Labels are hard-coded English, since dynamic select options and
# placeholders aren't translated.

NOT_LISTED = "not_listed"
NOT_LISTED_LABEL = "Not listed / not in Home Assistant"
NOT_FOUND_LABEL = "Panel not found now"


def serial_key(serial: str) -> str:
    """A serial as a dict key: two spellings `backend.same_serial` matches give
    the same key (the panel reports lowercase, SPAN's cloud uppercase, either
    may carry stray whitespace).
    """
    return serial.strip().casefold()


def upstream_choices(
    this_serial: str,
    candidates: Iterable[tuple[str | None, str | None]],
    names: Mapping[str, str | None],
) -> dict[str, str]:
    """Serial -> label for every other panel known, in the order first found.

    `candidates` are (serial, model) pairs from each source in turn; a pair
    without a serial is skipped, serials are compared case-insensitively and
    the first spelling is kept, with the first model known. `names` maps a
    serial (any spelling) to the user's name for its device. A label is that
    name, else "SPAN <model>", else "Panel N" numbered in serial order;
    repeated labels get " (2)", " (3)" and so on.
    """
    found: dict[str, tuple[str, str | None]] = {}
    for serial, model in candidates:
        if not serial or not serial.strip() or same_serial(serial, this_serial):
            continue
        key = serial_key(serial)
        if key not in found:
            found[key] = (serial.strip(), model or None)
        elif found[key][1] is None and model:
            found[key] = (found[key][0], model)
    by_name = {serial_key(serial): name for serial, name in names.items() if name}
    labels: dict[str, str | None] = {
        key: panel_label(by_name.get(key), model) for key, (_serial, model) in found.items()
    }
    unnamed = sorted(key for key, label in labels.items() if label is None)
    for number, key in enumerate(unnamed, start=1):
        labels[key] = f"Panel {number}"
    seen: dict[str, int] = {}
    out: dict[str, str] = {}
    for key, (serial, _model) in found.items():
        label = str(labels[key])
        seen[label] = seen.get(label, 0) + 1
        out[serial] = label if seen[label] == 1 else f"{label} ({seen[label]})"
    return out


def upstream_options(choices: Mapping[str, str], stored: str | None) -> tuple[dict[str, str], str]:
    """The upstream select's choices and default.

    A stored upstream that matches no panel known now is kept, as "Panel not
    found now", and is the default, so saving without a change keeps it.
    "Not listed" stands for no upstream.
    """
    out = dict(choices)
    default = NOT_LISTED
    if stored:
        match = next((serial for serial in out if same_serial(serial, stored)), None)
        if match is None:
            out[stored] = NOT_FOUND_LABEL
            match = stored
        default = match
    out[NOT_LISTED] = NOT_LISTED_LABEL
    return out, default


def spaces_value(spaces: Iterable[int]) -> str:
    """A circuit's select value: its breaker spaces, sorted."""
    return ",".join(str(space) for space in sorted(spaces))


def spaces_from_value(value: str) -> list[int]:
    return sorted(int(part) for part in value.split(",") if part.strip())


def cloud_circuit_label(info: CircuitInfo) -> str:
    """A cloud circuit's label: its `display_name` and spaces, as `circuit_choices`
    labels it, so both pickers name a circuit the same way.
    """
    spaces = ", ".join(str(space) for space in info.spaces)
    return f"{info.display_name} ({spaces})" if spaces else info.display_name


def breaker_choices(circuits: Iterable[tuple[str, Iterable[int]]]) -> dict[str, str]:
    """Spaces value -> label, in panel order, from (label, spaces) pairs.

    A circuit without spaces can't be stored, so it isn't offered.
    """
    rows: dict[str, tuple[int, str]] = {}
    for label, spaces in circuits:
        ordered = sorted(spaces)
        if ordered:
            rows.setdefault(spaces_value(ordered), (ordered[0], label))
    return {value: label for value, (_first, label) in sorted(rows.items(), key=lambda r: r[1])}


def topology_loops(
    this_serial: str,
    topology: Mapping[str, Any],
    others: Mapping[str, Any],
    cloud_answers: Iterable[tuple[str, Feed]] = (),
    serials: Iterable[str] = (),
) -> bool:
    """Whether this panel's new answer closes a loop through saved answers.

    `others` maps another local panel's serial to its saved Topology (not a
    derived one). `cloud_answers` are an account's (panel key, feed) pairs;
    only those whose key and upstream are both among `serials` count. A
    panel with no saved answer ends the chain.
    """
    chain: dict[str, Feed] = {}
    for serial, saved in others.items():
        answer = saved_topology(saved)
        if answer is not None and "upstream" in answer:
            chain[serial_key(serial)] = {"upstream": serial_key(answer["upstream"])}
    known = {serial_key(serial) for serial in serials}
    for key, feed in cloud_answers:
        upstream = feed.get("upstream")
        if not isinstance(upstream, str) or feed.get("type") not in (LUGS, BRANCH):
            continue
        if serial_key(key) in known and serial_key(upstream) in known:
            chain.setdefault(serial_key(key), {"upstream": serial_key(upstream)})
    answer = saved_topology(topology)
    if answer is not None and "upstream" in answer:
        chain[serial_key(this_serial)] = {"upstream": serial_key(answer["upstream"])}
    else:
        chain.pop(serial_key(this_serial), None)
    return has_cycle(chain)


_PHRASES = {
    OWN_SUPPLY: "fed from the utility meter",
    BEHIND_DER: "fed from the utility meter, with solar or a battery before it",
    LUGS: "fed from {upstream}'s feed-through lugs",
    BRANCH: "fed from a breaker on {upstream}",
}


def others_summary(
    this_serial: str,
    answers: Iterable[tuple[str, Mapping[str, Any]]],
    labels: Mapping[str, str],
) -> str:
    """The Topology step's read-only lines about the other panels' answers.

    `answers` are (serial, answer) pairs, each `{"supply", "upstream"?}`: a
    local panel's saved Topology, or a cloud answer turned into that shape by
    the caller. Panels are named by `labels` (serial -> label, any spelling);
    one without a label, or with a legacy answer, is left out. An upstream
    that is this panel reads "this panel", an unknown one "another panel".
    Empty when no other panel has an answer.
    """
    by_serial = {serial_key(serial): label for serial, label in labels.items()}
    lines: list[str] = []
    seen: set[str] = set()
    for serial, raw in answers:
        key = serial_key(serial)
        answer = saved_topology(raw)
        if key == serial_key(this_serial) or key in seen or key not in by_serial:
            continue
        if answer is None or answer["supply"] not in _PHRASES:
            continue
        seen.add(key)
        upstream = answer.get("upstream")
        if upstream and serial_key(upstream) == serial_key(this_serial):
            name = "this panel"
        elif upstream and serial_key(upstream) in by_serial:
            name = by_serial[serial_key(upstream)]
        else:
            name = "another panel"
        phrase = _PHRASES[answer["supply"]].format(upstream=name)
        lines.append(f"- {by_serial[key]}: {phrase}")
    if not lines:
        return ""
    return "\n\nOther panels:\n" + "\n".join(lines)


# --- saving -------------------------------------------------------------------------


def save_topology(config_entries: Any, entry: Any, topology: Mapping[str, Any]) -> dict[str, Any]:
    """Store a Topology answer on this entry alone and reload it; returns the options.

    Each panel answers for itself: another entry's answer is read, never written.
    """
    return save_options(config_entries, entry, with_topology(entry.options, topology))


async def remove_legacy_account(
    entry: Any,
    *,
    update: Callable[..., Any],
    discard_tokens: Callable[[], Awaitable[Any]],
    data_keys: Iterable[str],
    option_keys: Iterable[str],
    cloud_stem_key: str,
) -> bool:
    """At a local entry's setup: drop a SPAN account kept behind local only.

    Earlier versions could keep one, unused; it goes as the options flow's
    remove takes it, with the Topology answer the cloud gave kept.
    `update(entry, data=, options=, minor_version=)` stores the entry (it must
    leave `entry.data` updated, as Home Assistant's does). Then, if the entry
    has no account (any more), `discard_tokens()` deletes a token file left
    from one. Returns whether an account was removed.
    """
    removed = False
    if legacy_local_only(entry.data, entry.options):
        data, options, minor_version = cloud_removed_entry(
            entry.data,
            keep_topology(entry.options),
            data_keys=data_keys,
            option_keys=option_keys,
            cloud_stem_key=cloud_stem_key,
        )
        options[TRANSPORT_MODE] = LOCAL_ONLY
        update(entry, data=data, options=options, minor_version=minor_version)
        removed = True
    if not has_account(entry.data):
        await discard_tokens()
    return removed
