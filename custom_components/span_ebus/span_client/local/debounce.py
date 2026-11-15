"""Coalesce bursts of broker messages into at most one flush per interval.

Kept free of Home Assistant: the scheduler is passed in, so the window is
tested with fakes and run on the event loop by the transport.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

MAX_INTERVAL = 15.0

Cancel = Callable[[], None]


def clamp_interval(value: Any, default: float) -> float:
    """An interval in 0..15 s; anything unreadable is the default."""
    try:
        interval = float(value)
    except (TypeError, ValueError):
        return default
    if interval != interval:  # NaN
        return default
    return min(max(interval, 0.0), MAX_INTERVAL)


class Coalescer:
    """A trailing window: the first mark after a flush schedules the next flush
    `interval` seconds later, and every mark in between rides along with it.
    An interval of 0 flushes on the next loop iteration.
    """

    def __init__(
        self,
        interval: float,
        *,
        call_soon: Callable[[Callable[[], None]], Cancel],
        call_later: Callable[[float, Callable[..., None]], Cancel],
        flush: Callable[[], None],
    ) -> None:
        self.interval = interval
        self._call_soon = call_soon
        self._call_later = call_later
        self._flush = flush
        self._cancel: Cancel | None = None
        self._dirty = False
        self._stopped = False

    def mark(self) -> None:
        self._dirty = True
        if self._cancel is not None or self._stopped:
            return
        if self.interval <= 0:
            self._cancel = self._call_soon(self._fire)
        else:
            self._cancel = self._call_later(self.interval, self._fire)

    def _fire(self, *_: object) -> None:
        self._cancel = None
        if not self._dirty or self._stopped:
            return
        self._dirty = False
        self._flush()

    def stop(self) -> None:
        self._stopped = True
        cancel, self._cancel = self._cancel, None
        if cancel is not None:
            cancel()
