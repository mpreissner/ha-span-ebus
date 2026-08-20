"""Ably realtime client for SPAN telemetry — token exchange + SSE subscribe.

Once the gRPC `AblyToken` RPC hands us a signed Ably *TokenRequest*, the realtime
path is plain Ably REST/SSE (see docs/CLOUD-FLOW.md):

  1. POST the signed TokenRequest to
     `https://rest.ably.io/keys/<keyName>/requestToken`, which returns a
     `TokenDetails` whose `token` is the credential for the stream.
  2. Open a Server-Sent-Events stream against
     `https://realtime.ably.io/sse?channels=<channel>&access_token=<token>` and
     read `message` events. Each event's `data` is base64-encoded — the decoded
     bytes are the protobuf telemetry frame that `cloud_telemetry.decode_frame`
     understands.

We use SSE (`enveloped=true`, so each event `data` is a JSON Ably Message envelope)
rather than the app's comet long-poll: it is a single long-lived HTTP/2 GET, trivial
to consume with `httpx.stream`, and needs no client-side connection state machine.

Attaching is not enough to get data: SPAN publishes to a channel only once the
`SubscribeAndGetTraits` RPC has named it as a subscriber (see backends.cloud).
After that, frames arrive at ~1-2/sec.

The SSE *parsing* here is pure and unit-tested; the network calls are thin
wrappers around it.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

ABLY_REST = "https://rest.ably.io"
ABLY_SSE = "https://realtime.ably.io/sse"

# A live SSE connection is never silent for long: Ably interleaves `:keepalive`
# comment lines with the telemetry, so bytes keep arriving even on an idle
# channel. A read timeout therefore only trips on a genuinely dead socket — the
# half-open TCP that no FIN ever closed, after a NAT eviction or a node move —
# which is otherwise invisible, because reading such a socket blocks forever and
# the caller's reconnect loop never gets the exception it waits for.
DEFAULT_STREAM_TIMEOUT = httpx.Timeout(connect=15.0, read=45.0, write=15.0, pool=15.0)

# The public Ably app key-name SPAN's TokenRequests are signed against
# (docs/CLOUD-FLOW.md). Only the key *name* is public; requests are signed
# server-side by the AblyToken RPC, so no secret lives here.
DEFAULT_KEY_NAME = "v8kFxw.VMjbuw"


class AblyError(RuntimeError):
    """An Ably token exchange or stream failed."""


@dataclass(frozen=True)
class AblyTokenDetails:
    """The realtime credential returned by Ably's requestToken endpoint."""

    token: str
    expires: int | None = None  # epoch millis, if provided
    client_id: str | None = None


def request_token(
    token_request: dict,
    *,
    key_name: str = DEFAULT_KEY_NAME,
    client: httpx.Client | None = None,
    timeout: float = 20.0,
) -> AblyTokenDetails:
    """Exchange a signed Ably TokenRequest for a usable TokenDetails.

    `token_request` is the JSON object produced by the gRPC `AblyToken` RPC
    (fields like `keyName`, `ttl`, `capability`, `nonce`, `mac`).
    """
    url = f"{ABLY_REST}/keys/{key_name}/requestToken"
    owns = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        resp = client.post(url, json=token_request)
    except httpx.HTTPError as exc:
        raise AblyError(f"token exchange transport error: {exc}") from exc
    finally:
        if owns:
            client.close()
    # Ably returns 201 Created on success; accept any 2xx. The body carries the
    # token itself, so never echo it — surface only a short reason on failure.
    if not 200 <= resp.status_code < 300:
        reason = ""
        try:
            reason = resp.json().get("error", {}).get("message", "")
        except (ValueError, AttributeError):
            reason = resp.text[:120]
        raise AblyError(f"token exchange failed (HTTP {resp.status_code}): {reason}")
    data = resp.json()
    token = data.get("token")
    if not token:
        raise AblyError(f"token exchange returned no token: {data!r}")
    return AblyTokenDetails(
        token=token,
        expires=data.get("expires"),
        client_id=data.get("clientId"),
    )


