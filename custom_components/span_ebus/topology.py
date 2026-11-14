"""Which panel feeds which, as Home Assistant sees it: the fed flag an entry's
entities are named by, and the name of the panel a branch circuit feeds.

The rules are span_client.feeds'; this module only finds the answers and the
devices among the loaded entries. Nothing here logs or returns a serial,
host or key: a panel is shown by the user's name for its device or its model,
or not at all.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_FEEDS, CONF_LOCAL_SERIAL, CONF_PANELS, DOMAIN
from .span_client import feeds
from .span_client.link import link_from_options

FEEDS_PANEL = "feeds_panel"


def cloud_panel_fed(entry: ConfigEntry, cloud_key: str | None) -> bool:
    """Whether a cloud entry's answers say another panel feeds this one."""
    if cloud_key is None:
        return False
    options = entry.options
    return feeds.is_fed(options.get(CONF_FEEDS) or {}, cloud_key, options.get(CONF_PANELS) or ())


def _entries(hass: HomeAssistant) -> list[ConfigEntry]:
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]


def _identifiers(hass: HomeAssistant, key: str) -> Iterator[tuple[str, str]]:
    """The device identifiers a panel key may be shown under, best first."""
    for entry in _entries(hass):
        runtime = getattr(entry, "runtime_data", None)
        for coordinator in getattr(runtime, "coordinators", None) or ():
            if coordinator.cloud_key == key:
                yield (DOMAIN, coordinator.serial)
        local_serial = entry.data.get(CONF_LOCAL_SERIAL)
        link = link_from_options(entry.options)
        if local_serial and link is not None and link.cloud_key == key:
            yield (DOMAIN, str(local_serial))


def panel_name(hass: HomeAssistant, key: str) -> str | None:
    """How the device a panel key is shown as is named: the user's own name,
    else "SPAN <model>" (`feeds.panel_label`). None if no device is found.
    """
    registry = dr.async_get(hass)
    for identifier in _identifiers(hass, key):
        device = registry.async_get_device(identifiers={identifier})
        if device is not None:
            return feeds.panel_label(device.name_by_user, device.model)
    return None


# entry id -> (the options it was built from, upstream -> branch circuits). An
# options change replaces the mapping (and reloads the entry), so a stale
# lookup is never reused.
_circuits: dict[str, tuple[Mapping[str, Any], dict[str, dict[str, str]]]] = {}


def _branch_circuits(entry: ConfigEntry, upstream: str) -> dict[str, str]:
    options = entry.options
    cached = _circuits.get(entry.entry_id)
    if cached is None or cached[0] is not options:
        cached = (options, {})
        _circuits[entry.entry_id] = cached
    by_upstream = cached[1]
    if upstream not in by_upstream:
        by_upstream[upstream] = feeds.branch_circuits(
            options.get(CONF_FEEDS) or {}, upstream, options.get(CONF_PANELS) or ()
        )
    return by_upstream[upstream]


def feeds_panel_attrs(hass: HomeAssistant, upstream: str | None, circuit: Any) -> dict[str, str]:
    """`{"feeds_panel": name}` for a circuit on `upstream` that feeds another
    panel, by any loaded entry's answers; else empty. Only loaded entries
    count: a branch circuit is named for a panel HA is running.
    """
    if upstream is None or circuit is None:
        return {}
    for entry in _entries(hass):
        circuits = _branch_circuits(entry, upstream)
        downstream = circuits.get(str(circuit))
        if downstream is None:
            continue
        name = feeds.feeds_panel(circuits, circuit, {downstream: panel_name(hass, downstream)})
        if name is not None:
            return {FEEDS_PANEL: name}
    return {}
