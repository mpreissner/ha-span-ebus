"""Local-path client for the panel's own Electrification Bus (Homie 5 / MQTT).

Staged, not wired up. SPAN has not enabled the MAIN 40 local API yet — the
panel's broker answers on :8883 but the REST tier that issues credentials is
dormant, so nothing here is imported at runtime and the integration runs
entirely on the cloud path in the parent package. See docs/FINDINGS.md.

Once it is live this becomes the preferred transport — LAN-direct, no cloud
round trip — with the cloud path in the parent package kept as the fallback for
when the panel is unreachable or has not been provisioned. Both live in this
one integration and produce the same entities.

Fallback is the default, not the only option: a user who keeps the panel off
the internet must be able to choose local-only and have that honored. Whether
local-only can also skip SPAN account credentials depends on an authentication
question SPAN has not answered yet — see the open question in docs/FINDINGS.md.

Wiring it up needs `paho-mqtt` added to the integration manifest's
requirements; the cloud path has no such dependency.
"""
