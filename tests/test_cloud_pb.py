"""Tests for the hand-rolled protobuf wire codec.

No captured data: every message here is built with the writer and parsed back,
so the codec is exercised end-to-end on synthetic input.
"""

from span_bridge import cloud_pb as pb


def test_varint_roundtrip():
    for n in (0, 1, 127, 128, 300, 16384, 2**32, 2**63 - 1):
        buf = pb.write_varint(n)
        value, pos = pb.read_varint(buf, 0)
        assert value == n
        assert pos == len(buf)


def test_zigzag_decode():
    # protobuf's canonical sint mapping: 0,-1,1,-2,2 -> 0,1,2,3,4
    assert pb.zigzag_decode(0) == 0
    assert pb.zigzag_decode(1) == -1
    assert pb.zigzag_decode(2) == 1
    assert pb.zigzag_decode(3) == -2
    assert pb.zigzag_decode(4) == 2


def test_message_roundtrip_all_wire_types():
    body = (
        pb.field_varint(1, 42)
        + pb.field_string(2, "hello")
        + pb.field_message(3, pb.field_varint(1, 7))
        + pb.field_bytes(4, b"\x00\xff")
    )
    msg = pb.parse(body)
    assert msg.get_uint(1) == 42
    assert msg.get_str(2) == "hello"
    assert msg.get_msg(3).get_uint(1) == 7
    assert msg.get_bytes(4) == b"\x00\xff"


def test_repeated_fields_preserved_in_order():
    body = pb.field_varint(1, 10) + pb.field_varint(1, 20) + pb.field_varint(1, 30)
    msg = pb.parse(body)
    assert msg.values(1) == [10, 20, 30]
    # get_* accessors pull the first occurrence.
    assert msg.get_uint(1) == 10


def test_get_int_opt_is_tolerant():
    body = pb.field_string(3, "not a number")
    msg = pb.parse(body)
    # Strict accessor raises; tolerant one returns None instead.
    assert msg.get_int_opt(3) is None
    assert msg.get_int_opt(9) is None  # absent field


def test_truncated_varint_raises():
    try:
        pb.read_varint(b"\x80\x80", 0)  # continuation bit set, no terminator
    except pb.ProtoError:
        return
    raise AssertionError("expected ProtoError on truncated varint")
