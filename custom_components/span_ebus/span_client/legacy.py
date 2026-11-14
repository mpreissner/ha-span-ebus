"""Which old cloud entities of a local entry the legacy repair offers to remove.

Pure: no Home Assistant imports. A cloud entity's unique id is
`{stem}_<suffix>`, where the suffix is `transport`, `panel/<p>`, `site/<p>`,
`circuit-<n>/<p>` or `feed-<n>/<p>`. Local unique ids never take that shape:
their device part is `distribution-enclosure/`, `lugs/<id>/`, `circuit/<id>/`
or a subdevice class, each followed by a row key with a `/` of its own. The one
exception is the local transport sensor, `{serial}_transport`.

On an entry with a local panel, a cloud-shaped entity is one of:
- a stand-in, which a local row feeds (never listed);
- an unmatched circuit or a fallback-only row, kept by a link on the entry's
  cloud stem (`standin.classify_kept` decides which);
- a duplicate: any other stem, or the cloud's own transport sensor. Releases
  before the link step had the cloud coordinator add its own entities next to
  the local ones, and nothing creates them now.

As a safety net, a uid the entry's platforms create now (`produced_uids`: the
local rows, derived rows and the transport sensor) is never listed, whatever its
shape: a wrong removal is worse than a missed duplicate.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .identity import RegistryEntryView, transport_uid
from .local import model
from .standin import FALLBACK, STANDIN, UNMATCHED, classify_kept

DUPLICATE = "duplicate"
# The confirm step names at most this many entities; the rest are a count.
SHOWN = 20

_TRANSPORT = "transport"
_CLOUD_UID = re.compile(
    r"(?P<stem>[^/]+?)_(?P<suffix>transport|(?:panel|site|circuit-\d+|feed-\d+)/[^/]+)"
)


def cloud_suffix(unique_id: str, stem: str | None = None) -> str | None:
    """The cloud suffix of a cloud-shaped uid, else None.

    With `stem`, only a uid on that stem counts, and the suffix is what follows
    `{stem}_`. Without it, any stem does.
    """
    if stem is not None:
        prefix = f"{stem}_"
        if not unique_id.startswith(prefix):
            return None
        suffix = unique_id[len(prefix) :]
        return suffix if _CLOUD_UID.fullmatch(f"x_{suffix}") else None
    m = _CLOUD_UID.fullmatch(unique_id)
    return m.group("suffix") if m else None


def produced_uids(snapshot: model.Snapshot, panel_key: str) -> frozenset[str]:
    """Every uid the local platforms create, or may create, for this snapshot.

    A superset on purpose: rows are counted whether or not they exist yet, and
    whether or not a stand-in shows them instead.
    """
    uids = {transport_uid(panel_key)}
    for row in model.ROWS:
        for device_id in model.row_devices(snapshot, row):
            uids.add(model.row_uid(row, panel_key, device_id))
    uids.update(model.panel_uid(panel_key, row.source) for row in model.DERIVED_ROWS)
    return frozenset(uids)


@dataclass(frozen=True, slots=True)
class LegacyEntities:
    """The entity_ids to offer for removal, per group, each sorted."""

    duplicate: tuple[str, ...] = ()
    unmatched: tuple[str, ...] = ()
    fallback_only: tuple[str, ...] = ()

    @property
    def entity_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.duplicate + self.unmatched + self.fallback_only))

    def __len__(self) -> int:
        return len(self.duplicate) + len(self.unmatched) + len(self.fallback_only)

    def counts(self) -> dict[str, int]:
        return {
            DUPLICATE: len(self.duplicate),
            UNMATCHED: len(self.unmatched),
            FALLBACK: len(self.fallback_only),
        }

    def shown(self, limit: int = SHOWN) -> tuple[tuple[str, ...], int]:
        """The first `limit` entity_ids, and how many more there are."""
        ids = self.entity_ids
        return ids[:limit], max(0, len(ids) - limit)


def legacy_entities(
    entries: Iterable[RegistryEntryView],
    *,
    local_serial: str,
    cloud_stem: str | None,
    unmatched: Iterable[str] = (),
    produced: Iterable[str] = (),
) -> LegacyEntities:
    """Sort a local entry's cloud-shaped entities into the groups to list.

    `entries` are this entry's own registry rows (this integration's platform).
    `cloud_stem` and `unmatched` are what a link recorded (`CONF_CLOUD_STEM`,
    `CONF_CLOUD_UNMATCHED`); both are empty on an entry that was never a cloud
    entry. `produced` are the uids the entry's platforms create now
    (`produced_uids`). Those, the local transport sensor and stand-ins are never
    listed.
    """
    live = {transport_uid(local_serial), *produced}
    on_stem: dict[str, str] = {}
    duplicate: list[str] = []
    for entry in entries:
        if entry.unique_id in live:
            continue
        if cloud_stem and (suffix := cloud_suffix(entry.unique_id, cloud_stem)) is not None:
            if suffix == _TRANSPORT:
                # The link moved the cloud's transport sensor onto the local one;
                # one under the stem came back from an older release.
                duplicate.append(entry.entity_id)
            else:
                on_stem[suffix] = entry.entity_id
            continue
        if cloud_suffix(entry.unique_id) is not None:
            duplicate.append(entry.entity_id)

    kinds = classify_kept(on_stem, unmatched)
    groups: dict[str, list[str]] = {UNMATCHED: [], FALLBACK: []}
    for suffix, kind in kinds.items():
        if kind != STANDIN:
            groups[kind].append(on_stem[suffix])
    return LegacyEntities(
        duplicate=tuple(sorted(duplicate)),
        unmatched=tuple(sorted(groups[UNMATCHED])),
        fallback_only=tuple(sorted(groups[FALLBACK])),
    )


def still_listed(confirmed: Iterable[str], current: LegacyEntities) -> tuple[str, ...]:
    """What a confirmed fix removes: listed when confirmed and still listed now."""
    now = set(current.entity_ids)
    return tuple(sorted(eid for eid in set(confirmed) if eid in now))
