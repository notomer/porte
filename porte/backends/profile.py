"""Where a Windows program's time actually goes, measured on this machine.

A Windows program started by the Wine backend is an x86-64 Mach-O process that
macOS runs under Rosetta. `/usr/bin/sample` can therefore see it, and the
question this module answers is the one a nativeization strategy depends on:
how much of the run is the guest's own code, how much is the compatibility
runtime, and how much is the host.

The answer is deliberately reported with its own limit. Rosetta's translated
code lives in an anonymous JIT region: the sampler names it "Rosetta JIT" and
gives a JIT address, not the address the instruction came from. Wine's own
Win32 modules are translated the same way, so translated-code samples mix the
application with the runtime's implementation of Windows and cannot be split
further by public APIs. A profiler that claims per-function attribution inside
a Rosetta process is guessing.

Nothing here is inferred from symbols: the classifier works on the image names
the sampler itself reports.
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import util

#: Images that mean "code Rosetta translated from the guest's x86-64".
TRANSLATED_IMAGES = ("Rosetta JIT",)
#: Rosetta's own helper routines, called out of translated code: this is the
#: translation overhead itself, not the guest's work.
ROSETTA_RUNTIME_IMAGES = ("Rosetta Runtime Routines",)
#: Frames the sampler could not symbolize. These are address-space ranges with
#: no image record (system helpers, mostly `mach_msg` waits), so claiming them
#: as guest code would inflate the guest share.
UNATTRIBUTED_IMAGES = ("<unknown binary>", "???", "<unknown>")

#: Native system images, i.e. the macOS side of the process.
HOST_IMAGE_HINTS = ("libsystem", "libdyld", "dyld", "libdispatch",
                    "CoreFoundation", "Foundation", "AppKit", "libobjc",
                    "libc++", "libc++abi", "libxpc", "libmacho", "Security",
                    "Metal", "IOKit", "libcompiler_rt", "libsystem_platform")

TOP_FRAME_RE = re.compile(
    r"^\s+(?:\?\?\?|<unknown>)?\s*\(in (?P<image>[^)]+)\)"
    r"(?:\s+load address 0x[0-9a-f]+ \+ 0x[0-9a-f]+)?"
    r"\s+\[(?P<addr>0x[0-9a-f]+)\]\s+(?P<count>\d+)\s*$")
SAMPLE_HEADER_RE = re.compile(r"^Sampling .* pid (?P<pid>\d+)|^Analysis of sampling")
CODE_TYPE_RE = re.compile(r"^Code Type:\s+(?P<type>.+)$")
IMAGE_LINE_RE = re.compile(r"^\s+(?P<name>\S.*?)\s+\[(?P<addr>0x[0-9a-f]+)"
                           r"(?:,\s*0x[0-9a-f]+)?\]\s+(?P<path>\S.*)$")


@dataclass
class TopFrame:
    image: str
    address: str
    count: int


@dataclass
class ProfileReport:
    program: str
    pid: int | None
    code_type: str | None
    seconds: float
    total_samples: int
    classes: dict[str, int] = field(default_factory=dict)
    top_frames: list[TopFrame] = field(default_factory=list)
    guest_images: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def shares(self) -> dict[str, float]:
        total = self.total_samples or 1
        return {name: count / total for name, count in self.classes.items()}

    def to_dict(self) -> dict:
        return {
            "program": self.program,
            "pid": self.pid,
            "code_type": self.code_type,
            "seconds": self.seconds,
            "total_samples": self.total_samples,
            "classes": self.classes,
            "shares": {k: round(v, 4) for k, v in self.shares.items()},
            "top_frames": [{"image": f.image, "address": f.address,
                            "count": f.count} for f in self.top_frames],
            "guest_images": self.guest_images,
            "notes": self.notes,
        }


def classify_image(image: str, wine_root: Path | None = None) -> str:
    """Which layer of the process a sampled frame belongs to."""
    lowered = image.lower()
    if image in TRANSLATED_IMAGES:
        return "guest-translated"
    if image in ROSETTA_RUNTIME_IMAGES:
        return "rosetta-runtime"
    if image in UNATTRIBUTED_IMAGES:
        return "unattributed"
    if lowered.endswith(".so") or "wine" in lowered:
        return "compat-runtime"
    if any(hint.lower() in lowered for hint in HOST_IMAGE_HINTS):
        return "host"
    return "other"


def parse_sample(text: str, wine_root: Path | None = None) -> ProfileReport:
    """Turn `sample` output into a layer breakdown."""
    code_type = None
    frames: list[TopFrame] = []
    images: list[str] = []
    for line in text.splitlines():
        match = CODE_TYPE_RE.match(line)
        if match:
            code_type = match.group("type").strip()
            continue
        if "Binary Images:" in line:
            continue
        top = TOP_FRAME_RE.match(line)
        if top and top.group("count"):
            frames.append(TopFrame(image=top.group("image").strip(),
                                   address=top.group("addr"),
                                   count=int(top.group("count"))))
            continue
        image_line = IMAGE_LINE_RE.match(line)
        if image_line:
            images.append(image_line.group("path").strip())

    classes: dict[str, int] = {}
    for frame in frames:
        layer = classify_image(frame.image, wine_root)
        classes[layer] = classes.get(layer, 0) + frame.count
    report = ProfileReport(program="", pid=None, code_type=code_type,
                           seconds=0.0, total_samples=sum(classes.values()),
                           classes=classes,
                           top_frames=sorted(frames, key=lambda f: -f.count)[:12],
                           guest_images=_named_images(images, wine_root))
    if report.total_samples:
        report.notes.append(
            "guest-translated mixes the guest program with Wine's own Win32 "
            "modules: Rosetta reports JIT addresses, not the instruction's "
            "original address, so that split cannot be recovered from public "
            "APIs.")
        report.notes.append(
            "unattributed frames are system ranges the sampler could not "
            "symbolize; a thread waiting on a Wine event object lands there, "
            "so it is not compute time.")
    return report


def _named_images(paths: list[str], wine_root: Path | None) -> list[str]:
    found = []
    for path in paths:
        lowered = path.lower()
        if lowered.endswith((".exe", ".dll", ".drv")):
            found.append(path)
    return found


def profile_program(executable: str, *, arguments: list[str] | None = None,
                    seconds: int = 5, prefix: Path | None = None,
                    wine_root: Path | None = None) -> ProfileReport:
    """Run a Windows program under Wine and sample it while it works."""
    from . import wine as wine_backend

    runtime = wine_backend.select()
    if prefix is None:
        raise ValueError("a prepared prefix is required")
    command = [str(runtime.wine), executable] + [str(a) for a in (arguments or [])]
    env = wine_backend.clean_env(prefix)
    proc = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    started = time.time()
    try:
        # Wine needs a moment before the guest's own process exists; the pid
        # Popen returns is the launcher-shaped `wine` process, which is the
        # one that gets translated and sampled.
        time.sleep(1.5)
        out = Path(util.BUILD) / "profile" / f"sample-{proc.pid}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["/usr/bin/sample", str(proc.pid), str(seconds),
                        "-file", str(out)], capture_output=True, timeout=seconds + 60)
        text = out.read_text("utf-8", "replace") if out.exists() else ""
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
        wine_backend.terminate_prefix(runtime, prefix)
    report = parse_sample(text, wine_root=wine_root)
    report.program = executable
    report.pid = proc.pid
    report.seconds = round(time.time() - started, 2)
    if not report.total_samples:
        report.notes.append("the sampler collected no stacks; the program may "
                            "have finished before the sample window opened")
    return report


def summarise(report: ProfileReport) -> str:
    lines = [f"{Path(report.program).name}: {report.total_samples} samples "
             f"over {report.seconds:g}s"]
    if report.code_type:
        lines.append(f"  code type: {report.code_type}")
    for layer, share in sorted(report.shares.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {layer:>18}: {share * 100:5.1f}%")
    if report.top_frames:
        lines.append("  hottest sampled stacks:")
        for frame in report.top_frames[:5]:
            lines.append(f"    {frame.image} {frame.address} ({frame.count})")
    for note in report.notes:
        lines.append(f"  note: {note}")
    return "\n".join(lines)
