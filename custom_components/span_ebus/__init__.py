"""SPAN Panel (eBus) integration — cloud push backend."""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DEVICE_UUID,
    CONF_SERIAL,
    CONF_TOKENS,
    CONF_USER_ID,
    TOKEN_DIR,
)
from .coordinator import SpanCloudCoordinator
from .span_client import cloud_auth

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH]

type SpanConfigEntry = ConfigEntry[SpanCloudCoordinator]


def _write_token_file(path: Path, tokens: dict) -> None:
    """Materialize the config-entry tokens into the file the backend refreshes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cloud_auth.save_tokens(path, cloud_auth.CloudTokens(**tokens))


async def async_setup_entry(hass: HomeAssistant, entry: SpanConfigEntry) -> bool:
    token_path = Path(hass.config.path(TOKEN_DIR)) / f"{entry.entry_id}.json"
    await hass.async_add_executor_job(_write_token_file, token_path, entry.data[CONF_TOKENS])

    coordinator = SpanCloudCoordinator(
        hass,
        entry,
        token_path=token_path,
        device_uuid=entry.data[CONF_DEVICE_UUID],
        user_id=entry.data.get(CONF_USER_ID),
        serial=entry.data.get(CONF_SERIAL),
    )
    await coordinator.async_start()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SpanConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
        token_path = Path(hass.config.path(TOKEN_DIR)) / f"{entry.entry_id}.json"
        await hass.async_add_executor_job(_remove_quietly, token_path)
    return unloaded


def _remove_quietly(path: Path) -> None:
    with contextlib.suppress(OSError):
        os.remove(path)
