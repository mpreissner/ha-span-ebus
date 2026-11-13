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


# --- what a linked entry builds for its kept cloud entities ------------------------

STANDIN = "standin"
FALLBACK = "fallback_only"
UNMATCHED = "unmatched"
_TRANSPORT = "transport"
_ENERGY_SUFFIX = "_energy"


def classify_kept(suffixes: Iterable[str], unmatched: Iterable[str]) -> dict[str, str]:
    """suffix -> STANDIN, FALLBACK or UNMATCHED for each kept cloud uid suffix.

    `suffixes` are what follows `{stem}_` in the registry's kept uids;
    `unmatched` the ones the link could not pair, recorded at link time. The
    transport uid is the local transport sensor's and is left out. Anything
    else kept on the cloud uid has no local row: it is fallback-only.
    """
    skipped = set(unmatched)
    out: dict[str, str] = {}
    for suffix in suffixes:
        if suffix == _TRANSPORT:
            continue
        if suffix in STANDIN_SUFFIXES:
            out[suffix] = STANDIN
        elif suffix in skipped:
            out[suffix] = UNMATCHED
        else:
            out[suffix] = FALLBACK
    return out


def standin_by_suffix(suffix: str) -> StandIn | None:
    return next((s for s in STANDINS if s.uid_suffix == suffix), None)


def is_energy(suffix: str) -> bool:
    return suffix.endswith(_ENERGY_SUFFIX)


def ledger_key(suffix: str) -> str:
    """The cloud ledger key a kept `..._energy` entity totals: its power key."""
    return suffix[: -len(_ENERGY_SUFFIX)] if is_energy(suffix) else suffix


# device class -> (unit, state class), for kept rows with no local presentation.
# Read from the registry's original device class, not its unit: the user may
# have changed the display unit.
_LOOKS: dict[str, tuple[str, str]] = {
    "power": ("W", "measurement"),
    "current": ("A", "measurement"),
    "voltage": ("V", "measurement"),
    "frequency": ("Hz", "measurement"),
}


def look(device_class: str | None, suffix: str) -> tuple[str | None, str | None]:
    """(native unit, state class) for a kept cloud sensor, from its device class.

    The cloud's energy totals are kWh; the only other energy it reports is a
    raw Wh reading.
    """
    if device_class == "energy":
        return ("kWh", "total_increasing") if is_energy(suffix) else ("Wh", "total_increasing")
    return _LOOKS.get(device_class or "", (None, None))


class HeldTotal:
    """A fallback-only energy total: held outside the cloud, grown on it.

    Fed the cloud ledger's total for its key while the cloud carries the entry,
    and None otherwise. The first total after each cloud start only sets the
    mark; after that the total grows by what the ledger grew. A ledger that
    goes back is a new mark, not a negative step. With nothing restored the
    total adopts the ledger's.
    """

    __slots__ = ("mark", "total")

    def __init__(self, total: float | None = None) -> None:
        self.total = total
        self.mark: float | None = None

    def sample(self, ledger: float | None) -> bool:
        """Take one reading; True if the total changed."""
        if ledger is None:
            self.mark = None
            return False
        if self.total is None:
            self.total = self.mark = ledger
            return True
        mark, self.mark = self.mark, ledger
        if mark is None or ledger <= mark:
            return False
        self.total += ledger - mark
        return True
