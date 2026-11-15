"""The panel overhead formula shared by the cloud and local entries."""

import pytest
from span_client.overhead import FEEDTHROUGH_SIGN, overhead_w


def test_feed_less_circuits():
    assert overhead_w(1000.0, [400.0, 550.0], None, False) == pytest.approx(50.0)


def test_feedthrough_is_subtracted_when_present():
    assert overhead_w(1000.0, [400.0], 550.0, True) == pytest.approx(50.0)


def test_feedthrough_is_ignored_without_downstream_lugs():
    # Not an input at all, so even a missing reading does not block the result.
    assert overhead_w(1000.0, [400.0], None, False) == pytest.approx(600.0)
    assert overhead_w(1000.0, [400.0], 550.0, False) == pytest.approx(600.0)


def test_any_missing_input_gives_none():
    assert overhead_w(None, [400.0], None, False) is None
    assert overhead_w(1000.0, [400.0, None], None, False) is None
    assert overhead_w(1000.0, [400.0], None, True) is None


def test_floored_at_zero():
    assert overhead_w(100.0, [400.0], None, False) == 0.0


def test_no_circuits_is_the_whole_feed():
    assert overhead_w(12.0, [], None, False) == pytest.approx(12.0)


# A two-panel capture: an upstream panel whose feedthrough lugs feed a smaller
# downstream panel, with a ~1.4 kW test load switched on the downstream one.
# Means per phase, in W.
UPSTREAM_FEED = (1135.8, 2662.7, 1463.4, 2703.8, 1309.3)
UPSTREAM_THROUGH = (264.5, 1664.9, 361.1, 1595.5, 280.8)
UPSTREAM_CIRCUITS = (860.2, 937.1, 1100.8, 1101.0, 950.6)
UPSTREAM_OVERHEAD = (11.1, 60.7, 1.5, 7.3, 77.9)

DOWNSTREAM_FEED = (278.1, 1671.0, 382.1, 1716.4)
DOWNSTREAM_CIRCUITS = (267.6, 1658.1, 375.4, 1697.0)
DOWNSTREAM_OVERHEAD = (10.5, 12.9, 6.7, 19.4)


def test_feedthrough_reads_positive_towards_the_downstream_panel():
    # The upstream feed carries the downstream panel's whole load and the
    # feedthrough reads that load as a positive figure, so it is subtracted
    # as published. Flipping this would report ~0.3-3.3 kW of "overhead".
    assert FEEDTHROUGH_SIGN == 1


@pytest.mark.parametrize(
    ("feed", "through", "circuits", "expected"),
    list(zip(UPSTREAM_FEED, UPSTREAM_THROUGH, UPSTREAM_CIRCUITS, UPSTREAM_OVERHEAD, strict=True)),
)
def test_upstream_panel_overhead_from_the_capture(feed, through, circuits, expected):
    got = overhead_w(feed, [circuits], FEEDTHROUGH_SIGN * through, True)
    assert got == pytest.approx(expected, abs=0.1)
    assert 0.0 <= got < 100.0
    # Without the feedthrough, the downstream panel's load reads as overhead.
    assert overhead_w(feed, [circuits], None, False) > 250.0


@pytest.mark.parametrize(
    ("feed", "circuits", "expected"),
    list(zip(DOWNSTREAM_FEED, DOWNSTREAM_CIRCUITS, DOWNSTREAM_OVERHEAD, strict=True)),
)
def test_downstream_panel_overhead_from_the_capture(feed, circuits, expected):
    assert overhead_w(feed, [circuits], None, False) == pytest.approx(expected, abs=0.1)
