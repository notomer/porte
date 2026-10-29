"""Frame-path gap experiment.

Three arms render the same Metal frame with the same renderer source:

  native_arm64   the workload compiled for this machine
  rosetta_x86_64 the same workload compiled x86-64, run under Rosetta 2
  porte_pe       the CPU half as a Windows x86-64 PE executed by Porte, with
                 the draw submitted through the host stub into the same renderer

Three configurations of the same binaries:

  cpu_bound      many entities and 16 simulation substeps per frame
  mixed          few entities, one substep, a light fragment shader: GPU-bound
                 natively, CPU-bound once translated
  gpu_heavy      the same scene with a 1024-iteration fragment loop: GPU-bound
                 in every arm, so the CPU path is hidden

Each frame is attributed to four layers so a gap can be located rather than
guessed at:

  guest/sim      simulation work (native: `sim_ms`; Porte: frame minus stub)
  submit         the draw call (native: encode; Porte: time inside PresentFrame)
  wait           time blocked on the present ring
  gpu            GPU duration reported by Metal for the frame

A run is only reported if the entity state after the last frame hashes to the
same value in every arm, so a fast arm cannot be fast because it simulated less.

What this measures: the CPU translation/interpretation part of a frame, with the
GPU path held constant. What it does not measure: D3D -> Metal translation, Wine
API cost, or a real game. Those layers are not installed on this host.
"""
from __future__ import annotations

import json
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path

from .. import util

BUILD = util.BUILD / "frame"
CONFIGS = {
    "cpu_bound": {"entities": 65536, "substeps": 16, "work": 0,
                  "quad": 0.004, "frames": 60},
    "mixed": {"entities": 8192, "substeps": 1, "work": 64,
              "quad": 0.10, "frames": 120},
    "gpu_heavy": {"entities": 8192, "substeps": 1, "work": 1024,
                  "quad": 0.10, "frames": 90},
}
REPEATS = 3

# Fixed in fixtures/frame/app.c, frame_win.c and sim.c: the seed of the entity
# field. The state hash below is therefore the same across arms for a config.
ENTITY_BYTES = 32


def _stats(values: list[float]) -> dict:
    if not values:
        return {}
    ordered = sorted(values)
    n = len(ordered)
    return {
        "n": n,
        "median": statistics.median(ordered),
        "mean": statistics.fmean(ordered),
        "p99": ordered[min(n - 1, int(n * 0.99))],
        "min": ordered[0],
        "max": ordered[-1],
        "fps_median": 1000.0 / statistics.median(ordered) if statistics.median(ordered) else None,
        "fps_1pct_low": 1000.0 / ordered[min(n - 1, int(n * 0.99))] if ordered[-1] else None,
    }


def _run_native(binary: Path, cfg: dict, out: Path) -> dict:
    cmd = [str(binary), "--frames", str(cfg["frames"]),
           "--entities", str(cfg["entities"]),
           "--substeps", str(cfg["substeps"]),
           "--work", str(cfg["work"]), "--quad", str(cfg["quad"]),
           "--out", str(out)]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
    wall = time.perf_counter() - t0
    if proc.returncode != 0:
        raise RuntimeError(f"{binary.name} failed: {proc.stderr[-500:]}")
    raw = json.loads(out.read_text())
    meta = raw["meta"]
    frames = meta["frames"]
    warm = list(range(5, frames))
    wall_ms = [raw["sim_ms"][i] + raw["encode_ms"][i] + raw["wait_ms"][i]
               for i in warm]
    gpu_aligned = [raw["gpu_ms"][i] for i in warm]
    gpu = [g for g in gpu_aligned if g > 0]
    sustained = [max(w, g) for w, g in zip(wall_ms, gpu_aligned)]
    return {
        "meta": meta,
        "process_seconds": wall,
        "sim_ms": [raw["sim_ms"][i] for i in warm],
        "submit_ms": [raw["encode_ms"][i] for i in warm],
        "wait_ms": [raw["wait_ms"][i] for i in warm],
        "wall_ms": wall_ms,
        "gpu_ms": gpu,
        "startup_ms": meta.get("startup_ms"),
        "shader_ms": meta.get("shader_ms"),
        "final_hash": meta.get("final_hash"),
        "first_frame_ms": raw["sim_ms"][0] + raw["encode_ms"][0] + raw["wait_ms"][0],
        # CPU seconds spent inside the frame loop, /frames. A single-threaded
        # loop is ~1.0 core; the number is here so a "native baseline" cannot
        # hide work on another thread.
        "cpu_ms_per_frame": ((meta.get("cpu_seconds") or 0.0) * 1000.0
                             / max(1, frames)),
        "max_rss_kb": meta.get("max_rss_kb"),
        "sustained_ms": statistics.median(sustained),
        "sustained_p99": sorted(sustained)[int(len(sustained) * 0.99)],
    }


