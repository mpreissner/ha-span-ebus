"""The `analyze_leg_balance` action's Home Assistant side.

The analysis is `span_client.leg_balance`'s, free of Home Assistant. This module
finds a local entry's circuit power and lug current sensors by unique id, reads
their mean statistics through `recorder_api`, and works out which circuits may
be suggested for a move. Read-only: nothing is written or sent to the panel.

Entity ids, unique ids, serials and hosts never reach the response or a log
line; the response names circuits and spaces only.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from . import recorder_api
from .const import CONF_LOCAL_SERIAL, DOMAIN
from .span_client import feeds
from .span_client import leg_balance as lb
from .span_client.backend import same_serial
from .span_client.link import link_from_options
from .span_client.local import model
from .topology import feeds_panel_attrs

_LOGGER = logging.getLogger(__name__)

_POWER = "meter/active-power"
_CURRENT_A = "meter/current-a"
_CURRENT_B = "meter/current-b"


def _entity_id(registry: er.EntityRegistry, unique_id: str) -> str | None:
    return registry.async_get_entity_id("sensor", DOMAIN, unique_id)


def _recorded(hass: HomeAssistant, entity_id: str | None) -> bool:
    return entity_id is not None and recorder_api.is_recorded(hass, entity_id)


def _lug_ids(
    hass: HomeAssistant,
    registry: er.EntityRegistry,
    panel_key: str,
    lugs: model.LugsFacts | None,
) -> tuple[str, str] | None:
    """The recorded current A and B sensors of a lugs device, or None."""
    if lugs is None:
        return None
    ids = tuple(
        _entity_id(registry, model.lugs_uid(panel_key, lugs.device_id, key))
        for key in (_CURRENT_A, _CURRENT_B)
    )
    if not all(_recorded(hass, entity_id) for entity_id in ids):
        return None
    return ids[0], ids[1]  # type: ignore[return-value]


def _feeds_other_panels(
    hass: HomeAssistant, entry: ConfigEntry
) -> tuple[set[tuple[int, ...]], bool]:
    """(branch spaces, fed over the lugs) from other local entries' Topology
    answers naming this panel as their upstream.
    """
    serial = entry.data.get(CONF_LOCAL_SERIAL)
    branch: set[tuple[int, ...]] = set()
    over_lugs = False
    if not serial:
        return branch, over_lugs
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id == entry.entry_id or CONF_LOCAL_SERIAL not in other.data:
            continue
        topology = feeds.topology_of(other.options)
        if not same_serial(topology.get("upstream"), str(serial)):
            continue
        if topology["supply"] == feeds.LUGS:
            over_lugs = True
        elif topology["supply"] == feeds.BRANCH and topology.get("spaces"):
            branch.add(tuple(sorted(topology["spaces"])))
    return branch, over_lugs


def _read_failed(err: Exception) -> HomeAssistantError:
    """The action's error when the history can't be read or analyzed.

    Only the exception's type is logged: its text can name statistic ids.
    """
    _LOGGER.debug("Leg balance: reading or analyzing the history failed (%s)", type(err).__name__)
    return HomeAssistantError(translation_domain=DOMAIN, translation_key="leg_balance_read_failed")


async def async_analyze(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    days: int = lb.DEFAULT_DAYS,
    min_gain_a: float = lb.MIN_GAIN_A,
    balanced_below_a: float = lb.BALANCED_BELOW_A,
) -> dict[str, Any]:
    """The action's response for one loaded local entry."""
    transport = entry.runtime_data.transport
    snapshot = transport.snapshot
    panel_key = transport.panel_key
    registry = er.async_get(hass)
    options = model.ControlOptions.from_options(entry.options)
    recorder = recorder_api.loaded(hass)

    link = link_from_options(entry.options)
    branch_spaces, feeds_over_lugs = _feeds_other_panels(hass, entry)

    # Circuits by an opaque index key, so no device id reaches the analysis.
    plans: list[tuple[str, model.CircuitFacts, str | None, bool]] = []
    for index, circuit in enumerate(snapshot.circuits.values()):
        entity_id = _entity_id(registry, model.circuit_uid(panel_key, circuit.device_id, _POWER))
        recorded = recorder and _recorded(hass, entity_id)
        feeds_panel = tuple(sorted(circuit.spaces or ())) in branch_spaces
        if not feeds_panel and link is not None:
            instance = link.circuits.get(circuit.device_id)
            feeds_panel = bool(
                instance is not None and feeds_panel_attrs(hass, link.cloud_key, instance)
            )
        plans.append((f"c{index}", circuit, entity_id if recorded else None, feeds_panel))

    upstream_ids = _lug_ids(hass, registry, panel_key, snapshot.upstream) if recorder else None
    feedthrough = model.has_feedthrough(snapshot)
    feedthrough_ids = (
        _lug_ids(hass, registry, panel_key, snapshot.downstream)
        if recorder and feedthrough
        else None
    )
    wanted = [entity_id for _key, _c, entity_id, _f in plans if entity_id is not None]
    wanted += list(upstream_ids or ()) + list(feedthrough_ids or ())

    now = dt_util.utcnow().replace(second=0, microsecond=0)

    async def read(period: lb.Period, span_days: int) -> dict[str, dict[datetime, float]]:
        if not recorder or not wanted:
            return {}
        # Up to the last whole period: the one in progress has no row yet.
        step = 60 if period == "hour" else 5
        end = now.replace(minute=now.minute - now.minute % step)
        return await recorder_api.means(hass, wanted, end - timedelta(days=span_days), end, period)

    def inputs(
        means: Mapping[str, Mapping[datetime, float]], period: lb.Period
    ) -> tuple[list[lb.CircuitIn], lb.LegHistory | None, bool]:
        circuits: list[lb.CircuitIn] = []
        for key, circuit, entity_id, feeds_panel in plans:
            series = means.get(entity_id, {}) if entity_id is not None else {}
            reason = lb.held_still(circuit, options)
            if reason is None and entity_id is None:
                reason = lb.Exclusion.LOW_COVERAGE
            circuits.append(
                lb.CircuitIn(
                    key=key,
                    name=model.circuit_label(circuit),
                    spaces=tuple(circuit.spaces or ()),
                    poles=circuit.poles,
                    nominal_voltage=circuit.nominal_voltage,
                    movable=reason is None,
                    reason=reason,
                    mean_w=lb.load_series(circuit, series),
                    feeds_panel=feeds_panel,
                )
            )

        def pair(ids: tuple[str, str] | None) -> tuple[dict, dict] | None:
            return (means.get(ids[0], {}), means.get(ids[1], {})) if ids is not None else None

        legs, shared = lb.lug_history(
            period, pair(upstream_ids), pair(feedthrough_ids), has_feedthrough=feedthrough
        )
        return circuits, legs, shared

    spaces = lb.panel_spaces(snapshot.panel.model if snapshot.panel is not None else None)

    async def run(period: lb.Period, span_days: int) -> lb.Analysis:
        try:
            means = await read(period, span_days)
        except Exception as err:
            raise _read_failed(err) from err
        circuits, legs, shared = inputs(means, period)
        try:
            return await hass.async_add_executor_job(
                lambda: lb.analyze(
                    circuits,
                    legs,
                    spaces,
                    period=period,
                    min_gain_a=min_gain_a,
                    balanced_below_a=balanced_below_a,
                    lugs_shared=shared,
                )
            )
        except Exception as err:
            raise _read_failed(err) from err

    analysis = await run("hour", days)
    if lb.use_short_term(analysis):
        analysis = await run("5minute", min(days, lb.SHORT_TERM_DAYS))

    _LOGGER.debug(
        "Leg balance: %d %s periods, mapping %s, %d suggestions",
        analysis.periods_used,
        analysis.period,
        analysis.mapping,
        len(analysis.moves),
    )
    return lb.response(analysis, days, feeds_over_lugs=feeds_over_lugs)


