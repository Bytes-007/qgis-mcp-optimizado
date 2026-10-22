"""Reproducible side-by-side benchmark: original vs optimised plugin.

Usage: with both QGIS instances listening (original on 9876, optimised on 9877):
    uv run --no-sync benchmarks/context_bench.py

Prints bytes of each JSON response per port so the comparison in
docs/comparativa-contexto-original-vs-optimizado.md can be re-measured.
"""

import json
import sys
import time

from qgis_mcp.client import QgisMCPClient

PORTS = {"original": 9876, "optimizado": 9877}
FIELDS = ["region", "area_ha"]


def bench_one(port: int):
    c = QgisMCPClient(port=port)
    c.connect()
    rows = []

    def call(name, cmd, params=None):
        t0 = time.perf_counter()
        r = c.send_command(cmd, params or {})
        dt = time.perf_counter() - t0
        raw = json.dumps(r.get("result"))
        rows.append((name, r.get("status"), round(dt, 3), len(raw)))

    call("get_layers", "get_layers", {"limit": 3})
    fid = (c.send_command("get_layers", {"limit": 3}).get("result", {}).get("layers") or [{}])[0].get("id")
    if fid:
        call("feat_default", "get_layer_features", {"layer_id": fid, "limit": 3})
        call("feat_fields", "get_layer_features", {"layer_id": fid, "limit": 3, "fields": FIELDS})
    call("algs", "list_processing_algorithms")
    call("code_big", "execute_code", {"code": 'print("x" * 1000000)'})
    call("code_err", "execute_code", {"code": 'raise ValueError("boom")'})
    c.disconnect()
    return rows


def main():
    if len(sys.argv) > 1:
        PORTS["optimizado"] = int(sys.argv[1])
    for label, port in PORTS.items():
        print(f"--- {label} (:{port}) ---")
        try:
            for name, status, dt, size in bench_one(port):
                print(f"{name:12s} {status:7s} {dt:6.3f}s  {size:,} bytes")
        except Exception as e:
            print("ERR", repr(e))


if __name__ == "__main__":
    main()
