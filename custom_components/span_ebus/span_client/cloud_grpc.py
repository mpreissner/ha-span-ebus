"""Unary gRPC client for SPAN's mobilefrontend service — over httpx HTTP/2.

The homeowner data plane is a gRPC service reached at
`mobilefrontend.prd.span.io`, authenticated with the Cognito **access token** as
a `Bearer` credential (see docs/CLOUD-FLOW.md). We only need unary (request →
single response) RPCs, so rather than pull in `grpcio` (a C-extension that is
painful to vendor into a HACS integration) we speak the gRPC wire format directly
over an HTTP/2 connection from `httpx`.

gRPC message framing (both directions): a length-prefixed message

    [1 byte  compression flag = 0]
    [4 bytes big-endian message length]
    [   protobuf message bytes      ]

The RPC method is the URL path
`/io.span.services.mobilefrontend.MobileFrontendService/<Method>`, content-type
`application/grpc`, and the call status arrives as the `grpc-status` trailer
(0 = OK). httpx surfaces HTTP/2 trailers only for *trailers-only* responses (a
failure with no body); for a normal response we simply decode the returned
message frame. Both paths are handled below.

This module stays schema-agnostic: it returns the raw response protobuf bytes and
lets callers (`backends.cloud`) parse them with `cloud_pb`.
"""

from __future__ import annotations

import logging
from typing import Self

import httpx

log = logging.getLogger(__name__)

DEFAULT_HOST = "mobilefrontend.prd.span.io"
SERVICE = "io.span.services.mobilefrontend.MobileFrontendService"

# gRPC status codes we name; everything else is reported numerically.
_GRPC_STATUS = {
    0: "OK",
    3: "INVALID_ARGUMENT",
    7: "PERMISSION_DENIED",
    16: "UNAUTHENTICATED",
}


class GrpcError(RuntimeError):
    """A gRPC call returned a non-OK status or a malformed frame."""

    def __init__(self, status: int, message: str, method: str) -> None:
        name = _GRPC_STATUS.get(status, str(status))
        super().__init__(f"{method} failed: gRPC {name} ({status}): {message}")
        self.status = status
        self.grpc_message = message
        self.method = method


def frame_message(payload: bytes) -> bytes:
    """Wrap a protobuf message in the gRPC length-prefixed frame."""
    return b"\x00" + len(payload).to_bytes(4, "big") + payload


def unframe_message(body: bytes) -> bytes | None:
    """Return the first message from a gRPC response body, or None if empty.

    Only the leading frame is returned; unary responses carry exactly one.
    """
    if len(body) < 5:
        return None
    flag = body[0]
    length = int.from_bytes(body[1:5], "big")
    if 5 + length > len(body):
        raise GrpcError(2, "truncated gRPC response frame", "<unframe>")
    if flag & 0x01:
        # We advertise identity encoding and never expect a compressed reply.
        raise GrpcError(2, "unexpected compressed gRPC frame", "<unframe>")
    return body[5 : 5 + length]


class CloudGrpcClient:
    """Minimal unary gRPC client bound to one access token.

    Not thread-safe for concurrent calls on a single instance's client, but the
    bridge issues its handful of bootstrap RPCs sequentially before handing off
    to the Ably stream.
    """

    def __init__(
        self,
        access_token: str,
        *,
        host: str = DEFAULT_HOST,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._host = host
        self._token = access_token
        self._owns_client = client is None
        self._client = client or httpx.Client(http2=True, timeout=timeout)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def call(self, method: str, request: bytes = b"") -> bytes:
        """Invoke a unary RPC, returning the response protobuf bytes.

        `request` is the already-serialized request message (empty for RPCs that
        take no arguments, e.g. GetSitesForUser).
        """
        url = f"https://{self._host}/{SERVICE}/{method}"
        headers = {
            "content-type": "application/grpc",
            "te": "trailers",
            "grpc-encoding": "identity",
            "grpc-accept-encoding": "identity",
            "authorization": f"Bearer {self._token}",
            "user-agent": "ha-span-ebus-grpc/1.0",
        }
        try:
            resp = self._client.post(url, content=frame_message(request), headers=headers)
        except httpx.HTTPError as exc:
            raise GrpcError(14, f"transport error: {exc}", method) from exc

        # A trailers-only failure carries grpc-status in the header block.
        status_hdr = resp.headers.get("grpc-status")
        if status_hdr is not None and status_hdr != "0":
            raise GrpcError(int(status_hdr), resp.headers.get("grpc-message", ""), method)

        if resp.status_code != 200:
            raise GrpcError(2, f"HTTP {resp.status_code}", method)

        payload = unframe_message(resp.content)
        if payload is None:
            # No message and no explicit error status: treat as empty OK unless
            # the trailer says otherwise.
            if status_hdr not in (None, "0"):
                raise GrpcError(int(status_hdr), resp.headers.get("grpc-message", ""), method)
            return b""
        return payload

    # --- named RPCs -----------------------------------------------------------

    def get_sites_for_user(self) -> bytes:
        """GetSitesForUser: empty request → site topology (serials, resources)."""
        return self.call("GetSitesForUser")

    def ably_token(self, request: bytes) -> bytes:
        """AblyToken: request carries the device UUID → a signed Ably token."""
        return self.call("AblyToken", request)

    def search_resources_for_serial(self, request: bytes) -> bytes:
        """SearchResourcesForSerial: map a panel serial → its trait resources."""
        return self.call("SearchResourcesForSerial", request)

    def subscribe_and_get_traits(self, request: bytes) -> bytes:
        """SubscribeAndGetTraits: register for telemetry → a trait snapshot.

        This is what makes SPAN publish to our Ably channel; without it the
        channel attaches and stays silent. The response is a one-shot ~22 KB
        snapshot of the subscribed resources' traits.
        """
        return self.call("SubscribeAndGetTraits", request)

    def send_messages(self, request: bytes) -> bytes:
        """SendMessages: the single write path for every trait command.

        There is no per-trait RPC; a breaker toggle, like anything else that acts
        on a trait, is a `TraitMessage` posted here (see `cloud_commands`). The
        reply is an ack — the recovered schema has no `SendMessagesResponse` — so
        an empty body with `grpc-status: 0` is success. The command's real answer
        goes to the app's Ably trait channel, which we do not read; we confirm by
        re-reading the trait snapshot instead.
        """
        return self.call("SendMessages", request)
