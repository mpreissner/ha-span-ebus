# SPAN Panel (eBus)

Live per-circuit power for a **SPAN Panel MAIN 40 (Gen3)** in Home Assistant,
via SPAN's cloud realtime stream (Cognito → gRPC → Ably SSE).

This is the cloud path used while the panel's local API is unavailable. When
SPAN enables the local MAIN 40 API, the integration will move to the local
Electrification Bus (MQTT) with no change to your entities.

## Setup

1. Install via HACS, then restart Home Assistant.
2. **Settings → Devices & Services → Add Integration → SPAN Panel (eBus)**.
3. Sign in with your SPAN account email and password.

Your password is used **once** to obtain access tokens and is never stored. Only
the resulting tokens are kept, and they refresh automatically.

## What you get

- A device per panel, with a sensor for each circuit's power (and current /
  voltage / site flows where reported), updated at ~1–2 Hz.
