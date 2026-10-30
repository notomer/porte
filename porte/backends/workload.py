"""Measure one real Windows program, unchanged, under several runtimes.

Phase 3 could only compare a Windows program with *itself*: Wine under Rosetta
was the only backend on this Mac, so there was no second execution backend to
measure against. There is now (native ARM64 Wine with FEX), and this module is
the instrument that compares them on one workload.

What it records per run, and why:

* wall clock        — what a user waits for;
* user + sys CPU    — how much work actually happened, so a run that merely
                      slept less does not look faster;
* peak RSS          — translation caches and JIT regions cost memory;
* exit status       — a crash is not a fast run;
* SHA-256 of the    — the only correctness claim a benchmark may make on its
  output file         own: the same bytes came out.

A job describes the workload and the backends:

    {
      "name": "7zip-lzma",
      "input": "/tmp/porte-workload.bin",
      "repeats": 5,
      "backends": [
        {"label": "wine-rosetta", "kind": "windows",
         "wine": "${PORTE_WINE}", "prefix": "${PORTE_PREFIX}",
         "program": "C:\\\\Program Files\\\\7-Zip\\\\7z.exe",
         "args": ["a", "-t7z", "-mx=9", "-mmt=off", "{out}", "{in}"]},
        {"label": "native-arm64", "kind": "native",
         "program": "/opt/homebrew/bin/7zz",
         "args": ["a", "-t7z", "-mx=9", "-mmt=off", "{out}", "{in}"]}
      ]
    }

`{in}` and `{out}` are substituted per run, and `env` (optional) is applied on
top of the runtime's clean environment. That is what lets the same job measure a
*specialization*: it is the same backend with a different environment, so any
difference is attributable to the environment rather than to a different command.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import statistics
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import util

SAMPLE_INTERVAL = 0.1
THREAD_EVERY = 2          # sample thread counts every other interval


@dataclass
class Measurement:
    label: str
    repeat: int
    wall_ms: float
    cpu_ms: float
    rss_bytes: int
    exit_code: int
    output_sha256: str | None
    output_bytes: int
    threads_peak: int | None = None
    check_exit: int | None = None
    stdout: str = ""
    stderr_tail: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "label": self.label, "repeat": self.repeat,
            "wall_ms": round(self.wall_ms, 2),
            "cpu_ms": round(self.cpu_ms, 2),
            "rss_mb": round(self.rss_bytes / (1024 * 1024), 1),
            "exit_code": self.exit_code,
            "output_sha256": self.output_sha256,
            "output_bytes": self.output_bytes,
            "threads_peak": self.threads_peak,
            "check_exit": self.check_exit,
            "extra": self.extra,
        }


def _expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(str(value)))


def load_job(path: Path) -> dict:
    job = json.loads(Path(path).read_text(encoding="utf-8"))
    for backend in job.get("backends") or []:
        for key in ("wine", "prefix", "program"):
            if backend.get(key):
                backend[key] = _expand(backend[key])
    if job.get("input"):
        job["input"] = _expand(job["input"])
    check = job.get("check") or {}
    if check.get("program"):
        check["program"] = _expand(check["program"])
    return job


def _sha256(path: Path) -> tuple[str | None, int]:
    if not path.exists():
        return None, 0
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest(), path.stat().st_size


def parse_ps_time(text: str) -> float:
    """`ps` prints [[dd-]hh:]mm:ss[.cc] — return seconds."""
    days = 0
    if "-" in text:
        day_text, _, text = text.partition("-")
        days = int(day_text)
    parts = text.split(":")
    seconds = float(parts[-1])
    if len(parts) > 1:
        seconds += int(parts[-2]) * 60
    if len(parts) > 2:
        seconds += int(parts[-3]) * 3600
    return seconds + days * 86400


def process_table() -> dict[int, dict]:
    proc = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,time=,rss=,command="],
        capture_output=True, text=True)
    table: dict[int, dict] = {}
    if proc.returncode != 0:
        return table
    for line in proc.stdout.splitlines():
        fields = line.split(None, 5)
        if len(fields) < 6:
            continue
        try:
            pid = int(fields[0])
            table[pid] = {
                "ppid": int(fields[1]), "pgid": int(fields[2]),
                "cpu": parse_ps_time(fields[3]), "rss": int(fields[4]),
                "command": fields[5],
            }
        except ValueError:
            continue
    return table


def _family(table: dict[int, dict], root: int, target: str,
            preexisting: set[int]) -> dict[int, tuple[float, int]]:
    """The processes that belong to one run: pid -> (CPU seconds, KiB).

    Following the parent chain alone is wrong here, and noticing that is the
    point: Wine's loader hands the guest to a helper which exits, so the process
    that does the work is reparented before it has done anything, and its CPU
    never shows up under our pid. Its process group is its own as well (Wine
    calls setpgid), so group following misses it too.

    What does hold: the guest is a process whose command line names the program
    we asked to run, and it did not exist before the run started. Descendants
    and same-group processes are included as well, so a native run and a
    loader's own children are counted. A pid is counted once however many rules
    match it.
    """
    family: set[int] = {root}
    for pid, row in table.items():
        if row["pgid"] == root or row["ppid"] == root:
            family.add(pid)
    if target:
        for pid, row in table.items():
            if pid not in preexisting and target in row["command"]:
                family.add(pid)
    found: dict[int, tuple[float, int]] = {}
    for pid in family:
        row = table.get(pid)
        if row is not None:
            found[pid] = (row["cpu"], row["rss"])
    return found


def _threads_now(pid: int) -> int | None:
    """Thread count of a process, from the kernel's own view."""
    proc = subprocess.run(["ps", "-M", "-p", str(pid)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    rows = [line for line in proc.stdout.splitlines()[1:] if line.strip()]
    return len(rows) or None


def _command(backend: dict, program: str, args: list[str]) -> list[str]:
    if backend.get("kind") == "native":
        return [program] + args
    return [backend["wine"], program] + args


def _environment(backend: dict, prefix: Path | None) -> dict:
    if backend.get("kind") == "native":
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("FEX_", "WINE", "MTL_"))}
    else:
        from . import wine as wine_backend

        env = wine_backend.clean_env(prefix)
    env.update({str(k): str(v) for k, v in (backend.get("env") or {}).items()})
    return env


