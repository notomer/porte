# Experiment report: execution-guided nativeization on an M2 Max

All numbers below were produced by `python3 -m porte experiment --repeats 7` on
this machine and stored verbatim in `results/experiment.json` (raw per-run
samples included). Nothing is estimated unless labelled as such.

## 1. Setup

| item | value |
| --- | --- |
| host | Mac14,6, Apple M2 Max, 12 CPUs, 32 GB, macOS 27.2 (build 26B123) |
| toolchain | Apple clang 21.0.0 (clang-2100.3.34.2), SDK 27.0 |
| translated baseline | x86-64 Mach-O under Rosetta 2 |
| native baselines | arm64 generic module: libc `qsort`; and a control with an in-module introsort |
| workload | 2,000,000 elements, 200,000 rank queries / 2,000,000 audit records |
| repeats | 7 fresh processes per arm per workload, interleaved round-robin, 1 warm-up run per arm |

The experiment answers the primary hypothesis on a *controlled fixture*, not on
a Windows PE. The reasons and the migration path are in
`docs/01-architecture.md`; the short version is that this host has no Wine/FEX
runtime, Rosetta cannot mix native code into a translated process, and the part
under test (facts → guarded native replacement → proof → cache) is independent
of which translator supplies the baseline.

## 2. What was measured

Three questions, one pipeline:

1. Does an execution-derived specialization beat **the best generic native
   build of the same module** (not just the translated baseline)?
2. Does it stay correct on inputs that violate its assumptions, and does it
   actually fall back?
3. Is a verified artifact reusable across launches for materially less than the
   cold pipeline?

## 3. Profiling results (Component A)

Static inventory of the x86-64 module: 94 basic blocks, 475 instructions, 31
blocks ending in an indirect transfer.

| workload | samples | effective rate | in module | unattributed | instrumentation overhead |
| --- | ---: | ---: | ---: | ---: | ---: |
| `rank_small_range` | 46 | 342 Hz | 26 (20 in `cmp_i32`) | 20 (libc qsort, shared cache) | +1.25% |
| `audit_basic` | 140 | 2919 Hz | 4 | 134 in `host_emit` + 2 unattributed | +33.16% |

Overhead is the median of three interleaved profiled/plain runs with the probe
enabled in all three. It is strongly workload-dependent: 1.25% on rank, where
the 4 kHz timer collapsed to 342 Hz, and 33.16% on audit, where it sustained
~2.9 kHz. Frequent signals are expensive under Rosetta — they interrupt
translated blocks — so sampling is an attribution tool, not the source of
admission facts. Boundary records and the instrumented fact pass are.

What the profile said, and what drove each decision:

- **rank:** the hot *module* code is the comparator (`cmp_i32`, 77% of resolved
  module samples); the sort itself is libc code the translator sees as a black
  box. The instrumented pass observed `min=0, max=4095, n=2,000,000`: a value
  span 488× smaller than n. That fact — not the PC histogram — justifies
  replacing the sort with a counting sort under a range guard.
- **audit:** 97.1% of samples land in `host_emit`, one host transition per
  record (2,000,000 transitions, 2,000,000 ledger entries). That fact justifies
  batching the effect sequence.
- Boundary facts confirmed 64-byte-aligned buffers, disjoint parameter/input
  ranges, and a single call per run.

Sampling is honest about its limits: the effective rate under Rosetta was
342 Hz in the comparator-heavy run against 4000 Hz requested (timer coalescing),
and libc qsort's internals live in the dyld shared cache and cannot be
symbolized post-hoc, so they are counted as unattributed rather than guessed.
Neither limits the decision, which rests on boundary and fact records.

## 4. Steady-state results (Components B + C)

Median kernel time with p10–p90 in parentheses; wall is whole-process time; RSS
is median max RSS. `nativeized` is the generated module with guards and fallback.

### `rank_small_range` — observed window `[0,4095]`

| arm | kernel | wall | RSS | path |
| --- | ---: | ---: | ---: | --- |
| baseline (x86-64 → Rosetta) | 132.49 ms (131.80–144.94) | 150.03 ms | 26.3 MiB | generic |
| native generic (libc qsort) | 92.13 ms (91.83–93.67) | 100.34 ms | 24.8 MiB | generic |
| native generic (in-module introsort, control) | 72.26 ms (72.11–72.91) | 80.30 ms | 24.8 MiB | generic |
| **nativeized** | **4.97 ms (4.92–5.08)** | **13.21 ms** | 24.9 MiB | fast |

