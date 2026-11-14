"""Which cloud entity becomes which local one when a cloud entry gets its panel.

Pure: no Home Assistant imports. A cloud entry's entities have unique ids
`{K}_<node>/<prop>`. Upgrading the entry to a local panel re-points each one
that has a local row of the same meaning onto that row's unique id, so its
entity_id, history, statistics and Energy dashboard place survive. The rest
stay on their cloud uid: stand-ins fed from a local row, fallback-only rows
fed by the cloud alone, and circuits the matcher could not pair.

Every `{K}_` entity lands in exactly one of those groups. Entities whose uid
does not start with `{K}_` are not the cloud's and are left out.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .link import DOWNSTREAM, UPSTREAM, PanelLink
from .local.model import (
    DERIVED_ROWS,
    ROWS,
    DerivedRow,
    Row,
    Scope,
    Snapshot,
    circuit_uid,
    lugs_uid,
    panel_uid,
    row_devices,
    row_exists,
    row_uid,
)
from .standin import FALLBACK_ONLY, STANDIN_SUFFIXES


@dataclass(frozen=True, slots=True)
class RegistryEntryView:
    """The two registry fields the plan reads."""

    entity_id: str
    unique_id: str


@dataclass(frozen=True)
class RewritePlan:
    repoint: tuple[tuple[str, str, str], ...]  # (entity_id, old uid, new uid)
    standin: tuple[str, ...]  # entity_ids kept on K uids, fed locally
    fallback_only: tuple[str, ...]  # entity_ids with no local source
    unmatched: tuple[str, ...]  # entity_ids of unmatched cloud circuits
    energy: tuple[str, ...]  # entity_ids of re-pointed register energies

    @property
    def changes(self) -> tuple[tuple[str, str, str], ...]:
        """The re-points that change something (the transport uid may not)."""
        return tuple(r for r in self.repoint if r[1] != r[2])

    def reverse(self) -> tuple[tuple[str, str, str], ...]:
        """The re-points that undo `changes`, newest first."""
        return tuple((eid, new, old) for eid, old, new in reversed(self.changes))


def collisions(plan: RewritePlan, existing_uids: Iterable[str]) -> int:
    """How many new uids already belong to some entity (abort if any)."""
    existing = set(existing_uids)
    return sum(1 for _eid, _old, new in plan.changes if new in existing)


# Cloud property -> local source, per node kind.
_CIRCUIT_KEYS: Mapping[str, str] = {
    "power": "meter/active-power",
    "current": "meter/current",
    "relay": "switch/relay",
    "power_energy": "meter/exported-energy",
}
_CIRCUIT_ENERGY = "power_energy"
_PANEL_KEYS: Mapping[str, str] = {
    "voltage_l1": "meter/voltage-a",
    "voltage_l2": "meter/voltage-b",
    "frequency": "meter/frequency",
    "current": "meter/busbar-current",
    "overhead": "derived/overhead",
    "overhead_energy": "derived/overhead-energy",
}
_PANEL_LUGS_KEYS: Mapping[str, str] = {
    "current_l1": "meter/current-a",
    "current_l2": "meter/current-b",
}
_FEED_POWER = "power"
_LUGS_POWER = "meter/active-power"
_GRID_ENERGY = "site/grid_energy"
_LUGS_IMPORTED = "meter/imported-energy"
_TRANSPORT = "transport"

_CIRCUIT = re.compile(r"circuit-(\d+)/(.+)")
_FEED = re.compile(r"feed-(\d+)/(.+)")


def local_rows(snapshot: Snapshot) -> list[tuple[Row | DerivedRow, str]]:
    """Every row the local panel creates from `snapshot`, with its device id.

    The same rows the local platforms add: a register row per device that
    describes it, and each derived row whose inputs are published.
    """
    rows: list[tuple[Row | DerivedRow, str]] = [
        (row, device_id)
        for row in ROWS
        for device_id in row_devices(snapshot, row)
        if row_exists(snapshot, row, device_id)
    ]
    rows += [(row, "panel") for row in DERIVED_ROWS if row.when(snapshot)]
    return rows


def confirm_counts(plan: RewritePlan) -> dict[str, int]:
    """What the link confirmation shows: how many of each group."""
    total = len(plan.repoint) + len(plan.standin) + len(plan.fallback_only) + len(plan.unmatched)
    return {
        "matched": len(plan.repoint),
        "total": total,
        "standins": len(plan.standin),
        "fallback_only": len(plan.fallback_only),
        "unmatched": len(plan.unmatched),
    }


# What the cloud reports as its serial before it knows the panel's: names no panel.
PLACEHOLDER_SERIAL = "span-cloud"
LINK_NOT_RUNNING = "link_not_running"
LINK_NOTHING_MATCHED = "link_nothing_matched"


def stem_problem(stem: str | None) -> str | None:
    """An abort reason if the cloud entities' uid stem can't name this panel."""
    if not stem or stem == PLACEHOLDER_SERIAL:
        return LINK_NOT_RUNNING
    return None


def plan_problem(plan: RewritePlan) -> str | None:
    """An abort reason if linking would move no entity onto the panel."""
    return LINK_NOTHING_MATCHED if not plan.repoint else None


def transport_uid(panel_key: str) -> str:
    return f"{panel_key}_{_TRANSPORT}"


@dataclass(frozen=True, slots=True)
class _Local:
    """The local uids that exist, and which lugs device is U and which D."""

    uids: frozenset[str]
    upstream: str | None
    downstream: str | None


def _local(local_rows: Iterable[tuple[Row | DerivedRow, str]], serial: str) -> _Local:
    uids: set[str] = set()
    upstream = downstream = None
    for row, device_id in local_rows:
        if isinstance(row, DerivedRow):
            uids.add(panel_uid(serial, row.source))
            continue
        uids.add(row_uid(row, serial, device_id))
        if row.scope is Scope.UPSTREAM:
            upstream = device_id
        elif row.scope is Scope.DOWNSTREAM:
            downstream = device_id
    return _Local(frozenset(uids), upstream, downstream)


def _feed_sides(link: PanelLink, feeds_seen: Iterable[int]) -> dict[int, str]:
    """U or D for each cloud feed: the link's record, else a lone feed is U."""
    sides = dict(link.feeds)
    seen = set(feeds_seen)
    if not sides and len(seen) == 1:
        sides[next(iter(seen))] = UPSTREAM
    return sides


