"""Tests for the gRPC length-prefix framing (the network-free part)."""

import pytest
from span_client import cloud_grpc as g


def test_frame_roundtrip():
    payload = b"\x08\x2a"  # arbitrary protobuf bytes
    framed = g.frame_message(payload)
    assert framed[0] == 0  # uncompressed flag
    assert int.from_bytes(framed[1:5], "big") == len(payload)
    assert g.unframe_message(framed) == payload


def test_unframe_empty_body_is_none():
    assert g.unframe_message(b"") is None
    assert g.unframe_message(b"\x00\x00\x00\x00\x00") == b""


def test_unframe_truncated_raises():
    # header claims 4 bytes but only 2 follow
    with pytest.raises(g.GrpcError):
        g.unframe_message(b"\x00\x00\x00\x00\x04ab")


def test_unframe_compressed_flag_raises():
    with pytest.raises(g.GrpcError):
        g.unframe_message(b"\x01\x00\x00\x00\x01a")


def test_grpc_error_names_status():
    err = g.GrpcError(16, "no token", "AblyToken")
    assert "UNAUTHENTICATED" in str(err)
    assert err.status == 16
    assert err.method == "AblyToken"
