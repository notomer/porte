# Native-performance plan: what we can measure today, and what we cannot

Status: the experiment in this plan is implemented and reported in
[`05-native-baseline.md`](05-native-baseline.md). The numbers are bad for Porte.
That is the point of measuring first.

The objective for this phase is stated as a stretch target: a supported Windows
game should reach ~95% of the median FPS and ~90% of the 1% low of an equivalent
native ARM64 + Metal application, with no more than 5% CPU translation overhead
in selected steady-state hot paths. Nothing in this document or the baseline
claims that is achieved. What exists is the smallest experiment that can say how
far away it is and in which layer the distance lives.

---

## 1. Rules of evidence

These are binding for every later report in this directory.

1. **Same workload, both sides.** The Windows and native arms run the same
   simulation, the same scene, the same resolution, the same shader, and the
   same per-frame work. Percentages are only computed between arms of the same
   configuration.
2. **The GPU half is held constant.** All arms submit to the same Metal renderer
   source. A gap is therefore a CPU-path gap unless the GPU duration itself
   changes, which is reported separately.
3. **Correctness gates performance.** An arm's numbers are published only if the
   entity state after the last frame hashes identically in every arm. A fast arm
   that simulated less is not a result.
4. **Cold, warm and steady state are separated.** Process start-up, first frame,
   shader compilation, and the steady-state median are distinct fields.
5. **No manufactured percentages.** No cross-game, cross-hardware, or
   cross-configuration ratios. Where an arm does not exist on this host (a D3D
   backend, Wine, FEX), the report says so rather than substituting one.
6. **Failures are results.** A layer that cannot be measured is listed as
   unmeasured, not estimated.

## 2. Audit: what the repository actually is

| Capability | State | Consequence for this phase |
| --- | --- | --- |
| PE loading and entry-point execution | Works (`porte/pe/machine.py`): PE32+, relocations, imports, command line, host-backed image/heap | M1 is satisfied for freestanding images |
| Execution engine | Unicorn 2.1.4 — QEMU-derived, so guest code runs through QEMU's TCG translator, not a hand-written interpreter | Every PE instruction pays TCG dispatch; there is no AOT path for a whole module |
| Native trace path | One hot natural loop per image, selected by profile, lifted to C → ARM64 → dylib, guarded, cached (`porte/pe/lifter.py`, `pipeline.py`) | Exists, but only for single loops with liftable instructions; not used by the frame harness |
| Win32 surface | 15 stubs: `GetCommandLineA`, `GetStdHandle`, `WriteFile`, `ExitProcess`, `GetTickCount64`, `QueryPerformanceCounter`, `GetLastError`, `SetLastError`, `VirtualAlloc`, `VirtualFree`, `FlushFileBuffers`, `memset`, `memcpy`, `memmove`, `PresentFrame` | No threads, no files beyond stdout, no registry, no window, no D3D |
| Threading | None. One guest thread on one host thread | Multi-core scaling of a game is unmeasurable today |
| Graphics | No D3D, no Vulkan, no Wine, no D3DMetal/DXMT/CrossOver/GPTK on this host (verified: none installed) | A draw is submitted through a host stub into Metal; D3D→Metal cost is unmeasured |
| Shader path | None. Every arm compiles the same Metal source at runtime | Shader *translation* and pipeline-stall behavior of a real game is unmeasured |
| Differential admission | Corpus-based oracle, versioned cache, layout demotion (`porte/pe/pipeline.py`) | Reusable for frame-level specializations later |
| Benchmarks | Wall clock per call, per-frame layer attribution, block/instruction counts | Adequate for this phase; no sampling profiler of the guest |

### 2.1 What the earlier experiments did and did not establish

- Phase 1 (source-level modules, `docs/02-experiment-report.md`) reported 14.55×
  by *replacing an algorithm* (counting sort for libc `qsort`) in a known kernel.
  It says nothing about translation throughput or frame FPS.
