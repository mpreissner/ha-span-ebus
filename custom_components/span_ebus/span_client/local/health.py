"""When to leave the local link for the cloud, and when to come back.

Pure state, with time passed in, so the rules are testable without a broker
or a clock. The Home Assistant side feeds `LocalHealth` from the broker
callbacks and asks `TransportSwitch.evaluate` on a timer.

Failover, when any of these holds:

- the broker socket has been down for `DOWN_AFTER` (counted from start);
- the panel's root device has not been ready for `DOWN_AFTER`;
- the root is ready but nothing has arrived for `SILENT_AFTER`.

Failback once local has been healthy without a break for `FAILBACK_AFTER`.
"""

from __future__ import annotations

from enum import Enum, StrEnum

from .homie import DeviceState

DOWN_AFTER = 60.0
SILENT_AFTER = 120.0
FAILBACK_AFTER = 300.0
# How long on the cloud before telling the user, in the repairs panel.
FALLBACK_REPAIR_AFTER = 3600.0

# ALERT is a ready device with something to report, not an outage.
_UP_STATES = frozenset({DeviceState.READY, DeviceState.ALERT})


class Mode(StrEnum):
    """What a local entry may use. Cloud-only entries never get here."""

    LOCAL_FIRST = "local_first"
    LOCAL_ONLY = "local_only"


class Transport(StrEnum):
    """What the entry is reading from, as the Transport sensor shows it."""

    LOCAL = "local"
    CLOUD = "cloud"
    OFFLINE = "offline"


class Decision(Enum):
    START_CLOUD = "start_cloud"
    STOP_CLOUD = "stop_cloud"


class LocalHealth:
    """The local link's condition, from connection, state and traffic events."""

    def __init__(self, now: float) -> None:
        self._connected = False
        self._disconnected_since: float | None = now
        self._state: DeviceState | None = None
        self._not_up_since: float | None = now
        self._last_message: float | None = None
        self._healthy_since: float | None = None

    # --- events -----------------------------------------------------------

    def connected(self, connected: bool, now: float) -> None:
        if connected == self._connected:
            return
        self._connected = connected
        if connected:
            self._disconnected_since = None
            # A fresh connection counts as traffic: silence is measured from here.
            self._last_message = now
            self._resume(now)
        else:
            self._disconnected_since = now
            self._healthy_since = None

    def message(self, now: float) -> None:
        self._last_message = now
        self._resume(now)

    def state(self, state: DeviceState | None, now: float) -> None:
        """The root device's `$state`; None when it was cleared."""
        up, was_up = state in _UP_STATES, self._state in _UP_STATES
        self._state = state
        if up and not was_up:
            self._not_up_since = None
            self._resume(now)
        elif not up and was_up:
            self._not_up_since = now
            self._healthy_since = None

    # --- queries ----------------------------------------------------------

    def _silent(self, now: float) -> bool:
        return self._last_message is None or now - self._last_message >= SILENT_AFTER

    def is_up(self, now: float) -> bool:
        """Connected, ready and talking: usable right now."""
        return self._connected and self._state in _UP_STATES and not self._silent(now)

    def is_down(self, now: float) -> bool:
        """Bad for long enough to fail over."""
        if self._disconnected_since is not None and now - self._disconnected_since >= DOWN_AFTER:
            return True
        if self._not_up_since is not None and now - self._not_up_since >= DOWN_AFTER:
            return True
        return self._connected and self._state in _UP_STATES and self._silent(now)

    def healthy_for(self, now: float) -> float:
        """Seconds local has been up without a break."""
        self.observe(now)
        return 0.0 if self._healthy_since is None else now - self._healthy_since

    def observe(self, now: float) -> None:
        """Break the healthy run on silence; events break it otherwise."""
        if not self.is_up(now):
            self._healthy_since = None

    def _resume(self, now: float) -> None:
        if self._healthy_since is None and self.is_up(now):
            self._healthy_since = now


class TransportSwitch:
    """Which transport is active, and when to change it."""

    def __init__(self, mode: Mode) -> None:
        self.mode = mode
        self.active = Transport.LOCAL
        self.fallback_since: float | None = None

    def evaluate(self, health: LocalHealth, now: float) -> Decision | None:
        """Move between local and cloud; what the caller must do about it."""
        if self.active is Transport.LOCAL:
            health.observe(now)
            if self.mode is Mode.LOCAL_FIRST and health.is_down(now):
                self.active = Transport.CLOUD
                self.fallback_since = now
                return Decision.START_CLOUD
            return None
        if health.healthy_for(now) >= FAILBACK_AFTER:
            self.active = Transport.LOCAL
            self.fallback_since = None
            return Decision.STOP_CLOUD
        return None

    def needs_repair(self, now: float) -> bool:
        """On the cloud long enough that the user should look at the local link."""
        return self.fallback_since is not None and now - self.fallback_since > FALLBACK_REPAIR_AFTER

    def shown(self, health: LocalHealth, now: float, *, cloud_live: bool) -> Transport:
        """The Transport sensor's value."""
        if self.active is Transport.CLOUD:
            return Transport.CLOUD if cloud_live else Transport.OFFLINE
        return Transport.LOCAL if health.is_up(now) else Transport.OFFLINE
