"""Legacy Gen3 gRPC backend — retained for pre-r202627 firmware only.

Early MAIN 40 firmware exposed an unauthenticated gRPC service on :50065
(`io.span.panel.protocols.traithandler.TraitHandlerService`), reverse-engineered
by https://github.com/Griswoldlabs/span-panel-ha.

**This is dead on current firmware.** Verified against this panel on
`spanos3/r202627/06`: port 50065 refuses connections, and while :50058 still
speaks gRPC, reflection is disabled and every known service returns
`Unimplemented`. See docs/FINDINGS.md.

Kept as a stub because (a) it documents what was tried, and (b) panels held back
on older firmware can still use it. Implement against the upstream
`span.protoset` if that need ever arises.
"""

from __future__ import annotations

import logging

from . import ReadingCallback, SchemaCallback

log = logging.getLogger(__name__)

LEGACY_PORT = 50065
CURRENT_GRPC_PORT = 50058
PROTOSET_URL = (
    "https://raw.githubusercontent.com/Griswoldlabs/span-panel-ha/main/"
    "custom_components/span_panel/span.protoset"
)


class GrpcLegacyBackend:
    """Not implemented. Present to document a dead end."""

    name = "grpc_legacy"

    def __init__(self, host: str, port: int = LEGACY_PORT) -> None:
        self._host = host
        self._port = port

    def start(self, on_schema: SchemaCallback, on_reading: ReadingCallback) -> None:
        raise NotImplementedError(
            "The Gen3 gRPC API was removed in firmware r202627 / 7.2.0. "
            "Port 50065 is closed and the services on 50058 return Unimplemented. "
            "Use the 'ebus' backend instead — see docs/FINDINGS.md."
        )

    def stop(self) -> None:
        return None

    def send_command(self, key: str, value: str) -> None:
        raise NotImplementedError("gRPC backend is not implemented")
