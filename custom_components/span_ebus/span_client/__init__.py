"""SPAN panel data layer for the Home Assistant integration.

This package is the single source of truth for talking to the panel. It lives
under `custom_components/` because HACS ships only what is there, and the
integration is the only thing that consumes it.

`backend.CloudBackend` and the `cloud_*` modules are the live path: Cognito
auth → gRPC → Ably SSE. `local/` stages the panel's own Electrification Bus
for when SPAN enables the MAIN 40 local API, and is not imported at runtime.
"""
