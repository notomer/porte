"""Component A: execution profiler.

Runs the baseline (x86-64 module under Rosetta, or the arm64 generic module)
with the probe enabled, then reduces raw samples/boundaries/facts into a
profile document keyed by executable hash, module hash, host and runtime.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import build, macho, util

# --- workload catalogue (mirrors fixtures/app/app.c) ----------------------

WORKLOADS = {
    "rank_small_range": {"kernel": "rank", "n": 2_000_000, "q": 200_000,
                         "purpose": "primary compute workload; values in [0,4095]"},
    "rank_wide": {"kernel": "rank", "n": 2_000_000, "q": 200_000,
                  "purpose": "guard-violation: full int32 range"},
    "rank_unaligned": {"kernel": "rank", "n": 2_000_000, "q": 200_000,
                       "purpose": "guard-violation: src misaligned"},
    "rank_tiny": {"kernel": "rank", "n": 16, "q": 8,
                  "purpose": "guard-violation: below fast-path size floor"},
    "audit_basic": {"kernel": "audit", "n": 2_000_000,
                    "purpose": "host-boundary workload"},
    "audit_interleaved": {"kernel": "audit", "n": 2_000_000,
                          "purpose": "guard-violation: observer interleaved"},
}

PRIMARY = {"rank": "rank_small_range", "audit": "audit_basic"}

APPLICABLE = {
    "rank_small_range": True,
    "rank_wide": False,
    "rank_unaligned": False,
    "rank_tiny": False,
    "audit_basic": True,
    "audit_interleaved": False,
}

_EXTERNAL_NAMES = ("qsort", "libsystem", "libSystem", "dyld", "libc++",
                   "libobjc", "Rosetta", "runtime")


@dataclass
class RunResult:
    arch: str
    workload: str
    impl: str
    ret: int
    out_digest: str
    kernel_ns: int
    transitions: int
    entries: int
    fast: int
    fallback: int
    wall_ns: int
    maxrss: int
    user_s: float
    sys_s: float


def run_app(arch: str, kernel: Path, workload: str, ledger: Path | None = None,
            probe: Path | None = None, extra_env: dict | None = None,
            repeat_warm: int = 0) -> RunResult:
    """Run one arm and return its self-reported + measured numbers."""
    import json
    import os

    p = build.paths_for(arch)
    cmd = [str(p["app"]), "--kernel", str(kernel), "--workload", workload]
    if ledger:
        cmd += ["--ledger", str(ledger)]
    env = os.environ.copy()
    if probe:
        env["PORTE_PROBE"] = str(probe)
    if extra_env:
        env.update(extra_env)

    # one untimed warm-up keeps page-cache / dyld effects out of the sample
    for _ in range(repeat_warm):
        util.spawn_measure(cmd, env=env)

    res = util.spawn_measure(cmd, env=env)
    if res["exit"] != 0:
        raise RuntimeError(f"app failed: {res['output'].strip()}")
    payload = json.loads(res["output"].strip().splitlines()[-1])
    return RunResult(
        arch=arch, workload=workload, impl=payload["impl"],
        ret=int(payload["ret"]), out_digest=payload["out_digest"],
        kernel_ns=int(payload["kernel_ns"]),
        transitions=int(payload["transitions"]), entries=int(payload["entries"]),
        fast=int(payload["fast"]), fallback=int(payload["fallback"]),
        wall_ns=res["wall_ns"], maxrss=res["maxrss_bytes"],
        user_s=res["user_s"], sys_s=res["sys_s"])


def _parse_raw(path: Path) -> dict:
    samples: list[int] = []
    boundaries: list[dict] = []
    facts: list[dict] = []
    images: list[dict] = []
    module = None
    for line in path.read_text().splitlines():
        parts = line.split()
        if not parts:
            continue
        tag = parts[0]
        if tag == "sample":
            samples.append(int(parts[1], 16))
        elif tag == "boundary":
            boundaries.append({
                "kernel_id": int(parts[1]), "n": int(parts[2]),
                "q": int(parts[3]), "flags": int(parts[4]),
                "p": int(parts[5], 16), "src": int(parts[6], 16),
                "dst": int(parts[7], 16), "aux": int(parts[8], 16),
                "t_ns": int(parts[9]),
            })
        elif tag == "fact":
            facts.append({"kernel_id": int(parts[1]), "min": int(parts[2]),
                          "max": int(parts[3]), "n": int(parts[4])})
        elif tag == "img":
            images.append({"slide": int(parts[1], 16),
                           "path": " ".join(parts[2:])})
        elif tag == "module":
            module = {"tag": parts[1], "slide": int(parts[2], 16),
                      "path": " ".join(parts[3:])}
    return {"samples": samples, "boundaries": boundaries, "facts": facts,
            "images": images, "module": module}


def _attribute(samples: list[int], images: list[dict]) -> tuple[list[dict], dict]:
    """Map absolute PCs to (image, function) using each image's text range."""
    ranges = []
    for img in images:
        path = Path(img["path"])
        if not path.exists():
            continue
        vmaddr, vmsize = macho.text_segment(path)
        if not vmsize:
            continue
        base = vmaddr + img["slide"]
        ranges.append({"path": path, "slide": img["slide"], "base": base,
                       "end": base + vmsize, "funcs": {}})
    ranges.sort(key=lambda r: r["base"])

    by_image: dict[int, list[tuple[int, int]]] = {}
    unknown = 0
    for pc in samples:
        hit = None
        for idx, r in enumerate(ranges):
            if r["base"] <= pc < r["end"]:
                hit = idx
                break
        if hit is None:
            unknown += 1
            continue
        by_image.setdefault(hit, []).append((pc, pc - ranges[hit]["slide"]))

    resolved = []
    for idx, items in by_image.items():
        img = ranges[idx]
        addrs = [static for _, static in items]
        names = macho.symbolize(img["path"], addrs)
        for (pc, static), name in zip(items, names):
            resolved.append({"pc": pc, "image": img["path"].name,
                             "path": str(img["path"]), "function": _base_name(name),
                             "static": static, "slide": img["slide"]})
    return resolved, {"unmapped": unknown, "images": len(ranges)}


