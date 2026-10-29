"""A minimal Windows x86-64 user-mode machine on Unicorn.

Scope is deliberately small: map a PE32+ image, apply base relocations when it
is not loaded at its preferred base, resolve imports to host stubs, run an
exported function with the Windows x64 calling convention, and record enough
behaviour to compare executions. No full Win32, no SEH, no threads.
"""
from __future__ import annotations

import ctypes
import hashlib
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

from unicorn import (UC_ARCH_X86, UC_HOOK_BLOCK, UC_HOOK_CODE, UC_MODE_64,
                     UC_PROT_ALL, Uc, UcError)
from unicorn.x86_const import (
    UC_X86_REG_EFLAGS, UC_X86_REG_R10, UC_X86_REG_R11, UC_X86_REG_R12,
    UC_X86_REG_R13, UC_X86_REG_R14, UC_X86_REG_R15, UC_X86_REG_R8,
    UC_X86_REG_R9, UC_X86_REG_RAX, UC_X86_REG_RBP, UC_X86_REG_RBX,
    UC_X86_REG_RCX, UC_X86_REG_RDI, UC_X86_REG_RDX, UC_X86_REG_RIP,
    UC_X86_REG_RSI, UC_X86_REG_RSP)

from .image import PAGE, PEImage, align_up

GPR_NAMES = ["rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi",
             "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"]
GPR_IDS = [UC_X86_REG_RAX, UC_X86_REG_RCX, UC_X86_REG_RDX, UC_X86_REG_RBX,
           UC_X86_REG_RSP, UC_X86_REG_RBP, UC_X86_REG_RSI, UC_X86_REG_RDI,
           UC_X86_REG_R8, UC_X86_REG_R9, UC_X86_REG_R10, UC_X86_REG_R11,
           UC_X86_REG_R12, UC_X86_REG_R13, UC_X86_REG_R14, UC_X86_REG_R15]
GPR_INDEX = {name: i for i, name in enumerate(GPR_NAMES)}

IMAGE_BASE = 0x140000000
STACK_BASE = 0x20000000
STACK_SIZE = 0x100000
HEAP_BASE = 0x10000000
HEAP_SIZE = 8 << 20
THUNK_BASE = 0x30000000
THUNK_PAGE = 0x1000
SENTINEL = 0x2FFFF000  # unmapped on purpose; also used as the stop address


class ProcessExit(Exception):
    def __init__(self, code: int):
        self.code = code


class UnsupportedImport(RuntimeError):
    pass


@dataclass
class Region:
    guest: int
    size: int
    host_ptr: int | None
    kind: str
    _buffer: object = None

    def contains(self, addr: int, size: int) -> bool:
        return self.guest <= addr and addr + size <= self.guest + self.size


@dataclass
class RunResult:
    ret: int
    rip: int
    exit_code: int | None
    fault: str | None
    api_log: list[dict]
    writes: bytes
    heap_digest: str
    out: bytes
    gprs: list[int]
    rflags: int
    image_digest: str = ""
    blocks: dict[int, int] = field(default_factory=dict)
    first_seen: list[int] = field(default_factory=list)
    wall_ns: int = 0
    dispatches: int = 0
    native_status: dict = field(default_factory=dict)


