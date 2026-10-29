"""Measure the compatibility stack, one layer at a time.

The same arithmetic exists in three arms (`fixtures/win32/bench.c` is compiled
for Windows x86-64, macOS x86-64 and macOS arm64), which separates the layers
instead of producing a single flattering number:

* translation  — macOS x86-64 (Rosetta) against native arm64
* runtime      — the Windows PE inside the compatibility runtime against the
                 same x86-64 code as a plain macOS process
* startup      — process spawn to a visible window, measured from outside
* memory       — peak resident size of the guest process tree

What this cannot tell you: whether a *specific* application feels fast. A
CPU-bound loop is not a game, a browser or a word processor. The report says
what it measured and refuses to extrapolate.
"""
from __future__ import annotations

import json
import statistics
import subprocess
import threading
import time
from pathlib import Path

from .. import util
from . import wine as wine_backend
from . import verify as verify_mod

ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = ROOT / "build" / "win32"
WIN_BENCH = BENCH_DIR / "porte_bench.exe"
ARM64_BENCH = BENCH_DIR / "bench_arm64"
X86_BENCH = BENCH_DIR / "bench_x86_64"
GUI_EXE = BENCH_DIR / "porte_gui.exe"
DEFAULT_ITERATIONS = 20000000


def _ready() -> bool:
    return all(path.exists() for path in (WIN_BENCH, ARM64_BENCH, X86_BENCH))


def _parse(output: str) -> dict:
    for token in output.split():
        if token.startswith("ms="):
            return {"ms": float(token[3:])}
    raise ValueError(f"benchmark produced no timing: {output!r}")


def _tree_rss(needle: str) -> int:
    """Sum resident bytes of processes whose command mentions `needle`."""
    proc = subprocess.run(["ps", "-axo", "rss=,command="], capture_output=True,
                          text=True)
    total = 0
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        size_text, _, command = line.partition(" ")
        if needle not in command or "ps -axo" in command:
            continue
        try:
            total += int(size_text) * 1024
        except ValueError:
            continue
    return total


def _run_arm(command: list[str], *, env: dict | None = None,
             marker: str | None = None) -> dict:
    """Run one arm, sampling memory, and return timing plus peak RSS."""
    peak = 0
    stop = threading.Event()

    def sample() -> None:
        nonlocal peak
        while not stop.is_set():
            if marker:
                peak = max(peak, _tree_rss(marker))
            time.sleep(0.05)

    thread = threading.Thread(target=sample, daemon=True)
    started = time.perf_counter()
    thread.start()
    try:
        if env is None:
            proc = subprocess.run(command, capture_output=True, text=True,
                                  timeout=900)
            result = {"returncode": proc.returncode, "stdout": proc.stdout,
                      "stderr": proc.stderr}
        else:
            result = wine_backend.run_captured(command, env=env, timeout=900)
    finally:
        stop.set()
        thread.join(timeout=2)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if result["returncode"] != 0:
        raise RuntimeError(
            f"{Path(command[0]).name} failed with {result['returncode']}: "
            f"{result['stderr'].strip()[:400]}")
    return {**_parse(result["stdout"]), "startup_ms": elapsed_ms,
            "peak_rss_bytes": peak or None, "output": result["stdout"].strip()}


def _median_run(command: list[str], repeats: int, *, env: dict | None = None,
                marker: str | None = None) -> dict:
    runs = [_run_arm(command, env=env, marker=marker) for _ in range(repeats)]
    values = [run["ms"] for run in runs]
    return {
        "ms_median": statistics.median(values),
        "ms_min": min(values),
        "ms_max": max(values),
        "runs": repeats,
        "startup_ms_median": statistics.median(r["startup_ms"] for r in runs),
        "peak_rss_bytes": max((r["peak_rss_bytes"] or 0) for r in runs) or None,
        "sample_output": runs[0]["output"],
    }


