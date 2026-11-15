"""Panels found on the network, for the add-panel picker.

Home Assistant's zeroconf hands the config flow `_ebus._tcp` announcements;
this module only decides which of them to offer.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

SERVICE_TYPE = "_ebus._tcp.local."


@dataclass(frozen=True)
class Candidate:
    """A panel seen on the network. `serial` is known once it was probed."""

    host: str
    name: str
    serial: str | None = None

    @property
    def label(self) -> str:
        return self.host if self.name in ("", self.host) else f"{self.name} ({self.host})"


def instance_name(service_name: str) -> str:
    """`span-xyz._ebus._tcp.local.` -> `span-xyz`."""
    suffix = "." + SERVICE_TYPE
    return service_name.removesuffix(suffix)


def offerable(
    found: Iterable[Candidate],
    *,
    configured_hosts: Iterable[str],
    configured_serials: Iterable[str],
) -> list[Candidate]:
    """The candidates worth offering: new panels, once each, in a stable order.

    One panel can be seen twice (a discovery flow and a browse, or two
    addresses); the first sighting of a host or serial wins, and one with a
    serial is preferred over one without.
    """
    hosts, serials = set(configured_hosts), set(configured_serials)
    ordered = sorted(found, key=lambda c: c.serial is None)
    kept: list[Candidate] = []
    for candidate in ordered:
        if candidate.host in hosts or (candidate.serial and candidate.serial in serials):
            continue
        hosts.add(candidate.host)
        if candidate.serial:
            serials.add(candidate.serial)
        kept.append(candidate)
    return sorted(kept, key=lambda c: (c.name.lower(), c.host))
