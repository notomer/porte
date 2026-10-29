"""Runtime entry point used by generated application bundles.

Invoked as `python -m porte.app.runtime <path to .app>` by the bundle's ARM64
launcher. It owns everything after the macOS lifecycle: verifying the image,
loading it, running it through the Porte engine, and persisting per-application
state and logs.

This process is deliberately not the application's identity. The launcher is.
"""
from __future__ import annotations

import collections
import json
import os
import statistics
import sys
import time
from pathlib import Path

from .. import util
from . import bundle


def _now() -> float:
    return time.perf_counter()


def _read_env_ts(name: str) -> float | None:
    raw = os.environ.get(name)
    if not raw or not raw.isdigit():
        return None
    return int(raw) / 1e9


def _bundle_path(argv: list[str]) -> Path:
    if argv:
        return Path(argv[0])
    env = os.environ.get("PORTE_APP_BUNDLE")
    if env:
        return Path(env)
    raise SystemExit("usage: python -m porte.app.runtime <application.app>")


def _load_metadata(app: Path) -> dict:
    path = app / "Contents" / "Resources" / "PorteApp.json"
    if not path.exists():
        raise SystemExit(f"bundle metadata missing: {path}")
    return util.read_json(path)


def _engine_name(profile: str) -> str:
    return "porte-pe + metal-offscreen" if profile == "metal-frame" else "porte-pe"


def _cached_profile(state: Path, sha: str) -> dict | None:
    path = state / "cache" / "profile.json"
    if not path.exists():
        return None
    try:
        doc = util.read_json(path)
    except (ValueError, OSError):
        return None
    return doc if doc.get("pe_sha256") == sha else None


