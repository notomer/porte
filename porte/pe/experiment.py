"""Run the opaque-PE experiment and write results/opaque_pe.json.

Each program is treated the same way: a path, a workload argument, and a
generic verification corpus. Nothing here knows what the program computes.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from .. import util
from . import pipeline

PROGRAMS = [
    ("blob", "65536"),
    ("lookup", "65536"),
    ("sieve", "1000000"),
    ("fault", "16"),
    ("crc", "262144"),
]
FIXTURES = util.ROOT / "fixtures" / "opaque" / "out"
VERIFY_CORPUS = pipeline.CORPUS


def startup_cost(pe_path: Path, repeats: int = 5) -> dict:
    from .machine import Machine

    samples = []
    for _ in range(repeats):
        t0 = time.perf_counter_ns()
        Machine(Path(pe_path))
        samples.append((time.perf_counter_ns() - t0) / 1e6)
    return {"median_ms": sorted(samples)[len(samples) // 2], "samples": samples}


def native_reference(sizes: list[str], repeats: int = 7) -> dict:
    script = util.ROOT / "fixtures" / "opaque" / "native_bench.py"
    if not script.exists():
        return {}
    proc = subprocess.run([str(util.ROOT / ".venv" / "bin" / "python"), str(script),
                           *sizes, "--repeats", str(repeats)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return {"error": proc.stderr[-400:]}
    return json.loads(proc.stdout)


def run(repeats: int = 7) -> dict:
    report: dict = {
        "host": util.host_info(),
        "toolchain": util.toolchain_info(),
        "corpus": VERIFY_CORPUS,
        "programs": {},
    }
    for name, workload in PROGRAMS:
        pe_path = FIXTURES / f"{name}.exe"
        if not pe_path.exists():
            report["programs"][name] = {"error": "fixture missing"}
            continue
        entry: dict = {
            "argv": workload,
            "image": pipeline.describe(pe_path),
            "startup": startup_cost(pe_path),
        }
        t0 = time.perf_counter()
        result = pipeline.optimize(pe_path, profile_args=[workload],
                                   corpus=VERIFY_CORPUS, force=True)
        entry["cold_seconds"] = time.perf_counter() - t0
        if not result.get("ok"):
            entry["optimization"] = {
                "admitted": False,
                "reason": result.get("reason"),
                "first_failure": result.get("first_failure"),
                "artifact": {k: v for k, v in (result.get("artifact") or {}).items()
                             if k in ("head", "pinned_sites", "instructions")},
            }
            # Even a rejected program must still run: baseline only.
            entry["baseline"] = pipeline.run_baseline(pe_path, [workload]).to_dict()
            report["programs"][name] = entry
            continue

        manifest = result["manifest"]
        artifact = manifest["artifact"]
        entry["optimization"] = {
            "admitted": True,
            "cache_key": manifest["key"],
            "compile_seconds": artifact.get("compile_seconds"),
            "candidate": artifact.get("candidate"),
            "regions": artifact.get("regions"),
            "pinned_sites": artifact.get("pinned_sites"),
            "memory_ops": artifact.get("memory_ops"),
            "assumptions": [
                "the traced loop is entered at its header and leaves only via a "
                "modelled exit edge",
                "every memory access inside the trace stays inside the region the "
                "profile pinned for that site; otherwise the trace bails at that "
                "instruction",
                "the image's region layout is the one recorded in the artifact; a "
                "different layout demotes the whole run to interpretation",
            ],
            "verification": {
                "cases": len(manifest["verification"]["cases"]),
                "passed": manifest["verification"]["passed"],
                "corpus": manifest["verification"]["cases"],
            },
        }
        entry["bench"] = pipeline.bench(pe_path, manifest, [workload], repeats=repeats)
        reuse = pipeline.load_and_check(pe_path)
        entry["cache"] = {
            "hit": reuse is not None,
            "smoke_cases": pipeline.SMOKE,
            "check_seconds": reuse["check_seconds"] if reuse else None,
            "smoke_passed": reuse["smoke"]["passed"] if reuse else None,
        }
        entry["selftest"] = pipeline.selftest(pe_path, corpus=[workload, "64"],
                                              profile_args=[workload])
        entry["rollback"] = pipeline.rollback_check(pe_path, artifact, workload)
        entry["invalidation"] = pipeline.invalidation_check(pe_path)
        entry["peak_rss_mb"] = pipeline.peak_rss_mb()
        report["programs"][name] = entry

    sizes = [w for _, w in PROGRAMS]
    report["native_reference"] = native_reference(sorted(set(sizes)), repeats)
    report["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    util.write_json(util.RESULTS / "opaque_pe.json", report)
    return report


def summarise(report: dict) -> str:
    lines = []
    for name, entry in report["programs"].items():
        if "bench" not in entry:
            lines.append(f"{name}: no specialization admitted")
            continue
        arms = entry["bench"]["arms_ms"]
        base = arms["baseline"]["median"]
        spec = arms["specialized"]["median"]
        trans = arms["translated"]["median"]
        lines.append(
            f"{name}: baseline {base:.3f} ms, translated {trans:.3f} ms, "
            f"specialized {spec:.3f} ms, speedup {base / spec:.2f}x")
    return "\n".join(lines)


if __name__ == "__main__":
    report = run()
    print(summarise(report))