def _fnv1a(data: bytes) -> str:
    h = 0xcbf29ce484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def _run_porte(cfg: dict, repeats: int = 1,
               exe: str = "frame_win.exe") -> dict:
    from ..pe.gpu import GpuMachine
    from .renderer import FrameRenderer

    walls: list[float] = []
    sustained: list[float] = []
    sustained_medians: list[float] = []
    stubs: list[float] = []
    submits: list[float] = []
    waits: list[float] = []
    gpus: list[float] = []
    startups: list[float] = []
    process: list[float] = []
    hashes: list[str] = []
    dispatches: list[int] = []
    cpu_seconds: list[float] = []
    rss: list[int] = []
    for _ in range(repeats):
        renderer = FrameRenderer(quad=cfg["quad"], work=cfg["work"])
        t_build = time.perf_counter()
        machine = GpuMachine(BUILD / exe, renderer)
        build_ms = (time.perf_counter() - t_build) * 1000.0
        cpu0 = time.process_time()
        t0 = time.perf_counter()
        result = machine.run_entry([str(cfg["frames"]), str(cfg["entities"]),
                                    str(cfg["substeps"])])
        elapsed = time.perf_counter() - t0
        cpu_seconds.append(time.process_time() - cpu0)
        rss.append(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024)
        if result.exit_code != 0 or result.fault:
            raise RuntimeError(f"PE arm failed: {result.fault} exit={result.exit_code}")
        report = machine.frame_report(0)
        if machine.last_buffer is not None:
            addr, count = machine.last_buffer
            hashes.append(_fnv1a(machine.read(addr, count * ENTITY_BYTES)))
        warm = report["wall_ms"][5:]
        gpu_warm = report["gpu_all"][5:]
        walls.extend(warm)
        stubs.extend(report["stub_ms"][5:])
        submits.extend(report["submit_ms"][5:])
        waits.extend(report["wait_ms"][5:])
        gpus.extend(g for g in gpu_warm if g > 0)
        # A frame cannot present faster than its own GPU work completes, even
        # when the CPU-side interval is shorter.
        frame_sustained = [max(w, g) for w, g in zip(warm, gpu_warm)]
        sustained.extend(frame_sustained)
        sustained_medians.append(statistics.median(frame_sustained))
        startups.append(build_ms)
        process.append(elapsed)
        dispatches.append(machine.dispatches)
        renderer.close()
    # Guest time is everything the frame spent outside the host stub: the
    # emulated x86 simulation plus the import-dispatch hop back into Python.
    guest = [w - s for w, s in zip(walls, stubs)]
    return {
        "meta": {"frames": cfg["frames"], "entities": cfg["entities"],
                 "substeps": cfg["substeps"], "quad": cfg["quad"],
                 "work": cfg["work"], "arm": "porte_pe"},
        "process_seconds": statistics.median(process),
        "wall_ms": walls,
        "sim_ms": guest,
        "submit_ms": submits,
        "wait_ms": waits,
        "gpu_ms": gpus,
        "startup_ms": statistics.median(startups) if startups else None,
        "shader_ms": None,
        "final_hash": hashes[-1] if hashes else None,
        "dispatches": dispatches[-1] if dispatches else 0,
        "first_frame_ms": report["wall_ms"][0] if report.get("wall_ms") else None,
        # The host process is Python: its CPU time is the PE's guest time plus
        # the interpreter's own dispatch, which is labelled, not hidden.
        "cpu_ms_per_frame": statistics.median(cpu_seconds) * 1000.0 / max(1, cfg["frames"]),
        "max_rss_kb": statistics.median(rss) if rss else None,
        "sustained_ms": statistics.median(sustained),
        "sustained_p99": sorted(sustained)[int(len(sustained) * 0.99)],
        "sustained_repeats": sustained_medians,
    }