# --- the button ------------------------------------------------------------------------


def notification_id(entry: ConfigEntry) -> str:
    """One notification per entry: a new press replaces the last one."""
    return f"{DOMAIN}_leg_balance_{entry.entry_id}"


def _name_by_user(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    serial = entry.data.get(CONF_LOCAL_SERIAL)
    if not serial:
        return None
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, serial)})
    return device.name_by_user if device is not None else None


async def async_notify(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Run the analysis with the defaults and post the result as a notification.

    Never raises: the notification is the outcome, even of an error. A press while
    one is running is ignored.
    """
    lock = entry.runtime_data.leg_balance_lock
    if lock.locked():
        _LOGGER.debug("Leg balance: an analysis is already running; press ignored")
        return
    async with lock:
        response: dict[str, Any] | None
        try:
            response = await async_analyze(hass, entry)
        except Exception as err:  # noqa: BLE001 - the notification reports it
            _LOGGER.debug("Leg balance: the analysis failed (%s)", type(err).__name__)
            response = None
        try:
            persistent_notification.async_create(
                hass,
                lb.notification_markdown(response),
                title=lb.notification_title(_name_by_user(hass, entry)),
                notification_id=notification_id(entry),
            )
        except Exception as err:  # noqa: BLE001 - a press never raises
            _LOGGER.debug("Leg balance: posting the notification failed (%s)", type(err).__name__)
