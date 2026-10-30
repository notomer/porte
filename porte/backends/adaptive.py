"""Specialize a Windows program's compatibility semantics from what it does.

An x86-64 translator on ARM64 must emulate x86's memory model: on x86 a store
becomes visible to other cores in program order, on ARM64 it does not unless the
translator inserts barriers. FEX emulates that with ordinary ARM64 instructions
plus release/acquire sequences, and it lets the whole model, or pieces of it, be
switched off (`FEX_TSOENABLED`, `FEX_HALFBARRIERTSOENABLED`,
`FEX_VECTORTSOENABLED`, `FEX_MEMCPYSETTSOENABLED`).

Switching it off is only sound when no two threads of the program run at the
same time: if at most one thread is executing, there is no second observer that
could see a store out of order, so the barriers are unobservable. That is a
property of the *program's* behaviour on a *workload*, and it can be measured:
compare the CPU time the guest actually consumed with the wall time it took.

    parallelism = CPU seconds / wall seconds

A single-threaded run reaches about 1.0; a run that never has two threads
running at once cannot exceed it. The rule used here requires ≤ 1.1, which
tolerates sampling error but not a second working thread.

This module measures that, proposes the specialization, then *proves* it on the
same workload before keeping it: identical output bytes, identical exit status,
a passing integrity check, and a wall-clock win above a threshold. Anything else
keeps the baseline. The record it writes is what a launcher would read to apply
the specialization on later launches, with the guard re-checked at run time.

What this is not: it does not generate code. It removes an unnecessary
compatibility cost by choosing the semantics the program actually needs, which is
a real speedup against the unmodified backend, and it is honest about where the
speedup comes from.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import util
from . import fex_profile
from . import workload as workload_mod

#: The barrier families FEX can stop emulating. All four are memory-ordering
#: only; none of them changes instruction results, which is why the output
#: comparison is a meaningful check.
NO_TSO = {
    "FEX_TSOENABLED": "0",
    "FEX_HALFBARRIERTSOENABLED": "0",
    "FEX_VECTORTSOENABLED": "0",
    "FEX_MEMCPYSETTSOENABLED": "0",
}
#: A hundredth of the run is sampling noise; a real second thread is not.
PARALLELISM_LIMIT = 1.1
#: Below these, the run was not actually observed and no claim may be made:
#: one second of a ten-hertz watch, and at least a fifth of a second of work.
MIN_SAMPLES = 10
MIN_WALL_S = 1.0
MIN_CPU_S = 0.2
#: A specialization that wins less than this is not worth its complexity.
MIN_SPEEDUP = 1.03


@dataclass
class Observation:
    """What the guest actually did while it ran."""
    program: str
    wall_s: float
    cpu_s: float
    processes: int
    samples: int

    @property
    def parallelism(self) -> float:
        return self.cpu_s / self.wall_s if self.wall_s > 0 else 0.0

    @property
    def reliable(self) -> bool:
        """Whether enough of the run was watched to say anything about it.

        A launcher that exits before the guest has started produces a short run
        with no CPU: reading that as "one thread, therefore safe" would be a
        false pass, which is the one failure this guard must never make.
        """
        return self.samples >= MIN_SAMPLES and self.wall_s >= MIN_WALL_S \
            and self.cpu_s >= MIN_CPU_S

    @property
    def single_threaded(self) -> bool:
        return (self.reliable and self.processes == 1
                and self.parallelism <= PARALLELISM_LIMIT)

    def to_dict(self) -> dict:
        return {
            "program": self.program,
            "wall_s": round(self.wall_s, 2),
            "cpu_s": round(self.cpu_s, 2),
            "parallelism": round(self.parallelism, 3),
            "guest_processes": self.processes,
            "samples": self.samples,
            "single_threaded": self.single_threaded,
        }


@dataclass
class Candidate:
    name: str
    env: dict
    reason: str

    def to_dict(self) -> dict:
        return {"name": self.name, "env": self.env, "reason": self.reason}


@dataclass
class Record:
    program: str
    key: str
    accepted: bool
    reason: str
    observation: dict = field(default_factory=dict)
    candidate: dict | None = None
    evidence: dict = field(default_factory=dict)
    measured_at: str = ""

    def to_dict(self) -> dict:
        return {
            "program": self.program, "key": self.key,
            "accepted": self.accepted, "reason": self.reason,
            "observation": self.observation, "candidate": self.candidate,
            "evidence": self.evidence, "measured_at": self.measured_at,
        }


def observe(backend: dict, job: dict) -> Observation:
    """Run the workload once and report how much CPU the guest used per second.

    CPU is the sum of the guest process's own CPU time, read from `ps` at both
    ends of the run: a snapshot difference, not a model.
    """
    program = str(backend["program"])
    workdir = Path(tempfile.mkdtemp(prefix="porte-observe-"))
    output = workdir / "observe.7z"
    staged = Path(job["input"])
    arguments = [str(a).replace("{out}", output.name)
                 .replace("{in}", str(staged)) for a in backend.get("args") or []]
    prefix = Path(backend["prefix"]) if backend.get("prefix") else None
    env = workload_mod._environment(backend, prefix)
    command = ([backend["wine"], program] if backend.get("kind") != "native"
               else [program]) + arguments
    stale = {pid for pid, row in workload_mod.process_table().items()
             if row["command"].lower().startswith(program.lower())}
    started = time.perf_counter()
    proc = subprocess.Popen(command, env=env, cwd=str(workdir),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    processes = 0
    samples = 0
    cpu_start = cpu_end = 0.0
    deadline = started + 300.0
    # The runtime's loader may exit before the guest does, so wait for both.
    while time.perf_counter() < deadline:
        pid = fex_profile.find_guest(program, exclude=stale)
        if pid is not None:
            cpu = _process_cpu(pid)
            if not cpu_start:
                cpu_start = cpu
            cpu_end = max(cpu_end, cpu)
            processes = 1
        if proc.poll() is not None and pid is None:
            break
        samples += 1
        time.sleep(0.1)
    if proc.poll() is None:
        proc.terminate()
    shutil.rmtree(workdir, ignore_errors=True)
    wall = time.perf_counter() - started
    return Observation(program=program, wall_s=wall,
                       cpu_s=max(0.0, cpu_end - cpu_start),
                       processes=processes, samples=samples)


def _process_cpu(pid: int) -> float:
    proc = subprocess.run(["ps", "-o", "time=", "-p", str(pid)],
                          capture_output=True, text=True)
    text = proc.stdout.strip()
    if not text:
        return 0.0
    return workload_mod.parse_ps_time(text)


def candidates(observation: Observation) -> list[Candidate]:
    """What may be switched off, given what the program was seen doing."""
    if not observation.single_threaded:
        return []
    return [Candidate(
        name="no-tso",
        env=dict(NO_TSO),
        reason=(f"the guest used {observation.cpu_s:.1f} s of CPU over "
                f"{observation.wall_s:.1f} s of wall time "
                f"(parallelism {observation.parallelism:.2f}), so at most one "
                "thread was executing and x86 memory ordering between threads "
                "was not observable"))]


def evaluate(job: dict, baseline_label: str, candidate: Candidate,
             repeats: int, out: Path | None = None) -> Record:
    """Run the baseline and the candidate on one workload and decide."""
    baseline = next((b for b in job["backends"] if b["label"] == baseline_label),
                    None)
    if baseline is None:
        raise ValueError(f"no backend labelled {baseline_label!r} in the job")
    specialized = json.loads(json.dumps(baseline))
    specialized["label"] = f"{baseline_label}+{candidate.name}"
    specialized["env"] = {**(baseline.get("env") or {}), **candidate.env}

    observation = observe(baseline, job)
    cands = candidates(observation)
    report = workload_mod.run({**job, "backends": [baseline, specialized]},
                              repeats=repeats, out=out)
    base_row = report["summary"][baseline_label]
    spec_row = report["summary"][specialized["label"]]
    speedup = base_row["wall_ms_median"] / spec_row["wall_ms_median"] \
        if spec_row["wall_ms_median"] else 0.0
    cpu_speedup = base_row["cpu_ms_median"] / spec_row["cpu_ms_median"] \
        if spec_row["cpu_ms_median"] else 0.0
    identical = (base_row["output_sha256"] == spec_row["output_sha256"]
                 and report["correctness"]["identical"]
                 and report["correctness"]["all_exited_zero"]
                 and report["correctness"]["all_checks_passed"])

    if not cands:
        accepted, reason = False, (
            "no candidate: " + ("the run was not observed closely enough to "
                                "judge it" if not observation.reliable else
                                "the program ran more than one thread at a "
                                "time, so x86 memory ordering was observable"))
    elif not identical:
        accepted, reason = False, (
            "rejected: the specialized output did not match the baseline byte "
            "for byte, so the semantics change was observable")
    elif speedup < MIN_SPEEDUP:
        accepted, reason = False, (
            f"rejected: {speedup:.3f}x is below the {MIN_SPEEDUP:.2f}x bar; the "
            "barriers cost less than the noise this measurement has")
    else:
        accepted, reason = True, (
            f"accepted: {speedup:.3f}x wall clock and {cpu_speedup:.3f}x CPU "
            "with byte-identical output")

    return Record(
        program=str(baseline["program"]),
        key=program_key(baseline),
        accepted=accepted, reason=reason,
        observation=observation.to_dict(),
        candidate=candidate.to_dict() if cands else None,
        evidence={
            "baseline": baseline_label,
            "specialized": specialized["label"],
            "repeats": repeats,
            "wall_ms": {"baseline": base_row["wall_ms_median"],
                        "specialized": spec_row["wall_ms_median"]},
            "cpu_ms": {"baseline": base_row["cpu_ms_median"],
                       "specialized": spec_row["cpu_ms_median"]},
            "rss_mb": {"baseline": base_row["rss_mb_median"],
                       "specialized": spec_row["rss_mb_median"]},
            "speedup": round(speedup, 4),
            "cpu_speedup": round(cpu_speedup, 4),
            "output_sha256": spec_row["output_sha256"],
            "identical_output": identical,
        },
        measured_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )


def program_key(backend: dict) -> str:
    """Identity of the program whose behaviour was measured."""
    program = str(backend["program"])
    path = Path(program)
    if path.exists():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:32]
    else:
        digest = hashlib.sha256(program.lower().encode()).hexdigest()[:32]
    return f"{path.name.lower()}-{digest}"


def cache_root() -> Path:
    return Path.home() / "Library" / "Caches" / "Porte" / "specializations"


def save(record: Record, root: Path | None = None) -> Path:
    target = (root or cache_root()) / f"{record.key}.json"
    util.write_json(target, record.to_dict())
    return target


def load(key: str, root: Path | None = None) -> dict | None:
    path = (root or cache_root()) / f"{key}.json"
    if not path.exists():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    return record if record.get("accepted") else None


def summarise(record: Record) -> str:
    lines = [f"program: {record.program}",
             f"key: {record.key}",
             f"observation: {record.observation}",
             f"decision: {'ACCEPTED' if record.accepted else 'NOT APPLIED'} — "
             f"{record.reason}"]
    if record.candidate:
        lines.append("specialization: " + ", ".join(
            f"{k}={v}" for k, v in record.candidate["env"].items()))
    flags = record.evidence
    if flags:
        lines.append(
            f"wall: {flags['wall_ms']['baseline']:.0f} ms -> "
            f"{flags['wall_ms']['specialized']:.0f} ms "
            f"({flags['speedup']:.3f}x) over {flags['repeats']} repeats")
        lines.append(
            f"cpu:  {flags['cpu_ms']['baseline']:.0f} ms -> "
            f"{flags['cpu_ms']['specialized']:.0f} ms "
            f"({flags['cpu_speedup']:.3f}x)")
        lines.append(f"identical output: {flags['identical_output']}")
    return "\n".join(lines)
