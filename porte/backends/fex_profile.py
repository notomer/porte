"""Attribute a native-ARM64 Wine/FEX guest's time to its own x86-64 functions.

Phase 3 concluded that guest code inside a Wine process could not be named: under
Rosetta the sampler reports `Rosetta JIT` addresses with no way back to the
instruction that produced them. That is true of Rosetta. It is not true of FEX,
which publishes a perf map:

    /tmp/perf-<pid>.map
    0x7ff4ade70004 690 \\??\\c:\\program files\\7-zip\\7z.exe+0x61380

Each line is a translated block: host address, byte size, and the guest module
plus RVA it was compiled from. With `FEX_LIBRARYJITNAMING=1` FEX writes those
entries as it compiles, so a sampler running during the benchmark can map every
hot host address back to the guest function it came from.

This module does exactly that, and nothing more: it starts the guest, samples
it, and joins the two sources. It reports how much of the sampled time is the
application's own code, how much is FEX's runtime helpers (the translation
overhead that no application change can remove), how much is the host, and how
much is threads waiting. The split is measured, not modelled.

Limits that are stated rather than hidden:

* Samples are of *host* addresses; a block that has not been compiled yet has no
  perf-map entry, and is counted as unattributed rather than guessed.
* FEX may reuse the memory of a block it has since recompiled, so a very hot map
  can attribute an address to a block that replaced it. Counts are therefore
  shares of samples, not exact instruction counts.
* Only the guest process is sampled, not the whole Wine process tree.
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import util

PERF_LINE_RE = re.compile(r"^(0x[0-9a-f]+)\s+(\d+)\s+(\S.*)$")
#: `0x7ff4ade70004 690 \??\c:\...\7z.exe+0x61380 (0x7ff4ade70004)`
GUEST_NAME_RE = re.compile(r"^(?P<module>.+)\+(?P<rva>0x[0-9a-f]+)"
                          r"(?:\s+\(0x[0-9a-f]+\))?$")
#: A collapsed frame ends with its sample count: `name (in image) [0xaddr]  285`.
TOP_FRAME_RE = re.compile(r"^(?P<rest>.*?)\s+(?P<count>\d+)$")
IMAGE_RE = re.compile(r"\(in (?P<image>[^)]+)\)")
ADDRESS_RE = re.compile(r"\[\s*(?P<addr>0x[0-9a-f]+)\s*\]")
WAIT_SYMBOLS = (
    "__workq_kernreturn", "mach_msg", "psynch", "semaphore_wait", "read",
    "select", "__ulock_wait", "kevent", "poll", "wait4", "recvfrom",
    "___psynch_cvwait", "swtch_pri", "mach_msg2_trap", "mach_msg_overwrite",
    "mach_msg2_internal", "sem_wait", "pthread_cond_wait", "usleep",
)


@dataclass
class Block:
    """One translated block: where it lives on the host and what it came from."""
    start: int
    size: int
    module: str
    rva: int | None

    @property
    def end(self) -> int:
        return self.start + self.size


@dataclass
class Frame:
    count: int
    name: str
    image: str | None
    address: int | None


@dataclass
class Report:
    program: str
    pid: int | None
    code_type: str | None
    seconds: float
    total_samples: int = 0
    blocks: int = 0
    sampled_addresses: int = 0
    mapped_addresses: int = 0
    classes: dict[str, int] = field(default_factory=dict)
    modules: dict[str, int] = field(default_factory=dict)
    functions: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        total = max(1, self.total_samples)
        return {
            "program": self.program,
            "pid": self.pid,
            "code_type": self.code_type,
            "seconds": round(self.seconds, 2),
            "total_samples": self.total_samples,
            "translated_blocks": self.blocks,
            "sampled_addresses": self.sampled_addresses,
            "mapped_addresses": self.mapped_addresses,
            "classes": self.classes,
            "shares": {name: round(count / total, 4)
                       for name, count in self.classes.items()},
            "modules": self.modules,
            "top_guest_functions": self.functions,
            "notes": self.notes,
        }


def parse_perf_map(path: Path) -> list[Block]:
    """Read FEX's perf map. Malformed lines are skipped, not guessed."""
    blocks: list[Block] = []
    for line in Path(path).read_text(errors="replace").splitlines():
        match = PERF_LINE_RE.match(line.strip())
        if not match:
            continue
        start = int(match.group(1), 16)
        size = int(match.group(2))
        module, rva = match.group(3), None
        named = GUEST_NAME_RE.match(module)
        if named:
            module = named.group("module").replace("\\??\\", "")
            rva = int(named.group("rva"), 16)
        blocks.append(Block(start=start, size=size, module=module, rva=rva))
    blocks.sort(key=lambda block: block.start)
    return blocks


