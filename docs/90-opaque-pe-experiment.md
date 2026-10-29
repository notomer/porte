# Porte Phase 2 — opaque Windows PE nativeization

This document reports what was actually built and measured after Phase 1. It is
written to be falsifiable: every number has a command, every claim names the
evidence, and the things that do not work are listed with the same care as the
things that do.

Host: Apple M2 Max, macOS 27.2 (26B123), 32 GB, clang 17 (Xcode), Unicorn 2.1.4,
Capstone 5.0.7, Python 3.14. Baselines are always Porte's own Windows PE
execution on Unicorn — the only Windows execution path on this machine. There is
no Wine, no FEX, and Rosetta cannot run a PE.

---

## 1. Audit of the Phase 1 claims

Phase 1 (source-level modules, `docs/02-experiment-report.md`) reported 14.55×
over the fairest native generic build. What that actually was:

| Question | Answer |
| --- | --- |
| Where does specialization behaviour come from? | A named kernel (`porte_kernel_rank`, `porte_kernel_audit`) in a module the harness knows, plus profile facts (value window, host-call counts). The replacement is hand-written C selected by a transform catalogue; it is not derived from machine instructions. |
| What accounts for 14.55×? | Algorithm substitution. The generic arm sorts with libc `qsort` (indirect comparator per comparison); the specialized arm does a counting sort over the observed `[min,max]` window. The 14.55× is mostly the better algorithm plus removal of translation overhead for that kernel; the 26.68× over translated x86-64 includes the same substitution. It is not evidence that Porte translates faster. |
| Are host-fusion semantics preserved under callbacks, exceptions, concurrency, partial failure? | Not demonstrated. The audit kernel's host effects were all successful, single-threaded, with a fixed stub set. The fused path replays the same effect ledger; nothing exercised a failing call, a callback, or a second thread. |
| Can the cache be reused without full re-verification? | Partially. Cache load is sub-millisecond and the key is recomputed in a fresh process, but Phase 1 still ran a smoke corpus (~0.48 s) and did not re-verify at launch for new inputs. |
| Which optimizations work on an unknown stripped binary? | None. Every Phase 1 transform needs a kernel identity and a hand-written replacement for it. |

Phase 2 therefore had to start from the binary, and none of the Phase 1
machinery was reused except the idea of a versioned artifact plus differential
admission.

---

## 2. What Phase 2 demonstrates

A stripped, independently compiled Windows x86-64 PE with no exports is loaded,
executed from its entry point, profiled, and — without any source, symbol or
fixture knowledge — one hot loop is selected, translated to ARM64 machine code
through a checked C intermediate, guarded, proven equivalent against the
original image over a 22-case corpus, cached under a versioned key, and reused.

Five images were admitted, including `crc.exe`, which was generated and built
*after* the pipeline was frozen. Its first run exposed one general lifter bug
(a Capstone 5 operand-size accessor); after that fix, with no fixture-specific
change, it was admitted, verified 22/22, cached, and reused. That is the closest
this experiment gets to the acceptance test: an unseen binary, no source, one
narrow specialization, proven and reusable.

The loop selection, the memory specialization and the guard are all derived
from the binary and its execution profile. The optimizer path
(`image.py`, `machine.py`, `profiler.py`, `lifter.py`, `dispatch.py`,
`pipeline.py`) contains no fixture name, address or algorithm; only
`experiment.py`, which is the driver that chooses which programs to run, lists
them.

### 2.1 The fixtures are genuinely opaque

`fixtures/opaque/gen.py` (the fixture side of the wall) generates and builds
four freestanding Windows programs with `x86_64-w64-mingw32-gcc`, then runs
`strip --strip-all`:

| Program | Bytes | Sections | Imports | Exports |
| --- | ---: | --- | --- | --- |
| `blob.exe` | 527,360 | .text .rdata .bss .idata | 4 kernel32 | none |
| `lookup.exe` | 267,264 | .text .rdata .bss .idata | 4 kernel32 | none |
| `sieve.exe` | 3,072 | .text .rdata .bss .idata | 4 kernel32 + `memset` | none |
| `fault.exe` | 3,072 | .text .rdata .bss .idata | 4 kernel32 | none |
| `crc.exe` | 266,240 | .text .rdata .bss .idata | 4 kernel32 | none |

