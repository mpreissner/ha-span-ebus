"""Cloud backend — deliberately unimplemented.

The original plan for this project was to scrape SPAN's cloud, on the assumption
that a MAIN 40 has no local API. Reconnaissance showed otherwise: the panel runs
a first-party local MQTT broker (see docs/FINDINGS.md), so this path is
unnecessary.

It stays as a stub to mark the fallback and to record why it is not the default:

- **Resolution.** Cloud data is aggregated; the local Homie stream is real-time.
- **Latency and availability.** A WAN round-trip plus a vendor outage sits
  between you and your own panel, which defeats most panel-driven automations.
- **Fragility.** No published contract, so it can change without notice, and
  credentials would have to be extracted from the mobile app.

If SPAN ever removes the local API and this genuinely becomes the only option:
capture the app's HTTPS traffic once with mitmproxy (patch the APK with
`apk-mitm` to defeat certificate pinning), then reimplement the calls here as a
plain HTTP client. Do not ship an Android emulator as the runtime — use it only
to observe the protocol, then discard it.
"""

from __future__ import annotations

from . import ReadingCallback, SchemaCallback


class CloudBackend:
    """Not implemented. See the module docstring for rationale."""

    name = "cloud"

    def __init__(self, **kwargs) -> None:
        self._kwargs = kwargs

    def start(self, on_schema: SchemaCallback, on_reading: ReadingCallback) -> None:
        raise NotImplementedError(
            "The cloud backend is intentionally unimplemented. This panel exposes "
            "a local API on :8883 — use the 'ebus' backend. See docs/FINDINGS.md."
        )

    def stop(self) -> None:
        return None

    def send_command(self, key: str, value: str) -> None:
        raise NotImplementedError("cloud backend is not implemented")
