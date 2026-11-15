"""What an entity shows: readings rounded to what is worth showing, and whether
that changed since it was last written.

Kept free of Home Assistant imports so the tests can reach it. A reading that
only jitters below what anyone reads would otherwise write a recorder row on
every update.
"""

from __future__ import annotations

import math
from typing import Any

# Decimal places kept per unit. The keys are the unit strings Home Assistant's
# unit enums carry (UnitOfPower.WATT == "W"), so either can be looked up. Energy
# is not rounded: it is shown to sensor.ENERGY_PRECISION and statistics need
# the register.
ROUNDING: dict[str, int] = {
    "W": 0,
    "A": 1,
    "V": 1,
    "Hz": 2,
}


def round_value(value: Any, unit: str | None, digits: int | None = None) -> Any:
    """Round a reading to what its unit is worth showing. -0 is shown as 0.
    `digits` overrides the unit's default.
    """
    if digits is None:
        digits = ROUNDING.get(unit) if unit is not None else None
    if digits is None or isinstance(value, bool) or not isinstance(value, int | float):
        return value
    if digits == 0:
        return round(value)
    return round(value, digits) + 0.0


def cloud_reading(value: Any, unit: str | None) -> float | None:
    """A cloud reading as a rounded number, or None when it is not a finite one.

    Non-finite readings are dropped rather than rounded: round() raises on them,
    and a NaN never compares equal to itself, so it would be written every time.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round_value(number, unit)


class ChangeGate:
    """Remembers what was last written and says whether a new one differs.

    The first call always reports a change, so the first update after the
    entity is added is written.
    """

    __slots__ = ("_last",)

    _UNSET: Any = object()

    def __init__(self) -> None:
        self._last: Any = self._UNSET

    def changed(self, shown: Any) -> bool:
        if shown == self._last:
            return False
        self._last = shown
        return True