def parse_sample(path: Path) -> list[Frame]:
    """Top-of-stack frames from `/usr/bin/sample`'s collapsed summary."""
    text = Path(path).read_text(errors="replace")
    marker = text.find("Sort by top of stack")
    if marker < 0:
        return []
    frames: list[Frame] = []
    for line in text[marker:].splitlines()[1:]:
        if not line.strip():
            continue
        match = TOP_FRAME_RE.match(line.strip())
        if not match:
            continue
        rest = match.group("rest")
        address = ADDRESS_RE.search(rest)
        image = IMAGE_RE.search(rest)
        name = ADDRESS_RE.sub("", IMAGE_RE.sub("", rest)).strip()
        if not (name or image or address):
            continue
        frames.append(Frame(
            count=int(match.group("count")), name=name,
            image=image.group("image") if image else None,
            address=int(address.group("addr"), 16) if address else None))
    return frames


def _block_at(blocks: list[Block], address: int) -> Block | None:
    """The block covering `address`, preferring one with a guest origin.

    Not a binary search, deliberately: FEX's map contains **overlapping** ranges
    — region markers (`FEXJIT`, 1 MB) and several entries for one translated
    block at different addresses — so the usual sorted-interval assumption does
    not hold and a bisect silently returns "no block" for code that is mapped.
    A linear scan is fast enough here (a few thousand blocks, tens of addresses)
    and cannot be wrong in that way.
    """
    region: Block | None = None
    for block in blocks:
        if block.start <= address < block.end:
            if block.rva is not None:
                return block
            region = region or block
    return region


def _is_waiting(frame: Frame) -> bool:
    return any(name in frame.name for name in WAIT_SYMBOLS)


def attribute(report: Report, frames: list[Frame], blocks: list[Block]) -> None:
    """Join samples to blocks, and write the counts into `report`."""
    functions: dict[tuple[str, int], int] = {}
    for frame in frames:
        report.total_samples += frame.count
        block = _block_at(blocks, frame.address) if frame.address else None
        if frame.address:
            report.sampled_addresses += 1
            if block is not None and block.rva is not None:
                report.mapped_addresses += 1
        if block is not None and block.rva is not None:
            report.classes["guest"] = report.classes.get("guest", 0) + frame.count
            report.modules[block.module] = \
                report.modules.get(block.module, 0) + frame.count
            key = (block.module, block.rva)
            functions[key] = functions.get(key, 0) + frame.count
        elif block is not None:
            # Inside the JIT region but not a guest block: FEX's own helpers,
            # the part of translation that no guest optimization can remove.
            report.classes["fex-runtime"] = \
                report.classes.get("fex-runtime", 0) + frame.count
        elif _is_waiting(frame):
            report.classes["waiting"] = \
                report.classes.get("waiting", 0) + frame.count
        elif frame.image and frame.image != "<unknown binary>":
            report.classes["host"] = report.classes.get("host", 0) + frame.count
        else:
            report.classes["unattributed"] = \
                report.classes.get("unattributed", 0) + frame.count
    total = max(1, report.total_samples)
    report.functions = [
        {"module": module, "rva": f"0x{rva:x}", "samples": count,
         "share": round(count / total, 4)}
        for (module, rva), count in sorted(functions.items(),
                                           key=lambda item: -item[1])[:25]]


def find_guest(program: str, exclude: set[int] | None = None) -> int | None:
    """The guest process, not the `start.exe` wrapper that launched it.

    The test is that the command line *begins* with the program: a shell, an
    editor or a profiler that merely mentions the path in its arguments is not
    the guest, and picking one of those is how a measurement ends up describing
    the wrong process. `exclude` drops processes that were already running
    before this run started.
    """
    wanted = program.lower().rstrip()
    proc = subprocess.run(["ps", "-axo", "pid=,ppid=,%cpu=,command="],
                          capture_output=True, text=True)
    skip = exclude or set()
    best: tuple[float, int] | None = None
    for line in proc.stdout.splitlines():
        fields = line.split(None, 3)
        if len(fields) < 4:
            continue
        command = fields[3].strip().lower()
        if not command.startswith(wanted):
            continue
        try:
            pid, cpu = int(fields[0]), float(fields[2])
        except ValueError:
            continue
        if pid in skip:
            continue
        if best is None or cpu > best[0]:
            best = (cpu, pid)
    return best[1] if best else None