# --- SSE parsing (pure) -------------------------------------------------------


def iter_sse_events(lines: Iterator[str]) -> Iterator[tuple[str, str]]:
    """Group a stream of SSE text lines into (event, data) pairs.

    Implements the subset of the SSE grammar Ably emits: `event:` and `data:`
    fields terminated by a blank line. `data:` values that span multiple lines
    are joined with newlines per the SSE spec. Comment lines (starting `:`) and
    unknown fields are ignored.
    """
    event = "message"
    data_lines: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data_lines:
                yield event, "\n".join(data_lines)
            event = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)


def halt_when(lines: Iterator[str], stop: Callable[[], bool] | None) -> Iterator[str]:
    """Pass `lines` through, ending the stream once `stop()` goes True.

    The check is per *line*, not per telemetry event, and that distinction is the
    point: on a channel that has gone quiet the keepalive comments are the only
    traffic left, so a caller consulted only when a frame arrives can never be
    woken from precisely the state a liveness watchdog needs to break out of.
    """
    for line in lines:
        if stop is not None and stop():
            return
        yield line


def decode_message_data(sse_data: str) -> bytes | None:
    """Extract the telemetry payload from one SSE `message` event's data.

    Ably delivers a JSON Message object; the telemetry frame is its base64
    `data`. Returns the decoded protobuf bytes, or None for non-telemetry
    envelopes (heartbeats, connection/attach acks, or messages without base64).
    """
    try:
        msg = json.loads(sse_data)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(msg, dict):
        return None
    payload = msg.get("data")
    if not isinstance(payload, str):
        return None
    if msg.get("encoding") not in ("base64", "base64/utf-8", None):
        # Only base64 telemetry is expected; skip anything else.
        return None
    try:
        return base64.b64decode(payload)
    except (ValueError, base64.binascii.Error):
        return None


# --- streaming ----------------------------------------------------------------


def stream_frames(
    token: str,
    channel: str,
    on_frame: Callable[[bytes], None],
    *,
    stop: Callable[[], bool] | None = None,
    client: httpx.Client | None = None,
    timeout: httpx.Timeout | float | None = None,
) -> None:
    """Subscribe to `channel` and invoke `on_frame(raw_bytes)` per telemetry frame.

    Blocks until the stream ends, an error occurs, or `stop()` returns True —
    the latter checked once per line received, so a caller can pull the plug on a
    channel that is still connected but no longer publishing. Intended to run on
    a background thread (see backends.cloud).

    `timeout=None` applies `DEFAULT_STREAM_TIMEOUT`, whose read budget is what
    turns a silently dead socket into an exception the caller can reconnect from.
    Pass an explicit `httpx.Timeout(None)` to wait forever instead.
    """
    # `enveloped=true` is required: in the un-enveloped mode Ably's SSE endpoint
    # delivered no telemetry for this app key (empirically — see the live smoke).
    # Enveloped mode wraps each message as a JSON object carrying `data`/`encoding`,
    # which `decode_message_data` unwraps.
    params = {
        "channels": channel,
        "access_token": token,
        "enveloped": "true",
        "v": "1.2",
    }
    owns = client is None
    if timeout is None:
        timeout = DEFAULT_STREAM_TIMEOUT
    client = client or httpx.Client(http2=True, timeout=timeout)
    try:
        with client.stream("GET", ABLY_SSE, params=params) as resp:
            if resp.status_code != 200:
                body = resp.read().decode("utf-8", "replace")[:200]
                raise AblyError(f"SSE subscribe failed (HTTP {resp.status_code}): {body}")
            for event, data in iter_sse_events(halt_when(resp.iter_lines(), stop)):
                if event != "message":
                    continue
                frame = decode_message_data(data)
                if frame is not None:
                    on_frame(frame)
    except httpx.HTTPError as exc:
        raise AblyError(f"SSE stream transport error: {exc}") from exc
    finally:
        if owns:
            client.close()