class Machine:
    def __init__(self, pe_path: Path, image_base: int | None = None,
                 heap_size: int = HEAP_SIZE):
        self.image = PEImage.load(Path(pe_path))
        self.base = image_base or self.image.image_base
        self.uc = Uc(UC_ARCH_X86, UC_MODE_64)
        self.regions: list[Region] = []
        self.thunks: dict[int, tuple[str, str | None, int | None]] = {}
        self.api_log: list[dict] = []
        self.writes = bytearray()
        self.clock = 1_700_000_000_000
        self.fault: str | None = None
        self.exit_code: int | None = None
        self._block_counts: dict[int, int] = {}
        self._first_seen: list[int] = []
        self._blocks_enabled = False
        self._pending_dispatch = False
        self._dispatcher = None
        self._heap = ctypes.create_string_buffer(heap_size)
        self._heap_size = heap_size
        self._heap_next = 0
        self._cmdline_ptr = 0
        self.dispatches = 0
        self._load_time_ns = 0

        self._map_image()
        self._map_stack()
        self._map_heap()
        self._resolve_imports()
        self._hook_imports()

    # ---- mapping ------------------------------------------------------
    def _map_image(self) -> None:
        """Map the image into a host buffer so native code can address it.

        Unicorn's own mappings have no address a generated trace could write
        through, and a trace that cannot touch the image cannot specialize a
        loop that reads a constant table or writes a static buffer. Mapping
        host memory with mem_map_ptr keeps guest and host views coherent, and
        the emulator still enforces every permission it did before.
        """
        img = self.image
        size = align_up(img.size_of_image, PAGE)
        self._image_buf = ctypes.create_string_buffer(size)
        host = ctypes.addressof(self._image_buf)
        header = img.data[:img.size_of_headers]
        ctypes.memmove(host, header, len(header))
        for s in img.sections:
            data = img.data[s.raw_ptr:s.raw_ptr + s.raw_size] if s.raw_size else b""
            if data:
                ctypes.memmove(host + s.va, data, len(data))
        self.uc.mem_map_ptr(self.base, size, UC_PROT_ALL, host)
        self.regions.append(Region(self.base, size, host, "image", self._image_buf))
        if self.base != img.image_base:
            self._apply_relocations(self.base - img.image_base)

    def _apply_relocations(self, delta: int) -> None:
        for rva, rtype in self.image.relocations:
            if rtype == 0:
                continue
            if rtype == 10:  # IMAGE_REL_BASED_DIR64
                addr = self.base + rva
                val = struct.unpack("<Q", self.uc.mem_read(addr, 8))[0]
                self.uc.mem_write(addr, struct.pack("<Q", (val + delta) & 0xFFFFFFFFFFFFFFFF))
            elif rtype == 3:  # IMAGE_REL_BASED_HIGHLOW
                addr = self.base + rva
                val = struct.unpack("<I", self.uc.mem_read(addr, 4))[0]
                self.uc.mem_write(addr, struct.pack("<I", (val + delta) & 0xFFFFFFFF))
            else:
                raise ValueError(f"unsupported relocation type {rtype}")

    def _map_stack(self) -> None:
        self.uc.mem_map(STACK_BASE, STACK_SIZE, UC_PROT_ALL)
        self.regions.append(Region(STACK_BASE, STACK_SIZE, None, "stack"))

    def _map_heap(self) -> None:
        host = ctypes.addressof(self._heap)
        self.uc.mem_map_ptr(HEAP_BASE, self._heap_size, UC_PROT_ALL, host)
        self.regions.append(Region(HEAP_BASE, self._heap_size, host, "heap",
                                   self._heap))

    def _resolve_imports(self, ) -> None:
        self.uc.mem_map(THUNK_BASE, THUNK_PAGE, UC_PROT_ALL)
        slot = THUNK_BASE
        for imp in self.image.imports:
            if imp.name is None:
                raise UnsupportedImport(
                    f"ordinal import from {imp.dll} is not supported")
            if not hasattr(self, f"api_{imp.name}"):
                raise UnsupportedImport(
                    f"no host stub for {imp.dll}!{imp.name}")
            self.uc.mem_write(slot, b"\xCC")
            self.thunks[slot] = (imp.dll, imp.name, imp.ordinal)
            iat_rva = imp.iat_rva
            if iat_rva >= self.image.image_base:
                # pefile reports import addresses as VAs, not RVAs.
                iat_rva -= self.image.image_base
            self.uc.mem_write(self.base + iat_rva, struct.pack("<Q", slot))
            slot += 16
        if slot > THUNK_BASE + THUNK_PAGE:
            raise ValueError("too many imports for the thunk page")

    def _hook_imports(self) -> None:
        import inspect

        self._stub_arity = {}
        for dll, name, _ in set(self.thunks.values()):
            fn = getattr(self, f"api_{name}", None)
            if fn is not None:
                self._stub_arity[name] = len(inspect.signature(fn).parameters)
        if not self.thunks:
            return
        self.uc.hook_add(UC_HOOK_CODE, self._on_import,
                         begin=THUNK_BASE, end=THUNK_BASE + THUNK_PAGE - 1)

    # ---- host stubs ---------------------------------------------------
    def _read_arg(self, index: int) -> int:
        if index < 4:
            return self.uc.reg_read(
                [UC_X86_REG_RCX, UC_X86_REG_RDX, UC_X86_REG_R8, UC_X86_REG_R9][index])
        rsp = self.uc.reg_read(UC_X86_REG_RSP)
        return struct.unpack("<Q", self.uc.mem_read(rsp + 0x28 + 8 * (index - 4), 8))[0]

    def _on_import(self, uc, address, size, user) -> None:
        dll, name, _ = self.thunks[address]
        arity = self._stub_arity.get(name, 0)
        args = [self._read_arg(i) for i in range(arity)]
        try:
            ret = getattr(self, f"api_{name}")(*args)
            if ret is None:
                ret = 0
        except ProcessExit as exc:
            self.exit_code = exc.code
            raise
        self.api_log.append({"call": name, "args": args, "ret": ret & 0xFFFFFFFFFFFFFFFF})
        uc.reg_write(UC_X86_REG_RAX, ret & 0xFFFFFFFFFFFFFFFF)
        rsp = uc.reg_read(UC_X86_REG_RSP)
        retaddr = struct.unpack("<Q", uc.mem_read(rsp, 8))[0]
        uc.reg_write(UC_X86_REG_RSP, rsp + 8)
        uc.reg_write(UC_X86_REG_RIP, retaddr)

    def api_GetCommandLineA(self) -> int:
        return self._cmdline_ptr

    def api_GetStdHandle(self, which: int) -> int:
        return {0xFFFFFFF5: 0, 0xFFFFFFF6: 2}.get(which & 0xFFFFFFFF, 1)

    def api_WriteFile(self, handle: int, buf: int, length: int,
                      written_ptr: int, overlapped: int) -> int:
        data = bytes(self.uc.mem_read(buf, length & 0xFFFFFFFF))
        self.writes += data
        if written_ptr:
            self.uc.mem_write(written_ptr, struct.pack("<I", length & 0xFFFFFFFF))
        return 1

    def api_ExitProcess(self, code: int) -> None:
        raise ProcessExit(code & 0xFFFFFFFF)

    def api_GetTickCount64(self) -> int:
        self.clock += 1
        return self.clock

    def api_QueryPerformanceCounter(self, ptr: int) -> int:
        self.clock += 1
        if ptr:
            self.uc.mem_write(ptr, struct.pack("<q", self.clock))
        return 1

    def api_GetLastError(self) -> int:
        return 0

    def api_SetLastError(self, err: int) -> int:
        return 0

    def api_VirtualAlloc(self, addr: int, size: int, alloc_type: int, protect: int) -> int:
        size = align_up(max(size, 1), PAGE)
        # Windows hands back zeroed pages for MEM_COMMIT.
        base = self.heap_alloc(size)
        self.uc.mem_write(base, b"\x00" * size)
        return base

    def api_VirtualFree(self, addr: int, size: int, free_type: int) -> int:
        return 1

    def api_FlushFileBuffers(self, handle: int) -> int:
        return 1

    # CRT routines a compiler may emit calls to inside an otherwise
    # freestanding image. They are ordinary host stubs, like the kernel32 ones.
    def api_memset(self, dst: int, value: int, size: int) -> int:
        self.uc.mem_write(dst, bytes([value & 0xFF]) * (size & 0xFFFFFFFF))
        return dst

    def api_memcpy(self, dst: int, src: int, size: int) -> int:
        self.uc.mem_write(dst, self.uc.mem_read(src, size & 0xFFFFFFFF))
        return dst

    def api_memmove(self, dst: int, src: int, size: int) -> int:
        return self.api_memcpy(dst, src, size)

    # ---- memory helpers ----------------------------------------------
    def heap_alloc(self, size: int) -> int:
        size = align_up(size, 16)
        if self._heap_next + size > self._heap_size:
            raise MemoryError("guest heap exhausted")
        addr = HEAP_BASE + self._heap_next
        self._heap_next += size
        return addr

    def read(self, addr: int, size: int) -> bytes:
        return bytes(self.uc.mem_read(addr, size))

    def write(self, addr: int, data: bytes) -> None:
        self.uc.mem_write(addr, data)

    def heap_digest(self, addr: int, size: int) -> str:
        return hashlib.sha256(self.read(addr, size)).hexdigest()

    def image_digest(self) -> str:
        """Digest of writable image sections: catches data side effects.

        Read-only sections (constant tables, code) are excluded so the digest
        stays cheap on images with large .rdata, and so two runs cannot differ
        by a constant that neither run can change.
        """
        h = hashlib.sha256()
        for s in self.image.sections:
            if not (s.characteristics & 0x80000000):
                continue
            if s.raw_size == 0 and s.vsize == 0:
                continue
            h.update(self.read(self.base + s.va, s.vsize))
        return h.hexdigest()

    def region_for(self, addr: int) -> Region | None:
        for r in self.regions:
            if r.guest <= addr < r.guest + r.size:
                return r
        return None

    def native_regions(self) -> list[Region]:
        """Regions with host backing, in the order native code sees them.

        The translator's region index for a memory site and the dispatcher's
        runtime region array must agree; both use this function.
        """
        return [r for r in self.regions if r.host_ptr is not None]

    # ---- execution ----------------------------------------------------
    def enable_block_profile(self) -> None:
        if self._blocks_enabled:
            return
        self._blocks_enabled = True
        self.uc.hook_add(UC_HOOK_BLOCK, self._on_block)

    def _on_block(self, uc, address, size, user) -> None:
        self._block_counts[address] = self._block_counts.get(address, 0) + 1
        if len(self._first_seen) < 4096 and address not in self._first_seen:
            self._first_seen.append(address)
        if self._dispatcher is not None and address == self._dispatcher.head:
            self._pending_dispatch = True
            uc.emu_stop()

    def set_dispatcher(self, dispatcher) -> None:
        """Install the native-trace dispatcher at one guest address.

        A hook on the loop head only, rather than block profiling on the whole
        image: a specialized run must not pay the profiler's per-block cost,
        otherwise the comparison against baseline execution is dishonest.
        """
        from unicorn import UC_HOOK_CODE

        self._dispatcher = dispatcher
        self._dispatcher_handle = self.uc.hook_add(
            UC_HOOK_CODE, self._on_dispatch_point,
            begin=dispatcher.head, end=dispatcher.head)

    def _on_dispatch_point(self, uc, address, size, user) -> None:
        self._pending_dispatch = True
        uc.emu_stop()

    def demote(self) -> None:
        """Give up on native dispatch: roll all future blocks back to the
        emulator. Used when an artifact's recorded region layout does not
        match this machine, where continuing would risk mis-addressing."""
        handle = getattr(self, "_dispatcher_handle", None)
        if handle is not None:
            try:
                self.uc.hook_del(handle)
            except Exception:
                pass
        self._dispatcher = None
        self._dispatcher_handle = None

    def _reset_call_state(self) -> None:
        self.api_log = []
        self.writes = bytearray()
        self.fault = None
        self.exit_code = None
        self._block_counts = {}
        self._first_seen = []

    def call_export(self, export: str, args: list[int],
                    profile_blocks: bool = False) -> RunResult:
        _, rva = self.image.export_code(export)
        entry = self.base + rva
        return self.call_address(entry, args, profile_blocks=profile_blocks)

    def run_entry(self, args: list[str] | None = None,
                  profile_blocks: bool = False) -> RunResult:
        """Run the image the way the loader would: from AddressOfEntryPoint.

        No symbol names, no export table, no knowledge of what the program
        computes. `args` become a Windows command line in guest memory; that is
        the only input channel this machine gives a program.
        """
        text = " ".join([self.image.path.name if hasattr(self.image, "path") else "image.exe",
                         *(args or [])])
        raw = text.encode("ascii") + b"\x00"
        self._cmdline_ptr = self.heap_alloc(len(raw) + 1)
        self.uc.mem_write(self._cmdline_ptr, raw)
        entry = self.base + self.image.entry_rva
        return self.call_address(entry, [], profile_blocks=profile_blocks)

    def call_address(self, entry: int, args: list[int],
                     profile_blocks: bool = False) -> RunResult:
        self._reset_call_state()
        if profile_blocks:
            self.enable_block_profile()
        stack_top = STACK_BASE + STACK_SIZE - 0x100
        stack_top -= stack_top % 16
        rsp = stack_top - 8
        self.uc.mem_write(rsp, struct.pack("<Q", SENTINEL))
        self.uc.reg_write(UC_X86_REG_RSP, rsp)
        self.uc.reg_write(UC_X86_REG_RAX, 0)
        for idx, reg in enumerate([UC_X86_REG_RCX, UC_X86_REG_RDX,
                                   UC_X86_REG_R8, UC_X86_REG_R9]):
            if idx < len(args):
                self.uc.reg_write(reg, args[idx] & 0xFFFFFFFFFFFFFFFF)
        self._pending_dispatch = False
        t0 = time.perf_counter_ns()
        rip = entry
        while True:
            try:
                self.uc.emu_start(rip, SENTINEL)
            except ProcessExit:
                break
            except UcError as exc:
                self.fault = str(exc)
                break
            if self._pending_dispatch:
                self._pending_dispatch = False
                self.dispatches += 1
                rip = self._dispatcher.dispatch(self)
                if rip is None:
                    rip = self.uc.reg_read(UC_X86_REG_RIP)
                continue
            break
        wall_ns = time.perf_counter_ns() - t0
        gprs = [self.uc.reg_read(r) for r in GPR_IDS]
        return RunResult(
            ret=gprs[0], rip=self.uc.reg_read(UC_X86_REG_RIP),
            exit_code=self.exit_code, fault=self.fault,
            api_log=list(self.api_log), writes=bytes(self.writes),
            heap_digest=hashlib.sha256(bytes(self._heap)).hexdigest(),
            image_digest=self.image_digest(),
            out=b"", gprs=gprs,
            rflags=self.uc.reg_read(UC_X86_REG_EFLAGS),
            blocks=dict(self._block_counts), first_seen=list(self._first_seen),
            wall_ns=wall_ns, dispatches=self.dispatches)

    # ---- convenience for the compute fixture -------------------------
    def call_compute(self, export: str, n: int, seed: int,
                     buf_offset: int = 0, profile_blocks: bool = False) -> dict:
        buf_size = max(n, 1) * 4 + 64
        buf = self.heap_alloc(buf_size + buf_offset) + buf_offset
        out = self.heap_alloc(8)
        self.uc.mem_write(buf, make_input(n, seed))
        self.uc.mem_write(out, b"\x00" * 8)
        res = self.call_export(export, [buf, n, out, seed],
                               profile_blocks=profile_blocks)
        res.out = self.read(out, 8)
        res.buf = buf  # type: ignore[attr-defined]
        res.buf_size = n * 4  # type: ignore[attr-defined]
        return {
            "result": res, "buf": buf, "out": out, "n": n, "seed": seed,
            "buf_digest": hashlib.sha256(self.read(buf, n * 4)).hexdigest(),
        }


def make_input(n: int, seed: int) -> bytes:
    """Deterministic guest input. Shared by every arm of the comparison."""
    out = bytearray(4 * n)
    x = seed & 0xFFFFFFFFFFFFFFFF
    for i in range(n):
        x = (x + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
        z = x
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
        z ^= z >> 31
        out[4 * i:4 * i + 4] = struct.pack("<I", z & 0xFFFFFFFF)
    return bytes(out)
