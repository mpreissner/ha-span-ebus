"""The link between a panel added locally and the cloud that is its fallback.

Pure: no Home Assistant imports. The record lives in the entry's options under
`links`, keyed by the panel's cloud key, and says which cloud circuit stands for
which local one, which cloud feed is the panel's own and which feeds a panel
downstream, and how many panels share the cloud site.

Ids in here are private: the keys and serials, and the site's history ids.
Diagnostics redact them and logs give counts only.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

CONF_LINKS = "links"

# What a cloud feed is to this panel: the feed that supplies it, or the one it
# passes on to a panel downstream.
UPSTREAM = "U"
DOWNSTREAM = "D"

# Trait 1/21 feeder kinds (see span_client.cloud_traits). Repeated here so this
# module stays free of the client package.
FEEDER_MAIN_BREAKER = 3
FEEDER_FEEDTHROUGH = 4


class _LocalCircuit(Protocol):
    spaces: tuple[int, ...] | None
    poles: int | None
    is_remote_ct: bool


class _CloudCircuit(Protocol):
    instance_id: int
    spaces: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class HistoryTarget:
    """Where SPAN keeps the site's metered history: resource, instance and node."""

    resource: str
    instance: int
    node: str


@dataclass(frozen=True, slots=True)
class PanelLink:
    cloud_key: str
    local_serial: str
    circuits: Mapping[str, int] = field(default_factory=dict)
    feeds: Mapping[int, str] = field(default_factory=dict)
    unmatched_local: tuple[str, ...] = ()
    unmatched_cloud: tuple[int, ...] = ()
    site_history: HistoryTarget | None = None
    site_panel_count: int = 0
    linked_at: str = ""

    @property
    def single_panel_site(self) -> bool:
        """Whether the site's grid energy is this panel's alone (gates that mapping)."""
        return self.site_panel_count == 1


def _space_set(spaces: Iterable[int] | None) -> frozenset[int]:
    return frozenset(spaces or ())


def match_circuits(
    local: Mapping[str, _LocalCircuit], cloud: Iterable[_CloudCircuit]
) -> tuple[dict[str, int], tuple[str, ...], tuple[int, ...]]:
    """Pair local circuits with cloud ones by the breaker spaces they occupy.

    A pair needs the same set of spaces and the same number of poles; the cloud
    has no pole count, so its is the number of spaces. A remote CT occupies no
    space and never matches. A space set that appears twice on either side is
    ambiguous, so neither circuit with it matches anything.

    Returns the matches (local id -> cloud instance) and the unmatched ids on
    each side, sorted.
    """
    cloud = list(cloud)
    local_sets = {
        device_id: _space_set(facts.spaces)
        for device_id, facts in local.items()
        if not facts.is_remote_ct and facts.spaces
    }
    cloud_sets = {info.instance_id: _space_set(info.spaces) for info in cloud if info.spaces}
    local_seen = Counter(local_sets.values())
    cloud_seen = Counter(cloud_sets.values())
    by_set = {spaces: n for n, spaces in cloud_sets.items() if cloud_seen[spaces] == 1}

    matched: dict[str, int] = {}
    for device_id, spaces in local_sets.items():
        if local_seen[spaces] != 1 or spaces not in by_set:
            continue
        poles = local[device_id].poles
        if poles is not None and poles != len(spaces):
            continue
        matched[device_id] = by_set[spaces]

    taken = set(matched.values())
    unmatched_local = tuple(sorted(d for d in local if d not in matched))
    unmatched_cloud = tuple(
        sorted(info.instance_id for info in cloud if info.instance_id not in taken)
    )
    return matched, unmatched_local, unmatched_cloud


