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


def _pe_for_bundle(app: Path, metadata: dict) -> Path | None:
    """Where the Windows image actually lives.

    A portable executable is shipped inside the bundle. A program installed by
    a real Windows installer keeps its files in its own prefix, and the bundle
    records the Windows path; nothing is copied out of the product tree.
    """
    recorded = metadata.get("pe") or ""
    if metadata.get("pe_location") == "prefix":
        from ..backends import wine as wine_backend

        prefix = (metadata.get("runtime") or {}).get("bottle")
        if not prefix:
            return None
        return wine_backend.dos_to_unix(Path(prefix), recorded)
    return app / "Contents" / "Resources" / recorded


def _write_wine_record(state: Path, record: dict) -> None:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    util.write_json(state / "logs" / f"run-{stamp}-{os.getpid()}.json", record)
    with open(state / "logs" / "runs.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    previous = util.read_json(state / "state.json") \
        if (state / "state.json").exists() else {}
    util.write_json(state / "state.json", {
        "bundle_id": record["bundle_id"],
        "pe_sha256": record["pe_sha256"],
        "runs": int(previous.get("runs", 0)) + 1,
        "first_run": previous.get("first_run", record["finished_at"]),
        "last_run": record["finished_at"],
        "last_exit": record["exit_code"],
        "last_total_ms": record["total_ms"],
        "logs": str(state / "logs"),
    })


def _run_wine(app: Path, metadata: dict, pe: Path, args: list[str],
              state: Path, bundle_id: str, sha: str, entry_t: float,
              launched: float | None, spawned: float | None,
              launcher_ms: float | None,
              launcher_to_runtime_ms: float | None) -> int:
    """Execute the guest with the Wine runtime, as a real child process.

    Wine owns the guest's window, message loop, file dialogs and exit status.
    This function only supervises it and records what happened; the macOS
    application identity still belongs to the launcher that started us.
    """
    import subprocess

    from ..backends import wine as wine_backend

    runtime = metadata.get("runtime") or {}
    wine = Path(runtime.get("wine") or "")
    prefix = Path(runtime.get("bottle") or "")
    if not wine.exists():
        print(f"error: the Wine runtime recorded for this application is "
              f"missing: {wine}", file=sys.stderr)
        print("Open Porte, re-run setup, or reinstall the application. No "
              "component was downloaded or guessed.", file=sys.stderr)
        return 69
    if not prefix.exists():
        print(f"error: the compatibility environment recorded for this "
              f"application is missing: {prefix}", file=sys.stderr)
        print("Reinstall the application; its Windows files live in that "
              "environment.", file=sys.stderr)
        return 69

    windows_target = (metadata.get("pe") or "") \
        if metadata.get("pe_location") == "prefix" else str(pe)
    command = [str(wine), windows_target] + [str(a) for a in args]
    print(f"Porte runtime: {wine_backend.NAME} "
          f"{runtime.get('wine_version') or 'unknown version'}")
    print(f"compatibility environment: {prefix}")
    print(f"Windows program: {metadata.get('pe_display_path') or windows_target}")

    load_t = _now()
    proc = subprocess.run(command, env=wine_backend.clean_env(prefix),
                          cwd=str(pe.parent))
    run_ms = (_now() - load_t) * 1000.0
    total_ms = (_now() - entry_t) * 1000.0
    finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    record = {
        "bundle_id": bundle_id,
        "name": metadata.get("name"),
        "profile": metadata.get("profile"),
        "backend": "wine",
        "wine_version": runtime.get("wine_version"),
        "bottle": str(prefix),
        "pe_sha256": sha,
        "arguments": args,
        "exit_code": proc.returncode,
        "fault": None,
        "frames": None,
        "frame_ms_median": None,
        "gpu_ms": None,
        "guest_ms": run_ms,
        "load_ms": run_ms,
        "total_ms": total_ms,
        "launcher_ms": launcher_ms,
        "launcher_to_runtime_ms": launcher_to_runtime_ms,
        "bundle_start_ms": ((time.time() - launched) * 1000.0)
                            if launched else None,
        "api_calls": {},
        "output_sha256": None,
        "output_preview": "",
        "output_bytes": 0,
        "cache": None,
        "finished_at": finished_at,
    }
    print(f"guest process: exit {proc.returncode}, {run_ms:.0f} ms")
    if launcher_ms is not None and launcher_to_runtime_ms is not None:
        print(f"startup: launcher {launcher_ms:.1f} ms + interpreter "
              f"{launcher_to_runtime_ms:.1f} ms = "
              f"{launcher_ms + launcher_to_runtime_ms:.1f} ms before the "
              "guest process")
    _write_wine_record(state, record)
    print(f"run record: {state / 'logs'}")
    return int(proc.returncode & 0xFF)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    app = _bundle_path(argv).resolve()
    metadata = _load_metadata(app)
    bundle_id = metadata["bundle_id"]
    runtime_doc = metadata.get("runtime") or {}
    runtime_kind = runtime_doc.get("kind") or "engine"
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

    pe = _pe_for_bundle(app, metadata)
    if pe is None or not pe.exists():
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

    if runtime_kind == "wine":
        return _run_wine(app, metadata, pe, args, state, bundle_id, sha,
                         entry_t, launched, spawned, launcher_ms,
                         launcher_to_runtime_ms)

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