def rewrite_plan(
    registry_entries: Iterable[RegistryEntryView],
    link: PanelLink,
    local_rows: Iterable[tuple[Row | DerivedRow, str]],
    *,
    cloud_stem: str | None = None,
) -> RewritePlan:
    """Sort the cloud entry's entities into re-points, stand-ins and the rest.

    `cloud_stem` is what the cloud entity uids start with (`{stem}_...`): the
    running cloud coordinator's serial, which is not always the panel key the
    link records. It defaults to `link.cloud_key`.

    `local_rows` is every local row the panel creates, with its device id;
    derived rows are passed with any device id. A cloud entity whose local
    target the panel does not create stays on the cloud: a relay switch with
    no local switch gets no feed at all (unmatched), anything else is
    fallback-only. If two cloud entities would take the same local uid, the
    first in entity order does and the rest are fallback-only.
    """
    serial = link.local_serial
    prefix = f"{cloud_stem or link.cloud_key}_"
    entries = sorted(
        (e for e in registry_entries if e.unique_id.startswith(prefix)),
        key=lambda e: e.unique_id,
    )
    local = _local(local_rows, serial)
    by_cloud = {n: c for c, n in link.circuits.items()}
    feeds_seen = [
        int(m.group(1)) for e in entries if (m := _FEED.fullmatch(e.unique_id[len(prefix) :]))
    ]
    sides = _feed_sides(link, feeds_seen)
    lugs = {UPSTREAM: local.upstream, DOWNSTREAM: local.downstream}

    repoint: list[tuple[str, str, str]] = []
    standin: list[str] = []
    fallback: list[str] = []
    unmatched: list[str] = []
    energy: list[str] = []
    taken: set[str] = set()

    def target(suffix: str) -> tuple[str | None, bool]:
        """(new uid or None, whether it is a register energy)."""
        if suffix == _TRANSPORT:
            return transport_uid(serial), False
        if m := _CIRCUIT.fullmatch(suffix):
            prop = m.group(2)
            circuit = by_cloud.get(int(m.group(1)))
            if circuit is None or prop not in _CIRCUIT_KEYS:
                return None, False
            return circuit_uid(serial, circuit, _CIRCUIT_KEYS[prop]), prop == _CIRCUIT_ENERGY
        if m := _FEED.fullmatch(suffix):
            device = lugs.get(sides.get(int(m.group(1)), ""))
            if device is None or m.group(2) != _FEED_POWER:
                return None, False
            return lugs_uid(serial, device, _LUGS_POWER), False
        node, _, prop = suffix.partition("/")
        if node == "panel" and prop in _PANEL_KEYS:
            return panel_uid(serial, _PANEL_KEYS[prop]), False
        if node == "panel" and prop in _PANEL_LUGS_KEYS and local.upstream is not None:
            return lugs_uid(serial, local.upstream, _PANEL_LUGS_KEYS[prop]), False
        if suffix == _GRID_ENERGY and link.single_panel_site and local.upstream is not None:
            return lugs_uid(serial, local.upstream, _LUGS_IMPORTED), True
        return None, False

    for entry in entries:
        suffix = entry.unique_id[len(prefix) :]
        if suffix in STANDIN_SUFFIXES:
            standin.append(entry.entity_id)
            continue
        if suffix in FALLBACK_ONLY:
            fallback.append(entry.entity_id)
            continue
        circuit = _CIRCUIT.fullmatch(suffix)
        if circuit and int(circuit.group(1)) not in by_cloud:
            unmatched.append(entry.entity_id)
            continue
        new, is_energy = target(suffix)
        exists = new is not None and (suffix == _TRANSPORT or new in local.uids)
        if not exists or new in taken:
            if circuit and circuit.group(2) == "relay":
                unmatched.append(entry.entity_id)
            else:
                fallback.append(entry.entity_id)
            continue
        taken.add(new)
        repoint.append((entry.entity_id, entry.unique_id, new))
        if is_energy:
            energy.append(entry.entity_id)

    return RewritePlan(
        repoint=tuple(repoint),
        standin=tuple(standin),
        fallback_only=tuple(fallback),
        unmatched=tuple(unmatched),
        energy=tuple(energy),
    )
