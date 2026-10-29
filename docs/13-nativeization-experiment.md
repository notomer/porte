# 13 — The experiment: can Porte accelerate real Windows code on this Mac?

The claim under test is the one the product depends on: *Porte can make a real
Windows program run measurably better than an existing compatibility runtime by
generating its own native code.* Three measurements decide it. All were run on
this host: Apple M2 Max, macOS 27.2 (26B123), Wine Staging 11.20, Rosetta 2
present, 7-Zip 26.05 (x64, official MSI) as the third-party program.

Everything here is reproducible from this repository; the commands are quoted.

---

## 1. Experiment 1 — can native ARM64 code run inside a Wine process?

**Hypothesis (C1):** Porte can replace hot Windows functions with native ARM64
code inside the process the compatibility runtime starts.

**Method.** A Rosetta (x86-64) program that tries to load a native arm64 dylib:

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

**Conclusion.** Refuted. A Rosetta-translated process cannot host ARM64 code, so
C1 cannot be implemented on macOS with any amount of engineering. This is a
platform property, not a Porte limitation.

---

## 2. Experiment 2 — where does a real Windows program's time actually go?

**Question.** If C1 is impossible, is there anything else worth optimizing, and
can it be located?

**Instrument** (new in this phase): `porte/backends/profile.py`, exposed as

```bash
porte runtime profile 'C:\Program Files\7-Zip\7z.exe' \
  --windows-path 'C:\Program Files\7-Zip\7z.exe' \
  --bottle 7-zip-26-05-x64-edition-96360287 \
  --arg=a --arg=-t7z --arg=-mx=9 --arg=-mmt=off \
  --arg='C:\out4.7z' --arg='C:\big.zip' --seconds 8
```

It starts the guest under the discovered runtime, samples the process with
`/usr/bin/sample`, classifies every sampled frame by the layer it belongs to,
and writes `results/wine_profile.json`. The workload is 7-Zip compressing a
115 MB archive with LZMA at `-mx=9`, single-threaded — a genuine, CPU-bound,
third-party Windows x86-64 program.

**Result** (28,739 samples):

| layer | share |
| --- | ---: |
| unattributed system ranges (threads waiting in `mach_msg`) | 56.3% |
| Rosetta runtime routines | 22.3% |
| translated guest x86-64 | 21.3% |
| Wine unix-side modules / host | 0% of sampled leaves |

Of the samples that are executing rather than waiting, **≈49% are inside
Rosetta's runtime** and 51% in translated guest code. The process is reported by
the sampler as `Code Type: X86-64 (translated)`.

**The attribution limit, measured rather than assumed.** The sampler names the
guest's code `Rosetta JIT` and reports a JIT address such as `0x200196e5c`.
There is no mapping back to the `7z.exe` instruction that produced it, and
Wine's own Win32 PE modules are translated into the same JIT region. So the
21.3% cannot be split between the application and Wine, and a
profile-guided optimizer cannot obtain the per-function hot spots it would need.
This is the same fact as Experiment 1 seen from the profiling side.

---

## 3. Experiment 3 — the existing engine's ceiling, re-measured

**Question.** For code Porte *can* control (its own engine, not Wine), how close
does generated ARM64 code get to native ARM64?

**Method.**

```bash
porte pe experiment fixtures/opaque/out/crc.exe --workload 262144
```

Each image is stripped, has no exports, and is optimized without source or
symbols; the pipeline profiles, lifts one hot loop to ARM64, proves equivalence
over a 22-case corpus, caches it, and benches seven repeats.

**Result (2026-10-29, median ms per run):**

| image | interpreter (baseline) | translated ARM64 | specialized ARM64 | speedup vs interpreter | native ARM64 reference | still slower than native |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| blob (n=1e6) | 2.207 | 0.562 | **0.432** | 5.11× | 0.0221 | 19.5× |
| lookup (n=65536) | 2.478 | 0.538 | **0.563** | 4.40× | 0.256 | 2.2× |
| sieve (n=1e6) | 61.517 | 6.924 | **6.206** | 9.91× | 1.261 | 4.9× |
| crc (n=262144) | 1.623 | 1.072 | **1.024** | 1.58× | 0.776 | 1.3× |
| fault (n=16) | 0.104 | 0.180 | 0.169 | 0.61× (regression) | — | — |