def _base_name(name: str) -> str:
    """atos returns 'sym (in image) + offset'; keep the symbol."""
    name = name.split(" (in ")[0]
    if " + " in name:
        name = name.split(" + ")[0]
    return name


def _hot_functions(resolved: list[dict]) -> list[dict]:
    counts: dict[tuple[str, str], int] = {}
    for r in resolved:
        key = (r["image"], r["function"])
        counts[key] = counts.get(key, 0) + 1
    total = sum(counts.values()) or 1
    out = [{"image": k[0], "function": k[1], "samples": v,
            "share": v / total} for k, v in counts.items()]
    out.sort(key=lambda x: -x["samples"])
    return out


def _hot_blocks(resolved: list[dict], module_path: Path) -> list[dict]:
    blks = macho.blocks(module_path)
    if not blks:
        return []
    starts = [b.start for b in blks]
    import bisect
    counts: dict[int, int] = {}
    for r in resolved:
        if Path(r["path"]).resolve() != module_path.resolve():
            continue
        a = r["static"]
        i = bisect.bisect_right(starts, a) - 1
        if i >= 0 and blks[i].start <= a <= blks[i].end:
            counts[blks[i].start] = counts.get(blks[i].start, 0) + 1
    out = []
    for b in blks:
        c = counts.get(b.start, 0)
        if not c:
            continue
        out.append({"start": hex(b.start), "function": b.func, "samples": c,
                    "instructions": b.ninstr, "ends_indirect": b.ends_indirect})
    out.sort(key=lambda x: -x["samples"])
    return out[:24]