_find_guest = find_guest


def profile(program: str, wine: Path, prefix: Path, arguments: list[str] | None = None,
            seconds: float = 3.0, warmup: float = 2.0) -> Report:
    """Run `program` under an ARM64 Wine/FEX runtime and profile the guest."""
    from . import wine as wine_backend

    env = wine_backend.clean_env(prefix)
    env.update({
        "FEX_GLOBALJITNAMING": "1",
        "FEX_LIBRARYJITNAMING": "1",
        "FEX_BLOCKJITNAMING": "1",
    })
    command = [str(wine), program] + [str(a) for a in (arguments or [])]
    started = time.time()
    proc = subprocess.Popen(command, env=env, cwd=str(Path(program).parent)
                            if Path(program).exists() else None,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    report = Report(program=program, pid=None, code_type=None,
                    seconds=seconds)
    sample_file = Path(f"/tmp/porte-fex-profile-{proc.pid}.txt")
    try:
        time.sleep(warmup)
        pid = _find_guest(program)
        report.pid = pid
        if pid is None:
            report.notes.append("the guest process was not found while it ran; "
                                "nothing was sampled")
            return report
        subprocess.run(["/usr/bin/sample", str(pid), str(int(round(seconds))),
                        "-mayDie", "-file", str(sample_file)],
                       capture_output=True)
        frames = parse_sample(sample_file)
        perf_map = Path(f"/tmp/perf-{pid}.map")
        if not perf_map.exists():
            report.notes.append(
                "FEX wrote no perf map, so host addresses cannot be mapped "
                "back to guest code; shares are not attribution")
        blocks = parse_perf_map(perf_map) if perf_map.exists() else []
        report.blocks = len([b for b in blocks if b.rva is not None])
        text = sample_file.read_text(errors="replace") if sample_file.exists() else ""
        match = re.search(r"^Code Type:\s+(.+)$", text, re.MULTILINE)
        report.code_type = match.group(1).strip() if match else None
        attribute(report, frames, blocks)
        if report.classes.get("unattributed"):
            report.notes.append(
                "unattributed frames are addresses with no perf-map block yet "
                "(or FEX helpers outside a block); they are not claimed")
        if report.sampled_addresses and not report.mapped_addresses:
            report.notes.append(
                f"the perf map named {report.blocks} guest blocks, but none of "
                f"the {report.sampled_addresses} sampled top-of-stack addresses "
                "fall inside a named range in this build: the hot code is not "
                "attributable with FEX's own naming, so no per-function claim "
                "is made")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        for path in (sample_file,):
            path.unlink(missing_ok=True)
    report.seconds = time.time() - started
    return report


def summarise(report: Report) -> str:
    total = max(1, report.total_samples)
    lines = [f"program: {report.program}",
             f"guest pid: {report.pid}  code type: {report.code_type}",
             f"translated blocks with a guest origin: {report.blocks}",
             f"samples: {report.total_samples} over {report.seconds:.1f}s", ""]
    for name in ("guest", "fex-runtime", "host", "waiting", "unattributed"):
        count = report.classes.get(name, 0)
        lines.append(f"  {name:14s} {count:7d}  {100.0 * count / total:5.1f}%")
    if report.functions:
        lines.append("")
        lines.append(f"  {'guest function':44s} {'samples':>8s} {'share':>7s}")
        for row in report.functions[:12]:
            label = f"{Path(row['module']).name}+{row['rva']}"
            lines.append(f"  {label:44s} {row['samples']:8d} "
                         f"{100.0 * row['share']:6.1f}%")
    for note in report.notes:
        lines.append(f"note: {note}")
    return "\n".join(lines)


def write(report: Report, path: Path | None = None) -> Path:
    target = path or util.RESULTS / "arm64_wine_profile.json"
    util.write_json(target, report.to_dict())
    return target
