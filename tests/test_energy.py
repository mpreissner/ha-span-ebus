"""Tests for the power -> energy integration.

`energy` imports nothing from Home Assistant precisely so this can run: the
suite has no `homeassistant` installed, and the arithmetic is the part of the
energy sensors worth pinning down.
"""

import pytest
from energy import EnergyAccumulator


def test_a_steady_kilowatt_for_an_hour_is_a_kilowatt_hour():
    acc = EnergyAccumulator(max_gap_seconds=7200.0)
    acc.add(1000.0, 0.0)
    acc.add(1000.0, 3600.0)

    assert acc.total_kwh == pytest.approx(1.0)


def test_the_first_sample_only_sets_a_baseline():
    # There is no interval to close yet, so nothing is added however large it is.
    acc = EnergyAccumulator(max_gap_seconds=180.0)
    acc.add(5000.0, 100.0)

    assert acc.total_kwh == 0.0


def test_a_ramp_is_integrated_as_a_trapezoid_not_a_step():
    # 0 W -> 2000 W over an hour is 1 kWh by area; holding either endpoint would
    # give 0 or 2.
    acc = EnergyAccumulator(max_gap_seconds=7200.0)
    acc.add(0.0, 0.0)
    acc.add(2000.0, 3600.0)

    assert acc.total_kwh == pytest.approx(1.0)


def test_export_does_not_run_the_meter_backwards():
    # The site `grid` flow is signed and goes negative on export. A
    # total_increasing sensor that decreases reads as a meter reset, so only the
    # positive part is consumption.
    acc = EnergyAccumulator(max_gap_seconds=7200.0)
    acc.add(1000.0, 0.0)
    acc.add(1000.0, 3600.0)
    before = acc.total_kwh

    acc.add(-4000.0, 7200.0)

    assert acc.total_kwh >= before


def test_a_pure_export_window_contributes_nothing():
    acc = EnergyAccumulator(max_gap_seconds=7200.0)
    acc.add(-3000.0, 0.0)
    acc.add(-3000.0, 3600.0)

    assert acc.total_kwh == 0.0


def test_a_gap_longer_than_the_limit_is_not_bridged():
    # The stream was down; we have no readings for that window, and guessing one
    # would write a fabricated step into long-term statistics.
    acc = EnergyAccumulator(max_gap_seconds=180.0)
    acc.add(2000.0, 0.0)
    acc.add(2000.0, 1000.0)

    assert acc.total_kwh == 0.0

    # ...but the sample still becomes the baseline, so accounting resumes at once.
    acc.add(2000.0, 1180.0)
    assert acc.total_kwh == pytest.approx(0.1)


def test_a_repeated_or_out_of_order_timestamp_cannot_subtract_energy():
    # Frames carry SPAN's own epoch millis, so neither is hypothetical.
    acc = EnergyAccumulator(max_gap_seconds=180.0)
    acc.add(1000.0, 100.0)
    acc.add(1000.0, 100.0)
    assert acc.total_kwh == 0.0

    acc.add(1000.0, 50.0)
    assert acc.total_kwh == 0.0


def test_a_restored_total_is_carried_forward_not_restarted():
    acc = EnergyAccumulator(max_gap_seconds=180.0, total_kwh=12.5)
    acc.add(3600.0, 0.0)
    acc.add(3600.0, 10.0)

    assert acc.total_kwh == pytest.approx(12.51)
