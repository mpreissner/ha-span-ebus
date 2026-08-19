"""Local-path client for the panel's own Electrification Bus (Homie 5 / MQTT).

Staged, not wired up. SPAN has not enabled the MAIN 40 local API yet — the
panel's broker answers on :8883 but the REST tier that issues credentials is
dormant, so nothing here is imported at runtime and the integration runs
entirely on the cloud path in the parent package. See docs/FINDINGS.md.

Once it is live this becomes the preferred transport — LAN-direct, no cloud
round trip — with the cloud path in the parent package kept as the fallback for
when the panel is unreachable or has not been provisioned. Both live in this
one integration; which one is in use is an internal detail that entities and
setup do not expose.

Wiring it up needs `paho-mqtt` added to the integration manifest's
requirements; the cloud path has no such dependency.
"""
