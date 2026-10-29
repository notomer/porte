# Native baseline: measured Windows-vs-native frame gap

What this document is: the first honest measurement of the distance between
running a Windows x86-64 executable through Porte and running the equivalent
application natively on Apple Silicon, broken down per frame layer, with the
reasons for the remaining overhead stated as measured facts rather than
expectations.

Host: Apple M2 Max (12 cores), 32 GB, macOS 27.2 (26B123), clang 21 (Apple),
mingw-w64 gcc, Unicorn 2.1.4, Python 3.14. Build: `./fixtures/frame/build.sh`.
Run: `.venv/bin/python -m porte.cli frame --repeats 3`. Raw record:
`results/frame_baseline.json` (`2026-10-29T03:10:53Z`).

No Wine, FEX, CrossOver, GPTK/D3DMetal or DXMT is installed on this host
(checked). Everything below is therefore Porte's own Windows execution plus a
host Metal renderer; the D3D→Metal layer is **not** part of this measurement.

**Variance.** Three full runs were taken on the same build. The emulated
CPU-bound arm moved between 170 ms and 204 ms per frame (within-run spread 0.1%
to 22%) depending on host load; native, Rosetta and every GPU-bound arm moved by
less than 7%. The tables quote the committed run; §6 lists the cross-run range
for the headline numbers. Ratios are stable across runs, absolute emulation
times are not.

---

## 1. The workload

`fixtures/frame/` is one frame of a 2D particle field plus one instanced Metal
draw of the same entities at 1920×1080. The simulation is integer Q16.16, so its
state is exactly comparable between arms; the shader is compiled from source at
runtime in every arm, so the GPU half is literally the same code.

Four arms, same `sim.c` and `renderer.m`:

| arm | CPU build | engine |
| --- | --- | --- |
| `native_arm64` | clang `-O2 -arch arm64` | native, NEON auto-vectorized |
| `rosetta_x86_64` | clang `-O2 -arch x86_64` | Rosetta 2, SSE auto-vectorized |
| `porte_pe` | mingw-gcc `-O2`, stripped PE | Porte: Unicorn (QEMU TCG), scalar guest |
| `porte_pe_clang` | clang `-O2`, same PE source, stripped | Porte: Unicorn (QEMU TCG), vectorized guest |

Two Windows images are used on purpose. The mingw build is scalar (0 SIMD
references in `frame_sim_step`); the clang build is vectorized (41). Without the
second image, a translation result would be confounded with "the guest compiler
did not vectorize", which is not the translator's fault.

Correctness gate: after the last frame the entity array is hashed (FNV-1a). All
four arms produce the same hash in all three configurations
(`cpu_bound bfae1c1b40a3f312`, `mixed f23394f633f4da39`,
`gpu_heavy 01f5018d4be45bd4`). Nothing below compares runs that simulated
different work.

## 2. Results

Per-frame steady state (frames 5..n of each repeat; three process repeats per
native arm, two or three in-process repeats per Porte arm). `frame` is the
sustainable frame time, `max(CPU-side interval, GPU duration)` per frame; `1% low`
is the 99th percentile of that same series. `gap` is against `native_arm64` in
the same configuration; spread is max−min of the per-repeat medians.

### 2.1 `cpu_bound` — 65,536 entities × 16 substeps, fragment loop 0

| arm | guest ms | submit ms | ring wait ms | GPU ms | frame ms | FPS | 1% low | gap | spread |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `native_arm64` | 1.485 | 0.096 | 0.102 | 0.275 | **1.681** | 595 | 367 | — | 1.2% |
| `rosetta_x86_64` | 2.705 | 0.269 | 0.279 | 0.275 | 3.248 | 308 | 223 | +93% | 0.8% |
| `porte_pe` | 204.102 | 0.199 | 0.007 | 0.275 | 204.345 | 4.9 | 2.8 | **+12,056%** | 22.3% |
| `porte_pe_clang` | 189.682 | 0.200 | 0.007 | 0.276 | 190.023 | 5.3 | 4.2 | +11,204% | 11.5% |

Per entity step: native 1.4 ns, Rosetta 2.6 ns, Porte 195 ns (mingw guest) and
181 ns (clang guest). Per frame, Porte needs **121×** the native time (113× for
the vectorized guest) and 63× Rosetta's; per entity step the ratios are 139× and
75×. The Porte spread is large because a single emulated core is sensitive to
host load; the 1% low inherits that.

