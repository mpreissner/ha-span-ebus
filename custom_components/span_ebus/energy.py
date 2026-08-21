"""Turning a stream of power samples into an energy total.

SPAN's realtime channel reports instantaneous power and nothing else, so energy
has to be integrated on this side. The arithmetic lives here, deliberately free
of any Home Assistant import, so it can be tested on its own — the test suite
runs without `homeassistant` installed, and this is the part worth testing.
"""

from __future__ import annotations

# Watt-seconds per kilowatt-hour.
_WS_PER_KWH = 3_600_000.0


class EnergyAccumulator:
    """A trapezoidal Riemann sum over power samples, in kWh.

    Two rules make the result safe to publish as a `total_increasing` sensor:

    * **Only the positive part of each sample counts.** Such a sensor going down
      reads as a meter reset, and SPAN's site `grid` flow is signed — it goes
      negative whenever the site exports. Summing the positive part gives
      consumption, which is the quantity being asked for; SPAN's directional site
      flows (`grid_to_home`, `solar_to_grid`, …) are one-way already and measure
      the other directions on their own.
    * **Gaps longer than `max_gap_seconds` are not bridged.** A sample arriving
      after a long silence only re-establishes the baseline. A stalled stream is
      precisely where a trapezoid is least trustworthy, and a fabricated step in
      long-term statistics is worse than a flat spot — a reader can see through
      the second and not the first.
    """

    def __init__(self, max_gap_seconds: float, total_kwh: float = 0.0) -> None:
        self.max_gap_seconds = max_gap_seconds
        self.total_kwh = total_kwh
        self._last_power_w: float | None = None
        self._last_timestamp: float | None = None

    def add(self, power_w: float, timestamp: float) -> None:
        """Fold one sample in, advancing the total by the interval it closes."""
        previous_power, previous_timestamp = self._last_power_w, self._last_timestamp
        self._last_power_w, self._last_timestamp = power_w, timestamp

        if previous_power is None or previous_timestamp is None:
            return
        elapsed = timestamp - previous_timestamp
        # `elapsed <= 0` is not paranoia: frames are stamped with SPAN's own epoch
        # millis, so a repeated or out-of-order stamp is possible, and neither may
        # be allowed to subtract energy.
        if elapsed <= 0 or elapsed > self.max_gap_seconds:
            return
        average_w = (max(previous_power, 0.0) + max(power_w, 0.0)) / 2.0
        self.total_kwh += average_w * elapsed / _WS_PER_KWH
