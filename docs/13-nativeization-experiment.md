# 13 — The experiment: can Porte make real Windows code run faster on this Mac?

The claim under test is the one the product depends on: *Porte can make a real
Windows program run measurably better than the compatibility runtime that would
otherwise run it, without changing what the program does.*

Phase 3 answered "no" and named a platform constraint. Phase 4 found that the
constraint was true of one execution architecture and false of the other, built
the missing instrument, and produced a verified win. This document keeps the
Phase 3 experiments (they are still correct, now scoped) and adds the ones that
replaced its conclusion.

Host: Apple M2 Max, macOS 27.2 (26B123). Runtimes: Wine Staging 11.20 (x86-64
under Rosetta 2) and MetalSharp Wine 4.0 (Wine 11.20 native arm64 + FEX-2610).
Program: 7-Zip 26.05 x64, installed from the official MSI, no source.

---

## 0. What the Phase 3 conclusion got wrong

Phase 3 measured that a Rosetta process cannot load an arm64 dylib (§1) and
concluded that native code could never join a Wine process on macOS. The first
half is a fact about Rosetta. The second half assumed Rosetta was the only way
Wine runs here — which stopped being true when Wine itself was built for arm64
and given a macOS port of FEX (`docs/11` §3). In that process the guest is JIT
output inside an **arm64** process, and native code is welcome.

The correction is not a technicality: it decides whether strategy C1 is
impossible or merely unbuilt (`docs/12` §4).

---

## 1. Experiment 1 — arm64 code inside a Rosetta process (architecture R)

**Method.**

```bash
clang -dynamiclib -arch arm64 -o libnative.dylib native.c
clang -arch x86_64 -o loader loader.c
arch -x86_64 ./loader
```

**Result.**

```
dlopen failed: ... (mach-o file, but is an incompatible architecture
(have 'arm64', need 'x86_64'))
```

**Conclusion (unchanged, scoped).** Under Rosetta, native ARM64 code cannot enter
the process. Any plan that assumes it can, in *that* architecture, is wrong.

---

## 2. Experiment 2 — where a Rosetta guest's time goes (architecture R)

**Instrument:** `porte/backends/profile.py`, `porte runtime profile`.

**Result** (7-Zip LZMA, 28,739 samples): 56.3% of samples are threads waiting,
22.3% are Rosetta's own runtime routines, 21.3% are translated guest code — so
≈49% of *executing* samples are translation overhead. The sampler reports the
guest as `Rosetta JIT` with no mapping back to `7z.exe`, and Wine's Win32 modules
are translated into the same region.

**Conclusion (unchanged, scoped).** In architecture R the overhead is real, it is
inside Rosetta, and it cannot be named or removed from outside.

---

## 3. Experiment 3 — the baselines on native ARM64 Wine (new)

**Question.** Architecture N exists. Is it faster, and by how much against
native ARM64? Everything Porte might contribute has to be measured against these
numbers, not against an interpreter.

**Method.** `porte runtime workload` on one deterministic 30 MiB corpus
(`fixtures/workload/make_input.py`, sha256 `3b9fb805…`), LZMA at `-mx=9`,
`-mmt=off`, 5 repeats, **round-robin** between backends so a laptop that slows
down mid-run cannot hand the win to whichever backend ran first. CPU is the sum
of the guest process's own CPU time sampled from the kernel; memory is the sum of
descendant peak RSS; output is the SHA-256 of the archive.

```bash
porte runtime workload --job fixtures/workload/7zip-lzma.json \
  --out results/arm64_wine_bench.json
```

**Result** (`results/arm64_wine_bench.json`):

| backend | wall median | wall min | CPU | RSS | threads | vs first |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Wine 11.20 under Rosetta | 20,484 ms | 19,465 ms | 15,450 ms | 351 MB | 5 | 1.00× |
| MetalSharp Wine 4.0 (arm64 + FEX) | 21,190 ms | 20,370 ms | 16,670 ms | 420 MB | 4 | **0.97×** |
| the same, memory-model specialization (§5) | 19,430 ms | 18,287 ms | 14,900 ms | 417 MB | 5 | **1.09×** |
| native arm64 7-Zip 26.05 (`7zz`, Homebrew) | 13,505 ms | 12,757 ms | 13,030 ms | 336 MB | 1 | 1.57× |

All runs exited 0 and every archive passed an integrity check. The three Windows
archives are **byte-identical** to each other; the native archive differs in its
header (Windows vs POSIX file attributes), which is why correctness is reported
two ways in `docs/14`.

**What this means.**

1. Native ARM64 Wine is **not** faster than Rosetta here (0.97×). The architecture
   change buys address-space mixing and guest attribution, not speed.
2. The gap to native is **1.57×** — that is the entire ceiling for any
   optimization layer on this workload, and it is smaller than the "near-native"
   framing implies.
3. The only number that improved for free was memory-model emulation, and that is
   the one Porte could reach (§5).

---

## 4. Experiment 4 — can the guest be named? (new)