### 2.2 `mixed` — 8,192 entities, fragment loop 64

| arm | guest ms | submit ms | ring wait ms | GPU ms | frame ms | FPS | 1% low | gap | spread |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `native_arm64` | 0.014 | 0.025 | 0.440 | 0.537 | **0.537** | 1,863 | 969 | — | 27.5% |
| `rosetta_x86_64` | 0.024 | 0.050 | 0.388 | 0.507 | 0.497 | 2,012 | 1,164 | −7.4% | 20.5% |
| `porte_pe` | 1.440 | 0.023 | 0.000 | 0.307 | 1.473 | 679 | 631 | **+174%** | 1.7% |
| `porte_pe_clang` | 1.325 | 0.022 | 0.000 | 0.731 | 1.355 | 738 | 683 | +152% | 1.7% |

This is the crossover case: natively the frame is GPU-bound (0.54 ms GPU vs
0.09 ms CPU), but Porte's 1.3–1.4 ms CPU path exceeds the GPU budget, so the
translated frame becomes CPU-bound and loses ~60% of the native frame rate. The
native and Rosetta spreads are large here because their CPU-side interval runs
ahead of a 0.5 ms GPU at three frames in flight; the GPU-bound ordering between
the two is not resolved by this configuration.

### 2.3 `gpu_heavy` — 8,192 entities, fragment loop 1024

| arm | guest ms | submit ms | ring wait ms | GPU ms | frame ms | FPS | 1% low | gap | spread |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `native_arm64` | 0.014 | 0.030 | 5.756 | 8.350 | **8.350** | 120 | 79.0 | — | 1.2% |
| `rosetta_x86_64` | 0.024 | 0.058 | 5.691 | 8.388 | 8.388 | 119 | 78.8 | +0.5% | 1.2% |
| `porte_pe` | 1.579 | 0.031 | 1.270 | 8.420 | 8.420 | 119 | 78.7 | **+0.8%** | 1.3% |
| `porte_pe_clang` | 1.449 | 0.030 | 1.361 | 8.514 | 8.514 | 118 | 78.0 | **+2.0%** | 2.6% |

When the GPU is the limiter, the Porte arms are within 2% of native and their 1%
lows are indistinguishable. This is not evidence that translation is fast; it is
evidence that 1.5 ms of translated CPU work fits inside an 8.5 ms GPU frame and
disappears. GPU durations differ by 1–2% between arms because the CPU feeds the
queue differently; the repeat spread is of the same size.

## 3. Where the time goes

### 3.1 Layer attribution

| config | native guest share | Porte guest share | Porte engine cost |
| --- | ---: | ---: | ---: |
| `cpu_bound` | 88% | 99.9% | 204 ms of 204 ms |
| `mixed` | 2.6% | 98% | 1.44 ms of 1.47 ms |
| `gpu_heavy` | 0.2% | 19% (hidden) | 1.58 ms inside an 8.4 ms frame |

The submission boundary is not the problem in this harness: `PresentFrame`
through the host stub costs 0.02–0.20 ms per frame, the same order as the native
encode (0.023–0.096 ms). Neither the Windows API hop nor the Metal encode
explains anything above 1% of the gap. **Essentially all of the gap is guest
instruction execution.**

### 3.2 Engine throughput

Measured with a per-instruction hook at two small instance counts (differenced
to cancel start-up), then scaled to the `cpu_bound` frame:

| guest image | x86 instructions / entity step | instructions / frame | Porte guest time | effective rate | ns / guest instruction |
| --- | ---: | ---: | ---: | ---: | ---: |
| mingw PE (scalar) | 88 | 92,274,688 | 204.1 ms | 452 MIPS | 2.21 |
| clang PE (SSE) | 71 | 74,448,896 | 189.7 ms | 392 MIPS | 2.55 |

Two conclusions follow directly:

1. The engine executes the guest at ~400–540 MIPS depending on host load. That
   is what QEMU TCG dispatch costs; it is not competitive with native execution
   and it is far below a hand-written AOT translator's budget.
2. Vectorizing the guest (71 vs 88 instructions/step) did **not** make it faster,
   because each SIMD instruction costs more in the engine (2.55 vs 2.21 ns). The
   cost is per instruction dispatched, not per unit of algorithm work. This also
   means that "just make the guest code better" is not a lever for the current
   engine; only the engine is.