def _first_window_ms(exe: Path, timeout: float = 90.0) -> dict:
    """Spawn the GUI fixture and time the appearance of its window."""
    runtime = wine_backend.select()
    prefix = wine_backend.ensure_bottle(runtime, "bench-gui")
    started = time.perf_counter()
    proc = subprocess.Popen([str(runtime.wine), str(exe)],
                            env=wine_backend.clean_env(prefix),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    first: float | None = None
    peak = 0
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            frames = verify_mod.windows_frames(proc.pid)
            peak = max(peak, _tree_rss(str(exe)))
            if any(frame.get("width", 0) >= 400 and frame.get("height", 0) >= 300
                   for frame in frames):
                first = (time.perf_counter() - started) * 1000.0
                break
            time.sleep(0.05)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
    return {"first_window_ms": first, "peak_rss_bytes": peak or None}


def measure(repeats: int = 5, iterations: int = DEFAULT_ITERATIONS,
            include_gui_startup: bool = True) -> dict:
    if not _ready():
        raise RuntimeError("benchmark fixtures are missing; run "
                           "./fixtures/win32/build.sh")
    runtime = wine_backend.select()
    prefix = wine_backend.ensure_bottle(runtime, "bench")
    env = wine_backend.clean_env(prefix)
    args = [str(iterations)]

    arms: dict[str, dict] = {}
    arms["native_arm64"] = _median_run([str(ARM64_BENCH), *args], repeats)
    arms["macos_x86_64_rosetta"] = _median_run([str(X86_BENCH), *args], repeats)
    arms["windows_pe_wine"] = _median_run(
        [str(runtime.wine), str(WIN_BENCH), *args], repeats, env=env,
        marker=str(WIN_BENCH))

    hashes = {name: arm["sample_output"].split("hash=")[-1].split()[0]
              for name, arm in arms.items()}
    consistent = len(set(hashes.values())) == 1

    translation = (arms["macos_x86_64_rosetta"]["ms_median"] /
                   arms["native_arm64"]["ms_median"])
    runtime_cost = (arms["windows_pe_wine"]["ms_median"] /
                    arms["macos_x86_64_rosetta"]["ms_median"])

    report: dict = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": util.host_info(),
        "wine": runtime.to_dict(),
        "iterations": iterations,
        "repeats": repeats,
        "workload": "fixtures/win32/bench.c (integer hash + double accumulator)",
        "arms": arms,
        "checksums": hashes,
        "checksums_identical": consistent,
        "translation_overhead_x": round(translation, 3),
        "windows_api_overhead_x": round(runtime_cost, 3),
        "total_overhead_x": round(
            arms["windows_pe_wine"]["ms_median"] /
            arms["native_arm64"]["ms_median"], 3),
        "notes": [
            "translation_overhead_x compares macOS x86-64 (Rosetta 2) with "
            "native arm64 for identical code",
            "windows_api_overhead_x compares the same code as a Windows PE "
            "inside the runtime with the same code as a macOS x86-64 process",
            "this is a CPU-bound loop; it says nothing about graphics, disk, "
            "network or a particular application's responsiveness",
        ],
    }
    if not consistent:
        report["notes"].append(
            "the arms produced different checksums, so the timings are not "
            "comparable: the compiler specialised the loop differently")
    if include_gui_startup and GUI_EXE.exists():
        report["gui_startup"] = _first_window_ms(GUI_EXE)
        outcome = wine_backend.run_captured(
            [str(runtime.wine), str(WIN_BENCH), "1000000"], env=env,
            timeout=300)
        report["startup"] = {
            "console_execution_ms": _parse(outcome["stdout"])["ms"],
            "note": "console_execution_ms is the guest's own measurement of a "
                    "trivial run, including process start inside the runtime",
        }
    return report


def summarise(report: dict) -> str:
    lines = [
        f"host: {report['host'].get('machine')}  wine: "
        f"{(report.get('wine') or {}).get('version')}",
        f"iterations: {report['iterations']}  repeats: {report['repeats']}",
        f"{'arm':26s} {'median ms':>10s} {'min':>9s} {'max':>9s} "
        f"{'peak RSS':>10s}",
    ]
    for name, arm in report["arms"].items():
        rss = arm.get("peak_rss_bytes")
        lines.append(
            f"{name:26s} {arm['ms_median']:10.2f} {arm['ms_min']:9.2f} "
            f"{arm['ms_max']:9.2f} "
            f"{(rss / 1e6 if rss else 0):9.1f}M")
    lines.append("")
    lines.append(f"x86-64 translation (Rosetta 2): "
                 f"{report['translation_overhead_x']}x native")
    lines.append(f"Windows API / runtime layer:    "
                 f"{report['windows_api_overhead_x']}x the same x86-64 code")
    lines.append(f"combined:                       "
                 f"{report['total_overhead_x']}x native arm64")
    lines.append(f"checksums identical: {report['checksums_identical']}")
    if report.get("gui_startup"):
        first = report["gui_startup"].get("first_window_ms")
        lines.append(f"GUI first window: "
                     f"{first:.0f} ms" if first else "GUI first window: n/a")
    return "\n".join(lines)


def write(report: dict) -> Path:
    path = util.RESULTS / "runtime_bench.json"
    util.write_json(path, report)
    return path