They are not called by the harness: they run from `AddressOfEntryPoint`, parse
a size from `GetCommandLineA`, compute, write one line with `WriteFile`, and
`ExitProcess`. The only input channel the machine gives a program is its command
line. The optimizer receives a path and a decimal argument.

The generator also keeps an independent Python implementation of each
program's arithmetic (`fixtures/opaque/reference.json`). It is used to check the
*baseline emulator* — 25 cases across the four images, all exact — and it is
never visible to Porte.

---

## 3. The pipeline as built

```
PEImage  →  Machine (Unicorn x86-64, host-backed image/heap, Win32 stubs)
             │
             ├─ pass 1: run from entry, count basic blocks
             │            └─ CFG + dominators → candidate natural loop
             ├─ pass 2: fresh machine, code hook on the loop
             │            └─ per-site observed region + offset window
             ├─ lifter: x86-64 → C with explicit x86 semantics → clang -O3 -arch arm64
             ├─ dispatcher: guarded entry at the loop head, state sync, exact bail
             ├─ oracle: 22 argv cases, baseline vs specialized, byte-exact comparison
             └─ cache: key = image hash + loop bytes + site facts + lifter version
                        + host + toolchain; artifact copied into the key directory
```

| Component | File | Notes |
| --- | --- | --- |
| PE loader | `porte/pe/image.py` | pefile; imports, relocations, entry RVA; no symbols |
| Machine | `porte/pe/machine.py` | Unicorn; image and heap mapped through host buffers so generated code can address them; kernel32 + CRT stubs; block counting; fault capture |
| Profiler | `porte/pe/profiler.py` | CFG, dominators, multi-backedge loop bodies, per-site region observation |
| Lifter | `porte/pe/lifter.py` | integer subset of x86-64 → C; region-pinned accesses; exit per edge; bail per failed guard |
| Dispatcher | `porte/pe/dispatch.py` | ctypes ABI, region-layout check, exact resumption RIP, bail/demotion accounting |
| Oracle / cache | `porte/pe/pipeline.py` | verification corpus, keyed artifacts, smoke reuse, mutation self-test, rollback and invalidation checks |
| Experiment | `porte/pe/experiment.py` | drives all of the above and writes `results/opaque_pe.json` |

### 3.1 Candidate selection

Loops are natural loops over the static CFG: a header dominates a block that
branches back to it. Because a header can have several backedges, the body is
`dominated(header) ∩ reachable(header) ∩ can-reach(header)` rather than one
backedge's natural loop. That distinction mattered: the first implementation
missed a stride loop entirely and produced a trace that exited on the wrong arm.

Candidates are ranked by **executed basic blocks** inside the body, not by
instruction count. An interpreter pays per block (translation-unit lookup,
dispatch); ranking by instruction count picked a loop with more work per block
and a lower real cost.

Chosen loops, from the profile alone:

| Program | Head | Blocks | Guest instructions | Observed |
| --- | --- | ---: | ---: | --- |
| blob | `0x140001100` | 1 | 12 | 65,535 iterations |
| lookup | `0x140001100` | 1 | 15 | 65,534 iterations |
| sieve | `0x1400010d0` | 6 | 17 | 998 outer passes, 2.12 M block executions |
| fault | `0x1400010c9` | 3 | 16 | 9 iterations, then an unmapped read |
| crc | `0x1400010c0` | 1 | 8 | 262,142 iterations |

### 3.2 Binary-to-native trace, worked example

`blob`'s selected loop is the whole body of the transform. Guest code:

```asm
140001100: imul ecx, dword ptr [rbx + rdx*4], 0x19660d
140001107: add  ecx, 0x3c6ef35f
14000110d: mov  eax, ecx
14000110f: shr  eax, 0xd
140001112: xor  eax, ecx
140001114: imul eax, eax, 0x9e3779b1
14000111a: add  eax, edx
14000111c: mov  dword ptr [r9 + rdx*4], eax
140001120: add  rdx, 1
140001124: add  r10, rax
140001127: cmp  rdx, r11
14000112a: jb   0x140001100
```

The profile observed both memory sites inside region 0 (the image) on every
sampled access, so the emitted fast path is:

