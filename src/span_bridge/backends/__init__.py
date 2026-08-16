"""Pluggable panel data sources.

A backend's job is to produce a `PanelSchema` and a stream of `Reading`s, and to
accept commands for settable properties. Nothing downstream knows or cares which
backend is in use, so the transport can change without touching discovery or
publishing.
"""

from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

from ..models import PanelSchema, Reading

# Called by a backend whenever a new value arrives.
ReadingCallback = Callable[[Reading], None]
# Called by a backend once the panel's schema is known or changes.
SchemaCallback = Callable[[PanelSchema], None]


@runtime_checkable
class Backend(Protocol):
    """The contract every data source implements."""

    name: str

    def start(self, on_schema: SchemaCallback, on_reading: ReadingCallback) -> None:
        """Connect and begin streaming. Non-blocking."""
        ...

    def stop(self) -> None:
        """Disconnect cleanly."""
        ...

    def send_command(self, key: str, value: str) -> None:
        """Write a settable property. `key` is `<node-id>/<property-id>`."""
        ...


def load(name: str, **kwargs) -> Backend:
    """Instantiate a backend by name."""
    if name == "ebus":
        from .ebus import EbusBackend

        return EbusBackend(**kwargs)
    if name == "grpc_legacy":
        from .grpc_legacy import GrpcLegacyBackend

        return GrpcLegacyBackend(**kwargs)
    if name == "cloud":
        from .cloud import CloudBackend

        return CloudBackend(**kwargs)
    raise ValueError(f"unknown backend {name!r} (expected ebus, grpc_legacy, or cloud)")