### 3.3 Scaling check

`cpu_bound` at 16,384 and 65,536 entities, one substep, 24 frames:

| entities | Porte guest ms | native sim ms | Porte ns/step | native ns/step | ratio |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 16,384 | 2.867 | 0.026 | 175.0 | 1.56 | 112.4× |
| 65,536 | 11.336 | 0.099 | 173.0 | 1.50 | 115.1× |

4.0× the work costs 3.95× (Porte) and 3.86× (native). The gap is a throughput
ratio, not a fixed overhead, and the per-step constants agree with §3.2.

### 3.4 Cold shader compilation

Fresh process per sample, nonce that defeats the Metal shader cache, three
samples each:

| path | samples (ms) | median |
| --- | --- | ---: |
| native `frame_arm64` | 46.1, 35.0, 32.7 | 35.0 ms |
| Porte host (`FrameRenderer` via ctypes) | 35.9, 33.7, 33.5 | 33.7 ms |

One shader plus one pipeline costs ~34 ms cold in both paths (earlier runs:
31.9–33.2 ms). This is the component the objective calls "shader compilation
stutter": at 60 fps, one cold pipeline is two frames. The experiment has exactly
one pipeline, so it can measure the unit cost but not the aggregate stalls of a
real game (hundreds of pipelines, some with variants).

### 3.5 Start-up and first frame

| arm | start-up | first frame |
| --- | ---: | ---: |
| native (process + Metal device + shader) | 20.4–23.1 ms | 0.8–2.5 ms |
| Porte PE (load, relocate, map, resolve imports, in-process) | 1.0–2.0 ms | 1.5 ms (`mixed`) … 204 ms (`cpu_bound`) |

The Porte start-up figure is not comparable: the Python process and the Metal
device already exist. It is reported to show that PE loading is not a
bottleneck; the first emulated frame is.

### 3.6 CPU and memory

All arms are single-threaded. In `cpu_bound` every arm uses ~1.00 core. In
`gpu_heavy` the native arm's main thread is blocked ~5.8 ms of an 8.4 ms frame
(GPU-bound), while the Porte arm spends 1.6 ms executing guest code and 1.3 ms
blocked on the ring.

Resident memory is only meaningful for the native arms (25–76 MB across
configurations; Rosetta 19–71 MB). The Porte arm's RSS is the Python host
process plus guest buffers (160–550 MB across repeats) and is not a
like-for-like figure; it is reported as a limitation, not as a result.

## 4. What 95% would require

Using `cpu_bound` as the stress case (not as a typical game):

- Native guest time is 1.5 ms per frame, 9% of a 60 fps budget. 95% of native
  FPS means ≤1.76 ms per frame. Porte is at 190–204 ms, **~110–120× over**.
- A 1:1 x86→ARM64 translator at a realistic 3–6 GIPS would need 31 ms / 15 ms
  for the 92M guest instructions in the frame. That is still 9–18× above the
  native frame, because the native build executes the same algorithm 4-wide
  (NEON) and the mingw guest is scalar.
- Therefore, on a CPU-bound frame of this shape, **translation alone cannot reach
  native parity**. Parity requires the guest to be SIMD (as game compilers
  usually emit), the lowering of that SIMD to NEON to be efficient, and enough
  guest threads to use more than one core.
- The measured production reference is Rosetta: 1.9× native on this workload
  (SSE guest). That is the bar a Porte translator must first match; it is ~60×
  away on frame time (75× per unit of work).
- For GPU-bound frames the requirement is different and much weaker: keep the
  translated CPU cost below the GPU frame budget. At 8.4 ms of GPU work, Porte's
  1.6 ms already fits and the measured gap is +0.8–2.0% (inside repeat spread).

So the honest summary of the objective: **near-native is plausible only for
GPU-bound frames and only once the CPU path is at least an order of magnitude
faster; for CPU-bound frames it is not reachable by translation alone.**

## 5. Prioritized engineering roadmap

Priority is set by measured leverage, not by novelty. Each item names the
measurement that would show it worked; details in
[`04-native-performance-plan.md`](04-native-performance-plan.md).

