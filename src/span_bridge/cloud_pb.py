"""Minimal protobuf wire codec — no external dependencies.

SPAN's cloud data plane speaks protobuf (Connect/gRPC on the mobilefrontend
service, base64-protobuf on the Ably realtime channel), but there is no public
`.proto` and no compiled stubs. Rather than pull in `protobuf` + `grpcio` (a
heavy, C-extension dependency chain that is awkward to vendor into a HACS
integration), we hand-roll just enough of the wire format.

The recovered schema lives in `docs/reference/span-cloud-schema.txt`; this module
knows nothing about specific messages. It reads and writes the four wire types we
need — varint, 64-bit, length-delimited, 32-bit — and leaves semantic
interpretation to callers (`cloud_grpc`, `cloud_telemetry`).

Reference: https://protobuf.dev/programming-guides/encoding/
"""

from __future__ import annotations

from typing import Iterator

# Wire types (the low 3 bits of a field tag).
WIRE_VARINT = 0
WIRE_I64 = 1
WIRE_LEN = 2
WIRE_I32 = 5


class ProtoError(ValueError):
    """Malformed protobuf on the wire."""


# --- reading ------------------------------------------------------------------


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Return (value, new_pos). Raises on truncation or an over-long varint."""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ProtoError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ProtoError("varint exceeds 64 bits")


def zigzag_decode(value: int) -> int:
    """Undo protobuf's zig-zag mapping used by sint32/sint64."""
    return (value >> 1) ^ -(value & 1)


class Message:
    """A parsed protobuf message: field number -> list of raw wire values.

    Fields are grouped by number so repeated fields are preserved in order.
    Values are typed by wire kind: varint/I64/I32 as `int`, LEN as `bytes`.
    Helper accessors (`get_*`) pull and coerce a single occurrence.
    """

    __slots__ = ("fields",)

    def __init__(self) -> None:
        self.fields: dict[int, list[tuple[int, int | bytes]]] = {}

    def _add(self, no: int, wire: int, value: int | bytes) -> None:
        self.fields.setdefault(no, []).append((wire, value))

    def __contains__(self, no: int) -> bool:
        return no in self.fields

    def values(self, no: int) -> list[int | bytes]:
        return [v for _, v in self.fields.get(no, [])]

    def _first(self, no: int) -> tuple[int, int | bytes] | None:
        vs = self.fields.get(no)
        return vs[0] if vs else None

    def get_uint(self, no: int, default: int | None = None) -> int | None:
        v = self._first(no)
        if v is None:
            return default
        wire, value = v
        if not isinstance(value, int):
            raise ProtoError(f"field {no} is not numeric")
        return value

    def get_int_opt(self, no: int) -> int | None:
        """Return field `no` only if it is present *and* a numeric wire value.

        Unlike `get_uint`, this never raises on a type mismatch — a caller
        probing a positional slot that turns out to hold a nested message just
        gets `None`. Used by tolerant decoders walking heuristic layouts.
        """
        v = self._first(no)
        if v is None or not isinstance(v[1], int):
            return None
        return v[1]

    def get_sint(self, no: int, default: int | None = None) -> int | None:
        """A varint field carrying a zig-zag-encoded signed value (sint32/64)."""
        v = self.get_uint(no)
        return default if v is None else zigzag_decode(v)

    def get_bytes(self, no: int, default: bytes | None = None) -> bytes | None:
        v = self._first(no)
        if v is None:
            return default
        wire, value = v
        if not isinstance(value, bytes):
            raise ProtoError(f"field {no} is not length-delimited")
        return value

    def get_str(self, no: int, default: str | None = None) -> str | None:
        raw = self.get_bytes(no)
        return default if raw is None else raw.decode("utf-8", "replace")

    def get_msg(self, no: int) -> "Message | None":
        raw = self.get_bytes(no)
        return None if raw is None else parse(raw)

    def get_msgs(self, no: int) -> "list[Message]":
        return [parse(v) for v in self.values(no) if isinstance(v, bytes)]

    def __repr__(self) -> str:
        return f"Message({sorted(self.fields)})"


def iter_fields(buf: bytes) -> Iterator[tuple[int, int, int | bytes]]:
    """Yield (field_no, wire_type, value) for each field in a message body."""
    pos = 0
    n = len(buf)
    while pos < n:
        tag, pos = read_varint(buf, pos)
        no = tag >> 3
        wire = tag & 0x07
        if no == 0:
            raise ProtoError("field number 0 is invalid")
        if wire == WIRE_VARINT:
            value, pos = read_varint(buf, pos)
            yield no, wire, value
        elif wire == WIRE_I64:
            if pos + 8 > n:
                raise ProtoError("truncated 64-bit field")
            yield no, wire, int.from_bytes(buf[pos : pos + 8], "little")
            pos += 8
        elif wire == WIRE_LEN:
            length, pos = read_varint(buf, pos)
            if pos + length > n:
                raise ProtoError("truncated length-delimited field")
            yield no, wire, buf[pos : pos + length]
            pos += length
        elif wire == WIRE_I32:
            if pos + 4 > n:
                raise ProtoError("truncated 32-bit field")
            yield no, wire, int.from_bytes(buf[pos : pos + 4], "little")
            pos += 4
        else:
            raise ProtoError(f"unsupported wire type {wire}")


def parse(buf: bytes) -> Message:
    """Parse a message body into grouped fields."""
    msg = Message()
    for no, wire, value in iter_fields(buf):
        msg._add(no, wire, value)
    return msg


# --- writing ------------------------------------------------------------------


def write_varint(value: int) -> bytes:
    """Encode an unsigned varint. Callers must pre-encode signed values."""
    if value < 0:
        raise ProtoError("write_varint requires an unsigned value")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _tag(no: int, wire: int) -> bytes:
    return write_varint((no << 3) | wire)


def field_varint(no: int, value: int) -> bytes:
    return _tag(no, WIRE_VARINT) + write_varint(value)


def field_string(no: int, value: str) -> bytes:
    raw = value.encode("utf-8")
    return _tag(no, WIRE_LEN) + write_varint(len(raw)) + raw


def field_bytes(no: int, raw: bytes) -> bytes:
    return _tag(no, WIRE_LEN) + write_varint(len(raw)) + raw


def field_message(no: int, body: bytes) -> bytes:
    return _tag(no, WIRE_LEN) + write_varint(len(body)) + body
