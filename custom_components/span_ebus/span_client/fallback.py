"""What the cloud shows for a local value while the local link is down.

Pure: no Home Assistant imports. `resolve` maps one local row (scope, device,
property) onto the cloud readings of the linked panel, in the local row's
frame. `RegisterContinuation` keeps the panel's imported-energy register
climbing from where the local one stopped while the cloud stands in for it.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

from .link import DOWNSTREAM, UPSTREAM, PanelLink
from .local.model import NO_FALLBACK, Scope
from .overhead import FEEDTHROUGH_SIGN

ACTIVE_POWER = "meter/active-power"
IMPORTED_ENERGY = "meter/imported-energy"
RELAY = "switch/relay"
SITE_GRID = "site/grid"
RELAY_STATES = frozenset({"CLOSED", "OPEN"})

# Every (scope, local property) the fallback can supply.
FALLBACK_KEYS: frozenset[tuple[Scope, str]] = frozenset(
    {
        (Scope.CIRCUIT, ACTIVE_POWER),
        (Scope.CIRCUIT, "meter/current"),
        (Scope.CIRCUIT, RELAY),
        (Scope.PANEL, "meter/voltage-a"),
        (Scope.PANEL, "meter/voltage-b"),
        (Scope.PANEL, "meter/frequency"),
        (Scope.PANEL, "meter/busbar-current"),
        (Scope.PANEL, "power-flows/grid"),
        (Scope.PANEL, "power-flows/site"),
        (Scope.UPSTREAM, ACTIVE_POWER),
        (Scope.UPSTREAM, "meter/current-a"),
        (Scope.UPSTREAM, "meter/current-b"),
        (Scope.UPSTREAM, IMPORTED_ENERGY),
        (Scope.DOWNSTREAM, ACTIVE_POWER),
    }
)

# Panel values the cloud carries on the panel node, with the site node as the
# second choice.
_PANEL_KEYS: Mapping[str, tuple[str, ...]] = {
    "meter/voltage-a": ("panel/voltage_l1", "site/voltage_l1"),
    "meter/voltage-b": ("panel/voltage_l2", "site/voltage_l2"),
    "meter/frequency": ("panel/frequency", "site/frequency"),
    "meter/busbar-current": ("panel/current",),
    # Both are + import / + consumed on each side, so they pass as they are.
    "power-flows/grid": (SITE_GRID,),
    "power-flows/site": ("site/home",),
}
_UPSTREAM_CURRENTS: Mapping[str, str] = {
    "meter/current-a": "panel/current_l1",
    "meter/current-b": "panel/current_l2",
}


def _number(reading: Any) -> float | None:
    """A cloud reading as a float. Cloud values arrive as "%.3f" strings."""
    raw = getattr(reading, "value", reading)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _first(data: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key in data:
            return _number(data[key])
    return None


def _feed(link: PanelLink, kind: str) -> int | None:
    return next((n for n, k in link.feeds.items() if k == kind), None)


def _upstream_power(link: PanelLink, data: Mapping[str, Any]) -> float | None:
    feed = _feed(link, UPSTREAM)
    if feed is not None:
        return _number(data.get(f"feed-{feed}/power"))
    feeds = [key for key in data if key.startswith("feed-") and key.endswith("/power")]
    if len(feeds) == 1:
        return _number(data[feeds[0]])
    return _number(data.get("panel/power"))


def resolve(
    link: PanelLink,
    data: Mapping[str, Any],
    scope: Scope,
    device_id: str,
    key: str,
    *,
    producer: bool = False,
    energy: Callable[[str], float | None] | None = None,
) -> Any:
    """The cloud's value for a local row, in the local row's frame.

    NO_FALLBACK where the cloud has no counterpart (an unmapped row, a circuit
    the link did not match, a feedthrough the link does not know); None where
    it has one that carries no reading yet. `producer` says the local circuit
    is signed + producing (solar, generator), where the cloud signs every
    circuit + consuming. `energy` gives the cloud's running kWh for a power
    key; without it the imported-energy register has no fallback.
    """
    if scope is Scope.CIRCUIT:
        instance = link.circuits.get(device_id)
        if instance is None:
            return NO_FALLBACK
        node = f"circuit-{instance}"
        if key == ACTIVE_POWER:
            value = _number(data.get(f"{node}/power"))
            return -value if producer and value is not None else value
        if key == "meter/current":
            return _number(data.get(f"{node}/current"))
        if key == RELAY:
            reading = data.get(f"{node}/relay")
            state = getattr(reading, "value", reading)
            return state if state in RELAY_STATES else None
        return NO_FALLBACK

    if scope is Scope.PANEL:
        keys = _PANEL_KEYS.get(key)
        return NO_FALLBACK if keys is None else _first(data, keys)

    if scope is Scope.UPSTREAM:
        if key == ACTIVE_POWER:
            return _upstream_power(link, data)
        if key in _UPSTREAM_CURRENTS:
            return _number(data.get(_UPSTREAM_CURRENTS[key]))
        if key == IMPORTED_ENERGY:
            # The site's grid energy is this panel's only when the site has no
            # other panel to share it with.
            if energy is None or not link.single_panel_site:
                return NO_FALLBACK
            return energy(SITE_GRID)
        return NO_FALLBACK

    if scope is Scope.DOWNSTREAM and key == ACTIVE_POWER:
        feed = _feed(link, DOWNSTREAM)
        if feed is None:
            return NO_FALLBACK
        value = _number(data.get(f"feed-{feed}/power"))
        return None if value is None else FEEDTHROUGH_SIGN * value

    return NO_FALLBACK


class RegisterContinuation:
    """Carry a local energy register across a spell on the cloud, in kWh.

    On LOCAL the register shows the panel's own value. Once the cloud takes
    over, it shows the last local value plus what the cloud's total has grown
    by since the cloud started. On the way back it shows the larger of the
    local value and the highest shown, so it never steps down; a local value
    well below the last one is a reset and passes through. Nothing persists:
    a restart starts from the local value again.
    """

    RESET_RATIO = 0.9

    def __init__(self) -> None:
        self._local_last: float | None = None
        self._high: float | None = None
        self._mark: float | None = None

    def cloud_started(self) -> None:
        """The cloud has taken over: its next value is the mark to count from."""
        self._mark = None

    def local(self, value: float | None) -> float | None:
        if value is None:
            return None
        last = self._local_last
        self._local_last = value
        if last is not None and value < self.RESET_RATIO * last:
            self._high = None
            return value
        if self._high is None:
            return value
        if value >= self._high:
            self._high = None
            return value
        return self._high

    def cloud(self, total: float | None) -> float | None:
        if self._local_last is None:
            return None
        if total is None:
            return self._high if self._high is not None else self._local_last
        if self._mark is None or total < self._mark:
            # The first value, or a cloud total that went back: count from here,
            # holding what is shown.
            held = self._high if self._high is not None else self._local_last
            self._mark = total - (held - self._local_last)
        shown = self._local_last + max(0.0, total - self._mark)
        if self._high is not None:
            shown = max(shown, self._high)
        self._high = shown
        return shown