**Hypothesis (C1's prerequisite).** A profiler can attribute a native-ARM64 Wine
guest's samples to the Windows functions that produced them.

**Instrument:** `porte/backends/fex_profile.py`, `porte runtime fex-profile`.

FEX writes a perf map when told to name its JIT output
(`FEX_LIBRARYJITNAMING=1`): one line per translated block, host address, size and
the guest module + RVA it came from.

**Result** (`results/arm64_wine_profile.json`, 7-Zip LZMA, 10,015 samples):

| class | samples | share |
| --- | ---: | ---: |
| threads waiting in the kernel | 7,761 | 77.5% |
| executing inside FEX's JIT region, **no named block covers the address** | 2,254 | 22.5% |
| in a named guest block | **0** | 0% |
| host code | 0 | 0% |

The map itself is substantial and correct in form: **6,277** blocks with a guest
origin were written during the run, including 3,631 for `7z.dll` and 3,399 for
`7z.exe` — exactly the modules the hot loop lives in. But **none of the 57
distinct top-of-stack addresses** in the sample falls inside a named range: the
addresses sit in gaps between the blocks FEX named.

That last sentence cost this phase a bug to establish. The first implementation
looked blocks up with a binary search, which is wrong for this data twice over:
FEX's map contains overlapping ranges (1 MB region markers, several entries for
the same translated block at different addresses), so a bisect can report "no
block" for code that is mapped. The instrument now scans, prefers entries that
carry a guest origin, and only then falls back to the region marker — which is
how the same samples moved from "unattributed" to "inside the JIT region but
outside every named block". A profiler that gets this wrong flatters itself in
both directions.

**Conclusion.** Guest attribution is *possible* on this stack — the mechanism
exists, is unprivileged, and is produced by the runtime itself — and it is *not
working* in MetalSharp Wine 4.0 as shipped: the hot code is unnamed. The most
plausible reading is that the hot blocks are exactly the ones FEX rewrites while
linking (relocation, block splitting), and those versions are not re-named; that
is a hypothesis, not a measurement. It is an implementation property of a
MIT-licensed translator, not a platform limit. Until it is fixed, no
profile-guided, function-level optimization can be aimed from a profile of this
program.

---

## 5. Experiment 5 — the optimization Porte can actually ship (new)

**Question.** Is there a measurable compatibility cost that Porte can remove for
a real program, prove removed, and keep across launches — without changing the
program and without forking FEX?

**The cost.** x86 has a strong memory model; ARM64 does not. FEX emulates it in
software with release/acquire sequences (`FEX_TSOENABLED`, plus separate switches
for half-barriers, vector operations and `memcpy`/`memset`). A program that never
runs two threads at the same time **cannot observe** those barriers: there is no
second observer to see a store out of order.

**The guard (measured, not assumed).** Porte runs the workload once and compares
the guest's own CPU time with wall time:

| 7-Zip 26.05, same input | CPU | wall | parallelism | decision |
| --- | ---: | ---: | ---: | --- |
| `-mmt=1` | 15.5 s | 17.3 s | **0.90** | candidate proposed |
| `-mmt=4` | 13.4 s | 9.7 s | **1.39** | **refused** |

(`results/arm64_specialization_guard.json`.) The same binary, the same input, two
thread settings: the guard separates them. It also refuses a run it did not
actually observe (`samples ≥ 10`, `wall ≥ 1 s`, `CPU ≥ 0.2 s`), because "no data"
must never read as "one thread".

**The proof.** Porte then runs the baseline and the specialization on the same
workload, interleaved, and accepts only if the output is byte-identical, every
run exited 0, the archive passes an integrity check, and the median wall clock
improves by ≥ 3%:

```bash
porte runtime specialize --job fixtures/workload/7zip-lzma.json \
  --baseline arm64-wine-fex --repeats 5 \
  --out results/arm64_specialization.json
```

**Result** (`results/arm64_specialization_record.json`, 5 repeats):

| | baseline | specialized | change |
| --- | ---: | ---: | ---: |
| wall clock (median) | 16,279 ms | 14,951 ms | **1.089×** |
| wall clock (min) | 15,708 ms | 14,553 ms | 1.079× |
| CPU | 15,960 ms | 14,650 ms | **1.089×** |
| peak RSS | 418.3 MB | 417.8 MB | unchanged |
| output | sha256 `e85b8f18…` | sha256 `e85b8f18…` | identical |

Four evaluation runs of the same job gave 1.034×, 1.084×, 1.089× and 1.114× wall
clock (median of the three five-repeat runs: 1.084×); the reported record is the
5-repeat run. The CPU reduction is the more stable number (1.06–1.11×), which is
what an emulation-cost change should look like.

**What is stored.** A record keyed by the program's digest, in
`~/Library/Caches/Porte/specializations/`, holding the observation, the guard
result, the environment to apply, and the evidence. Only accepted records load
back (`adaptive.load`), so a rejected specialization cannot be applied by
accident.