def _summarise(arm: dict) -> dict:
    wall = _stats(arm["wall_ms"])
    sim = _stats(arm["sim_ms"])
    submit = _stats(arm["submit_ms"])
    wait = _stats(arm["wait_ms"])
    gpu = _stats(arm["gpu_ms"])
    # A three-deep present ring lets the CPU run ahead of the GPU, so the CPU
    # frame cost alone understates a GPU-bound frame. Sustainable frame time is
    # the larger of the CPU-side interval and the GPU duration, per frame.
    sustained = arm["sustained_ms"]
    sustained_p99 = arm["sustained_p99"]
    breakdown = {
        "guest_ms": sim.get("median"),
        "submit_ms": submit.get("median"),
        "wait_ms": wait.get("median"),
        "gpu_ms": gpu.get("median"),
        "guest_share": (sim["median"] / wall["median"]) if sim and wall else None,
        "submit_share": (submit["median"] / wall["median"]) if submit and wall else None,
        "gpu_share": (gpu["median"] / wall["median"]) if gpu and wall else None,
    }
    return {
        "wall": wall,
        "sim": sim,
        "submit": submit,
        "wait": wait,
        "gpu": gpu,
        "sustained_ms": sustained,
        "sustained_p99": sustained_p99,
        "sustained_fps": (1000.0 / sustained) if sustained else None,
        "sustained_fps_1pct_low": (1000.0 / sustained_p99) if sustained_p99 else None,
        "breakdown": breakdown,
        "startup_ms": arm.get("startup_ms"),
        "first_frame_ms": arm.get("first_frame_ms"),
        "shader_ms": arm.get("shader_ms"),
        "process_seconds": arm.get("process_seconds"),
        "final_hash": arm.get("final_hash"),
        "cpu_ms_per_frame": arm.get("cpu_ms_per_frame"),
        "cpu_cores_used": ((arm.get("cpu_ms_per_frame") or 0.0)
                           / wall["median"]) if wall else None,
        "max_rss_kb": arm.get("max_rss_kb"),
        "sustained_repeats": arm.get("sustained_repeats"),
        "sustained_spread_pct": (
            (max(reps) - min(reps)) / sustained * 100.0
            if (reps := arm.get("sustained_repeats")) and sustained else None),
    }


def _pool(runs: list[dict]) -> dict:
    return {
        "wall_ms": [v for r in runs for v in r["wall_ms"]],
        "sim_ms": [v for r in runs for v in r["sim_ms"]],
        "submit_ms": [v for r in runs for v in r["submit_ms"]],
        "wait_ms": [v for r in runs for v in r["wait_ms"]],
        "gpu_ms": [v for r in runs for v in r["gpu_ms"]],
        "startup_ms": statistics.median([r["startup_ms"] for r in runs]),
        "shader_ms": statistics.median([r["shader_ms"] for r in runs]),
        "first_frame_ms": statistics.median([r["first_frame_ms"] for r in runs]),
        "process_seconds": statistics.median([r["process_seconds"] for r in runs]),
        "cpu_ms_per_frame": statistics.median([r["cpu_ms_per_frame"] for r in runs]),
        "max_rss_kb": runs[0].get("max_rss_kb"),
        "sustained_ms": statistics.median([r["sustained_ms"] for r in runs]),
        "sustained_p99": statistics.median([r["sustained_p99"] for r in runs]),
        "sustained_repeats": [r["sustained_ms"] for r in runs],
        "meta": runs[0]["meta"],
        "final_hash": runs[0]["final_hash"],
    }


def _host_cold_compile(nonce: str) -> float | None:
    """Compile the shader in a fresh host process (no in-process cache)."""
    import os

    env = dict(os.environ)
    env["PORTE_SHADER_NONCE"] = nonce
    root = Path(__file__).resolve().parents[2]
    code = ("from porte.frame.renderer import FrameRenderer;"
            "r=FrameRenderer(quad=0.004,work=0);print(r.compile_ms);r.close()")
    proc = subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.strip().splitlines()[-1])
    except ValueError:
        return None


def cold_shader(samples: int = 3) -> dict:
    """Force a shader-cache miss and measure compilation, both paths."""
    import os
    import uuid

    out = BUILD / "runs" / "cold_shader.json"
    native = []
    host = []
    for _ in range(samples):
        nonce = uuid.uuid4().hex
        env = dict(os.environ)
        env["PORTE_SHADER_NONCE"] = nonce
        proc = subprocess.run([str(BUILD / "frame_arm64"), "--frames", "2",
                               "--entities", "64", "--substeps", "1",
                               "--out", str(out)], env=env, capture_output=True,
                              text=True, timeout=120)
        if proc.returncode == 0:
            native.append(json.loads(out.read_text())["meta"]["shader_ms"])
        # A fresh process for each sample: Metal's in-process cache would
        # otherwise turn samples 2..n into cache hits and halve the median.
        host.append(_host_cold_compile(nonce))
    host = [h for h in host if h is not None]
    return {
        "native_arm64_ms": statistics.median(native) if native else None,
        "native_samples": native,
        "porte_host_ms": statistics.median(host) if host else None,
        "porte_samples": host,
        "note": "one shader, one pipeline, no disk cache hit",
    }


