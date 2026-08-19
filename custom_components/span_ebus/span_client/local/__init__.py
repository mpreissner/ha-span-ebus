"""Local-path client for the panel's own Electrification Bus (Homie 5 / MQTT).

Staged, not wired up. SPAN has not enabled the MAIN 40 local API yet — the
panel's broker answers on :8883 but the REST tier that issues credentials is
dormant, so nothing here is imported at runtime and the integration runs
entirely on the cloud path in the parent package. See docs/FINDINGS.md.

When it does go live, `ebus.EbusBackend` needs `paho-mqtt` added to the
integration manifest's requirements; the cloud path has no such dependency.
"""