def run_once(backend: dict, job: dict, repeat: int,
             out_dir: Path) -> Measurement:
    """One run, with the input copied in so every backend sees one name.

    The archive records the input's name, so handing a backend an absolute path
    and another a relative one would produce different bytes for reasons that
    have nothing to do with execution. Both get `input.bin` in the same working
    directory instead.
    """
    program = str(backend["program"])
    staged_input = out_dir / "input.bin"
    if not staged_input.exists():
        shutil.copyfile(job["input"], staged_input)
    output = out_dir / f"{backend['label']}-{repeat}.out"
    if output.exists():
        output.unlink()
    args = [str(a).replace("{out}", output.name).replace("{in}", staged_input.name)
            for a in backend.get("args") or []]
    command = _command(backend, program, args)
    prefix = Path(backend["prefix"]) if backend.get("prefix") else None
    env = _environment(backend, prefix)

    target = os.path.basename(program)
    preexisting = {pid for pid, row in process_table().items()
                   if target in row["command"]}
    started = time.perf_counter()
    proc = subprocess.Popen(command, env=env, cwd=str(out_dir),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    cpu_by_pid: dict[int, float] = {}
    rss_by_pid: dict[int, int] = {}
    threads_peak = 0
    intervals = 0
    while proc.poll() is None:
        table = process_table()
        for pid, (cpu_s, rss_kb) in _family(table, proc.pid, target,
                                            preexisting).items():
            cpu_by_pid[pid] = max(cpu_by_pid.get(pid, 0.0), cpu_s)
            rss_by_pid[pid] = max(rss_by_pid.get(pid, 0), rss_kb)
        if intervals % THREAD_EVERY == 0:
            # Threads of the *program*, not of the runtime's service processes:
            # this number is the guard an optimization uses to decide whether
            # cross-thread memory ordering can matter at all.
            for pid, row in table.items():
                if pid in preexisting or target not in row["command"]:
                    continue
                count = _threads_now(pid)
                if count:
                    threads_peak = max(threads_peak, count)
        intervals += 1
        time.sleep(SAMPLE_INTERVAL)
    stdout, stderr = proc.communicate()
    wall_ms = (time.perf_counter() - started) * 1000.0

    digest, size = _sha256(output)
    check_exit = None
    check = job.get("check") or {}
    if check.get("program") and digest:
        check_args = [str(a).replace("{out}", output.name)
                      for a in check.get("args") or []]
        verified = subprocess.run([check["program"]] + check_args,
                                  cwd=str(out_dir), capture_output=True,
                                  text=True)
        check_exit = verified.returncode
    # The guest is a child of the runtime's loader, so the loader's own rusage
    # says nothing about the work: CPU is integrated from the whole descendant
    # tree while it runs. Resolution is one sample interval.
    cpu_s = sum(cpu_by_pid.values())
    rss_bytes = sum(rss_by_pid.values()) * 1024
    return Measurement(
        label=backend["label"], repeat=repeat, wall_ms=wall_ms,
        cpu_ms=cpu_s * 1000.0, rss_bytes=rss_bytes,
        exit_code=proc.returncode, output_sha256=digest, output_bytes=size,
        threads_peak=threads_peak or 1, check_exit=check_exit,
        stdout=(stdout or "").strip()[:2000],
        stderr_tail=(stderr or "").strip()[-2000:],
        extra={"cpu_source": "descendant rusage via ps, 100 ms samples",
               "rss_source": "sum of descendant peaks",
               "samples": intervals})


def _summarise(measurements: list[Measurement]) -> dict:
    walls = [m.wall_ms for m in measurements]
    return {
        "runs": len(measurements),
        "wall_ms_median": round(statistics.median(walls), 2),
        "wall_ms_min": round(min(walls), 2),
        "wall_ms_max": round(max(walls), 2),
        "wall_ms_stdev": round(statistics.stdev(walls), 2)
                          if len(walls) > 1 else 0.0,
        "cpu_ms_median": round(statistics.median([m.cpu_ms for m in measurements]), 2),
        "rss_mb_median": round(statistics.median(
            [m.rss_bytes for m in measurements]) / (1024 * 1024), 1),
        "exit_codes": sorted({m.exit_code for m in measurements}),
        "output_sha256": sorted({m.output_sha256 for m in measurements
                                 if m.output_sha256}),
        "check_exits": sorted({m.check_exit for m in measurements
                               if m.check_exit is not None}),
        "threads_peak": max((m.threads_peak or 0) for m in measurements),
    }


def run(job: dict, out: Path | None = None, repeats: int | None = None,
        keep: bool = False) -> dict:
    """Run every backend in `job` on the same input and compare them."""
    repeats = int(repeats or job.get("repeats") or 3)
    input_digest, input_bytes = _sha256(Path(job["input"]))
    declared = job.get("input_sha256")
    if declared and declared != input_digest:
        raise ValueError(
            f"input is not the corpus the job names: {job['input']} is "
            f"{input_digest}, job declares {declared}")
    workdir = Path(tempfile.mkdtemp(prefix="porte-workload-"))
    measurements: dict[str, list[Measurement]] = {
        backend["label"]: [] for backend in job["backends"]}
    try:
        # Round-robin rather than one backend at a time: a laptop that heats up
        # or slows down over ten minutes would otherwise hand the win to
        # whichever backend ran first. Each repeat samples every backend once.
        for index in range(repeats):
            for backend in job["backends"]:
                measurements[backend["label"]].append(
                    run_once(backend, job, index, workdir))
    finally:
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)

    summary = {label: _summarise(runs) for label, runs in measurements.items()}
    digests = {label: row["output_sha256"] for label, row in summary.items()}
    reference = next((label for label in digests if digests[label]), None)
    correctness = {
        "reference": reference,
        "identical": bool(reference) and all(
            digests[label] == digests[reference] for label in digests),
        "per_backend": {label: digests[label] for label in digests},
        "all_exited_zero": all(
            row["exit_codes"] == [0] for row in summary.values()),
        "all_checks_passed": all(
            row["check_exits"] in ([], [0]) for row in summary.values()),
    }
    report = {
        "job": job.get("name"),
        "input": job.get("input"),
        "input_sha256": input_digest,
        "input_bytes": input_bytes,
        "repeats": repeats,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": summary,
        "correctness": correctness,
        "runs": [m.to_dict() for label in measurements
                 for m in measurements[label]],
    }
    if out:
        util.write_json(Path(out), report)
    return report


