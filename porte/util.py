"""Shared helpers: paths, hashing, subprocess execution, host identity."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
CACHE = BUILD / "cache"
PROFILES = ROOT / "profiles"
RESULTS = ROOT / "results"
RUNTIME = ROOT / "runtime"
FIXTURES = ROOT / "fixtures"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_json(obj) -> str:
    return sha256_text(json.dumps(obj, sort_keys=True, separators=(",", ":")))


def read_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, obj) -> None:
    ensure_dir(path.parent)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")


def run(cmd, env=None, cwd=None, check=True, timeout=600) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    proc = subprocess.run(
        [str(c) for c in cmd],
        env=full_env,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(str(c) for c in cmd)}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc


def host_info() -> dict:
    def sysctl(name: str) -> str:
        try:
            return run(["sysctl", "-n", name], check=False).stdout.strip()
        except Exception:
            return ""

    return {
        "model": sysctl("hw.model"),
        "chip": sysctl("machdep.cpu.brand_string"),
        "ncpu": sysctl("hw.ncpu"),
        "memory_mb": sysctl("hw.memsize"),
        "os": platform.mac_ver()[0],
        "os_build": run(["sw_vers", "-buildVersion"], check=False).stdout.strip(),
        "machine": platform.machine(),
    }


def toolchain_info() -> dict:
    clang = shutil.which("clang") or "/usr/bin/clang"
    version = run([clang, "--version"], check=False).stdout.splitlines()[0]
    sdk = run(["xcrun", "--show-sdk-version"], check=False).stdout.strip()
    return {"clang": version, "clang_path": clang, "sdk": sdk}


def spawn_measure(cmd, env=None, timeout=600) -> dict:
    """Run a child with fork/exec and return wall time plus wait4 rusage."""
    import signal

    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    cmd = [str(c) for c in cmd]
    r, w = os.pipe()
    t0 = time.perf_counter_ns()
    pid = os.fork()
    if pid == 0:  # child
        try:
            os.close(r)
            os.dup2(w, 1)
            os.dup2(w, 2)
            os.close(w)
            signal.alarm(timeout)
            os.execve(cmd[0], cmd, full_env)
        except BaseException:
            pass
        os._exit(127)
    os.close(w)
    chunks = []
    with os.fdopen(r, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
    _, status, ru = os.wait4(pid, 0)
    t1 = time.perf_counter_ns()
    return {
        "status": status,
        "exit": os.waitstatus_to_exitcode(status),
        "wall_ns": t1 - t0,
        "output": b"".join(chunks).decode("utf-8", "replace"),
        "maxrss_bytes": ru.ru_maxrss,
        "user_s": ru.ru_utime,
        "sys_s": ru.ru_stime,
    }


def now_ns() -> int:
    return time.perf_counter_ns()


def human_ns(ns: float) -> str:
    if ns < 1e3:
        return f"{ns:.0f} ns"
    if ns < 1e6:
        return f"{ns / 1e3:.1f} us"
    if ns < 1e9:
        return f"{ns / 1e6:.2f} ms"
    return f"{ns / 1e9:.3f} s"


def pct(a: float, b: float) -> float:
    return (a / b - 1.0) * 100.0 if b else 0.0


def eprint(*args) -> None:
    print(*args, file=sys.stderr)
