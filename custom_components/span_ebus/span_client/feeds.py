"""What feeds each panel: the user's answers, keyed by panel.

Plain functions, so the rules can be tested without Home Assistant. Nothing here
decides which device a circuit belongs to, on one site or two.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .cloud_traits import CircuitInfo

OWN_SUPPLY = "own_supply"
LUGS = "lugs"
BRANCH = "branch"

Feed = dict[str, Any]


def feed_of(feeds: Mapping[str, Feed], key: str, panels: Iterable[str]) -> Feed:
    """A panel's feed. No answer, or an upstream that has gone, means its own supply."""
    feed = feeds.get(key)
    if feed is None or feed.get("upstream") == key or feed.get("upstream") not in set(panels):
        return {"type": OWN_SUPPLY}
    return dict(feed)


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
    return f"{feed['type']}:{feed['upstream']}" if "upstream" in feed else OWN_SUPPLY


def from_form(value: str, prior: Feed | None = None) -> Feed:
    """Read a select's value, keeping a circuit already chosen on the same upstream."""
    if value == OWN_SUPPLY:
        return {"type": OWN_SUPPLY}
    feed_type, _, upstream = value.partition(":")
    feed: Feed = {"type": feed_type, "upstream": upstream}
    if feed_type == BRANCH and prior and prior.get("upstream") == upstream:
        circuit = prior.get("circuit")
        if circuit is not None:
            feed["circuit"] = circuit
    return feed


def feed_choices(key: str, labels: Mapping[str, str], current: Feed) -> dict[str, str]:
    """Select value -> label: its own supply, then each other panel's lugs and branches."""
    choices = {OWN_SUPPLY: "Its own supply"}
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

    def label(info: CircuitInfo) -> str:
        spaces = ", ".join(str(space) for space in info.spaces)
        return f"{info.display_name} ({spaces})" if spaces else info.display_name

    return {str(info.instance_id): label(info) for info in sorted(circuits.values(), key=place)}