Speedups: **14.55×** vs the fair native control, **18.55×** vs the libc-qsort
native build, **26.68×** vs the translated baseline. RSS is flat.

### `audit_basic` — one host transition per record

| arm | kernel | wall | transitions | path |
| --- | ---: | ---: | ---: | --- |
| baseline (Rosetta) | 36.30 ms (35.98–37.17) | 62.70 ms | 2,000,000 | generic |
| native generic | 17.05 ms (16.91–17.10) | 33.65 ms | 2,000,000 | generic |
| **nativeized** | **9.53 ms (9.31–9.73)** | **25.92 ms** | **15,625** | fast |

**1.79×** vs native generic and **3.81×** vs translated, with a byte-identical
ledger of 2,000,000 entries. The transition count drops 128×; the effect
sequence is unchanged, which is the point: the number of `WriteFile`-shaped
calls is not observable, their effects are.

### Guard-violating cases

| workload | native generic | nativeized | result |
| --- | ---: | ---: | --- |
| `rank_wide` (full int32 range) | 175.54 ms | 175.66 ms | fallback, identical digest |
| `rank_unaligned` (misaligned, in-window) | 92.18 ms | 4.96 ms | fast, identical digest |
| `rank_tiny` (n=16) | 1 µs | 2 µs | fallback; wall 3.67 → 3.59 ms (noise) |
| `audit_interleaved` (observer) | 16.90 ms | 16.76 ms | fallback, identical ledger |

The guard costs one streaming min/max pass on the fallback path; on a 2 ms
workload that is free, on a 1 µs workload it doubles kernel time but stays
inside process-wall noise. `rank_wide` also exposes a baseline issue rather than
a specialization issue: the generic module's libc qsort is 65% slower than the
in-module introsort control on wide data (175.54 vs 106.58 ms), so a fallback
into it inherits that (0.61× of the inline control).

## 5. Correctness evidence (Component C)

`porte verify` over the six-case corpus: **6/6 pass**. Every case compares
return value, output digest and ledger bytes across translated baseline, native
generic and candidate, and asserts the expected fast/fallback path.

`porte selftest` compiles a counting-sort variant with prefix sums starting at 1
and requires the oracle to reject it. Result: rejected, failing
`return_matches_baseline`, `output_matches_baseline`,
`return_matches_native`, `output_matches_native`, and `fast_path_taken`.
The oracle is therefore sensitive to a one-line semantic bug; it is not a
rubber stamp.

This is evidence, not proof. Guards bound the assumptions, the corpus covers the
guards that exist, and any change to executable, module, profile, toolchain,
host, OS build or corpus version changes the cache key and forces re-verification.

## 6. Reuse and cost (Component D)

| stage | time |
| --- | ---: |
| cold: profile (2 workloads) | 1.82 s |
| cold: compile generated module | 0.093 s |
| cold: full differential verification | 1.67 s |
| cold total (profile + compile + verify) | ≈ 3.58 s |
| warm: cache load | 350 µs |
| warm: smoke re-verification (3 cases) | 479 ms |

Cache key `c1785936a00fdd36fe49df8e7e19c466` for this run. Changing the simulated
OS build produces exactly one invalidation reason (`host.os_build`); changing
the module hash produces exactly one (`module_sha256`). The key covers schema,
Porte version, arch, application hash, module hash, profile digest, corpus
digest, transform name/version/source hash, host identity and toolchain. The
profile digest deliberately excludes raw boundary pointers, which change with
ASLR on every run; including them would make every key unique and turn the
cache into a no-op. `porte warm` recomputes the key from the saved profile in a
fresh process, reports `key_matches: true` and re-verifies the artifact.

The warm path is ~7.5× cheaper than the cold path, but it is not free: smoke
verification dominates it (479 ms vs 350 µs to load). That trade is deliberate —
an artifact is never trusted on its key alone.

## 7. First-run (translation) overhead

Fresh byte-identical copies at a new path, first run versus second run:

| arm | first run | second run | overhead |
| --- | ---: | ---: | ---: |
| x86-64 under Rosetta | 554.9 ms | 150.8 ms | 404.1 ms |
| arm64 native control | 237.8 ms | 100.8 ms | 137.0 ms |