**What this is not.** It does not generate ARM64 code. The code is FEX's; Porte
decides which semantics FEX must emulate and proves the choice. The claim it
supports is precise: **Porte makes a real Windows program faster than the same
runtime without Porte, by removing compatibility work the program does not use.**
It is not a translation speedup and it is not near-native.

---

## 6. The engine's ceiling, re-measured (retained from Phase 3)

For code Porte *can* own entirely, the numbers have not changed
(`results/opaque_pe.json`): 5.11× (blob), 4.40× (lookup), 9.91× (sieve), 1.58×
(crc) over Porte's own interpreter, 0.61× on `fault` (a regression), and still
1.3×–19.5× slower than native ARM64. Correctness is byte-exact over a 22-case
corpus on five stripped images.

That is a research vehicle that proves guarded specialization can be *verified*,
not one that has shown it can be *fast*.

---

## 7. Status against the milestone criteria

Phase 4 gate (from `docs/12` §8, as revised): *a specialization that beats the
unmodified backend on a real Windows program, with correctness.*

| # | criterion | status |
| --- | --- | --- |
| 1 | a real third-party program is specialized | **met** — 7-Zip 26.05 x64, official MSI, no source |
| 2 | the win reproduces | **met** — 1.03–1.11× wall over four runs, ≥ 1.06× CPU |
| 3 | correctness holds | **met for this workload** — byte-identical archives over 20/20 runs, integrity checks pass |
| 4 | the guard refuses unsafe cases | **met** — 0 candidates at `-mmt=4` (parallelism 1.39) |
| 5 | the specialization persists | **met** — record cached by program digest, reloaded by `adaptive.load` |
| 6 | no FEX fork | **met** — stock MetalSharp Wine 4.0, environment variables only |

Milestone for the next phase, and it is deliberately harsher: criterion 1 must
hold for a **second, different program**, criterion 2 on both, criterion 3 over
≥ 20 inputs, or the result is a measurement rather than a capability
(`docs/12` §8).

---

## 8. Novelty assessment

- **Novel and demonstrated:** an automatically *guarded* semantic specialization
  of an opaque Windows binary — the guard derived from measured CPU/wall
  parallelism, the effect proven byte-exact against the unmodified runtime, and
  the decision persisted per program. The audit found no project shipping this;
  MetalSharp ships a *documented global switch* (`FEX_TSOENABLED=0`, "faster for
  programs that don't need it") that the user must judge per game by hand. Porte's
  contribution is the decision and the proof, not the flag.
- **Novel and demonstrated:** a workload comparison harness that measures a real
  Windows program across two runtimes, native code and a specialization on
  identical input, with CPU and memory taken from the kernel rather than from the
  program's own output.
- **Novel mechanism, incomplete result:** FEX perf-map guest attribution. The
  instrument is Porte's; the data source is FEX's; the coverage is currently zero
  for hot code (§4).
- **Not novel:** FEX, Rosetta, Wine, ARM64EC, JIT translation, prefix management,
  `.app` bundling, Finder integration, MSI handling, per-app environment tuning.

---

## 9. Limitations, stated plainly

1. **One program, one workload.** 7-Zip compressing one deterministic corpus.
   CPU-bound LZMA is not a game, a browser or an installer, and the guard's
   verdict is a property of the workload as much as of the program.
2. **The guard is empirical.** "No two threads ran at once, on this input" is
   evidence, not proof. A different input can make the same program overlap two
   threads; the recorded evidence would then be stale, which is why the record
   stores the observation and why the runtime-side re-check matters before this is
   wired into a launcher for arbitrary documents.
3. **The gain is bounded.** 1.09× of a 1.57× gap. Nothing here closes the
   translation gap, which lives in the guest's own code.
4. **No per-function attribution.** §4 measured zero mapped hot samples in this
   FEX build.
5. **No ARM64EC generator.** C1 is reachable but unbuilt; the toolchain path
   (`llvm-mingw` with ARM64EC runtimes) was identified, not attempted.
6. **Thermal and load variance is real.** Wall-clock medians moved by up to 20%
   between sessions, which is why repeats are interleaved and why CPU is reported
   alongside wall clock.
7. **The engine measurements still use toy fixtures** (3 KB–527 KB) with no OS
   interaction.

---

## 10. Recommendation

Go, narrowly, and only on numbers:

1. **Ship the specialization as it stands** — `porte runtime specialize`, with the
   guard, the proof and the cached record. It is the first Porte feature that
   makes a real Windows program measurably faster than the runtime it sits on.
2. **Reproduce it on a second program** before calling it a capability. Different
   vendor, different workload shape, ideally not a compressor.
3. **Do not build C1 yet.** The mechanism is proven (ARM64EC modules do load into
   an x86-64 guest) but the generator and the guest-call path are a compiler
   project, and the profiling input it would need is currently unavailable (§4).
4. **Do not fork FEX.** Two upstreamable contributions are already identified: a
   complete perf map for relinked blocks, and the measured cost of each TSO
   component. Both are worth more to Porte than a private fork.
5. **Stop if the second program does not reproduce the win.** At that point the
   honest outcome is to contribute the instrument and the finding and stop
   claiming an execution platform.
