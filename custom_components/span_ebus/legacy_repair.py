"""Offer to remove old cloud entities a local entry no longer feeds.

Raised at setup on entries with a local panel. What it lists is decided by
`span_client.legacy`: cloud entities an earlier release added next to the local
ones, circuits a link could not pair, and fallback-only rows. Stand-ins are
fed locally and never listed. Nothing is removed until the user confirms, and
statistics stay in the recorder.

Logs carry counts only: entity ids are the user's.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.translation import async_get_translations

from .const import (
    CONF_CLOUD_STEM,
    CONF_CLOUD_UNMATCHED,
    CONF_LOCAL_SERIAL,
    CONF_TOKENS,
    CONF_TRANSPORT_MODE,
    DOMAIN,
)
from .energy_step import panel_name
from .span_client.identity import RegistryEntryView
from .span_client.legacy import (
    DUPLICATE,
    LegacyEntities,
    legacy_entities,
    live_uids,
    produced_uids,
    still_listed,
)
from .span_client.local import model
from .span_client.local.health import Mode
from .span_client.standin import FALLBACK, UNMATCHED

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

ISSUE_KEY = "legacy_entities"
# Lines the confirm step shows inside a placeholder: the issues schema has no room
# for them, so they are kept as selector options.
GROUP_SELECTOR = "legacy_group"
FALLBACK_LOCAL_ONLY = "fallback_only_local"
MORE = "more"


def issue_id(entry_id: str) -> str:
    return f"{ISSUE_KEY}_{entry_id}"


def _local_only(entry: ConfigEntry) -> bool:
    """No cloud to feed a fallback-only row: removed, or switched off."""
    return (
        CONF_TOKENS not in entry.data
        or entry.options.get(CONF_TRANSPORT_MODE) == Mode.LOCAL_ONLY.value
    )


@callback
def live(hass: HomeAssistant, entry: ConfigEntry, serial: str) -> frozenset[str]:
    """The uids this entry's platforms create now: never listed.

    The loaded entities, less the kept ones no local row feeds, plus the model's
    local rows for any a platform has not added yet.
    """
    loaded = [
        (entity.unique_id, getattr(entity, "kept_without_local_row", False))
        for platform in entity_platform.async_get_platforms(hass, DOMAIN)
        if platform.config_entry is not None and platform.config_entry.entry_id == entry.entry_id
        for entity in platform.entities.values()
    ]
    runtime = getattr(entry, "runtime_data", None)
    transport = getattr(runtime, "transport", None)
    if transport is not None:
        produced = produced_uids(transport.snapshot, transport.panel_key)
    else:
        produced = produced_uids(model.EMPTY, serial)
    return live_uids(loaded, produced)


@callback
def current(hass: HomeAssistant, entry: ConfigEntry) -> LegacyEntities:
    """What the repair lists for this entry now."""
    if CONF_LOCAL_SERIAL not in entry.data:
        return LegacyEntities()
    serial = entry.data[CONF_LOCAL_SERIAL]
    registry = er.async_get(hass)
    views = [
        RegistryEntryView(row.entity_id, row.unique_id)
        for row in er.async_entries_for_config_entry(registry, entry.entry_id)
        if row.platform == DOMAIN
    ]
    return legacy_entities(
        views,
        local_serial=serial,
        cloud_stem=entry.data.get(CONF_CLOUD_STEM),
        unmatched=entry.data.get(CONF_CLOUD_UNMATCHED) or (),
        live=live(hass, entry, serial),
    )


@callback
def async_check(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Raise the issue when something qualifies, else delete it."""
    listed = current(hass, entry)
    if not listed:
        ir.async_delete_issue(hass, DOMAIN, issue_id(entry.entry_id))
        return
    _LOGGER.debug("legacy cloud entities: %s", listed.counts())
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(entry.entry_id),
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_KEY,
        translation_placeholders={"title": panel_name(hass, entry)},
        data={"entry_id": entry.entry_id},
    )


@callback
def async_remove_issue(hass: HomeAssistant, entry_id: str) -> None:
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry_id))


async def _lines(hass: HomeAssistant) -> dict[str, str]:
    prefix = f"component.{DOMAIN}.selector.{GROUP_SELECTOR}.options."
    strings = await async_get_translations(hass, hass.config.language, "selector", {DOMAIN})
    return {key[len(prefix) :]: text for key, text in strings.items() if key.startswith(prefix)}


async def confirm_placeholders(
    hass: HomeAssistant, entry: ConfigEntry, listed: LegacyEntities
) -> dict[str, str]:
    """Counts per group and up to `SHOWN` entity_ids; never a unique id."""
    lines = await _lines(hass)
    fallback_key = FALLBACK_LOCAL_ONLY if _local_only(entry) else FALLBACK
    counts = listed.counts()
    groups = [
        f"- {lines.get(key, key).replace('{count}', str(counts[group]))}"
        for group, key in (
            (DUPLICATE, DUPLICATE),
            (UNMATCHED, UNMATCHED),
            (FALLBACK, fallback_key),
        )
        if counts[group]
    ]
    shown, more = listed.shown()
    entities = [f"- {eid}" for eid in shown]
    if more:
        entities.append(lines.get(MORE, MORE).replace("{count}", str(more)))
    return {
        "title": panel_name(hass, entry),
        "count": str(len(listed)),
        "groups": "\n".join(groups),
        "entities": "\n".join(entities),
    }


@callback
def async_apply(hass: HomeAssistant, entry: ConfigEntry, confirmed: tuple[str, ...]) -> int:
    """Remove what was confirmed and still qualifies; then re-check the issue."""
    registry = er.async_get(hass)
    removed = 0
    for entity_id in still_listed(confirmed, current(hass, entry)):
        if registry.async_get(entity_id) is not None:
            registry.async_remove(entity_id)
            removed += 1
    _LOGGER.info("removed %d old cloud entities; their statistics stay", removed)
    async_check(hass, entry)
    return removed