def _remember_profile(state: Path, sha: str, facts: dict) -> None:
    util.write_json(state / "cache" / "profile.json", {
        "pe_sha256": sha,
        "imports": facts["import_names"],
        "profile": facts["profile"],
        "cached_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


def _run_console(pe: Path, args: list[str]) -> dict:
    from ..pe.machine import Machine

    machine = Machine(pe)
    t0 = _now()
    result = machine.run_entry(args)
    elapsed = _now() - t0
    out = bytes(machine.writes)
    calls = collections.Counter(entry["call"] for entry in machine.api_log)
    return {
        "exit_code": result.exit_code if result.exit_code is not None else 0,
        "fault": result.fault,
        "run_ms": elapsed * 1000.0,
        "output": out.decode("latin-1", "replace"),
        "api_calls": dict(calls),
        "frames": None,
        "gpu_ms": None,
        "dispatches": machine.dispatches,
    }


def _run_frame(pe: Path, args: list[str], config: dict) -> dict:
    from ..frame.renderer import FrameRenderer
    from ..pe.gpu import GpuMachine

    renderer = FrameRenderer(quad=float(config.get("quad", 0.004)),
                             work=int(config.get("work", 0)))
    try:
        machine = GpuMachine(pe, renderer)
        t0 = _now()
        result = machine.run_entry(args)
        elapsed = _now() - t0
        report = machine.frame_report(0)
    finally:
        renderer.close()
    frames = report.get("wall_ms") or []
    gpu = report.get("gpu_ms") or []
    return {
        "exit_code": result.exit_code if result.exit_code is not None else 0,
        "fault": result.fault,
        "run_ms": elapsed * 1000.0,
        "output": bytes(machine.writes).decode("latin-1", "replace"),
        "api_calls": {"PresentFrame": len(machine.calls)},
        "frames": len(frames),
        "frame_ms_median": statistics.median(frames) if frames else None,
        "fps": (1000.0 / statistics.median(frames)) if frames else None,
        "gpu_ms": statistics.median(gpu) if gpu else None,
        "dispatches": machine.dispatches,
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    app = _bundle_path(argv).resolve()
    metadata = _load_metadata(app)
    bundle_id = metadata["bundle_id"]
    state = bundle.app_state_dir(bundle_id)
    (state / "logs").mkdir(parents=True, exist_ok=True)
    (state / "cache").mkdir(parents=True, exist_ok=True)

    launched = _read_env_ts("PORTE_LAUNCH_TS_NS")
    spawned = _read_env_ts("PORTE_SPAWN_TS_NS")
    entry_t = _now()
    launcher_ms = ((spawned - launched) * 1000.0
                   if launched and spawned else None)
    launcher_to_runtime_ms = ((time.time() - spawned) * 1000.0
                              if spawned else None)
    if launcher_ms is not None:
        print(f"launcher start-up (AppKit, consent, window): {launcher_ms:.1f} ms")
    if launcher_to_runtime_ms is not None:
        print(f"launcher→runtime (process start): {launcher_to_runtime_ms:.1f} ms")

    pe = app / "Contents" / "Resources" / metadata["pe"]
    if not pe.exists():
        print(f"error: Windows image missing: {pe}", file=sys.stderr)
        return 66
    sha = util.sha256_file(pe)
    if sha != metadata["pe_sha256"]:
        print("error: Windows image does not match its recorded hash; refusing "
              "to run. Reinstall the application.", file=sys.stderr)
        return 65
    consent = state / "consent.json"
    if not consent.exists():
        print("error: no recorded user consent for this executable; refusing to run.",
              file=sys.stderr)
        return 77

    config_path = state / "config.json"
    config = util.read_json(config_path) if config_path.exists() else {}
    if config.get("pe_sha256") and config["pe_sha256"] != sha:
        print("error: application configuration belongs to a different image.",
              file=sys.stderr)
        return 65
    args = [str(a) for a in (config.get("arguments") or metadata.get("arguments") or [])]

    profile = metadata.get("profile", "console")
    cached = _cached_profile(state, sha)
    if cached is None:
        from .install import inspect_pe

        facts = inspect_pe(pe)
        _remember_profile(state, sha, facts)
        cache_note = "miss"
    else:
        cache_note = "hit"

    print(f"Porte runtime: {_engine_name(profile)}")
    print(f"profile cache: {cache_note}")
    print(f"arguments: {' '.join(args) if args else '(none)'}")

    load_t = _now()
    if profile == "metal-frame":
        run = _run_frame(pe, args, config)
    else:
        run = _run_console(pe, args)
    load_ms = (load_t - entry_t) * 1000.0
    total_ms = (_now() - entry_t) * 1000.0

    if run["output"].strip():
        print("--- Windows program output ---")
        print(run["output"].rstrip())
        print(f"--- {len(run['output'])} bytes, sha256 "
              f"{util.sha256_text(run['output'])[:16]}… ---")
    if run["fault"]:
        print(f"guest fault: {run['fault']}", file=sys.stderr)
    if run.get("frames"):
        print(f"frames: {run['frames']}  median {run['frame_ms_median']:.3f} ms  "
              f"({run['fps']:.1f} fps)  GPU {run['gpu_ms']:.3f} ms")
    print(f"api calls: {run['api_calls'] or '{}'}")
    print(f"guest time: {run['run_ms']:.1f} ms  runtime total: {total_ms:.1f} ms")
    if launcher_ms is not None and launcher_to_runtime_ms is not None:
        print(f"startup: launcher {launcher_ms:.1f} ms + interpreter "
              f"{launcher_to_runtime_ms:.1f} ms = "
              f"{launcher_ms + launcher_to_runtime_ms:.1f} ms before guest code")

    record = {
        "bundle_id": bundle_id,
        "name": metadata.get("name"),
        "profile": profile,
        "pe_sha256": sha,
        "arguments": args,
        "exit_code": run["exit_code"],
        "fault": run["fault"],
        "frames": run.get("frames"),
        "frame_ms_median": run.get("frame_ms_median"),
        "gpu_ms": run.get("gpu_ms"),
        "guest_ms": run["run_ms"],
        "load_ms": load_ms,
        "total_ms": total_ms,
        "launcher_ms": launcher_ms,
        "launcher_to_runtime_ms": launcher_to_runtime_ms,
        "bundle_start_ms": ((time.time() - launched) * 1000.0) if launched else None,
        "api_calls": run["api_calls"],
        "output_sha256": util.sha256_text(run["output"]),
        "output_preview": run["output"][:400],
        "output_bytes": len(run["output"]),
        "cache": cache_note,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    util.write_json(state / "logs" / f"run-{stamp}-{os.getpid()}.json", record)
    with open(state / "logs" / "runs.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")

    state_path = state / "state.json"
    previous = util.read_json(state_path) if state_path.exists() else {}
    util.write_json(state_path, {
        "bundle_id": bundle_id,
        "pe_sha256": sha,
        "runs": int(previous.get("runs", 0)) + 1,
        "first_run": previous.get("first_run", record["finished_at"]),
        "last_run": record["finished_at"],
        "last_exit": run["exit_code"],
        "last_total_ms": total_ms,
        "logs": str(state / "logs"),
    })
    print(f"run record: {state / 'logs'}")

    if run["fault"]:
        return 70
    return int(run["exit_code"] & 0xFF)


if __name__ == "__main__":
    raise SystemExit(main())
