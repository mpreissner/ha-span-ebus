"""The panel's overhead: the power it takes in that nothing it feeds accounts for.

What enters through the upstream lugs, minus what leaves through the branch
circuits and through the feedthrough lugs to a downstream panel, is what the
panel's own electronics and busbar take. One formula serves the cloud entry and
the local one; each reads its own inputs into it. Pure, so it is tested without
Home Assistant.
"""

from __future__ import annotations

from collections.abc import Iterable

# The sign that turns a published feedthrough reading into "+ = leaving towards
# the downstream panel". A two-panel capture (the upstream panel's feed carries
# the downstream panel's whole load, and its feedthrough reads that load as a
# positive figure) settles it as +1; tests/test_overhead.py pins it.
FEEDTHROUGH_SIGN = 1


def overhead_w(
    feed_w: float | None,
    circuits_w: Iterable[float | None],
    feedthrough_w: float | None,
    has_feedthrough: bool,
) -> float | None:
    """max(0, feed - Σcircuits - feedthrough); None if any required input is None.

    `feed_w` is + into the panel, each circuit + out to its load, and
    `feedthrough_w` + out to a downstream panel. Without downstream lugs
    (`has_feedthrough` False) the feedthrough is not an input at all.
    """
    if feed_w is None:
        return None
    total = 0.0
    for watts in circuits_w:
        if watts is None:
            return None
        total += watts
    drawn = 0.0
    if has_feedthrough:
        if feedthrough_w is None:
            return None
        drawn = feedthrough_w
    return max(0.0, feed_w - total - drawn)