def scaling(repeats: int = 2) -> dict:
    """Does frame time scale with simulated work, in every arm?

    A constant per-entity cost is what makes the gap a throughput ratio instead
    of an artifact of one configuration: 4x the entities should cost 4x.
    """
    rows = []
    for entities in (16384, 65536):
        cfg = {"entities": entities, "substeps": 1, "work": 0,
               "quad": 0.004, "frames": 24}
        runs = [_run_native(BUILD / "frame_arm64", cfg,
                            BUILD / "runs" / f"scale_native_{entities}.json")
                for _ in range(repeats)]
        native = statistics.median(
            [statistics.median(r["sim_ms"]) for r in runs])
        porte_runs = [_run_porte(cfg) for _ in range(repeats)]
        porte = statistics.median(
            [statistics.median(r["sim_ms"]) for r in porte_runs])
        work = entities * cfg["substeps"]
        rows.append({
            "entities": entities, "frames": cfg["frames"],
            "native_sim_ms": native, "porte_guest_ms": porte,
            "native_ns_per_entity_step": native * 1e6 / work,
            "porte_ns_per_entity_step": porte * 1e6 / work,
            "ratio": porte / native if native else None,
        })
    small, large = rows[0], rows[1]
    return {
        "rows": rows,
        "work_factor": large["entities"] / small["entities"],
        "native_scaling": large["native_sim_ms"] / small["native_sim_ms"],
        "porte_scaling": large["porte_guest_ms"] / small["porte_guest_ms"],
    }


def instruction_profile(sizes: tuple[int, int] = (1024, 4096),
                        frames: int = 2,
                        exes: tuple[str, ...] = ("frame_win.exe",
                                                "frame_win_clang.exe")) -> dict:
    """Instructions retired per entity step, per guest image.

    Counted with a per-instruction hook at two small instance counts; the hook
    is far too expensive at full size, and differencing the two sizes cancels
    image start-up and the constant per-frame cost.
    """
    from unicorn import UC_HOOK_CODE

    from ..pe.gpu import GpuMachine
    from .renderer import FrameRenderer

    images: dict[str, dict] = {}
    for exe in exes:
        counts: dict[int, int] = {}
        for n in sizes:
            renderer = FrameRenderer(quad=0.004, work=0)
            machine = GpuMachine(BUILD / exe, renderer)
            total = [0]

            def hook(uc, address, size, user, total=total):
                total[0] += 1

            machine.uc.hook_add(UC_HOOK_CODE, hook)
            result = machine.run_entry([str(frames), str(n), "1"])
            renderer.close()
            if result.exit_code != 0 or result.fault:
                raise RuntimeError(f"instruction probe failed: {result.fault}")
            counts[n] = total[0]
        small, large = sizes
        images[exe] = {
            "sizes": {str(k): v for k, v in counts.items()},
            "instructions_per_entity_step": ((counts[large] - counts[small])
                                            / (frames * (large - small))),
        }
    return {"frames": frames, "images": images}