def summarise(report: dict) -> str:
    """A table a human can check against the JSON."""
    lines = [f"workload: {report['job']}  ({report['repeats']} repeats each)",
             f"input: {report['input']} "
             f"{report['input_bytes']} bytes sha256 {report['input_sha256']}",
             "",
             f"{'backend':26s} {'median':>9s} {'min':>9s} {'cpu':>9s} "
             f"{'rss MB':>7s} {'threads':>7s} output"]
    for label, row in report["summary"].items():
        digest = (row["output_sha256"] or ["-"])[0]
        lines.append(
            f"{label:26s} {row['wall_ms_median']:8.0f}ms {row['wall_ms_min']:8.0f}ms "
            f"{row['cpu_ms_median']:8.0f}ms {row['rss_mb_median']:7.1f} "
            f"{row['threads_peak']:7d} {digest[:16]}")
    correctness = report["correctness"]
    lines.append("")
    lines.append(f"outputs byte-identical across backends: "
                 f"{correctness['identical']} (reference "
                 f"{correctness['reference']})")
    lines.append(f"every run exited 0: {correctness['all_exited_zero']}")
    lines.append(f"every output passed the check: "
                 f"{correctness['all_checks_passed']}")
    labels = list(report["summary"])
    if len(labels) > 1:
        base = report["summary"][labels[0]]["wall_ms_median"]
        lines.append("")
        for label in labels[1:]:
            ratio = base / report["summary"][label]["wall_ms_median"]
            lines.append(f"{label} is {ratio:.2f}x the speed of {labels[0]}")
    return "\n".join(lines)
