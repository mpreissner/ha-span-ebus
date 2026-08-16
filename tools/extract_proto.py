"""Recover protobuf-es v1 message schemas from the Hermes disassembly.

protobuf-es builds each message's runtime descriptor by calling makeMessageType
with an array of field literals. hbc-disassembler resolves those constant object
buffers into readable comments:

    # Object: {'no': 6, 'name': 'count', 'kind': 'scalar', 'T': 13}
    ...
    # Array: ['io.span.services.common.Measurement']

The trailing Array holds the fully-qualified type name. So per function we collect
the ordered Object literals (the fields) and flush them when the type name appears.
"""
from __future__ import annotations

import ast
import re
import sys

FUNC_RE = re.compile(r"=> \[Function #\d+")
OBJ_RE = re.compile(r"# Object: (\{.*\})\s*$")
ARR_RE = re.compile(r"# Array: (\[.*\])\s*$")

# protobuf-es scalar type codes (ScalarType enum).
SCALAR = {
    1: "double", 2: "float", 3: "int64", 4: "uint64", 5: "int32",
    6: "fixed64", 7: "fixed32", 8: "bool", 9: "string", 10: "group",
    12: "bytes", 13: "uint32", 14: "enum", 15: "sfixed32", 16: "sfixed64",
    17: "sint32", 18: "sint64",
}


def parse(path: str) -> dict[str, list[dict]]:
    messages: dict[str, list[dict]] = {}
    fields: list[dict] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if FUNC_RE.search(line):
                # A new function boundary: only reset if we're not mid-message.
                # Object literals live inside one function ending in the Array.
                fields = []
                continue
            m = OBJ_RE.search(line)
            if m:
                try:
                    fields.append(ast.literal_eval(m.group(1)))
                except (ValueError, SyntaxError):
                    pass
                continue
            m = ARR_RE.search(line)
            if m:
                try:
                    arr = ast.literal_eval(m.group(1))
                except (ValueError, SyntaxError):
                    arr = []
                if (
                    len(arr) == 1
                    and isinstance(arr[0], str)
                    and arr[0].startswith("io.span")
                    and fields
                ):
                    # Only keep arrays that are a lone fq type-name with pending
                    # fields — that's the makeMessageType(name, fields) shape.
                    name = arr[0]
                    if all(isinstance(f, dict) and "no" in f for f in fields):
                        messages.setdefault(name, fields)
                fields = []
    return messages


def fmt_field(f: dict) -> str:
    no = f.get("no")
    name = f.get("name")
    kind = f.get("kind")
    if kind == "scalar":
        t = SCALAR.get(f.get("T"), f"T{f.get('T')}")
    elif kind == "message":
        t = "message"
    elif kind == "enum":
        t = "enum"
    elif kind == "map":
        t = "map"
    else:
        t = kind
    extra = []
    if f.get("oneof"):
        extra.append(f"oneof={f['oneof']}")
    if f.get("repeated"):
        extra.append("repeated")
    tail = f"  ({', '.join(extra)})" if extra else ""
    return f"  {no}: {name} = {t}{tail}"


def main() -> None:
    path = sys.argv[1]
    want = sys.argv[2] if len(sys.argv) > 2 else None
    messages = parse(path)
    names = sorted(messages)
    if want:
        names = [n for n in names if want in n]
    for name in names:
        print(name)
        for f in messages[name]:
            print(fmt_field(f))
        print()
    print(f"# {len(messages)} messages total", file=sys.stderr)


if __name__ == "__main__":
    main()
