#!/usr/bin/env python3
"""Turn a span_dump/ folder into something a person can read.

The raw dump is protobuf decoded without a schema, so it is all field numbers.
This uses the integration's own decoders, which know what those fields mean,
to print each panel and its circuits and write span_dump/readable.json.

    python3 tools/span_readable.py            # reads ./span_dump
    python3 tools/span_readable.py some/dir
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "custom_components" / "span_ebus"))
logging.disable(logging.INFO)

from span_client.backend import parse_sites  # noqa: E402
from span_client.cloud_traits import parse_trait_snapshot  # noqa: E402


def main() -> int:
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "span_dump")
    sites = parse_sites((folder / "sites.bin").read_bytes())
    out = []
    for n, site in enumerate(sites, 1):
        snap = folder / f"traits-site-{n}.bin"
        circuits = parse_trait_snapshot(snap.read_bytes()) if snap.exists() else {}
        rows = sorted(circuits.values(), key=lambda c: (min(c.spaces or (999,)), c.instance_id))
        print(f"\n== Site {n}: SPAN {site.model}  serial {site.serial}  ({len(rows)} circuits)")
        print(f"   {'spaces':<8} {'amps':>4}  {'relay':<6} {'id':>3}  name")
        for c in rows:
            spaces = ",".join(map(str, c.spaces)) or "-"
            relay = {True: "on", False: "OFF", None: "?"}[c.relay_closed]
            amps = c.breaker_amps or ""
            print(f"   {spaces:<8} {amps:>4}  {relay:<6} {c.instance_id:>3}  {c.label}")
        out.append(
            {
                "site": n,
                "site_id": site.site_id,
                "model": site.model,
                "model_code": site.model_code,
                "serial": site.serial,
                "hardware_ids": list(site.hardware_ids),
                "circuits": [
                    {
                        "id": c.instance_id,
                        "name": c.label,
                        "spaces": list(c.spaces),
                        "breaker_amps": c.breaker_amps,
                        "relay_on": c.relay_closed,
                    }
                    for c in rows
                ],
            }
        )
    target = folder / "readable.json"
    target.write_text(json.dumps(out, indent=2))
    print(f"\nwrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
