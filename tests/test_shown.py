"""Rounding readings and gating state writes on change (`shown.py`).

The cloud sensors round with these and write only when the gate says the
shown (available, value) pair changed; the entity wiring itself needs Home
Assistant and is not importable here.
"""

from __future__ import annotations

import math

import pytest
from shown import ROUNDING, ChangeGate, cloud_reading, round_value


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (1234.6, "W", 1235),
        (-0.4, "W", 0),
        (12.345, "A", 12.3),
        (241.96, "V", 242.0),
        (59.9987, "Hz", 60.0),
        (60.004, "Hz", 60.0),
        (12.3456, "Wh", 12.3456),  # energy is never rounded
        (12.3456, None, 12.3456),
        ("on", "W", "on"),
        (True, "W", True),
        (None, "W", None),
    ],
)
def test_round_value_keeps_what_each_unit_is_worth(value, unit, expected) -> None:
    result = round_value(value, unit)
    assert result == expected
    assert type(result) is type(expected)


def test_negative_zero_is_shown_as_zero() -> None:
    assert math.copysign(1.0, round_value(-0.04, "A")) == 1.0
    assert str(round_value(-0.4, "W")) == "0"


def test_digits_override_the_unit() -> None:
    assert round_value(1.23456, "W", 3) == 1.235


def test_rounding_covers_the_measurement_units_only() -> None:
    assert ROUNDING == {"W": 0, "A": 1, "V": 1, "Hz": 2}


def test_unit_enum_keys_look_up_the_same_as_their_strings() -> None:
    from enum import StrEnum

    class Unit(StrEnum):  # what Home Assistant's UnitOfPower etc. are
        WATT = "W"

    assert round_value(10.6, Unit.WATT) == 11


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        ("1234.6", "W", 1235),
        (1234.4, "W", 1234),
        ("120.04", "V", 120.0),
        (7.25, "A", 7.2),
        ("12.3456", "Wh", 12.3456),
        ("12.3456", None, 12.3456),
        (None, "W", None),
        ("", "W", None),
        ("abc", "W", None),
        (object(), "W", None),
        (True, "W", None),
        ("nan", "W", None),
        (float("inf"), "W", None),
        (float("-inf"), "A", None),
    ],
)
def test_cloud_reading_is_a_rounded_finite_number_or_none(value, unit, expected) -> None:
    assert cloud_reading(value, unit) == expected


def test_jitter_below_the_rounding_does_not_count_as_a_change() -> None:
    gate = ChangeGate()
    assert gate.changed((True, cloud_reading("1500.2", "W")))
    assert not gate.changed((True, cloud_reading("1499.6", "W")))
    assert not gate.changed((True, cloud_reading(1500.4, "W")))
    assert gate.changed((True, cloud_reading(1500.6, "W")))


def test_the_first_update_is_always_written() -> None:
    assert ChangeGate().changed((False, None))
    assert ChangeGate().changed(None)


def test_an_availability_flip_is_written_even_when_the_value_holds() -> None:
    gate = ChangeGate()
    assert gate.changed((True, 230.0))
    assert gate.changed((False, 230.0))  # the stream went stale
    assert not gate.changed((False, 230.0))
    assert gate.changed((True, 230.0))  # and came back


def test_unknown_and_unavailable_are_different_states() -> None:
    gate = ChangeGate()
    assert gate.changed((True, None))
    assert gate.changed((False, None))


@pytest.mark.parametrize("unit", ["W", "A", "V", "Hz"])
def test_a_non_finite_fallback_reading_is_unknown_and_written_once(unit) -> None:
    # The fallback-only sensor shows (True, cloud_reading(...)) while the cloud
    # carries the entry: a NaN must neither raise nor count as a change each time.
    gate = ChangeGate()
    assert gate.changed((True, cloud_reading(float("nan"), unit)))
    assert not gate.changed((True, cloud_reading(float("nan"), unit)))
    assert not gate.changed((True, cloud_reading(float("inf"), unit)))
    assert not gate.changed((True, cloud_reading(None, unit)))
    assert gate.changed((True, cloud_reading(1.0, unit)))
