"""Turning SPAN's metered intervals into a running energy total.

The panel meters energy itself and reports it as buckets — "0.0175 kWh between
17:15 and 17:30" — never as a counter. Home Assistant wants the opposite: one
number per entity that only ever goes up. This is the piece in between, kept
deliberately free of any Home Assistant import so it can be tested on its own;
the suite runs without `homeassistant` installed, and this is the part worth
testing.

Two facts about the data shape everything here:

* **The newest bucket is still open.** A 15-minute bucket returned at 17:20 is
  five minutes' worth and will come back larger. Waiting for it to close would
  step the sensor every quarter hour; counting its growth each poll keeps it
  moving, at the cost of having to remember what was already counted.
* **Buckets can be revisited, and can be re-cut.** A backfill at hourly
  resolution and a live poll at 15-minutely describe the same time twice. Only
  one of them may be counted, and which is settled by arrival order — see
  `EnergyLedger.apply`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only; see the module docstring
    from .span_client.models import EnergySample


@dataclass
class _Entry:
    """One key's total, and the bucket the total was last advanced by.

    `last_start_ms is None` means the key has a total but no mark yet — a
    restored total waiting to be tied to a bucket — so the next batch sets the
    mark without counting anything into it.
    """

    total_kwh: float = 0.0
    last_start_ms: int | None = None
    last_end_ms: int | None = None
    counted_kwh: float = 0.0


class EnergyLedger:
    """Running kWh per property key, advanced by metered intervals.

    Every total is `total_increasing` as far as Home Assistant is concerned, so
    the one thing this must never do is go backwards or count an interval twice.
    Three rules do that work, applied per key to buckets sorted oldest first:

    * A bucket with **the same bounds as the last one counted** is the open
      interval growing. Only the growth is added, and only if it is positive: a
      figure the server revises down is left alone rather than subtracted.
    * A bucket **starting at or after the last one ended** is new time. It is
      added whole and becomes the new mark.
    * Anything else **overlaps** time already accounted for — a coarse bucket
      arriving alongside fine ones, or a re-cut boundary — and is ignored.

    A key's first appearance only sets the mark; its buckets are not counted.
    The window a poll asks for is hours wide, and counting all of it would book
    hours of energy in the instant an entity appears. What the sensor is for is
    what happens from then on.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        # Keys whose total came from storage, and keys already adopted this run.
        # Both are closed to `adopt`, which may only ever seed a total.
        self._sealed: set[str] = set()

    # --- reading -----------------------------------------------------------

    def total(self, key: str) -> float | None:
        """The running total for `key`, or None if it has never been seen."""
        entry = self._entries.get(key)
        return None if entry is None else entry.total_kwh

    @property
    def covered_through_ms(self) -> int:
        """The end of the newest interval counted, across all keys.

        This is the poll cursor: everything before it has been accounted for,
        and the gap between it and now is what a restart has to make up. The
        newest rather than the oldest, because a circuit that draws nothing
        reports no buckets at all and would otherwise hold the cursor back
        forever.
        """
        ends = [e.last_end_ms for e in self._entries.values() if e.last_end_ms is not None]
        return max(ends) if ends else 0

    # --- writing -----------------------------------------------------------

    def adopt(self, key: str, total_kwh: float) -> bool:
        """Seed a key's total from elsewhere, once, before it is ever metered.

        This is how an upgrade from the locally integrated energy keeps its
        history: the entity restores the total it had and hands it over here, and
        measured intervals carry on from there. Rebasing onto SPAN's lifetime
        figure instead would book the difference — hundreds of kWh — as an hour's
        consumption on the Energy dashboard.

        Ignored once the key has a stored or adopted total, so re-adding an
        entity cannot rewind it.
        """
        if key in self._sealed or key in self._entries:
            return False
        self._sealed.add(key)
        self._entries[key] = _Entry(total_kwh=total_kwh)
        return True

    def apply(self, samples: Iterable[EnergySample]) -> bool:
        """Fold a batch of metered intervals in. True if any total moved.

        Order within the batch does not matter — buckets are sorted per key — but
        order *between* batches does: supply the coarse backfill before the fine
        live window, since whichever arrives first claims the time.
        """
        grouped: dict[str, list[EnergySample]] = {}
        for sample in samples:
            grouped.setdefault(sample.key, []).append(sample)

        changed = False
        for key, group in grouped.items():
            group.sort(key=lambda s: (s.start_ms, s.end_ms))
            entry = self._entries.setdefault(key, _Entry())
            if entry.last_start_ms is None:
                newest = group[-1]
                entry.last_start_ms = newest.start_ms
                entry.last_end_ms = newest.end_ms
                entry.counted_kwh = newest.kwh
                changed = True
                continue
            for sample in group:
                changed |= self._advance(entry, sample)
        return changed

    def _advance(self, entry: _Entry, sample: EnergySample) -> bool:
        """Apply one bucket to one entry under the rules in the class docstring."""
        if sample.start_ms == entry.last_start_ms and sample.end_ms == entry.last_end_ms:
            growth = sample.kwh - entry.counted_kwh
            if growth <= 0:
                return False
            entry.counted_kwh = sample.kwh
            entry.total_kwh += growth
            return True

        if entry.last_end_ms is not None and sample.start_ms >= entry.last_end_ms:
            entry.last_start_ms = sample.start_ms
            entry.last_end_ms = sample.end_ms
            entry.counted_kwh = sample.kwh
            if sample.kwh <= 0:
                # An idle interval still moves the mark; there is just nothing
                # to add. Leaving the mark behind would re-count it later.
                return False
            entry.total_kwh += sample.kwh
            return True

        return False

    # --- persistence -------------------------------------------------------

    def as_dict(self) -> dict[str, dict[str, Any]]:
        """A JSON-safe snapshot, for the config entry's store."""
        return {
            key: {
                "total": entry.total_kwh,
                "start": entry.last_start_ms,
                "end": entry.last_end_ms,
                "counted": entry.counted_kwh,
            }
            for key, entry in self._entries.items()
        }

    def load(self, stored: Mapping[str, Any] | None) -> None:
        """Restore a snapshot written by `as_dict`, discarding unreadable keys.

        Everything restored is sealed against `adopt`: a stored total is the
        authoritative one, and an entity restoring its pre-measurement value
        must not overwrite it.
        """
        if not stored:
            return
        for key, raw in stored.items():
            if not isinstance(raw, Mapping):
                continue
            try:
                entry = _Entry(
                    total_kwh=float(raw["total"]),
                    last_start_ms=_opt_int(raw.get("start")),
                    last_end_ms=_opt_int(raw.get("end")),
                    counted_kwh=float(raw.get("counted", 0.0)),
                )
            except (KeyError, TypeError, ValueError):
                continue
            self._entries[key] = entry
            self._sealed.add(key)


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)
