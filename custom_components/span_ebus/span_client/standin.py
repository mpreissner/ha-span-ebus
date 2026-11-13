"""Cloud entities kept on their cloud unique ids and fed from local rows.

Pure: no Home Assistant imports. When a cloud entry is upgraded to a local
panel, most cloud entities are re-pointed onto the local row's unique id (see
`identity`). A few have no local row of the same meaning yet: the site flows,
whose values a capture still has to confirm, and the integrated site energies.
These keep their `{K}_<suffix>` unique id and show a local row's value through
a fixed transform. They exist only where the registry already has one; none is
ever created new.

The rest of the cloud's site rows have no local source at all. They are
fallback-only: they show the cloud's reading while the cloud carries the entry,
and are unavailable otherwise.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .local import model
from .local.model import BATTERY_TO_CLOUD_SIGN, PV_TO_CLOUD_SIGN, Scope


def _identity(value: float) -> float:
    return value


def _times(sign: int) -> Callable[[float], float]:
    def transform(value: float) -> float:
        return sign * value

    return transform


@dataclass(frozen=True, slots=True)
class StandIn:
    """One cloud entity fed from a local row.

    `source_row` is a `model.Row` key, or a `model.DerivedRow` key for the
    integrated energies (scope PANEL). `suppresses` says whether the local
    row's own entity is left out while the stand-in shows its value; the
    voltage, frequency and panel power pairs keep both, since the local entity
    already took over another cloud entity.
    """

    uid_suffix: str
    scope: Scope
    source_row: str
    transform: Callable[[float], float]
    suppresses: bool = True
    energy: bool = False

    def uid(self, cloud_key: str) -> str:
        return f"{cloud_key}_{self.uid_suffix}"


# The facts layer already flips the raw `power-flows/grid` to + import, which is
# the cloud's sign too, so the grid stand-in is the identity on the facts value.
STANDINS: tuple[StandIn, ...] = (
    StandIn("site/grid", Scope.PANEL, "grid_power", _identity),
    StandIn("site/home", Scope.PANEL, "site_power", _identity),
    StandIn("site/solar", Scope.PANEL, "pv_power", _times(PV_TO_CLOUD_SIGN)),
    StandIn("site/battery", Scope.PANEL, "battery_power", _times(BATTERY_TO_CLOUD_SIGN)),
    StandIn("site/voltage_l1", Scope.PANEL, "voltage_a", _identity, suppresses=False),
    StandIn("site/voltage_l2", Scope.PANEL, "voltage_b", _identity, suppresses=False),
    StandIn("site/frequency", Scope.PANEL, "frequency", _identity, suppresses=False),
    StandIn("panel/power", Scope.UPSTREAM, "current_power", _identity, suppresses=False),
    StandIn("site/home_energy", Scope.PANEL, "site_energy", _identity, energy=True),
    StandIn("site/solar_energy", Scope.PANEL, "pv_energy", _identity, energy=True),
    StandIn("site/battery_energy", Scope.PANEL, "battery_energy", _identity, energy=True),
)

STANDIN_SUFFIXES: frozenset[str] = frozenset(s.uid_suffix for s in STANDINS)

_DIRECTIONAL = (
    "grid_to_home",
    "grid_to_battery",
    "solar_to_home",
    "solar_to_battery",
    "solar_to_grid",
    "battery_to_home",
    "battery_to_grid",
)
_NO_LOCAL_PROPERTY = ("generator", "balance", "residual")

# Site rows with no local source: the panel publishes only the four net flows,
# so no directional split can be derived from them. A multi-panel site's grid
# energy and unclassified feeds are fallback-only too, but that depends on the
# link, so `identity.rewrite_plan` decides those.
FALLBACK_ONLY: frozenset[str] = frozenset(
    f"site/{flow}{suffix}"
    for flow in _DIRECTIONAL + _NO_LOCAL_PROPERTY
    for suffix in ("", "_energy")
)


def standins_for(registry_uids: Iterable[str], cloud_key: str) -> tuple[StandIn, ...]:
    """The stand-ins this entry has: those whose cloud uid is in the registry."""
    present = set(registry_uids)
    return tuple(s for s in STANDINS if s.uid(cloud_key) in present)


def suppressed_rows(standins: Iterable[StandIn]) -> frozenset[tuple[Scope, str]]:
    """The local rows (scope, row key) whose own entity a stand-in replaces."""
    return frozenset((s.scope, s.source_row) for s in standins if s.suppresses)


def row_keys() -> frozenset[tuple[Scope, str]]:
    """Every local row a stand-in may read, for checking the table."""
    rows = {(row.scope, row.key) for row in model.ROWS}
    rows |= {(Scope.PANEL, row.key) for row in model.DERIVED_ROWS}
    return frozenset(rows)