**Correctness.** The differential oracle compares baseline and specialized
execution from the entry point over the 22-case corpus (`0, 1, 2, 3, 7, 16, 63,
64, 65, 127, 255, 256, 257, 511, 1023, 1024, 1025, 2047, 2048, 4096, 65536,
1000000`): stdout bytes, exit code, fault, heap digest and image digest. The
Phase-2 report records 22/22 on all five images, a corrupted-trace rejection,
and demotion to interpretation when the image's memory layout changes.

**What these numbers do and do not mean.** The 1.6×–9.9× is real and measured,
but it is measured *against Porte's own interpreter*. Against a native ARM64
build of the same algorithm, the specialized code is 1.3×–19.5× slower. Porte's
engine is not competitive with a general translator; it is a research vehicle
that proves guarded specialization can be made *verifiable*, not that it can be
made *fast enough*.

---

## 4. Putting the three together

| question | answer | evidence |
| --- | --- | --- |
| Can Porte's ARM64 code run inside Wine? | no | §1 |
| Can Porte locate hot code inside Wine to optimize it? | no | §2 |
| Is the overhead worth removing? | yes, ~49% of executing samples are Rosetta runtime | §2 |
| Can Porte's own engine reach native speed? | no, 1.3×–19.5× behind | §3 |
| Can Porte's engine run a real program? | no: 15 host stubs, no `user32`/`gdi32`/SEH | `docs/12` §3 |

The picture is consistent: the overhead is real, it is inside Rosetta, and every
mechanism Porte has for removing overhead lives outside Rosetta. That is the
whole finding, and it is why the brief's B+C cannot be delivered as stated.

---

## 5. Status against the milestone criteria

From `docs/12` §8 ("native execution of a real, unmodified Windows console
program"):

| # | criterion | status |
| --- | --- | --- |
| 1 | a real third-party console program runs in the engine | **not met** — no real program's imports are served |
| 2 | 100% of its imports stubbed, no silent fallback | **not met** — 15 stubs (`ExitProcess`, `WriteFile`, `VirtualAlloc`, …) |
| 3 | ≥ 1.0× vs Wine on the same workload | **undefined** — cannot run the same workload yet |
| 4 | no divergence on the boundary corpus | met for the five fixtures only |
| 5 | bounded host-code cost | untested |

Two of the five criteria cannot even be evaluated until the engine runs one real
program. That is the next milestone, and it is the only one worth funding.

---

## 6. Novelty assessment

- **Novel and demonstrated here:** a guarded, profile-driven specialization of an
  *opaque* Windows binary with a differential oracle and a versioned cache. The
  competitive audit (`docs/11`) found no project doing this. The mechanism is
  real; the performance is not competitive.
- **Novel and demonstrated here:** a layer-classifying sampler for Wine guests on
  macOS that states its own attribution limit instead of inventing per-function
  numbers.
- **Not novel:** runtime discovery, prefixes, installer recipes, `.app`
  generation, Finder integration. Those exist elsewhere (§11).
- **Not novel, and not built:** instruction-level x86-64 rewriting, JIT
  translation, graphics translation.

---

## 7. Limitations, stated plainly

1. One third-party program was profiled (7-Zip/LZMA). It is representative of
   CPU-bound Win32 code, not of games or I/O-bound applications.
2. `sample` aggregates threads; 56% of samples are waiting threads. The
   executing-only split (49/51) is derived from the same sample set, not from a
   separate instrumented run.
3. The engine measurements use tiny fixtures (3 KB–527 KB) with a single hot
   loop and no OS interaction; they say nothing about real application
   performance.
4. No GPU measurement was taken in this phase; the frame numbers in
   `results/frame_baseline.json` predate it and are unchanged.
5. The arm64-in-x86-64 restriction was verified with `dlopen`; other
   hypothetical channels (for example a native helper *process* called over
   IPC) were not attempted, because per-instruction IPC is not a plausible
   speedup.

---

## 8. Recommendation for the next milestone

Build the narrow version of B and gate it on numbers:

1. Pick one real Windows **console** program whose import surface is small
   (a file/stream utility). Implement the host APIs it needs, one DLL family at
   a time.
2. Run it under Wine and under the engine on identical inputs; require
   byte-identical output over the corpus.
3. Bench both. If the engine is not at least at parity with Wine on that program
   after it is complete, stop: Wine + Rosetta is better, and Porte should not
   ship an execution path that is slower and less compatible.

Until that runs, Porte's honest position is: **a research project with one
original mechanism and no product that beats what exists.**
