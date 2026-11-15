"""Failover and failback rules for the local link."""

import pytest
from span_client.local.health import (
    DOWN_AFTER,
    FAILBACK_AFTER,
    FALLBACK_REPAIR_AFTER,
    SILENT_AFTER,
    Decision,
    LocalHealth,
    Mode,
    Transport,
    TransportSwitch,
)
from span_client.local.homie import DeviceState


def up(now: float = 0.0) -> LocalHealth:
    health = LocalHealth(now)
    health.connected(True, now)
    health.state(DeviceState.READY, now)
    return health


# --- LocalHealth ------------------------------------------------------------


def test_never_connected_is_down_after_a_minute():
    health = LocalHealth(0.0)
    assert not health.is_down(DOWN_AFTER - 1)
    assert health.is_down(DOWN_AFTER)


def test_connected_and_ready_is_up():
    health = up()
    assert health.is_up(1.0)
    assert not health.is_down(DOWN_AFTER * 2 - 1)  # traffic counted from connect


def test_disconnect_counts_from_the_drop():
    health = up()
    health.message(100.0)
    health.connected(False, 100.0)
    assert not health.is_up(100.0)
    assert not health.is_down(100.0 + DOWN_AFTER - 1)
    assert health.is_down(100.0 + DOWN_AFTER)


def test_reconnecting_in_time_avoids_failover():
    health = up()
    health.connected(False, 10.0)
    health.connected(True, 50.0)
    assert not health.is_down(10.0 + DOWN_AFTER + 5)


@pytest.mark.parametrize(
    "state", [DeviceState.LOST, DeviceState.DISCONNECTED, DeviceState.INIT, None]
)
def test_root_not_ready_for_a_minute_is_down(state):
    health = up()
    health.state(state, 10.0)
    assert not health.is_up(11.0)
    assert not health.is_down(10.0 + DOWN_AFTER - 1)
    assert health.is_down(10.0 + DOWN_AFTER)


def test_alert_counts_as_up():
    health = up()
    health.state(DeviceState.ALERT, 5.0)
    assert health.is_up(6.0)
    assert not health.is_down(5.0 + DOWN_AFTER)


def test_silence_while_ready():
    health = up()
    health.message(10.0)
    assert not health.is_down(10.0 + SILENT_AFTER - 1)
    assert health.is_down(10.0 + SILENT_AFTER)


def test_connected_without_a_state_is_down_after_a_minute():
    health = LocalHealth(0.0)
    health.connected(True, 0.0)
    assert health.is_down(DOWN_AFTER)


def test_healthy_run_breaks_on_a_blip():
    health = up()
    assert health.healthy_for(100.0) == 100.0
    health.connected(False, 101.0)
    health.connected(True, 102.0)
    assert health.healthy_for(110.0) == 8.0  # counted from the reconnect
    health.state(DeviceState.LOST, 120.0)
    health.state(DeviceState.READY, 125.0)
    assert health.healthy_for(150.0) == 25.0


def test_silence_breaks_the_run_and_traffic_restarts_it():
    health = up()
    assert health.healthy_for(SILENT_AFTER) == 0.0
    health.message(200.0)
    assert health.healthy_for(210.0) == 10.0


# --- TransportSwitch --------------------------------------------------------


def test_local_first_fails_over_once_and_back():
    health, switch = up(), TransportSwitch(Mode.LOCAL_FIRST)
    assert switch.evaluate(health, 1.0) is None
    health.connected(False, 10.0)
    assert switch.evaluate(health, 10.0 + DOWN_AFTER - 1) is None
    assert switch.evaluate(health, 10.0 + DOWN_AFTER) is Decision.START_CLOUD
    assert switch.active is Transport.CLOUD
    assert switch.evaluate(health, 10.0 + DOWN_AFTER + 5) is None  # only once

    back = 200.0
    health.connected(True, back)
    health.state(DeviceState.READY, back)
    assert switch.evaluate(health, back) is None  # starts the healthy run
    health.message(back + FAILBACK_AFTER - 1)
    assert switch.evaluate(health, back + FAILBACK_AFTER - 1) is None
    health.message(back + FAILBACK_AFTER)
    assert switch.evaluate(health, back + FAILBACK_AFTER) is Decision.STOP_CLOUD
    assert switch.active is Transport.LOCAL
    assert switch.fallback_since is None


def test_local_only_never_fails_over():
    health, switch = LocalHealth(0.0), TransportSwitch(Mode.LOCAL_ONLY)
    assert switch.evaluate(health, DOWN_AFTER * 10) is None
    assert switch.active is Transport.LOCAL


def test_failback_waits_for_an_unbroken_run():
    health, switch = LocalHealth(0.0), TransportSwitch(Mode.LOCAL_FIRST)
    assert switch.evaluate(health, DOWN_AFTER) is Decision.START_CLOUD
    health.connected(True, 100.0)
    health.state(DeviceState.READY, 100.0)
    switch.evaluate(health, 100.0)
    health.state(DeviceState.LOST, 250.0)  # a flap restarts the clock
    health.state(DeviceState.READY, 260.0)
    for t in range(260, 260 + int(FAILBACK_AFTER), 5):
        health.message(float(t))
        assert switch.evaluate(health, float(t)) is None
    health.message(260.0 + FAILBACK_AFTER)
    assert switch.evaluate(health, 260.0 + FAILBACK_AFTER) is Decision.STOP_CLOUD


def test_repair_after_an_hour_on_the_cloud():
    health, switch = LocalHealth(0.0), TransportSwitch(Mode.LOCAL_FIRST)
    switch.evaluate(health, DOWN_AFTER)
    assert not switch.needs_repair(DOWN_AFTER + FALLBACK_REPAIR_AFTER)
    assert switch.needs_repair(DOWN_AFTER + FALLBACK_REPAIR_AFTER + 1)


def test_shown_transport():
    health, switch = up(), TransportSwitch(Mode.LOCAL_FIRST)
    assert switch.shown(health, 1.0, cloud_live=False) is Transport.LOCAL
    health.connected(False, 2.0)
    assert switch.shown(health, 3.0, cloud_live=True) is Transport.OFFLINE
    switch.evaluate(health, 2.0 + DOWN_AFTER)
    assert switch.shown(health, 70.0, cloud_live=False) is Transport.OFFLINE
    assert switch.shown(health, 70.0, cloud_live=True) is Transport.CLOUD
