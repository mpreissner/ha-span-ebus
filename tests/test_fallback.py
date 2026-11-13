"""The cloud fallback's mapping of local rows, and the energy register's continuation."""

from __future__ import annotations

import pytest
from span_client.fallback import FALLBACK_KEYS, RegisterContinuation, resolve
from span_client.link import DOWNSTREAM, UPSTREAM, PanelLink
from span_client.local.model import NO_FALLBACK, Scope
from span_client.models import Reading
from span_client.overhead import FEEDTHROUGH_SIGN

CIRCUIT = "c-0001"


def _link(**kwargs) -> PanelLink:
    base = {
        "cloud_key": "nt-0000-test",
        "local_serial": "nt-0000-test",
        "circuits": {CIRCUIT: 7},
        "feeds": {1: UPSTREAM},
        "site_panel_count": 1,
    }
    return PanelLink(**{**base, **kwargs})


def _data(values: dict[str, str]) -> dict[str, Reading]:
    return {key: Reading(key, value, 0.0) for key, value in values.items()}


FULL = _data(
    {
        "circuit-7/power": "120.500",
        "circuit-7/current": "1.000",
        "circuit-7/relay": "CLOSED",
        "panel/voltage_l1": "120.000",
        "panel/voltage_l2": "121.000",
        "panel/frequency": "60.000",
        "panel/current": "30.000",
        "panel/current_l1": "15.000",
        "panel/current_l2": "16.000",
        "panel/power": "999.000",
        "site/grid": "1200.000",
        "site/home": "1500.000",
        "feed-1/power": "2000.000",
        "feed-2/power": "300.000",
    }
)


def _device(scope: Scope) -> str:
    return CIRCUIT if scope is Scope.CIRCUIT else "dev"


@pytest.mark.parametrize(("scope", "key"), sorted(FALLBACK_KEYS))
def test_every_key_maps(scope, key):
    link = _link(feeds={1: UPSTREAM, 2: DOWNSTREAM})
    got = resolve(link, FULL, scope, _device(scope), key, energy=lambda k: 5.0)
    assert got is not NO_FALLBACK
    assert got is not None


@pytest.mark.parametrize(("scope", "key"), sorted(FALLBACK_KEYS))
def test_every_key_without_a_reading_is_none(scope, key):
    link = _link(feeds={1: UPSTREAM, 2: DOWNSTREAM})
    got = resolve(link, {}, scope, _device(scope), key, energy=lambda k: None)
    assert got is None


@pytest.mark.parametrize(
    ("scope", "key"),
    [
        (Scope.PANEL, "power-flows/pv"),
        (Scope.PANEL, "power-flows/battery"),
        (Scope.PANEL, "breaker/rating"),
        (Scope.UPSTREAM, "meter/exported-energy"),
        (Scope.DOWNSTREAM, "meter/imported-energy"),
        (Scope.CIRCUIT, "meter/exported-energy"),
        (Scope.BESS, "meter/active-power"),
    ],
)
def test_unmapped_keys(scope, key):
    assert resolve(_link(), FULL, scope, _device(scope), key) is NO_FALLBACK


def test_unmatched_circuit_has_no_fallback():
    assert resolve(_link(), FULL, Scope.CIRCUIT, "c-9999", "meter/active-power") is NO_FALLBACK


def test_circuit_values():
    link = _link()
    assert resolve(link, FULL, Scope.CIRCUIT, CIRCUIT, "meter/active-power") == 120.5
    assert resolve(link, FULL, Scope.CIRCUIT, CIRCUIT, "meter/current") == 1.0


def test_producer_circuit_is_flipped_to_the_local_frame():
    got = resolve(_link(), FULL, Scope.CIRCUIT, CIRCUIT, "meter/active-power", producer=True)
    assert got == -120.5


def test_unparseable_value_is_none():
    data = _data({"circuit-7/power": "n/a"})
    assert resolve(_link(), data, Scope.CIRCUIT, CIRCUIT, "meter/active-power") is None


@pytest.mark.parametrize(
    ("raw", "shown"), [("CLOSED", "CLOSED"), ("OPEN", "OPEN"), ("UNKNOWN", None)]
)
def test_relay(raw, shown):
    data = _data({"circuit-7/relay": raw})
    assert resolve(_link(), data, Scope.CIRCUIT, CIRCUIT, "switch/relay") == shown


def test_panel_values_fall_back_to_the_site_node():
    data = _data({"site/voltage_l1": "119.000", "site/voltage_l2": "118.000",
                  "site/frequency": "59.900"})  # fmt: skip
    assert resolve(_link(), data, Scope.PANEL, "dev", "meter/voltage-a") == 119.0
    assert resolve(_link(), data, Scope.PANEL, "dev", "meter/voltage-b") == 118.0
    assert resolve(_link(), data, Scope.PANEL, "dev", "meter/frequency") == 59.9