```c
L_140001100: ;
    (*iters)++;
    uint64_t f2 = r3 + (r2 * 4);
    if (!(f2 >= g0 && f2 - g0 <= s0 - 4u)) BAIL(0x140001100ull);
    uint64_t f1 = porte_ld(h0 + (f2 - g0), 4);
    ...
    cf = of = (p_140001100 != (int64_t)(int32_t)f5);
    ...
```

`h0/g0/s0` are the host pointer, guest base and size of the pinned region,
captured once at trace entry. The guard is a real check, not an assumption: a
miss stores the architectural state with RIP at the faulting instruction and
returns to the emulator. Registers, flags and RIP are synchronized at entry and
at every exit; the emulator resumes exactly where the trace stopped.

### 3.3 Cache

Key components (all invalidation inputs): PE SHA-256, loop head/end and bytes
SHA-256, per-site pinned regions, lifter version, host identity, toolchain
identity. The artifact is copied into `build/pe_cache/<key>/` so a rebuild in a
working directory cannot silently replace a dylib an older manifest points at.
Reuse checks the artifact hash, the runtime region layout (a mismatch demotes
the run to interpretation), and a two-case smoke corpus.

Keys from the recorded experiment:

```
blob     8933ba16734524e439c7d73d
lookup   fd4923c29503504be36ea362
sieve    9e1ea08cb2ab132e606ba7a6
crc      b6fd105c2a9e77b08997ad7f
fault    1c81ab0ee05bcd17c650118d
```

The full keys are in `results/opaque_pe.json`. Rebuilding the fixtures produces
different keys because the PE header carries a build timestamp, which is the
correct behaviour for an artifact keyed on the image.

---

## 4. Correctness evidence

`porte pe verify` re-runs the recorded artifact against the original PE on the
generic corpus `0,1,2,3,7,16,63,64,65,127,255,256,257,511,1023,1024,1025,2047,
2048,4096,65536,1000000`. Every case compares stdout bytes, exit code, fault
string, API call ledger, heap digest and writable-image digest. Each run uses a
fresh process-of-record (fresh machine, fresh image buffer, fresh heap).

| Program | Cases | Result | Guard behaviour |
| --- | ---: | --- | --- |
| blob | 22 | 22/22 exact | 1 dispatch covers all iterations; 0 bails |
| lookup | 22 | 22/22 exact | 1 dispatch; 0 bails |
| sieve | 22 | 22/22 exact | 1 dispatch covers 2.12 M block executions; 0 bails |
| crc | 22 | 22/22 exact | 1 dispatch covers 262,142 iterations; 0 bails |
| fault | 22 | 22/22 exact | 1 dispatch, **1 bail** at the unmapped read; emulator reproduces `UC_ERR_READ_UNMAPPED` at the same RIP |

Additional checks, all recorded in `results/opaque_pe.json`:

| Check | Result |
| --- | --- |
| Boundary inputs (`0,1,2,3`, max size) | exact; loops that execute zero times still match |
| Repeated launches | every corpus case is a separate process-of-record |
| Cache reuse | hit + smoke in 20–61 ms, versus 1.7–10.0 s cold |
| Cache invalidation | a one-byte change in `.rdata` produces a different SHA-256 and a cache miss |
| Rollback | a machine with a different heap size is detected as a layout mismatch, demoted, and produces byte-identical output |
| Mutation test (oracle can fail) | a trace with one loaded value bit-flipped is rejected on every tested case, for all four programs |
| Failure path | faulting program above |

Not covered: concurrency (the machine has one thread and no thread creation),
SEH/unwinding, callbacks, and self-modifying code. Stated rather than implied.

---

## 5. Benchmarks

Method: seven interleaved fresh-process runs per arm, median reported; the
workload argument is the same for every arm. "Translated" is the same lifter
with generic (region-search) memory handling; "specialized" is the cached
artifact with profile-pinned accesses. "Native" is an ARM64 reference built by
the fixture side from the same data; it is context, not a baseline, and it does
not include PE loading or I/O.

