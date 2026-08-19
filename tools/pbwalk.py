"""Generic protobuf wire-format walker.

No .proto needed: the wire format is self-describing enough to recover the tree
of (field number, wire type, value) for every message. Length-delimited fields
are heuristically re-parsed as nested messages when they look like valid
protobuf, else shown as string/bytes. Used to reverse the SPAN telemetry frames.
"""

from __future__ import annotations

import struct


def _read_varint(buf, i):
    shift = 0
    result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def _looks_like_message(buf) -> bool:
    """True if buf parses cleanly as protobuf to its exact end."""
    if not buf:
        return False
    try:
        i = 0
        n = len(buf)
        seen = 0
        while i < n:
            key, i = _read_varint(buf, i)
            wt = key & 7
            fno = key >> 3
            if fno == 0 or wt in (3, 4, 6):
                return False
            if wt == 0:
                _, i = _read_varint(buf, i)
            elif wt == 1:
                i += 8
            elif wt == 2:
                ln, i = _read_varint(buf, i)
                i += ln
            elif wt == 5:
                i += 4
            else:
                return False
            if i > n:
                return False
            seen += 1
        return i == n and seen > 0
    except (IndexError, ValueError):
        return False


def walk(buf, depth=0, out=None):
    if out is None:
        out = []
    pad = "  " * depth
    i = 0
    n = len(buf)
    while i < n:
        key, i = _read_varint(buf, i)
        wt = key & 7
        fno = key >> 3
        if wt == 0:
            val, i = _read_varint(buf, i)
            # show signed-zigzag interpretation too, cheaply
            out.append(f"{pad}#{fno} varint = {val}")
        elif wt == 1:
            raw = buf[i : i + 8]
            i += 8
            d = struct.unpack("<d", raw)[0] if len(raw) == 8 else None
            out.append(f"{pad}#{fno} fixed64 = {int.from_bytes(raw, 'little')} (double {d})")
        elif wt == 5:
            raw = buf[i : i + 4]
            i += 4
            f = struct.unpack("<f", raw)[0] if len(raw) == 4 else None
            out.append(f"{pad}#{fno} fixed32 = {int.from_bytes(raw, 'little')} (float {f})")
        elif wt == 2:
            ln, i = _read_varint(buf, i)
            sub = buf[i : i + ln]
            i += ln
            if _looks_like_message(sub) and len(sub) > 1:
                out.append(f"{pad}#{fno} msg ({ln}b) {{")
                walk(sub, depth + 1, out)
                out.append(f"{pad}}}")
            else:
                try:
                    s = sub.decode("utf-8")
                    if s.isprintable():
                        out.append(f"{pad}#{fno} str = {s!r}")
                    else:
                        raise ValueError
                except (UnicodeDecodeError, ValueError):
                    out.append(f"{pad}#{fno} bytes({ln}) = {sub.hex()}")
        else:
            out.append(f"{pad}#{fno} ??? wt={wt} (stop)")
            break
    return out


if __name__ == "__main__":
    import base64
    import sys

    data = sys.stdin.buffer.read().strip()
    try:
        raw = base64.b64decode(data)
    except ValueError:
        raw = bytes.fromhex(data.decode())
    print("\n".join(walk(raw)))
