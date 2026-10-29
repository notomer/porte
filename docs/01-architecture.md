# Architecture and technical risks

## 1. Design constraint that shapes everything

On Apple Silicon the translation boundary is the *process*. Rosetta translates
an entire x86-64 Mach-O process; native ARM64 code cannot be entered from inside
it. FEX-based systems (MetalSharp, Ferrum) translate in-process, which is why
they can mix native arm64 Wine with translated x86-64 application code. Any
real Windows nativeization therefore has to happen at one of these seams:

1. **module boundary** — replace a hot DLL/EXE image with a native one (the
   shape used by this prototype), or
2. **call boundary** — replace or fuse a guest→host call sequence with native
   code that preserves its effect ledger (ARM64EC thunks are the existing
   mechanism; fusion is the new capability).

The prototype implements both shapes against a controllable fixture, because a
real PE runtime is not installable on this host (Wine's Homebrew cask is
disabled; upstream FEX targets Linux; MetalSharp/Ferrum would have to be forked
to expose an injection point). The profiler, plan format, oracle and cache are
translator-agnostic: the runtime adapter is the only part that changes when the
baseline moves from Rosetta-on-Mach-O to FEX-on-PE.

## 2. Components

```
                 fixtures/app (same source, two arches)
                     │  boundary call
                     ▼
  ┌─────────────────────────────┐        ┌──────────────────────────┐
  │ generic module (baseline)   │        │ probe (PC sampling +     │
  │  rank: libc qsort           │◄───────┤ boundary args + facts)   │
  │  audit: host_emit per record│        └────────────┬─────────────┘
  └─────────────────────────────┘                     │ raw
                 ▲                                    ▼
                 │ fallback                Component A: profiler
                 │                          (symbolize, hot blocks, facts)
  ┌──────────────┴──────────────┐                     │
  │ specialization (generated)  │◄──── Component B ───┘
  │  guarded fast paths         │      (plan → C → clang -arch arm64)
  └──────────────┬──────────────┘
                 │ admitted by
                 ▼
  Component C: differential oracle (3 arms × corpus, guards as cases,
  mutation test that must fail) ──► Component D: versioned cache
                                     (key = exe+module+profile+host+
                                      OS+toolchain+corpus+transform)
```

### A. Execution profiler (`porte/profile.py`, `runtime/porte_probe.[ch]`)

- **PC sampling** via `ITIMER_PROF` at a requested 4 kHz, sampling only the
  module call window. Under Rosetta the signal frame carries guest state, so
  samples are x86-64 PCs; they are mapped to on-disk addresses using the
  image slide recorded at `dlopen` time. Sample attribution walks every loaded
  image's `__TEXT` range, so libc/shared-cache code is separated from module
  code instead of being silently mis-attributed.
- **Boundary records** are written by the *caller*, keeping the measured module
  uninstrumented: `n`, `q`, pointer arguments, flags, timestamps. This is what
  supplies the alias-disjointness and call-shape facts.
- **Facts** that require reading the data (observed min/max) come from an
  instrumented build of the generic module (`-DPORTE_PROFILE`). Its cost is
  measured against the plain build and reported: +1.25% on rank (the timer
  collapsed to 342 Hz) and +33.16% on audit (it sustained ~2.9 kHz). Sampling
  cost under Rosetta is workload-dependent and can be large, which is why it is
  an attribution tool, not the source of admission facts.
- Output: `profiles/<workload>.json` with module hash, runtime, host,
  toolchain, sampling accounting, boundary facts, hot functions and hot blocks.
- Profile identity = executable hash + module hash + workload + host + runtime
  config. A profile is not reused when any of those change.

### B. Application-specialized compiler (`porte/specialize.py`)

Two transform families, both narrow on purpose:

| transform | fact used | guard | fallback |
| --- | --- | --- | --- |
| `rank-counting-v1` | every value in `[0,4095]`, n=2,000,000 | one streaming min/max pass + window/span/size checks | libc-qsort generic module |
| `host-batch-v1` | 1 host transition per record | host capability + `params[1]==0` + size floor | per-record `host_emit` |

Code generation emits C with the shared contract header, then compiles with
`clang -O3 -arch arm64` into a dylib that links the generic module for fallback.
The generator does not re-implement the observable trailer (checksum, queries,
per-record transformation); both fast and slow paths compile the same inline
functions from `runtime/kernel_contract.h`, so the oracle is testing the
transformation, not two hand-written copies.

The catalogue is deliberately not a general optimizer. The research question is
whether *execution-derived, guarded, verified, cached* specialization pays; the
transforms are vehicles.

### C. Correctness oracle (`porte/verify.py`)

Three arms per case: translated x86-64 baseline, native generic, candidate.
Compared: return value, digest of the output buffer, byte-for-byte host effect
ledger, exit status. Native-vs-translated equality is checked too, so a broken
baseline cannot silently bless a candidate.

The corpus encodes the guard contract:

