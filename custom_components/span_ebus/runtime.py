"""What a running entry holds: its coordinators, and its local transport."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .coordinator import SpanCloudCoordinator
    from .energy_step import EnergyStepTracker
    from .transport import SpanTransportManager

CoordinatorHook = Callable[["SpanCloudCoordinator"], None]


@dataclass
class SpanRuntime:
    """An entry's runtime data.

    A linked entry builds its cloud coordinators only on the first failover,
    after the platforms have set up, so each platform leaves a hook that is
    run for every coordinator, now and later.
    """

    coordinators: list[SpanCloudCoordinator] = field(default_factory=list)
    transport: SpanTransportManager | None = None
    energy_step: EnergyStepTracker | None = None
    hooks: list[CoordinatorHook] = field(default_factory=list)

    def add_platform(self, hook: CoordinatorHook) -> None:
        self.hooks.append(hook)
        for coordinator in self.coordinators:
            hook(coordinator)

    def add_coordinator(self, coordinator: SpanCloudCoordinator) -> None:
        self.coordinators.append(coordinator)
        for hook in self.hooks:
            hook(coordinator)
