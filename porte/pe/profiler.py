"""Execution profiling and loop discovery for opaque PE images.

Inputs are a PE image and an execution. No symbol names, no exports, no source,
no fixture knowledge. A loop is a hot basic block with a control-flow edge back
to itself or to an earlier block, ranked by dynamic instruction count.

The second pass exists because a translated loop needs to know *where* its
memory accesses land. It re-runs the program with a code hook over the chosen
loop and records, per memory-access instruction, which mapped region the
effective address fell in and over what offset window. That is the observation
the code generator is allowed to exploit.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from capstone import CS_ARCH_X86, CS_MODE_64, Cs
from capstone.x86 import X86_OP_IMM, X86_OP_MEM, X86_OP_REG
from unicorn import UC_HOOK_CODE
from unicorn.x86_const import UC_X86_REG_RIP

from .image import PEImage
from .machine import GPR_INDEX, GPR_NAMES, Machine

MAX_LOOP_BYTES = 1024
MAX_SITE_SAMPLES = 4096


@dataclass
class Candidate:
    head: int
    back_edge: int
    end: int
    iterations: int
    instructions: list = field(default_factory=list)
    exit_targets: list[int] = field(default_factory=list)
    bytes_sha256: str = ""
    size: int = 0
    dynamic_instructions: int = 0
    block_starts: list[int] = field(default_factory=list)
    back_edge_insn: int = 0

    def to_dict(self) -> dict:
        return {
            "head": hex(self.head),
            "back_edge": hex(self.back_edge),
            "end": hex(self.end),
            "observed_iterations": self.iterations,
            "instructions": len(self.instructions),
            "dynamic_instructions": self.dynamic_instructions,
            "size": self.size,
            "bytes_sha256": self.bytes_sha256,
            "exit_targets": [hex(t) for t in self.exit_targets],
            "blocks": [hex(b) for b in self.block_starts],
        }


@dataclass
class SiteProfile:
    """What one memory-access instruction did while the loop ran."""

    address: int
    size: int
    regions: list[int] = field(default_factory=list)
    min_offset: int | None = None
    max_offset: int | None = None
    samples: int = 0
    invariant_base: str | None = None

    def pinned_region(self) -> int | None:
        if self.samples and len(self.regions) == 1:
            return self.regions[0]
        return None

    def to_dict(self) -> dict:
        return {
            "address": hex(self.address),
            "size": self.size,
            "regions": self.regions,
            "min_offset": self.min_offset,
            "max_offset": self.max_offset,
            "samples": self.samples,
            "pinned_region": self.pinned_region(),
            "invariant_base": self.invariant_base,
        }


def disassemble_image(image: PEImage, base: int) -> tuple[list, dict[int, object]]:
    text = image.section_for_rva(image.entry_rva) or image.sections[0]
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    md.detail = True
    raw = image.read_rva(text.va, min(text.raw_size, len(image.data) - text.raw_ptr))
    insns = list(md.disasm(raw, base + text.va))
    return insns, {i.address: i for i in insns}


@dataclass
class BasicBlock:
    start: int
    insns: list
    terminator: object
    succ: list[int]

    @property
    def end(self) -> int:
        last = self.insns[-1]
        return last.address + last.size


def build_cfg(insns: list) -> dict[int, BasicBlock]:
    """Split .text into basic blocks and compute successors."""
    by_addr = {i.address: i for i in insns}
    leaders = {insns[0].address}
    for insn in insns:
        m = insn.mnemonic
        if m.startswith("j") or m in ("call", "ret", "retq"):
            nxt = insn.address + insn.size
            if nxt in by_addr:
                leaders.add(nxt)
            target = _branch_target(insn)
            if target is not None and target in by_addr:
                leaders.add(target)
    blocks: dict[int, BasicBlock] = {}
    current: list = []
    for insn in insns:
        if insn.address in leaders and current:
            _close_block(blocks, current, by_addr)
            current = []
        current.append(insn)
    if current:
        _close_block(blocks, current, by_addr)
    return blocks


def _close_block(blocks: dict[int, BasicBlock], insns: list,
                 by_addr: dict[int, object]) -> None:
    term = insns[-1]
    m = term.mnemonic
    nxt = term.address + term.size
    succ: list[int] = []
    if m.startswith("j") and m != "jmp":
        target = _branch_target(term)
        if target is not None and target in by_addr:
            succ.append(target)
        if nxt in by_addr:
            succ.append(nxt)
    elif m == "jmp":
        target = _branch_target(term)
        if target is not None and target in by_addr:
            succ.append(target)
        elif nxt in by_addr:
            succ.append(nxt)  # indirect jump: treat as fallthrough for CFG shape
    elif m in ("ret", "retq"):
        pass
    else:
        if nxt in by_addr:
            succ.append(nxt)
    blocks[insns[0].address] = BasicBlock(insns[0].address, insns, term, succ)


def dominators(blocks: dict[int, BasicBlock]) -> dict[int, set[int]]:
    if not blocks:
        return {}
    entry = min(blocks)
    reachable = _reachable(blocks, entry)
    preds: dict[int, set[int]] = {a: set() for a in blocks}
    for a in blocks:
        for s in blocks[a].succ:
            if s in preds:
                preds[s].add(a)
    dom: dict[int, set[int]] = {}
    for a in blocks:
        dom[a] = {entry} if a == entry else set(reachable)
    changed = True
    while changed:
        changed = False
        for a in reachable:
            if a == entry:
                continue
            pred = preds[a] & reachable
            if not pred:
                new = {a}
            else:
                new = {a} | set.intersection(*(dom[p] for p in pred))
            if new != dom[a]:
                dom[a] = new
                changed = True
    return dom


def _reachable(blocks: dict[int, BasicBlock], entry: int) -> set[int]:
    seen = {entry}
    stack = [entry]
    while stack:
        a = stack.pop()
        for s in blocks[a].succ:
            if s in blocks and s not in seen:
                seen.add(s)
                stack.append(s)
    return seen


def loop_body(blocks: dict[int, BasicBlock], dom: dict[int, set[int]],
              head: int) -> set[int]:
    """Blocks dominated by the header that can leave it again.

    A loop can have several backedges to the same header (an if-then back
    edge and a longer stride path, for example). Taking the union of natural
    loops - or only one backedge's natural loop - misses part of the cycle and
    produces a trace that exits on the first arm. This formulation keeps every
    block the header dominates that is reachable from the header and can reach
    the header again inside that region, which is exactly the loop's body.
    """
    dominated = {b for b in blocks if head in dom.get(b, set())}
    edges = {b: [s for s in blocks[b].succ if s in dominated] for b in dominated}
    forward = _closure(edges, head)
    reverse_edges: dict[int, list[int]] = {b: [] for b in dominated}
    for a, succs in edges.items():
        for s in succs:
            reverse_edges[s].append(a)
    back = _closure(reverse_edges, head)
    return (forward & back) | {head}


def _closure(edges: dict[int, list[int]], start: int) -> set[int]:
    seen = {start}
    stack = [start]
    while stack:
        a = stack.pop()
        for s in edges.get(a, []):
            if s not in seen:
                seen.add(s)
                stack.append(s)
    return seen


def find_loop(machine: Machine, counts: dict[int, int]) -> Candidate | None:
    """Pick the hottest natural loop by dynamic instruction count.

    Back edges come from the static CFG (target dominates source), so the body
    is a real loop, not a scan of the instruction stream. Loops are ranked by
    executed instructions, which is what makes the choice about cost rather
    than about how many times one block happened to run.
    """
    if not counts:
        return None
    insns, _ = disassemble_image(machine.image, machine.base)
    if not insns:
        return None
    blocks = build_cfg(insns)
    dom = dominators(blocks)
    headers: set[int] = set()
    for tail_addr, block in blocks.items():
        for target in block.succ:
            if target in dom.get(tail_addr, set()):
                headers.add(target)

    best: Candidate | None = None
    best_weight = 0
    for head in sorted(headers):
        body = loop_body(blocks, dom, head)
        body_insns = [i for a in sorted(body) for i in blocks[a].insns]
        size = sum(i.size for i in body_insns)
        if size > MAX_LOOP_BYTES:
            continue
        if any(i.mnemonic in ("call", "ret", "retq") for i in body_insns):
            continue
        if any(i.mnemonic == "jmp" and _is_indirect(i) for i in body_insns):
            continue
        # An interpreter pays per basic block (dispatch, translation-unit
        # lookup, exits), so executed block count is the cost proxy that
        # decides which loop is worth nativeizing.
        executed = sum(counts.get(a, 0) for a in body)
        if executed <= best_weight:
            continue
        exits: list[int] = []
        for a in sorted(body):
            for s in blocks[a].succ:
                if s not in body:
                    exits.append(s)
        end = max(blocks[a].end for a in body)
        back_edges = [a for a in body
                      for s in blocks[a].succ if s == head]
        raw = b"".join(machine.read(i.address, i.size) for i in body_insns)
        best_weight = executed
        best = Candidate(
            head=head, back_edge=back_edges[0] if back_edges else head, end=end,
            iterations=counts.get(head, 0), instructions=body_insns,
            exit_targets=sorted(set(exits)),
            bytes_sha256=hashlib.sha256(raw).hexdigest(), size=size,
            dynamic_instructions=sum(counts.get(a, 0) * len(blocks[a].insns)
                                     for a in body),
            block_starts=sorted(body),
            back_edge_insn=(blocks[back_edges[0]].terminator.address
                            if back_edges else head))
    return best


def _branch_target(insn) -> int | None:
    for op in insn.operands:
        if op.type == X86_OP_IMM:
            return int(op.imm)
        if op.type == X86_OP_MEM:
            return None
    return None


def _is_indirect(insn) -> bool:
    return bool(insn.operands) and insn.operands[0].type != X86_OP_IMM


def _written_registers(insns: list) -> set[str]:
    out: set[str] = set()
    for insn in insns:
        if not insn.operands:
            continue
        op = insn.operands[0]
        if op.type == X86_OP_REG and insn.mnemonic not in ("cmp", "test", "push"):
            out.add(insn.reg_name(op.reg))
        elif op.type == X86_OP_MEM and len(insn.operands) > 1:
            # A memory destination is not a register write.
            pass
    return out


def observe_sites(pe_path: Path, candidate: Candidate,
                  args: list[str] | None) -> dict[int, SiteProfile]:
    """Second pass, fresh machine: which region does each access land in?"""
    machine = Machine(Path(pe_path))
    sites: dict[int, SiteProfile] = {}
    by_addr = {i.address: i for i in candidate.instructions}
    for insn in candidate.instructions:
        for op in insn.operands:
            if op.type == X86_OP_MEM:
                size = getattr(op, "size", 0) or 8
                sites[insn.address] = SiteProfile(address=insn.address, size=size)
                break
    if not sites:
        return {}
    invariant = _written_registers(candidate.instructions)
    for insn in candidate.instructions:
        site = sites.get(insn.address)
        if site is None:
            continue
        op = next(o for o in insn.operands if o.type == X86_OP_MEM)
        base_name = None
        if op.mem.base:
            base_name = insn.reg_name(op.mem.base)
        if base_name and base_name not in invariant and base_name != "rip":
            site.invariant_base = base_name

    def hook(uc, address, _size, _user):
        site = sites.get(address)
        if site is None or site.samples >= MAX_SITE_SAMPLES:
            return
        op = next(o for o in by_addr[address].operands if o.type == X86_OP_MEM)
        ea = _effective_address(uc, by_addr[address], op)
        region_idx = None
        for i, region in enumerate(machine.native_regions()):
            if region.contains(ea, site.size):
                region_idx = i
                break
        if region_idx is None:
            return
        if region_idx not in site.regions:
            site.regions.append(region_idx)
        region = machine.native_regions()[region_idx]
        off = ea - region.guest
        site.min_offset = off if site.min_offset is None else min(site.min_offset, off)
        site.max_offset = off if site.max_offset is None else max(site.max_offset, off)
        site.samples += 1

    handle = machine.uc.hook_add(UC_HOOK_CODE, hook,
                                 begin=candidate.head, end=candidate.end - 1)
    try:
        machine.run_entry(args)
    finally:
        machine.uc.hook_del(handle)
    return sites


def _effective_address(uc, insn, op) -> int:
    mem = op.mem
    ea = 0
    if mem.base:
        name = insn.reg_name(mem.base)
        if name == "rip":
            ea += insn.address + insn.size
        else:
            ea += uc.reg_read(reg_id(name))
    if mem.index:
        ea += uc.reg_read(reg_id(insn.reg_name(mem.index))) * mem.scale
    return (ea + mem.disp) & 0xFFFFFFFFFFFFFFFF


def reg_id(name: str) -> int:
    from unicorn import x86_const
    return getattr(x86_const, "UC_X86_REG_" + GPR_NAMES[GPR_INDEX[name]].upper())


def profile_program(pe_path: Path, args: list[str] | None = None,
                    observe: bool = True) -> dict:
    """Profile one program from its entry point, in fresh processes-of-record.

    Every pass gets a new Machine: this Unicorn build does not honour a hook
    added after the emulator has already been started once, and a fresh machine
    also guarantees the observation pass sees the same initial state.
    """
    machine = Machine(Path(pe_path))
    result = machine.run_entry(args, profile_blocks=True)
    candidate = find_loop(machine, result.blocks)
    doc = {
        "image": machine.image.path.name,
        "args": list(args or []),
        "fault": result.fault,
        "exit_code": result.exit_code,
        "writes": result.writes.decode("utf-8", "replace"),
        "blocks": len(result.blocks),
        "dynamic_blocks": sum(result.blocks.values()),
        "candidate": candidate,
        "sites": {},
        "wall_ns": result.wall_ns,
    }
    if candidate is not None and observe:
        doc["sites"] = observe_sites(Path(pe_path), candidate, args)
    return doc


def profile_entry(machine: Machine, args: list[str] | None = None,
                  observe: bool = True) -> dict:
    """Profile an already-constructed machine (single pass, no observation)."""
    result = machine.run_entry(args, profile_blocks=True)
    candidate = find_loop(machine, result.blocks)
    return {
        "image": machine.image.path.name,
        "args": list(args or []),
        "fault": result.fault,
        "exit_code": result.exit_code,
        "writes": result.writes.decode("utf-8", "replace"),
        "blocks": len(result.blocks),
        "dynamic_blocks": sum(result.blocks.values()),
        "candidate": candidate,
        "sites": {},
        "wall_ns": result.wall_ns,
    }


def facts_of(doc: dict) -> dict:
    """Stable, run-independent facts for the cache key and the codegen."""
    candidate = doc.get("candidate")
    if candidate is None:
        return {"eligible": False, "reason": "no loop found"}
    sites = {str(a): s.to_dict() for a, s in sorted(doc.get("sites", {}).items())}
    return {
        "eligible": True,
        "head": candidate.head,
        "end": candidate.end,
        "static_instructions": len(candidate.instructions),
        "observed_iterations": candidate.iterations,
        "bytes_sha256": candidate.bytes_sha256,
        "sites": sites,
    }
