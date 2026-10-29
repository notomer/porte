"""Mach-O inspection helpers built on the platform's objdump/nm/atos."""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import util

_INSN_RE = re.compile(r"^\s*([0-9a-f]+):\s+(.*)$")
_TERMINATORS = ("jmp", "ret", "retq", "b", "br", "blr", "cbz", "cbnz", "tbz",
                "tbnz", "ud2", "int3", "hlt", "svc")


def arch_of(path: Path) -> str:
    out = util.run(["file", "-b", str(path)], check=False).stdout
    if "arm64" in out:
        return "arm64"
    if "x86_64" in out:
        return "x86_64"
    return "unknown"


def symbols(path: Path) -> list[dict]:
    """Function symbols, sorted by address."""
    out = util.run(["nm", "-n", str(path)], check=False).stdout
    syms = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        addr_s, kind, name = parts[0], parts[1], parts[2]
        if kind.lower() not in ("t",):
            continue
        try:
            addr = int(addr_s, 16)
        except ValueError:
            continue
        syms.append({"addr": addr, "name": name.lstrip("_")})
    syms.sort(key=lambda s: s["addr"])
    return syms


@dataclass
class Insn:
    addr: int
    text: str


@dataclass
class Block:
    start: int
    end: int
    insns: list[Insn] = field(default_factory=list)
    func: str = "<unknown>"
    ends_indirect: bool = False

    @property
    def ninstr(self) -> int:
        return len(self.insns)


def disassemble(path: Path) -> list[Insn]:
    out = util.run(["objdump", "-d", "--no-show-raw-insn", str(path)],
                   check=False).stdout
    insns = []
    for line in out.splitlines():
        m = _INSN_RE.match(line)
        if not m:
            continue
        try:
            addr = int(m.group(1), 16)
        except ValueError:
            continue
        insns.append(Insn(addr, m.group(2).strip()))
    insns.sort(key=lambda i: i.addr)
    return insns


def text_segment(path: Path) -> tuple[int, int]:
    """Preferred vmaddr and vmsize of __TEXT, from otool -l."""
    out = util.run(["otool", "-l", str(path)], check=False).stdout
    in_text = False
    vmaddr = vmsize = 0
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("segname "):
            in_text = s.split()[-1] == "__TEXT"
        elif in_text and s.startswith("vmaddr "):
            vmaddr = int(s.split()[1], 16)
        elif in_text and s.startswith("vmsize "):
            vmsize = int(s.split()[1], 16)
        elif s == ")" and in_text and vmsize:
            break
    return vmaddr, vmsize


def text_range(path: Path) -> tuple[int, int]:
    insns = disassemble(path)
    if not insns:
        return (0, 0)
    return (insns[0].addr, insns[-1].addr + 16)


def _is_branch(text: str) -> bool:
    op = text.split()[0] if text.split() else ""
    return op.startswith("j") or op in _TERMINATORS


def _is_indirect(text: str) -> bool:
    op = text.split()[0] if text.split() else ""
    if op in ("callq", "call", "bl", "blr"):
        return "(" in text or "*" in text
    if op in ("jmpq", "jmp", "br", "ret", "retq"):
        return True
    return False


def _branch_targets(text: str) -> list[int]:
    targets = []
    for tok in re.findall(r"0x([0-9a-f]+)", text):
        try:
            targets.append(int(tok, 16))
        except ValueError:
            pass
    return targets


def blocks(path: Path) -> list[Block]:
    """Recover basic blocks from the disassembly and function symbols."""
    insns = disassemble(path)
    if not insns:
        return []
    syms = symbols(path)
    sym_addrs = [s["addr"] for s in syms]

    def func_for(addr: int) -> str:
        i = bisect.bisect_right(sym_addrs, addr) - 1
        return syms[i]["name"] if i >= 0 else "<unknown>"

    leaders: set[int] = set()
    # function entries
    for s in syms:
        leaders.add(s["addr"])
    # branch targets and fallthrough after a terminator
    for idx, insn in enumerate(insns):
        if _is_branch(insn.text):
            for t in _branch_targets(insn.text):
                leaders.add(t)
            if idx + 1 < len(insns):
                leaders.add(insns[idx + 1].addr)

    ordered = sorted(leaders)
    starts = [insn.addr for insn in insns]
    out: list[Block] = []
    for leader in ordered:
        pos = bisect.bisect_left(starts, leader)
        if pos >= len(insns) or insns[pos].addr != leader:
            continue  # leader is not an instruction boundary
        blk = Block(start=leader, end=leader, func=func_for(leader))
        j = pos
        while j < len(insns):
            insn = insns[j]
            blk.insns.append(insn)
            blk.end = insn.addr
            j += 1
            if j < len(insns) and insns[j].addr in leaders:
                break
            if _is_branch(insn.text):
                break
        if blk.insns:
            blk.ends_indirect = _is_indirect(blk.insns[-1].text)
            out.append(blk)
    return out


def symbolize(path: Path, addrs: list[int]) -> list[str]:
    """Resolve static addresses to function symbols via atos."""
    if not addrs:
        return []
    arch = arch_of(path)
    names: list[str] = []
    chunk = 512
    for i in range(0, len(addrs), chunk):
        part = addrs[i:i + chunk]
        proc = util.run(["atos", "-arch", arch, "-o", str(path)]
                        + [hex(a) for a in part], check=False)
        lines = proc.stdout.splitlines()
        if len(lines) != len(part):
            lines = (lines + ["<unknown>"] * len(part))[:len(part)]
        names.extend(line.strip() or "<unknown>" for line in lines)
    return names


def attribute_to_blocks(path: Path, addrs: list[int], blks: list[Block]) -> dict:
    """Count samples per block/function, discarding addresses outside .text."""
    starts = [b.start for b in blks]
    per_func: dict[str, int] = {}
    per_block: dict[int, int] = {}
    inside = outside = 0
    for a in addrs:
        i = bisect.bisect_right(starts, a) - 1
        if i >= 0 and blks[i].start <= a <= blks[i].end:
            inside += 1
            key = blks[i].start
            per_block[key] = per_block.get(key, 0) + 1
            per_func[blks[i].func] = per_func.get(blks[i].func, 0) + 1
        else:
            outside += 1
    return {"inside": inside, "outside": outside,
            "per_func": per_func, "per_block": per_block}
