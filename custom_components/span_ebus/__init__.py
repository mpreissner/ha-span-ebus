"""SPAN Panel (eBus) integration: the cloud push backend, and panels added locally."""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CA_PEM,
    CONF_CLIENT_NAME,
    CONF_DECLINED,
    CONF_DEVICE_UUID,
    CONF_LOCAL_SERIAL,
    CONF_PANELS,
    CONF_PRIMARY_PANEL,
    CONF_SERIAL,
    CONF_TOKENS,
    CONF_TRANSPORT_MODE,
    CONF_USER_ID,
    DOMAIN,
    TOKEN_DIR,
)
from .coordinator import SpanCloudCoordinator, energy_store
from .runtime import SpanRuntime
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
from .span_client.local import onboard
from .span_client.local.health import Mode
from .transport import SpanTransportManager, fallback_issue_id, local_tree_store

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SELECT, Platform.SENSOR, Platform.SWITCH]

# One coordinator per panel. A site has one stream: a panel alone on its site takes
# all of it, and panels sharing a site take their split of it, since circuit
# instance ids restart per panel. A shared site can also have a site device. A
# panel added locally also has its transport, and its cloud coordinators, if
# any, only once it has failed over.
type SpanConfigEntry = ConfigEntry[SpanRuntime]


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


def _local_pending_issue(entry: SpanConfigEntry) -> str:
    return f"local_pending_{entry.entry_id}"


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


def _transport_mode(entry: SpanConfigEntry) -> Mode:
    try:
        return Mode(entry.options.get(CONF_TRANSPORT_MODE, Mode.LOCAL_FIRST))
    except ValueError:
        return Mode.LOCAL_FIRST


async def async_setup_entry(hass: HomeAssistant, entry: SpanConfigEntry) -> bool:
    runtime = SpanRuntime()
    entry.runtime_data = runtime
    if CONF_LOCAL_SERIAL in entry.data:
        # Added over the local API. Earlier versions left this issue to say
        # local telemetry was still to come.
        ir.async_delete_issue(hass, DOMAIN, _local_pending_issue(entry))

        async def build_cloud() -> list[SpanCloudCoordinator]:
            return await _cloud_coordinators(hass, entry)

        runtime.transport = SpanTransportManager(
            hass,
            entry,
            runtime,
            mode=_transport_mode(entry),
            build_cloud=build_cloud if CONF_TOKENS in entry.data else None,
        )
        await runtime.transport.async_start()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        return True

    # Prove the credentials before starting anything, and learn the topology in
    # the same round-trip. A rejection is the user's to fix; anything else is a
    # cloud hiccup Home Assistant should retry on its own schedule.
    try:
        coordinators = await _cloud_coordinators(hass, entry)
    except cloud_auth.CloudCredentialsRejected as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except Exception as err:
        raise ConfigEntryNotReady(f"cannot reach SPAN cloud: {err}") from err

    for coordinator in coordinators:
        await coordinator.async_start()
    runtime.coordinators = coordinators

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _cloud_coordinators(
    hass: HomeAssistant, entry: SpanConfigEntry
) -> list[SpanCloudCoordinator]:
    """Read the account and build a coordinator per panel, unstarted.

    Raises CloudCredentialsRejected when the stored tokens are dead, and
    anything else when the cloud can't be read.
    """
    token_path = Path(hass.config.path(TOKEN_DIR)) / f"{entry.entry_id}.json"
    await hass.async_add_executor_job(_write_token_file, token_path, entry.data[CONF_TOKENS])
    sites = await hass.async_add_executor_job(_discover_sites, token_path)

    common = {
        "token_path": token_path,
        "device_uuid": entry.data[CONF_DEVICE_UUID],
        "user_id": entry.data.get(CONF_USER_ID),
        "serial": entry.data.get(CONF_SERIAL),
    }
    if not sites:
        # Unfamiliar topology shape: fall back to the original single stream
        # rather than refusing to start.
        _LOGGER.warning("could not split the SPAN account into sites; using one stream")
        return [SpanCloudCoordinator(hass, entry, **common)]

    _LOGGER.info("SPAN account has %d site(s)", len(sites))
    _LOGGER.debug("SPAN sites: %s", ", ".join(f"{s.model} {s.serial}" for s in sites))
    # The key's presence is what makes this once-only: None is an answer, and
    # asking again after a panel leaves would hand a scoped panel the entry's
    # empty store.
    if CONF_PRIMARY_PANEL not in entry.data:
        data = {**entry.data, CONF_PRIMARY_PANEL: primary_panel_for(sites)}
        hass.config_entries.async_update_entry(entry, data=data)
    primary_panel = entry.data[CONF_PRIMARY_PANEL]
    plan = panel_plan(sites, entry.options.get(CONF_PANELS), entry.options.get(CONF_DECLINED, ()))
    if CONF_PANELS not in entry.options:
        options = {**entry.options, CONF_PANELS: list(plan.chosen)}
        hass.config_entries.async_update_entry(entry, options=options)
    if CONF_LOCAL_SERIAL not in entry.data:
        # A local panel's cloud fallback is that one panel by design; the
        # account's other panels are not news.
        _note_panels(hass, entry, plan)
    return [
        coordinator
        for site in plan.running
        for coordinator in _site_coordinators(
            hass, entry, site, primary_panel, plan.chosen, **common
        )
    ]


async def async_unload_entry(hass: HomeAssistant, entry: SpanConfigEntry) -> bool:
    runtime = entry.runtime_data
    # The transport goes first: a failover in flight would otherwise add cloud
    # coordinators, and entities, to platforms that are being taken down.
    if runtime.transport is not None:
        await runtime.transport.async_stop()
    runtime.hooks.clear()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        for coordinator in runtime.coordinators:
            await coordinator.async_shutdown()
        token_path = Path(hass.config.path(TOKEN_DIR)) / f"{entry.entry_id}.json"
        if CONF_TOKENS in entry.data:
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
    ir.async_delete_issue(hass, DOMAIN, _local_pending_issue(entry))
    ir.async_delete_issue(hass, DOMAIN, fallback_issue_id(entry.entry_id))
    if CONF_ACCESS_TOKEN in entry.data:
        # Best effort: the panel keeps a client registration until it is removed.
        await onboard.discard_client(
            async_get_clientsession(hass),
            entry.data[CONF_HOST],
            entry.data[CONF_CA_PEM],
            entry.data[CONF_ACCESS_TOKEN],
            entry.data[CONF_CLIENT_NAME],
        )
    await energy_store(hass, entry.entry_id).async_remove()
    await local_tree_store(hass, entry.entry_id).async_remove()
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
