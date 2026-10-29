"""Guarded dispatch between the emulator and a compiled ARM64 trace.

The dispatcher is invoked from a Unicorn block hook at the trace head. It
synchronizes the architectural state into the native ABI, calls the trace, and
writes the result back. If the trace bails (a memory access left the mapped
region), the RIP it returns is the exact instruction to resume at.
"""
from __future__ import annotations

import ctypes
from pathlib import Path

from unicorn.x86_const import UC_X86_REG_EFLAGS, UC_X86_REG_RIP

from .machine import GPR_IDS, Machine

MASK64 = (1 << 64) - 1


class PorteRegion(ctypes.Structure):
    _fields_ = [("host", ctypes.c_void_p), ("guest", ctypes.c_uint64),
                ("size", ctypes.c_uint64)]


class PorteMem(ctypes.Structure):
    _fields_ = [("region", PorteRegion * 4), ("count", ctypes.c_uint64)]


class PorteCpu(ctypes.Structure):
    _fields_ = [("gpr", ctypes.c_uint64 * 16), ("rip", ctypes.c_uint64),
                ("rflags", ctypes.c_uint64)]


class TraceDispatcher:
    def __init__(self, dylib: Path, head: int,
                 expected_regions: list[tuple[int, int]] | None = None):
        self.head = head
        self.lib = ctypes.CDLL(str(dylib))
        self.fn = self.lib.porte_trace
        self.fn.argtypes = [ctypes.POINTER(PorteCpu), ctypes.POINTER(PorteMem),
                            ctypes.POINTER(ctypes.c_uint64)]
        self.fn.restype = ctypes.c_uint64
        self.expected_regions = [(int(a), int(b)) for a, b in (expected_regions or [])]
        self.dispatches = 0
        self.bails = 0
        self.iterations = 0
        self.ok = 0
        self.demotions = 0
        self.region_mismatch = False

    def _memory(self, machine: Machine) -> PorteMem:
        mem = PorteMem()
        mem.count = 0
        for region in machine.regions:
            if region.host_ptr is None:
                continue
            if mem.count >= 4:
                break
            r = mem.region[mem.count]
            r.host = region.host_ptr
            r.guest = region.guest
            r.size = region.size
            mem.count += 1
        return mem

    def dispatch(self, machine: Machine) -> int | None:
        regions = [(r.guest, r.size) for r in machine.native_regions()]
        if self.expected_regions and regions != self.expected_regions:
            # The artifact's pinned region indices were derived on a different
            # layout. Do not guess: drop back to interpretation for good.
            self.region_mismatch = True
            self.demotions += 1
            machine.demote()
            return None
        cpu = PorteCpu()
        for i, reg in enumerate(GPR_IDS):
            cpu.gpr[i] = machine.uc.reg_read(reg) & MASK64
        cpu.rflags = machine.uc.reg_read(UC_X86_REG_EFLAGS) & MASK64
        cpu.rip = machine.uc.reg_read(UC_X86_REG_RIP) & MASK64
        mem = self._memory(machine)
        iters = ctypes.c_uint64(0)
        status = self.fn(ctypes.byref(cpu), ctypes.byref(mem), ctypes.byref(iters))
        for i, reg in enumerate(GPR_IDS):
            machine.uc.reg_write(reg, int(cpu.gpr[i]) & MASK64)
        machine.uc.reg_write(UC_X86_REG_EFLAGS, int(cpu.rflags) & MASK64)
        machine.uc.reg_write(UC_X86_REG_RIP, int(cpu.rip) & MASK64)
        self.dispatches += 1
        self.iterations += int(iters.value)
        if status == 0:
            self.ok += 1
        else:
            self.bails += 1
        return int(cpu.rip) & MASK64

    def stats(self) -> dict:
        return {"dispatches": self.dispatches, "ok": self.ok,
                "bails": self.bails, "iterations": self.iterations,
                "demotions": self.demotions,
                "region_mismatch": self.region_mismatch}
