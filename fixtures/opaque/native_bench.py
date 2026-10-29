#!/usr/bin/env python3
"""Fixture-side native ARM64 reference timing.

The reference implementations were built by gen.py from the same data tables
as the Windows binaries. They compute the same algorithm natively on this
machine. That is the "what if the work were already native" number; it is a
reference, not Porte's baseline.

Usage: python3 fixtures/opaque/native_bench.py 65536 1000000 --repeats 7
"""
from __future__ import annotations

import argparse
import ctypes
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
REF = ROOT / "fixtures" / "opaque" / "reference.json"


def median(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sizes", nargs="+", type=int)
    ap.add_argument("--repeats", type=int, default=7)
    args = ap.parse_args(argv)
    ref = json.loads(REF.read_text())
    out: dict[str, dict] = {}
    for name, info in ref.items():
        if "native" not in info:
            continue
        lib = ctypes.CDLL(str(ROOT / info["native"]))
        fn = getattr(lib, f"ref_{name}")
        fn.argtypes = [ctypes.c_uint64]
        fn.restype = ctypes.c_uint64
        for n in args.sizes:
            fn(n)  # warm-up
            samples = []
            for _ in range(args.repeats):
                t0 = time.perf_counter_ns()
                value = fn(n)
                samples.append((time.perf_counter_ns() - t0) / 1e6)
            out.setdefault(name, {})[str(n)] = {
                "median_ms": median(samples),
                "min_ms": min(samples),
                "samples": samples,
                "value": value,
            }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