| case | purpose | expected |
| --- | --- | --- |
| `rank_small_range` | observed window | fast |
| `rank_unaligned` | misaligned but in-window | fast (no alignment assumption) |
| `rank_wide` | full int32 range | fallback |
| `rank_tiny` | n < floor | fallback |
| `audit_basic` | no interleaved observer | fast |
| `audit_interleaved` | observer between calls | fallback |

A guard that stops guarding is a failure even if results happen to match, which
is why the expected path is asserted.

**Mutation test.** A module with an off-by-one in the counting-sort prefix sums
must be rejected. This runs before any artifact is cached. A suite that cannot
fail is not evidence.

### D. Progressive optimization cache (`porte/cache.py`)

```
key = sha256(schema, porte_version, arch, application_sha256, module_sha256,
             profile_digest, corpus_digest, transform, transform_version,
             transform_source_sha256, host{model,chip,ncpu,mem,os,os_build},
             toolchain{clang,sdk})[:32]
```

Stored next to the manifest: generated C, dylib, verification summary, bench
summary. `cache.invalidation_reasons()` diffs a stored environment against the
current one field by field. Cold path = profile + compile + full verify
(~3.6 s here); warm path = load (350 µs) + smoke verification (~0.48 s).

Reuse is not blind: the artifact is only loaded when the key matches, and a
smoke verification re-runs the fast-path and one fallback case before the module
is used. Full verification remains available (`porte verify`).

## 3. Apple Silicon specifics

- **Memory model.** No relaxation. The fixture is single-threaded so TSO is not
  exercised; a real port must either rely on Apple's hardware TSO path (as
  Rosetta does) or emulate it (as FEX on macOS does, per MetalSharp's notes).
  Relaxing ordering to win a benchmark is out of scope and would invalidate the
  correctness claim.
- **Pages and W^X.** Generated code is compiled ahead of time and loaded as a
  signed/ad-hoc dylib; the prototype does not JIT at run time, so it avoids
  `MAP_JIT` and hardened-runtime JIT entitlements. A production system that
  compiles on the user's machine will need either a helper process that signs
  artifacts or the JIT entitlement with `MAP_JIT`.
- **Instruction selection.** The generator emits C and lets LLVM choose ARM64
  instructions; no hand-written NEON. The counting-sort path is scalar and
  memory-bound, so SIMD is not where its win comes from.
- **16K pages / `x18` / 4 GB pagezero.** Not exercised by the Mach-O fixture;
  they are load-bearing for the PE path (MetalSharp documents the `x18` TEB and
  `__PAGEZERO` workarounds).
- **Unified memory.** RSS is measured per arm (24.8–26.3 MiB for rank,
  ~49.5 MiB for audit); no arm regresses materially.

## 4. Risks

| risk | likelihood | impact | mitigation / status |
| --- | --- | --- | --- |
| Oracle incompleteness: a wrong specialization passes the corpus | medium | high | guards cover every assumption; adversarial cases in corpus; mutation test; full re-verify on key change; never claim proof, only evidence |
| Assumption drift between profile and later runs | high | medium | cheap runtime guards; fallback path; profile digest in key |
| Guard cost dominates small workloads | high | low | measured (`rank_tiny` 0.50× kernel, +2% wall); floor on n |
| Benchmarks flattered by a weak baseline | high | high | added in-module introsort control; report 14.55× vs it, not only 18.55× vs libc qsort |
| Benchmark artifacts from codegen asymmetry | medium | high | discovered an affine-recurrence folding case where x86-64 code ran 6× faster than arm64; documented and designed out (data-dependent kernels) |
| Rosetta disappears after macOS 27 | high | high | profile format and cache are translator-agnostic; PE path targets FEX, not Rosetta |
| No PE runtime on the development host | certain | high (scope) | experiment bounded to Mach-O/Rosetta; PE path specified in roadmap |
| macOS code-signing / JIT restrictions | high | medium | AOT-compiled dylibs now; later: helper signing or `MAP_JIT` entitlement |
| Sampling too coarse or too costly under Rosetta | high | medium | measured 342–2919 Hz effective and up to +33% overhead at 2.9 kHz; boundary and fact records carry the decisive facts |
| Shared-cache symbolication gaps | certain | low | unattributed samples reported explicitly, not guessed |
| Thermal/noise pollution | medium | medium | interleaved repeats, p10/p90 reported, medians used |
| Cache poisoning / stale artifacts | low | high | key covers content, environment, corpus, transform; verification summary stored |
| Anti-cheat / DRM interaction | n/a | — | out of scope; no bypass attempted |
| "Nativeized" overclaimed in UI | medium | medium | vocabulary rule: an artifact is "specialized with fallback", never "fully native" |

## 5. What the prototype does *not* do

- It does not lift arbitrary x86-64 code. The specialization is generated from a
  declarative module contract plus execution facts, i.e. it assumes a source
  model of the hot module (a human porter, a decompiler with review, or a future
  Porte lifter). The pipeline around it — facts, guards, oracle, cache — is the
  part being tested.
- It does not run a Windows PE, a Win32 API, or a graphics stack.
- It does not prove equivalence; it accumulates differential evidence and
  guarantees fallback.