| Program / workload | Baseline PE | Translated | Specialized | Speedup | Native reference |
| --- | ---: | ---: | ---: | ---: | ---: |
| blob, n=65,536 | 2.076 ms | 0.482 ms | **0.405 ms** | **5.13×** | 0.011 ms |
| lookup, n=65,536 | 2.451 ms | 0.544 ms | **0.554 ms** | **4.42×** | 0.256 ms |
| sieve, n=1,000,000 | 58.542 ms | 6.619 ms | **6.005 ms** | **9.75×** | 1.261 ms |
| crc, n=262,144 | 1.642 ms | 1.131 ms | **1.041 ms** | **1.58×** | 0.776 ms |
| fault, n=16 | 0.096 ms | 0.170 ms | 0.142 ms | 0.68× | — |

Costs and resources (whole process, measured by the experiment):

| Program | Build (profile+codegen+verify) | clang | Startup (load+map) | Cache smoke | Peak RSS |
| --- | ---: | ---: | ---: | ---: | ---: |
| blob | 2.01 s | 0.11 s | 12.7 ms | 62 ms | 223 MB |
| lookup | 2.36 s | 0.17 s | 5.9 ms | 39 ms | 325 MB |
| sieve | 10.23 s | 0.16 s | 1.0 ms | 27 ms | 449 MB |
| crc | 2.47 s | 0.22 s | 6.2 ms | 41 ms | 466 MB |
| fault | 1.62 s | 0.14 s | 0.6 ms | 19 ms | 453 MB |

Repeat experiments moved these medians by a few percent (blob 5.13–5.60×,
sieve 9.72–10.18×, lookup 4.06–4.61×); `lookup`'s pinned arm is within noise of
its generic arm in some runs and slower in others, and `crc`'s 1.58× is the
honest case of a loop whose body is too small to gain much from translation.
The committed `results/opaque_pe.json` is one complete run of the command above.

### 5.1 How to read these numbers

- **The baseline is a fast interpreter.** Unicorn chains translated blocks and
  needs roughly 10–15 ns per basic block. On short loops the fixed dispatch
  cost (~60 µs for Python-side state synchronization through ctypes) is larger
  than the work being replaced. `fault` is the honest example: a 9-iteration
  loop cannot pay for a dispatch, and the specialized arm is 1.5× *slower*.
- **Specialization beyond translation is real but modest here.** Pinning buys
  ~9–16% over the generic trace on two of the compute loops (blob 0.482 →
  0.405 ms, sieve 6.619 → 6.005 ms) and is a wash or slightly negative on
  `lookup` (0.544 → 0.554 ms) and `crc` (1.131 → 1.041 ms is the only pinning
  gain there), where the pinned sites pay as much in guards as the region search
  they replace. The large gains are from native execution, not from pinning.
- **Native is still 1.3–37× faster than the specialized path.** `blob`'s native
  reference is auto-vectorized (37× ahead); `crc` is only 1.34× ahead because
  its table lookup forces the same serial dependency. The trace is scalar, keeps
  per-iteration guards and recomputes x86 flags in full; that gap is the honest
  headroom.
- **Sieve's 10 s build is profiling, not compilation.** The observation pass
  runs a Python hook on every instruction of a 2.12 M-block loop. Compilation
  is 0.16 s.

---

## 6. Reproduction

```bash
cd /Users/omer/Local_Repo/maxe
.venv/bin/python fixtures/opaque/gen.py --clean          # build fixtures + references
.venv/bin/python -m porte.cli pe analyze fixtures/opaque/out/blob.exe
.venv/bin/python -m porte.cli pe optimize fixtures/opaque/out/blob.exe --workload 65536 --force
.venv/bin/python -m porte.cli pe verify   fixtures/opaque/out/blob.exe
.venv/bin/python -m porte.cli pe run      fixtures/opaque/out/blob.exe --workload 65536
.venv/bin/python -m porte.cli pe bench    fixtures/opaque/out/blob.exe --workload 65536
.venv/bin/python -m porte.cli pe selftest fixtures/opaque/out/blob.exe --workload 65536
.venv/bin/python -m porte.cli pe warm     fixtures/opaque/out/blob.exe
.venv/bin/python -m porte.cli pe experiment --repeats 7    # writes results/opaque_pe.json
```

Raw numbers: `results/opaque_pe.json`. Generated traces and cached artifacts:
`build/pe_cache/<key>/trace.c`, `trace.dylib`.