The native control shows that not all first-run cost is translation (page cache,
frequency ramp), so the translation-attributable upper bound is roughly
**267 ms** for this workload. This is an indirect measurement of an opaque
cache, reported as an upper bound, not as "Rosetta translation time".

## 8. Threats to validity

1. **Baseline strength.** The obvious criticism of a 27× number is that libc
   `qsort` is a weak baseline. That is why the in-module introsort control
   exists; the claim is 14.55× against it. The remaining gap is the
   specialization, not the comparator call.
2. **Synthetic fixture.** The kernel is our code, with a source-level contract.
   The specialization is generated from a model of the module, not lifted from
   an arbitrary binary. This bounds the claim to the pipeline, not to "arbitrary
   EXE support".
3. **No PE, no Win32, no GPU.** Runtime transitions here cost a lock and an
   append, not a Win32 thunk; graphics are untouched.
4. **Guard cost on small inputs** is a real, measured regression (0.5× kernel
   time at n=16), mitigated only by the size floor.
5. **Sampling resolution and cost** under Rosetta are uneven: 342 Hz observed
   for comparator-heavy code and +33% overhead when the timer does sustain
   ~2.9 kHz. Shared-cache frames are not symbolizable, so block-level
   attribution is coarse.
6. **Cold-start measurement** confounds translation with page-cache and
   frequency-ramp effects; the native control quantifies the confound but does
   not eliminate it.
7. **Statistical scope.** 7 repeats, medians with p10–p90 reported; the machine
   is otherwise idle but not isolated (no pinning, no fixed frequency).
8. **Equivalence is not proven.** It is evidenced on a corpus plus guards. An
   untested input that violates an unguarded assumption is the residual risk.
9. **One mutant only.** The mutation test demonstrates sensitivity to one class
   of bug; a broader mutant set (guard removal, wrong window, reordered effects,
   dropped entries) is required before trusting admission at scale.
10. **Thermal/frequency drift** is mitigated by interleaving, not controlled.

## 9. Negative and abandoned results

- **First transform abandoned.** The initial plan was to exploit an aliasing
  ambiguity (`p->mode` reloaded per iteration) and specialize the loop. Clang
  had already hoisted the value, so the specialization measured 1.00×. It was
  dropped instead of reported as a win.
- **Benchmark trap discovered.** A microbenchmark of an LCG loop ran at
  0.145 ns/iteration on x86-64 under Rosetta versus 0.933 ns/iteration natively
  — 6.4× faster while translated. Disassembly showed the x86 backend had
  composed the 8-step affine recurrence into a single multiply-add, while the
  arm64 build did not. This is a codegen asymmetry, not a translation effect.
  It is why the fixture kernels are data-dependent and why every arm is built
  from identical source with the same flags.
- **Tiny-input regression.** `rank_tiny` is 0.5× native in kernel time (2 µs
  vs 1 µs) because the guard is a full pass; the fast path is correctly
  declined, and process wall is unchanged within noise.
- **Sampling can cost a third of a short kernel.** On `audit_basic` the 4 kHz
  timer sustained ~2.9 kHz and the profiling build was 33% slower than the plain
  build, while on `rank_small_range` the timer collapsed to 342 Hz and cost
  1.25%. This is why the admission facts come from boundary and fact records and
  sampling is treated as an occasional audit, not a continuous profiler.
- **Modest boundary win.** 1.79× is a lower bound for API fusion: a real Win32
  transition costs far more than a mutex + append, but that is not measured here
  and is not claimed.
- **No free lunch for wide data.** When the guard fails, the candidate is
  exactly as fast as its baseline (1.00×) and slower than the best possible
  generic implementation (0.61×). Specialization does not improve the general
  case; it moves work from the general case to the observed one.

## 10. Reproduce

```bash
python3 -m porte build --force
python3 -m porte experiment --repeats 7     # writes results/experiment.json
python3 -m porte selftest                   # oracle must reject the mutant
python3 -m porte verify --dylib build/spec/staging/libporte_kernel_spec_arm64.dylib
python3 -m porte cache list
```

`results/experiment.json` contains every per-run sample, the full profile
documents, the plan assumptions, the oracle verdicts, the mutation result and
the environment used for the cache key.