- The opaque-PE phase (`docs/90-opaque-pe-experiment.md`) translated one hot loop
  per image from machine code, with profile-pinned memory addressing, and proved
  it over a 22-case corpus. Its 1.6–9.8× numbers are against Porte's own
  emulator, and its own native reference is still 2.8–42× faster. It is a
  compiler experiment, not a gaming result.
- Neither experiment measures a frame, a GPU, a Windows API boundary, or a
  shader. That is why the new baseline had to be built.

### 2.2 The performance question the repo can answer today

Given a Windows x86-64 binary that calls into a Metal renderer once per frame,
how much slower is the frame than the same simulation compiled natively for
ARM64, and where does the difference live?

That question is answerable on this host. "How much slower is a D3D12 game" is
not, because no D3D backend is installed and none can be shipped in-tree without
resolving its license.

## 3. The minimum experiment (implemented)

Workload: `fixtures/frame/` — a bounded 2D particle field (integer Q16.16, so
state is exactly comparable) plus one instanced Metal draw of the same entities
at 1920×1080 with a runtime-compiled fragment shader. It is not a game; it is
one frame's CPU work and one frame's GPU work with a controllable ratio.

Four arms, all built from the same `sim.c` and `renderer.m`:

| arm | CPU side | graphics path |
| --- | --- | --- |
| `native_arm64` | clang `-O2 -arch arm64` (NEON auto-vectorized) | Metal, linked |
| `rosetta_x86_64` | clang `-O2 -arch x86_64` (SSE auto-vectorized) | Metal, linked, under Rosetta 2 |
| `porte_pe` | mingw-gcc `-O2` Windows PE, stripped, entry point only (scalar x86) | same Metal renderer through `PresentFrame` host stub |
| `porte_pe_clang` | the same PE built by clang (vectorized x86), stripped | same |

`porte_pe_clang` exists because a translator result is meaningless if the guest
code itself is 4× worse than the native build: it separates "the guest compiler
did not vectorize" from "the execution engine costs this much per instruction".

Three configurations of the same binaries:

| config | entities | substeps | fragment loop | what it isolates |
| --- | ---: | ---: | ---: | --- |
| `cpu_bound` | 65,536 | 16 | 0 | CPU path under a 10× frame budget |
| `mixed` | 8,192 | 1 | 64 | GPU-bound natively, CPU-bound once translated |
| `gpu_heavy` | 8,192 | 1 | 1024 | GPU-bound in every arm; CPU cost must be hidden |

Per-frame attribution: guest/simulation time, host submission (encode), time
blocked on the three-deep present ring, and GPU duration. Sustainable frame time
is `max(CPU-side interval, GPU duration)` per frame, because a frame cannot
present faster than its own GPU work completes; using the CPU interval alone
would flatter GPU-bound arms (and does flatter the Porte arm's 1% lows if
reported naively).

Additional probes:

- **State hash.** FNV-1a over the entity array at the end of the run in every
  arm. Also catches a translator that silently skips work.
- **Work scaling.** 4× the entities must cost 4× in every arm, otherwise the
  gap could be a fixed overhead rather than a throughput ratio.
- **Instruction count.** Per-instruction hook at two small instance counts,
  differenced to cancel start-up; yields guest instructions per entity step and
  the engine's ns/instruction at full size.
- **Cold shader.** A fresh process per sample with a nonce that defeats the
  Metal shader cache (in-process sampling turns samples 2..n into cache hits).
- **Repeats.** Per-arm repeat medians and their spread, so a 2% difference is
  not reported as a result when the spread is 3%.

## 4. Milestones

