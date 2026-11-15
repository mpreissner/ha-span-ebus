"""Finding a SPAN device in the device registry, on any supported Home Assistant.

Home Assistant 2026.x deprecates `async_get_device` (it breaks in 2027.8) because
identifiers are no longer unique across config entries. This integration still
supports releases that only have `async_get_device`, so each lookup picks the
newest call the running release has. No Home Assistant imports at run time.
"""

from __future__ import annotations

from typing import Any


def device_by_identifier(
    registry: Any, identifier: tuple[str, str], entry_id: str | None = None
) -> Any | None:
    """The device with this identifier.

    With `entry_id`, only a device owned by that entry. Without it (a lookup
    across this integration's entries, such as another panel's device), the
    first device with the identifier.
    """
    if entry_id is not None and hasattr(registry, "async_get_device_by_identifier"):
        return registry.async_get_device_by_identifier(identifier, entry_id)
    if hasattr(registry, "async_get_devices"):
        found = registry.async_get_devices(identifiers={identifier})
        return found[0] if found else None
    return registry.async_get_device(identifiers={identifier})
