"""Reproducible measurement across the three arms.

Every sample is a fresh process. Repeats are interleaved round-robin so a
thermal excursion or a background task does not systematically favour one arm.
"""
from __future__ import annotations

import statistics
from pathlib import Path

from . import build, profile, util


def arms_for(spec_dylib: Path) -> list[dict]:
    return [
        {"name": "baseline-x86_64", "arch": "x86_64",
         "kernel": build.paths_for("x86_64")["generic"],
         "note": "translated baseline (Rosetta)"},
        {"name": "native-generic", "arch": "arm64",
         "kernel": build.paths_for("arm64")["generic"],
         "note": "generic native build, same source (libc qsort)"},
        {"name": "native-generic-inline", "arch": "arm64",
         "kernel": build.paths_for("arm64")["inline"],
         "note": "control: in-module sort, no libc comparator calls"},
        {"name": "nativeized", "arch": "arm64", "kernel": Path(spec_dylib),
         "note": "Porte specialization + guard + fallback"},
    ]


def _stats(values: list[float]) -> dict:
    values = sorted(values)
    n = len(values)

    def q(p: float) -> float:
        if n == 1:
            return values[0]
        pos = p * (n - 1)
        lo = int(pos)
        hi = min(lo + 1, n - 1)
        frac = pos - lo
        return values[lo] * (1 - frac) + values[hi] * frac

    return {"median": statistics.median(values), "p10": q(0.10),
            "p90": q(0.90), "min": values[0], "max": values[-1], "n": n}


def bench_workload(workload: str, spec_dylib: Path, repeats: int = 7,
                   warmup: int = 1) -> dict:
    arms = arms_for(spec_dylib)
    raw = {a["name"]: [] for a in arms}
    for _ in range(warmup):
        for a in arms:
            profile.run_app(a["arch"], a["kernel"], workload, repeat_warm=0)
    for _ in range(repeats):
        for a in arms:
            r = profile.run_app(a["arch"], a["kernel"], workload, repeat_warm=0)
            raw[a["name"]].append({
                "kernel_ns": r.kernel_ns, "wall_ns": r.wall_ns,
                "maxrss_bytes": r.maxrss, "user_s": r.user_s, "sys_s": r.sys_s,
                "transitions": r.transitions, "entries": r.entries,
                "fast": r.fast, "fallback": r.fallback, "impl": r.impl,
            })

    summary = {}
    for a in arms:
        rows = raw[a["name"]]
        summary[a["name"]] = {
            "note": a["note"], "arch": a["arch"],
            "kernel_ns": _stats([r["kernel_ns"] for r in rows]),
            "wall_ns": _stats([r["wall_ns"] for r in rows]),
            "maxrss_bytes": _stats([r["maxrss_bytes"] for r in rows]),
            "cpu_s": _stats([r["user_s"] + r["sys_s"] for r in rows]),
            "transitions": rows[0]["transitions"],
            "entries": rows[0]["entries"],
            "fast": rows[0]["fast"], "fallback": rows[0]["fallback"],
            "impl": rows[0]["impl"],
        }

    def ratio(a: str, b: str) -> float:
        return summary[b]["kernel_ns"]["median"] / summary[a]["kernel_ns"]["median"]

    return {
        "workload": workload,
        "repeats": repeats,
        "summary": summary,
        "speedup_vs_native_generic": ratio("nativeized", "native-generic"),
        "speedup_vs_native_inline": ratio("nativeized", "native-generic-inline"),
        "speedup_vs_translated_baseline": ratio("nativeized", "baseline-x86_64"),
        "raw": raw,
    }


def run_bench(spec_dylib: Path, workloads: list[str], repeats: int = 7) -> dict:
    return {
        "schema": 1,
        "host": util.host_info(),
        "toolchain": util.toolchain_info(),
        "candidate_sha256": util.sha256_file(Path(spec_dylib)),
        "workloads": {w: bench_workload(w, spec_dylib, repeats=repeats)
                      for w in workloads},
    }


def format_table(result: dict) -> str:
    lines = [f"## {result['workload']}", "",
             "| arm | kernel median | p10 | p90 | wall median | max RSS |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for name, s in result["summary"].items():
        k = s["kernel_ns"]
        lines.append(
            f"| {name} | {util.human_ns(k['median'])} | "
            f"{util.human_ns(k['p10'])} | {util.human_ns(k['p90'])} | "
            f"{util.human_ns(s['wall_ns']['median'])} | "
            f"{s['maxrss_bytes']['median'] / (1 << 20):.1f} MiB |")
    lines += ["",
              f"speedup vs native generic: {result['speedup_vs_native_generic']:.2f}x; "
              f"vs translated baseline: {result['speedup_vs_translated_baseline']:.2f}x",
              ""]
    return "\n".join(lines)
