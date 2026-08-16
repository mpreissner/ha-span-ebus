"""Tests for the Ably SSE parsing (pure, network-free)."""

import base64
import json

import httpx
import pytest

from span_bridge import cloud_ably as a


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_request_token_accepts_201():
    # Ably returns 201 Created on success — must be accepted, not treated as error.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201, json={"token": "v8kFxw.tok", "expires": 123, "clientId": "c:u:d"}
        )

    details = a.request_token({"keyName": "k", "mac": "m"}, client=_client(handler))
    assert details.token == "v8kFxw.tok"
    assert details.expires == 123
    assert details.client_id == "c:u:d"


def test_request_token_error_does_not_echo_body_token():
    # On failure we surface the Ably error message only — never the raw body,
    # which could carry a credential.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"message": "invalid mac", "code": 40101}, "token": "leak"},
        )

    with pytest.raises(a.AblyError) as exc:
        a.request_token({"keyName": "k"}, client=_client(handler))
    assert "invalid mac" in str(exc.value)
    assert "leak" not in str(exc.value)


def test_iter_sse_events_groups_by_blank_line():
    lines = iter(
        [
            "event: message",
            "data: {\"a\":1}",
            "",
            "event: heartbeat",
            "data: keepalive",
            "",
        ]
    )
    events = list(a.iter_sse_events(lines))
    assert events == [("message", '{"a":1}'), ("heartbeat", "keepalive")]


def test_iter_sse_events_multiline_data_joined():
    lines = iter(["data: line1", "data: line2", ""])
    events = list(a.iter_sse_events(lines))
    assert events == [("message", "line1\nline2")]


def test_iter_sse_events_ignores_comments_and_trailing_partial():
    lines = iter([": this is a comment", "data: x", ""])
    assert list(a.iter_sse_events(lines)) == [("message", "x")]
    # No trailing blank line -> the last, unfinished event is not emitted.
    assert list(a.iter_sse_events(iter(["data: y"]))) == []


def test_decode_message_data_base64_payload():
    frame = b"\x08\x96\x01"
    envelope = json.dumps(
        {"name": "message", "encoding": "base64", "data": base64.b64encode(frame).decode()}
    )
    assert a.decode_message_data(envelope) == frame


def test_decode_message_data_skips_non_telemetry():
    assert a.decode_message_data("not json") is None
    assert a.decode_message_data(json.dumps({"data": 123})) is None
    assert a.decode_message_data(json.dumps({"data": "x", "encoding": "json"})) is None