**P0 — Replace TCG with an AOT/dynamic translator for whole modules.**
Current engine: ~400–540 MIPS at 2.2–2.6 ns per guest instruction. Target:
≥3 GIPS effective. This is 6–8× on every CPU-bound frame and is the precondition
for everything else. Gate: `cpu_bound` guest < 30 ms, state hash unchanged, same
oracle.

**P0 — Reduce instructions per unit of work (SIMD lowering, trace-level
optimization).** 88 guest instructions per entity step vs a 4-wide native loop is
the reason even a perfect 1:1 translator loses. Needed: SSE/AVX lowering quality
and recognition of scalar loops that the guest compiler failed to vectorize.
Gate: on the clang PE, Porte's cost per instruction within 2× of Rosetta's; on
the scalar PE, ≥2× fewer effective instructions with identical state.

**P1 — Guest threading.** Every arm here is single-threaded; a game frame is not.
With 12 cores and a 1.9–120× per-core deficit, spreading the frame over 6–8
threads is the only lever that multiplies. Requires `CreateThread`, TLS,
synchronization primitives, and a thread-aware frame harness.

**P1 — Measure a real graphics boundary.** The submission stub is ~0.03 ms here
because there is no D3D, no state validation, no resource lookup. Until a
licensed D3D→Metal backend (D3DMetal/GPTK or DXMT) is running a workload with a
native Metal counterpart, the largest untested layer is the one the objective
cares most about. Gate: draw-call/state-change overhead and GPU-bound parity
measured on a real backend.

**P2 — Shader precompilation and pipeline prewarming.** Unit cost is ~34 ms per
cold pipeline on this host; a game has many. Measure aggregate cold-start and
hitch counts, then cache compiled pipelines keyed by shader hash + state.

**P2 — Frame-time profiler inside the guest.** Current attribution stops at
"guest". Add per-block/per-function sampling during frames so P0/P1 work is
aimed at the code that is actually on the critical path, not the hottest code.

**P3 — Cross-layer specialization.** Only after P0/P1/P2: identify repeated
game→API→backend sequences with stable arguments, generate a fused native path,
verify it with the existing differential machinery, keep it only if the frame
gets faster. No candidate has been observed yet, so no claim is made.

**P3 — Closed-loop selection and revert.** The admission/cache machinery from the
previous phases exists; wiring it to frame-time budgets (cold, warm, steady
state, 1% lows) is the last step, not the first.

## 6. Cross-run range

Three full runs of the same build, same host, different background load:

| quantity | run A | run B | run C (committed) |
| --- | ---: | ---: | ---: |
| `porte_pe` `cpu_bound` frame | 170.5 ms | 176.4 ms | 204.3 ms |
| `porte_pe_clang` `cpu_bound` frame | 164.8 ms | 164.9 ms | 190.0 ms |
| `native_arm64` `cpu_bound` frame | 1.63 ms | 1.63 ms | 1.68 ms |
| `rosetta_x86_64` `cpu_bound` frame | 3.07 ms | 3.09 ms | 3.25 ms |
| `cpu_bound` gap, `porte_pe` | +10,344% | +10,778% | +12,056% |
| `mixed` gap, `porte_pe` | +162% | +166% | +174% |
| `gpu_heavy` gap, `porte_pe` / `porte_pe_clang` | +0.9% / +0.5% | +0.9% / +2.8% | +0.8% / +2.0% |
| state hashes | match | match | match |

The GPU-bound and Rosetta results are stable; the emulated CPU-bound result is
not, which is itself a finding: a single emulated core is both the bottleneck and
the source of jitter.

## 7. Limitations of this baseline

- One synthetic workload, one machine. It has no AI, physics, audio, file I/O,
  input, driver, or multi-threading.
- The Windows arms call one host function per frame. Windows API transition cost
  in a real application (hundreds of calls per frame, some blocking) is
  unmeasured.
- No D3D, no shader translation, no pipeline-state churn: the graphics layer is
  identical by construction, which isolates the CPU path but leaves the
  graphics-path objective untouched.
- GPU durations vary by up to ~20% depending on how the CPU feeds the queue
  (visible when a slow CPU leaves the GPU idle); conclusions use ratios that
  survive the reported spread.
- Porte's memory and start-up numbers include the Python host and are not
  comparable to the native process.
- Rosetta 2 is not a Windows translator and is used only as a "production
  translator on this silicon" reference.