| milestone | state after this phase |
| --- | --- |
| M1 real PE execution | **Done** for freestanding images: stripped PE, entry point, command line, host stubs, byte-identical state against native |
| M2 native gaming baseline | **Done in miniature**: reproducible Metal workload with Windows and native variants, same scene, same shader |
| M3 frame-time profiler | **Partial**: per-frame attribution into guest / submission / ring wait / GPU / shader compile, with cold-vs-warm separation. No sampling of *where inside* the guest the time goes, no Windows API ledger per frame |
| M4 binary-derived optimization | Not in this experiment. The opaque pipeline demonstrates one verified loop specialization; nothing in the frame path is specialized yet |
| M5 cross-layer optimization | Not demonstrated. No candidate sequence has been observed, generated, verified and measured across a CPU/API/graphics boundary |
| M6 parity evaluation | **Partial**: the gap is measured and attributed; D3D→Metal, threading, and shader translation are outside it |

## 5. What would falsify the near-native target

The plan commits to abandoning the target, or narrowing it, if any of these
hold after the next two experiments:

1. Whole-module AOT/dynamic translation cannot reach ~4 GIPS effective on this
   workload, i.e. the 92M guest instructions of one `cpu_bound` frame cannot be
   executed in under ~23 ms on one core.
2. Guest SIMD cannot be lowered at better than ~2× the cost of the equivalent
   native instruction stream (measured with the Rosetta arm as the reference).
3. Threading the guest cannot recover the per-core deficit: a game frame split
   across 6–8 threads should hide most of a 2–4× per-thread penalty.
4. The GPU-bound arm shows a gap that does not shrink with a faster CPU path
   (i.e. the graphics boundary, not translation, is the limiter).

Each of these is measurable with the same harness plus one addition (AOT engine,
SIMD lowering, guest threads, D3D backend respectively).

## 6. Ordered plan

**E1 (this phase).** Frame-gap baseline and layer attribution. Done; results in
`05-native-baseline.md`.

**E2. Whole-module translation.** Replace TCG execution of the frame PE with
Porte's own AOT translation of the whole `.text` (or a block dispatcher with a
persistent code cache), keeping the same oracle and the same frame harness.
Gate: `cpu_bound` guest time < 30 ms with the state hash unchanged. This is the
single largest lever and the only one that can be evaluated without new host
software.

**E3. SIMD-aware lowering.** The guest's scalar loop costs 88 x86 instructions
per entity step; the native build does the same step with NEON. Gate: the
vectorized guest (`porte_pe_clang`) executes at no worse than 2× the cost of the
Rosetta arm per instruction, and a scalar-loop vectorization pass reduces
instructions per entity step by ≥2× with identical state.

**E4. Guest threads.** Windows `CreateThread`/futex-equivalent stubs, one host
thread per guest thread, and a frame harness that reports per-thread utilization.
Gate: `cpu_bound` wall time scales with 4 guest threads (≥2.5× throughput).

**E5. Graphics boundary.** Install a licensed D3D→Metal backend (D3DMetal via
GPTK, or DXMT) on a host where that is permitted, run a D3D11/12 workload with a
known native Metal counterpart, and measure the same layers plus draw-call and
state-change counts. Gate: the graphics-boundary share of frame time is
quantified, not assumed.

**E6. Cross-layer specialization.** Only after E2–E5: observe repeated
game→API→backend sequences, generate a fused native path, and admit it only if
the differential oracle passes and the frame is actually faster. Gate: a verified
improvement on a boundary that E5 identified as material, with callbacks,
ordering and failure behavior preserved.

**E7. Closed loop.** Automate E2–E6 selection per frame-time bottleneck with
revert-on-regression, using the existing artifact/admission machinery.

## 7. Risks this plan does not retire

- **A single synthetic frame is not a game.** No AI, physics, audio, file I/O,
  input latency, or driver behavior. The ratios here are a lower bound on the
  difficulty, not an upper bound.
- **No D3D or shader translation is measured.** Shader compilation stalls and
  pipeline state churn — explicitly in scope for the objective — are outside this
  baseline.
- **The host is a single machine** (M2 Max, macOS 27.2). GPU durations moved by
  ~20% between runs when the CPU fed the queue differently; conclusions are
  limited to ratios that survive the reported spread.
- **Rosetta 2 is a Mach-O translator.** It cannot run a PE, so it is a reference
  for "a production translator on this silicon", not a Windows comparison.