def feeds_from_kinds(kinds: Mapping[int, int]) -> dict[int, str]:
    """Which cloud feed supplies the panel and which feeds a panel downstream.

    Read from the feeder kinds of the trait snapshot. A lone feed of no known
    kind is taken to be the panel's own supply.
    """
    feeds: dict[int, str] = {}
    for instance, kind in kinds.items():
        if kind == FEEDER_MAIN_BREAKER:
            feeds[instance] = UPSTREAM
        elif kind == FEEDER_FEEDTHROUGH:
            feeds[instance] = DOWNSTREAM
    if not feeds and len(kinds) == 1:
        feeds[next(iter(kinds))] = UPSTREAM
    return feeds


# --- storage ------------------------------------------------------------------


def _history_to_options(target: HistoryTarget | None) -> dict[str, Any] | None:
    if target is None:
        return None
    return {"resource": target.resource, "instance": target.instance, "node": target.node}


def _history_from_options(raw: Any) -> HistoryTarget | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        return HistoryTarget(str(raw["resource"]), int(raw["instance"]), str(raw["node"]))
    except (KeyError, TypeError, ValueError):
        return None


def link_record(link: PanelLink) -> dict[str, Any]:
    """The stored form: plain JSON, so feed instances become string keys."""
    return {
        "local_serial": link.local_serial,
        "circuits": dict(link.circuits),
        "feeds": {str(n): kind for n, kind in link.feeds.items()},
        "unmatched_local": list(link.unmatched_local),
        "unmatched_cloud": list(link.unmatched_cloud),
        "site_history": _history_to_options(link.site_history),
        "site_panel_count": link.site_panel_count,
        "linked_at": link.linked_at,
    }


def link_from_options(options: Mapping[str, Any]) -> PanelLink | None:
    """The entry's link, or None if it has none or the record can't be read."""
    links = options.get(CONF_LINKS)
    if not isinstance(links, Mapping) or not links:
        return None
    cloud_key, record = next(iter(links.items()))
    if not isinstance(record, Mapping):
        return None
    try:
        return PanelLink(
            cloud_key=str(cloud_key),
            local_serial=str(record["local_serial"]),
            circuits={str(c): int(n) for c, n in dict(record.get("circuits") or {}).items()},
            feeds={
                int(n): kind
                for n, kind in dict(record.get("feeds") or {}).items()
                if kind in (UPSTREAM, DOWNSTREAM)
            },
            unmatched_local=tuple(str(c) for c in record.get("unmatched_local") or ()),
            unmatched_cloud=tuple(int(n) for n in record.get("unmatched_cloud") or ()),
            site_history=_history_from_options(record.get("site_history")),
            site_panel_count=int(record.get("site_panel_count") or 0),
            linked_at=str(record.get("linked_at") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


def link_to_options(options: Mapping[str, Any], link: PanelLink) -> dict[str, Any]:
    """Options with this link stored as the entry's only link."""
    return {**options, CONF_LINKS: {link.cloud_key: link_record(link)}}


def options_without_link(options: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in options.items() if key != CONF_LINKS}


def unlinked_entry(
    data: Mapping[str, Any],
    options: Mapping[str, Any],
    *,
    data_keys: Iterable[str],
    option_keys: Iterable[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """An entry's data and options with its cloud fallback taken out.

    Drops the link and the given account keys; everything local stays.
    """
    dropped_data, dropped_options = set(data_keys), set(option_keys)
    return (
        {key: value for key, value in data.items() if key not in dropped_data},
        {
            key: value
            for key, value in options_without_link(options).items()
            if key not in dropped_options
        },
    )


# --- entry versions -------------------------------------------------------------

# Entries with a link are minor version 2, so a version that knows nothing of
# links can tell it is looking at one. The rest stay 1.
UNLINKED_MINOR_VERSION = 1
LINKED_MINOR_VERSION = 2


def migration_ok(version: int, minor_version: int) -> bool:
    """Whether this code can run an entry stored at this version.

    Any minor of major 1 is fine: a minor bump only ever adds options. A
    newer major is not, and setup must refuse it rather than misread it.
    """
    return version <= 1