def run(repeats: int = REPEATS, with_scaling: bool = True) -> dict:
    outdir = util.ensure_dir(BUILD / "runs")
    report: dict = {"host": util.host_info(), "toolchain": util.toolchain_info(),
                    "configs": CONFIGS, "repeats": repeats, "arms": {},
                    "correctness": {}}
    for name, cfg in CONFIGS.items():
        entry = {}
        for arm, binary in (("native_arm64", BUILD / "frame_arm64"),
                            ("rosetta_x86_64", BUILD / "frame_x86_64")):
            runs = [_run_native(binary, cfg, outdir / f"{name}_{arm}_{i}.json")
                    for i in range(repeats)]
            entry[arm] = _summarise(_pool(runs))
        for arm, exe in (("porte_pe", "frame_win.exe"),
                         ("porte_pe_clang", "frame_win_clang.exe")):
            # The emulated CPU-bound arm costs seconds per repeat; two is enough
            # to expose spread without making the experiment a coffee break.
            reps = 2 if name == "cpu_bound" else repeats
            entry[arm] = _summarise(_run_porte(cfg, repeats=reps, exe=exe))
        native = entry["native_arm64"]["wall"]["median"]
        native_sustained = entry["native_arm64"]["sustained_ms"]
        for arm in ("rosetta_x86_64", "porte_pe", "porte_pe_clang"):
            wall = entry[arm]["wall"]["median"]
            sustained = entry[arm]["sustained_ms"]
            entry[arm]["cpu_gap_vs_native_pct"] = (wall - native) / native * 100.0
            entry[arm]["gap_vs_native_pct"] = (
                (sustained - native_sustained) / native_sustained * 100.0)
            entry[arm]["fps_ratio_vs_native"] = (native_sustained / sustained
                                                 if sustained else None)
            entry[arm]["p99_gap_vs_native_pct"] = (
                entry[arm]["wall"]["p99"] - entry["native_arm64"]["wall"]["p99"]
            ) / entry["native_arm64"]["wall"]["p99"] * 100.0
        entry["native_arm64"]["gap_vs_native_pct"] = 0.0
        entry["native_arm64"]["cpu_gap_vs_native_pct"] = 0.0
        entry["native_arm64"]["fps_ratio_vs_native"] = 1.0
        work = cfg["entities"] * cfg["substeps"]
        for arm in entry:
            entry[arm]["ns_per_entity_step"] = (
                entry[arm]["sim"]["median"] * 1e6 / work)
        hashes = {arm: entry[arm].get("final_hash") for arm in entry}
        entry["porte_pe"]["guest_code"] = "mingw-gcc x86-64, scalar"
        entry["porte_pe_clang"]["guest_code"] = "clang x86-64, auto-vectorized"
        report["correctness"][name] = {
            "hashes": hashes,
            "match": len({h for h in hashes.values() if h}) == 1
                     and all(hashes.values()),
        }
        report["arms"][name] = entry
    report["shader_cold"] = cold_shader()
    if with_scaling:
        report["scaling"] = scaling()
    emu = instruction_profile()
    cfg = CONFIGS["cpu_bound"]
    for exe, arm in (("frame_win.exe", "porte_pe"),
                     ("frame_win_clang.exe", "porte_pe_clang")):
        prof = emu["images"][exe]
        per_frame = (prof["instructions_per_entity_step"] * cfg["entities"]
                     * cfg["substeps"])
        guest = report["arms"]["cpu_bound"][arm]["sim"]["median"]
        prof["cpu_bound"] = {
            "instructions_per_frame": per_frame,
            "guest_ms": guest,
            "ns_per_instruction": guest * 1e6 / per_frame if per_frame else None,
            "mips": per_frame / guest / 1000.0 if guest else None,
        }
    report["emulation"] = emu
    report["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    util.write_json(util.RESULTS / "frame_baseline.json", report)
    return report


def table(report: dict) -> str:
    lines = []
    header = (f"{'config':10s} {'arm':16s} {'cpu ms':>8s} {'frame ms':>9s} "
              f"{'fps':>8s} {'1% low':>8s} {'guest':>8s} {'submit':>7s} "
              f"{'gpu':>7s} {'gap':>9s}")
    lines.append(header)
    for cfg, arms in report["arms"].items():
        for arm, s in arms.items():
            b = s["breakdown"]
            gpu = b["gpu_ms"] if b["gpu_ms"] is not None else float("nan")
            lines.append(
                f"{cfg:10s} {arm:16s} {s['wall']['median']:8.3f} "
                f"{s['sustained_ms']:9.3f} {s['sustained_fps']:8.1f} "
                f"{s['sustained_fps_1pct_low']:8.1f} "
                f"{b['guest_ms']:8.3f} {b['submit_ms']:7.3f} {gpu:7.3f} "
                f"{s.get('gap_vs_native_pct', 0.0):+8.1f}%")
        ok = report["correctness"][cfg]["match"]
        lines.append(f"{'':10s} state hash match: {ok}")
    return "\n".join(lines)


def breakdown(report: dict) -> str:
    lines = []
    for cfg, arms in report["arms"].items():
        native = arms["native_arm64"]["wall"]["median"]
        lines.append(f"== {cfg} (native {native:.3f} ms/frame)")
        for arm, s in arms.items():
            b = s["breakdown"]
            excess = s["wall"]["median"] - native
            lines.append(
                f"  {arm:16s} guest {b['guest_ms']:8.3f} ms  "
                f"submit {b['submit_ms']:7.3f} ms  "
                f"gpu {b['gpu_ms'] if b['gpu_ms'] is not None else float('nan'):7.3f} ms  "
                f"excess over native {excess:+8.3f} ms")
    return "\n".join(lines)


if __name__ == "__main__":
    report = run()
    print(table(report))
    print()
    print(breakdown(report))