---

## 7. Prior art this overlaps

| System | Overlap | Difference here |
| --- | --- | --- |
| Dynamo / DynamoRIO | Hot-trace formation and native code with exits back to interpretation | Trace formation is profile-driven offline, not a runtime fragment cache; the oracle gate and the cache are the research object |
| QEMU / FEX-Emu | x86→ARM64 translation with explicit architectural state | Both translate whole images ahead of time; this translates one loop and keeps the rest interpreted, with per-access guards |
| Rosetta 2 | AOT + runtime translation of x86-64 macOS binaries | Not available for PE here; Porte's baseline is an interpreter, so the speedups are not comparable |
| PGO + value specialization (HotSpot uncommon traps, profile-guided devirtualization) | Guarded fast paths from observed facts, bail to the general path | Applied to opaque stripped binaries with no source instrumentation |
| Region pinning in DBT memory handlers | Hoisting address translation after profiling which region is hot | Small and explicit: one pinned region index per site, guarded per access |
| Differential compiler testing (EMI, randomized translation testing) | Compare optimized and unoptimized execution | Used as an admission gate here, with a documented mutation test |

---

## 8. Novelty claim (narrow)

Not: "Porte translates Windows games", "Porte beats Rosetta", or "guarded
specialization is new". The claim supported by this work is:

> An opaque, stripped Windows x86-64 PE can be profiled from its entry point,
> have one natural loop selected automatically, translated to ARM64 with
> profile-derived memory-region pinning and exact deoptimization points, and
> admitted into a versioned cache only after differential equivalence over a
> generic input corpus — with the artifact demoting itself on a changed
> environment, and with the oracle demonstrated to reject a deliberately
> corrupted trace.

That is a capability demonstration, not a performance win over production
translators, which were not runnable on this host.

---

## 9. Unsupported behaviours

- **Instruction subset.** Integer GP-register operations only: `mov`, `movzx`,
  `movsx`, `movsxd`, `lea`, `add`, `sub`, `and`, `or`, `xor`, `cmp`, `test`,
  `imul` (two/three operand), `shl/shr/sar/rol/ror`, `inc`, `dec`, `neg`, `not`,
  and conditional branches. x87/SSE/AVX, `cmov`, `setcc`, `popcnt`, `bswap`,
  `push`/`pop`, string instructions and `call`/`ret` inside the loop make the
  candidate ineligible.
- **Control flow.** Indirect jumps, calls and returns inside a loop are
  rejected. Switch tables in hot loops are therefore not specialized.
- **Memory model.** Region granularity is the mapped region (image, heap); an
  access is guarded against that region, not against a page or an object. No
  self-modifying code detection; the trace assumes its own code bytes are
  unchanged after translation.
- **Windows.** Import stubs only for `GetCommandLineA`, `GetStdHandle`,
  `WriteFile`, `ExitProcess`, `VirtualAlloc`, `VirtualFree`, `GetLastError`,
  `SetLastError`, `GetTickCount64`, `QueryPerformanceCounter`,
  `FlushFileBuffers`, `memset`, `memcpy`, `memmove`. No SEH, TLS callbacks,
  threads, APC, file system beyond the write stub, registry, or DLL loading.
- **Only one loop per image** is specialized. The rest is interpreted.
- **Sharing state.** The dispatcher synchronizes registers through ctypes on
  every entry/exit; there is no inline caching of guest state across dispatches.

---

## 10. Next blocking technical problem

The measurement, not the translation, is now the bottleneck. Dispatcher entry
costs ~60 µs because 16 registers plus flags are read and written through
Python for every trace entry. That single number decides whether a loop is
worth specializing, and it is why `fault` regresses. The next step is to move
the dispatcher and the state sync into the emulator's address space (a C
harness or a Unicorn hook that calls the trace directly), then re-measure. Until
that exists, the honest threshold for admission is "the loop must execute tens
of thousands of times", and the pipeline does not yet enforce it.

After that: multi-loop specialization (several traces per image), trace
chaining, and validating the whole approach against a production translator
(FEX on Linux, or a Rosetta-hosted x86-64 Mach-O build of the same code) rather
than against a Python-hosted interpreter.
