"""Lock platform: an EV charger's lock, on a panel's local API.

Only chargers that declare the lock settable get one. The cloud path has no
locks.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SpanConfigEntry
from .local_entity import SpanLocalEntity, async_setup_local
from .span_client.local.commands import Control

# The charger's own vocabulary.
STATE_LOCKED = "LOCKED"
STATE_UNLOCKED = "UNLOCKED"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SpanConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    transport = entry.runtime_data.transport
    if transport is not None:
        async_setup_local(entry, transport, async_add_entities, "lock", SpanLocalEvseLock)


class SpanLocalEvseLock(SpanLocalEntity, LockEntity):
    """Locked or unlocked; locking or unlocking while a command awaits the echo."""

    _control = Control.EVSE_LOCK

    @property
    def is_locked(self) -> bool | None:
        if self._value == STATE_LOCKED:
            return True
        if self._value == STATE_UNLOCKED:
            return False
        return None

    @property
    def is_locking(self) -> bool:
        return self._pending is not None and self._pending.value == STATE_LOCKED

    @property
    def is_unlocking(self) -> bool:
        return self._pending is not None and self._pending.value == STATE_UNLOCKED

    async def async_lock(self, **kwargs: Any) -> None:
        await self._async_command(STATE_LOCKED)

    async def async_unlock(self, **kwargs: Any) -> None:
        await self._async_command(STATE_UNLOCKED)
