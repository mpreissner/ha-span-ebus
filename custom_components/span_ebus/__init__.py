"""SPAN Panel (eBus) integration — cloud push backend."""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir

from .const import (
    CONF_DECLINED,
    CONF_DEVICE_UUID,
    CONF_PANELS,
    CONF_PRIMARY_PANEL,
    CONF_SERIAL,
    CONF_TOKENS,
    CONF_USER_ID,
    DOMAIN,
    TOKEN_DIR,
)
from .coordinator import SpanCloudCoordinator, energy_store
from .span_client import cloud_auth
from .span_client.backend import (
    PanelPlan,
    SiteInfo,
    SiteStream,
    discover_sites,
    panel_plan,
    primary_panel_for,
    site_routes,
    stream_identity,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH]

# One coordinator per panel. A site has one stream: a panel alone on its site takes
# all of it, and panels sharing a site take their split of it, since circuit
# instance ids restart per panel. A shared site can also have a site device.
type SpanConfigEntry = ConfigEntry[list[SpanCloudCoordinator]]


def _write_token_file(path: Path, tokens: dict) -> None:
    """Materialize the config-entry tokens into the file the backend refreshes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cloud_auth.save_tokens(path, cloud_auth.CloudTokens(**tokens))


def _site_coordinators(
    hass: HomeAssistant,
    entry: SpanConfigEntry,
    site: SiteInfo,
    primary_panel: str | None,
    chosen: tuple[str, ...],
    **common,
) -> list[SpanCloudCoordinator]:
    """Build the coordinators for one site: one, or one per device if it is shared."""
    routes = site_routes(site, primary_panel, chosen)
    if routes is None:
        return [SpanCloudCoordinator(hass, entry, site=site, primary_panel=primary_panel, **common)]
    identity = stream_identity(
        site,
        device_uuid=common["device_uuid"],
        serial=common["serial"],
        primary_panel=primary_panel,
    )
    stream = SiteStream(
        common["token_path"],
        identity.device_uuid,
        routes=routes,
        user_id=common["user_id"],
        serial=identity.serial,
    )
    return [
        SpanCloudCoordinator(
            hass,
            entry,
            site=site,
            primary_panel=primary_panel,
            stream=stream,
            panel=panel,
            **common,
        )
        for panel in routes.devices
    ]


def _discover_sites(token_path: Path) -> list[SiteInfo]:
    """Executor thread: prove the credentials and read the account's sites."""
    return discover_sites(cloud_auth.access_token_from_store(token_path))


def _new_panels_issue(entry: SpanConfigEntry) -> str:
    return f"new_panels_{entry.entry_id}"


def _note_panels(hass: HomeAssistant, entry: SpanConfigEntry, plan: PanelPlan) -> None:
    """Tell the user about panels the stored list doesn't account for."""
    if plan.departed:
        _LOGGER.info("%d chosen SPAN panel(s) are no longer on the account", plan.departed)
    if not plan.running:
        _LOGGER.warning("none of the chosen SPAN panels are on the account; nothing will run")
    if plan.new:
        ir.async_create_issue(
            hass,
            DOMAIN,
            _new_panels_issue(entry),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="new_panels",
            translation_placeholders={"count": str(len(plan.new))},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, _new_panels_issue(entry))


async def async_setup_entry(hass: HomeAssistant, entry: SpanConfigEntry) -> bool:
    token_path = Path(hass.config.path(TOKEN_DIR)) / f"{entry.entry_id}.json"
    await hass.async_add_executor_job(_write_token_file, token_path, entry.data[CONF_TOKENS])

    # Prove the credentials before starting anything, and learn the topology in
    # the same round-trip. A rejection is the user's to fix; anything else is a
    # cloud hiccup Home Assistant should retry on its own schedule.
    try:
        sites = await hass.async_add_executor_job(_discover_sites, token_path)
    except cloud_auth.CloudCredentialsRejected as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except Exception as err:
        raise ConfigEntryNotReady(f"cannot reach SPAN cloud: {err}") from err

    common = {
        "token_path": token_path,
        "device_uuid": entry.data[CONF_DEVICE_UUID],
        "user_id": entry.data.get(CONF_USER_ID),
        "serial": entry.data.get(CONF_SERIAL),
    }
    if sites:
        _LOGGER.info("SPAN account has %d site(s)", len(sites))
        _LOGGER.debug("SPAN sites: %s", ", ".join(f"{s.model} {s.serial}" for s in sites))
        # The key's presence is what makes this once-only: None is an answer, and
        # asking again after a panel leaves would hand a scoped panel the entry's
        # empty store.
        if CONF_PRIMARY_PANEL not in entry.data:
            data = {**entry.data, CONF_PRIMARY_PANEL: primary_panel_for(sites)}
            hass.config_entries.async_update_entry(entry, data=data)
        primary_panel = entry.data[CONF_PRIMARY_PANEL]
        plan = panel_plan(
            sites, entry.options.get(CONF_PANELS), entry.options.get(CONF_DECLINED, ())
        )
        if CONF_PANELS not in entry.options:
            options = {**entry.options, CONF_PANELS: list(plan.chosen)}
            hass.config_entries.async_update_entry(entry, options=options)
        _note_panels(hass, entry, plan)
        coordinators = [
            coordinator
            for site in plan.running
            for coordinator in _site_coordinators(
                hass, entry, site, primary_panel, plan.chosen, **common
            )
        ]
    else:
        # Unfamiliar topology shape: fall back to the original single stream
        # rather than refusing to start.
        _LOGGER.warning("could not split the SPAN account into sites; using one stream")
        coordinators = [SpanCloudCoordinator(hass, entry, **common)]

    for coordinator in coordinators:
        await coordinator.async_start()
    entry.runtime_data = coordinators

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SpanConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        for coordinator in entry.runtime_data:
            await coordinator.async_shutdown()
        token_path = Path(hass.config.path(TOKEN_DIR)) / f"{entry.entry_id}.json"
        await hass.async_add_executor_job(_remove_quietly, token_path)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: SpanConfigEntry) -> None:
    """Drop the energy totals along with the entry that earned them.

    Unloading keeps the stores — that is a restart, and the totals are the point.
    Deleting the entry means the panels are gone, and leaving files behind would
    resurrect stale kilowatt-hours if the same panels were ever set up again.
    The per-panel stores are named after panel keys that are only known while the
    entry runs, so they are found by prefix.
    """
    ir.async_delete_issue(hass, DOMAIN, _new_panels_issue(entry))
    await energy_store(hass, entry.entry_id).async_remove()
    storage = Path(hass.config.path(".storage"))
    prefix = f"{DOMAIN}.{entry.entry_id}."
    await hass.async_add_executor_job(_remove_by_prefix, storage, prefix)


def _remove_by_prefix(directory: Path, prefix: str) -> None:
    with contextlib.suppress(OSError):
        for path in directory.glob(f"{prefix}*.energy"):
            _remove_quietly(path)


def _remove_quietly(path: Path) -> None:
    with contextlib.suppress(OSError):
        os.remove(path)