def profile_workload(workload: str, arch: str = "x86_64",
                     kernel: Path | None = None) -> dict:
    """Profile one workload and return the profile document."""
    p = build.paths_for(arch)
    kernel = kernel or p["generic_profile"]
    raw_dir = util.ensure_dir(util.BUILD / "profiles")
    # Interleaved repeats keep frequency drift from being charged to one arm.
    # Every profiled run keeps the probe enabled so the median really is the
    # cost of profiling, not one profiled run diluted by two plain ones.
    profiled, plain = [], []
    raws = []
    for i in range(3):
        raws.append(raw_dir / f"raw_{arch}_{workload}_{i}.txt")
        profiled.append(run_app(arch, kernel, workload, probe=raws[i],
                                repeat_warm=0))
        plain.append(run_app(arch, p["generic"], workload, repeat_warm=0))
    run = profiled[0]
    raw = raws[0]
    plain_ns = sorted(x.kernel_ns for x in plain)[len(plain) // 2]
    prof_ns = sorted(x.kernel_ns for x in profiled)[len(profiled) // 2]
    raw_data = _parse_raw(raw)

    resolved, mapping = _attribute(raw_data["samples"], raw_data["images"])
    module_path = kernel
    blks = macho.blocks(module_path)
    hits = _hot_blocks(resolved, module_path)
    inside_module = sum(1 for r in resolved
                        if Path(r["path"]).resolve() == module_path.resolve())
    kernel_s = prof_ns / 1e9
    fact = raw_data["facts"][0] if raw_data["facts"] else None
    b = raw_data["boundaries"][0] if raw_data["boundaries"] else None
    facts_by_kernel = {}
    if fact:
        facts_by_kernel[workload.split("_")[0]] = {
            "min": fact["min"], "max": fact["max"],
            "span": fact["max"] - fact["min"] + 1,
            "observations": len(raw_data["facts"]), "n": fact["n"],
        }

    profile = {
        "schema": 1,
        "workload": workload,
        "runtime": {"name": "rosetta" if arch == "x86_64" else "native",
                    "arch": arch},
        "module": {"path": str(module_path),
                   "sha256": util.sha256_file(module_path),
                   "arch": macho.arch_of(module_path),
                   "blocks": len(blks),
                   "instructions": sum(b.ninstr for b in blks)},
        "application": {"sha256": util.sha256_file(p["app"]),
                        "impl": run.impl},
        "host": util.host_info(),
        "toolchain": util.toolchain_info(),
        "sampling": {
            "requested_hz": 4000,
            "samples": len(raw_data["samples"]),
            "effective_hz": round(len(raw_data["samples"]) / kernel_s, 1) if kernel_s else 0,
            "inside_module": inside_module,
            "resolved_other_images": len(resolved) - inside_module,
            "unmapped": mapping["unmapped"],
            "kernel_ns": run.kernel_ns,
            "kernel_ns_profiled_median": prof_ns,
            "kernel_ns_plain": plain_ns,
            "instrumentation_overhead_pct": round(
                (prof_ns / plain_ns - 1) * 100, 2) if plain_ns else 0,
            "repeats": 3,
        },
        "boundary": {
            "calls": len(raw_data["boundaries"]),
            "n": b["n"] if b else None, "q": b["q"] if b else None,
            "flags": b["flags"] if b else None,
            "params": hex(b["p"]) if b else None,
            "src": hex(b["src"]) if b else None,
            "dst": hex(b["dst"]) if b else None,
            "alias_possible": _alias_possible(b) if b else None,
        },
        "facts": facts_by_kernel,
        "hottest_functions": _hot_functions(resolved)[:12],
        "hottest_blocks": hits,
        "transition_accounting": {
            "host_transitions": run.transitions,
            "host_entries": run.entries,
            "entries_per_transition": (run.entries / run.transitions)
            if run.transitions else None,
        },
        "raw_probe": str(raw),
    }
    return profile


def _alias_possible(b: dict | None) -> bool | None:
    if not b:
        return None
    n = b["n"]
    p_lo, p_hi = b["p"], b["p"] + 4
    src_lo, src_hi = b["src"], b["src"] + 4 * n
    dst_lo, dst_hi = b["dst"], b["dst"] + 4 * n
    return not (p_hi <= src_lo or p_lo >= src_hi) or not (p_hi <= dst_lo or p_lo >= dst_hi)


def measure_cold_start(arch: str = "x86_64", workload: str = "rank_small_range",
                       copies: int = 2) -> dict:
    """Estimate first-run translation cost with a fresh copy of the images.

    Rosetta's cache policy is opaque, so this is an indirect measurement: a
    byte-identical copy at a new path is executed once (cold) and again
    (warm). The native arm gets the same treatment as a control, where the
    only cold cost is dyld binding.
    """
    import shutil
    import tempfile

    p = build.paths_for(arch)
    cold_wall, warm_wall, cold_kernel = [], [], []
    for _ in range(copies):
        d = Path(tempfile.mkdtemp(prefix=f"porte-cold-{arch}-"))
        app = d / "app"
        kernel = d / "kernel.dylib"
        shutil.copy2(p["app"], app)
        shutil.copy2(p["generic"], kernel)
        for key in ("probe", "host"):
            shutil.copy2(p[key], d / p[key].name)

        def rewrite(binary: Path) -> None:
            for old in (p["probe"], p["host"], p["generic"]):
                util.run(["install_name_tool", "-change", str(old),
                          str(d / old.name), str(binary)], check=False)

        rewrite(app)
        rewrite(kernel)
        cmd = [app, "--kernel", kernel, "--workload", workload]
        first = util.spawn_measure(cmd)
        second = util.spawn_measure(cmd)
        cold_wall.append(first["wall_ns"])
        warm_wall.append(second["wall_ns"])
        cold_kernel.append(json.loads(first["output"].strip().splitlines()[-1])["kernel_ns"])
        shutil.rmtree(d, ignore_errors=True)

    def med(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2]

    return {
        "arch": arch, "workload": workload, "copies": copies,
        "cold_wall_ns": med(cold_wall), "warm_wall_ns": med(warm_wall),
        "first_run_kernel_ns": med(cold_kernel),
        "cold_start_overhead_ns": med(cold_wall) - med(warm_wall),
    }


def profile_all(arch: str = "x86_64") -> dict[str, dict]:
    out = {}
    for name, meta in WORKLOADS.items():
        if meta["kernel"] not in ("rank", "audit"):
            continue
        out[name] = profile_workload(name, arch=arch)
    return out