def test_grid_flow_passes_unchanged():
    assert resolve(_link(), FULL, Scope.PANEL, "dev", "power-flows/grid") == 1200.0
    assert resolve(_link(), FULL, Scope.PANEL, "dev", "power-flows/site") == 1500.0


def test_upstream_power_prefers_the_upstream_feed():
    assert resolve(_link(), FULL, Scope.UPSTREAM, "dev", "meter/active-power") == 2000.0


def test_upstream_power_without_feeds_uses_the_lone_feed_then_the_panel():
    link = _link(feeds={})
    lone = _data({"feed-3/power": "50.000", "panel/power": "999.000"})
    assert resolve(link, lone, Scope.UPSTREAM, "dev", "meter/active-power") == 50.0
    # Two feeds and no kinds: neither can be told apart, so the panel's own.
    assert resolve(link, FULL, Scope.UPSTREAM, "dev", "meter/active-power") == 999.0


def test_feedthrough_maps_only_with_a_downstream_feed():
    key = "meter/active-power"
    assert resolve(_link(), FULL, Scope.DOWNSTREAM, "dev", key) is NO_FALLBACK
    link = _link(feeds={1: UPSTREAM, 2: DOWNSTREAM})
    assert resolve(link, FULL, Scope.DOWNSTREAM, "dev", key) == FEEDTHROUGH_SIGN * 300.0


def test_imported_energy_maps_only_on_a_single_panel_site():
    key = "meter/imported-energy"
    seen: list[str] = []

    def energy(name: str) -> float:
        seen.append(name)
        return 42.0

    assert resolve(_link(), FULL, Scope.UPSTREAM, "dev", key, energy=energy) == 42.0
    assert seen == ["site/grid"]
    shared = _link(site_panel_count=2)
    assert resolve(shared, FULL, Scope.UPSTREAM, "dev", key, energy=energy) is NO_FALLBACK
    unknown = _link(site_panel_count=0)
    assert resolve(unknown, FULL, Scope.UPSTREAM, "dev", key, energy=energy) is NO_FALLBACK
    assert resolve(_link(), FULL, Scope.UPSTREAM, "dev", key) is NO_FALLBACK


# --- RegisterContinuation ------------------------------------------------------------


def test_local_passes_through():
    cont = RegisterContinuation()
    assert cont.local(None) is None
    assert cont.local(100.0) == 100.0
    assert cont.local(101.0) == 101.0


def test_cloud_without_any_local_value_is_none():
    cont = RegisterContinuation()
    cont.cloud_started()
    assert cont.cloud(500.0) is None


def test_first_cloud_value_only_sets_the_mark():
    cont = RegisterContinuation()
    cont.local(100.0)
    cont.cloud_started()
    assert cont.cloud(500.0) == 100.0
    assert cont.cloud(502.5) == pytest.approx(102.5)
    # Idempotent for a repeated value.
    assert cont.cloud(502.5) == pytest.approx(102.5)


def test_cloud_going_back_reseeds_and_holds():
    cont = RegisterContinuation()
    cont.local(100.0)
    cont.cloud_started()
    cont.cloud(500.0)
    assert cont.cloud(503.0) == pytest.approx(103.0)
    assert cont.cloud(10.0) == pytest.approx(103.0)
    assert cont.cloud(11.0) == pytest.approx(104.0)


def test_cloud_none_holds():
    cont = RegisterContinuation()
    cont.local(100.0)
    cont.cloud_started()
    assert cont.cloud(None) == 100.0
    cont.cloud(500.0)
    cont.cloud(501.0)
    assert cont.cloud(None) == pytest.approx(101.0)


def test_failback_never_steps_down():
    cont = RegisterContinuation()
    cont.local(100.0)
    cont.cloud_started()
    cont.cloud(500.0)
    cont.cloud(505.0)  # shown 105
    # The panel kept metering but says less than the cloud counted.
    assert cont.local(103.0) == pytest.approx(105.0)
    assert cont.local(103.0) == pytest.approx(105.0)
    # Once local reaches the high it shows itself again.
    assert cont.local(106.0) == 106.0
    assert cont.local(105.5) == 105.5


def test_local_reset_passes_through():
    cont = RegisterContinuation()
    cont.local(100.0)
    cont.cloud_started()
    cont.cloud(500.0)
    cont.cloud(505.0)
    assert cont.local(5.0) == 5.0
    assert cont.local(6.0) == 6.0


def test_second_spell_continues_from_what_is_shown():
    cont = RegisterContinuation()
    cont.local(100.0)
    cont.cloud_started()
    cont.cloud(500.0)
    cont.cloud(505.0)  # shown 105
    cont.local(103.0)  # held at 105
    cont.cloud_started()
    assert cont.cloud(800.0) == pytest.approx(105.0)
    assert cont.cloud(801.0) == pytest.approx(106.0)
