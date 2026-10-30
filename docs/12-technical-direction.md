# 12 — Technical direction: what to build, what to stop building

This document decides between three strategies using evidence from this
repository and this host. It is written to be argued with: every claim names
what would falsify it.

- **A. General-purpose compatibility frontend** — engines, bottles, installers,
  Finder integration, compatibility data.
- **B. Independent binary-nativeization engine** — translate Windows x86-64 into
  optimized ARM64 and execute it natively.
- **C. Performance optimization layer** — verified, application-specific
  optimizations that work alongside an existing compatibility runtime.

The brief prefers B+C. The audit in `docs/11-competitive-analysis.md` and the
measurements in `docs/13-nativeization-experiment.md` say which parts of B+C can
work on macOS, which cannot, and where the boundary between them lies.

**Revision note (Phase 4).** The first edition of this document opened with "a
native ARM64 optimizer cannot put ARM64 code into the process that runs the
Windows program". That is true of one execution architecture on macOS and false of
the other, and the edition only knew the first. §1 is rewritten around both; §4
and §6–§8 follow.

---

## 1. The constraint, stated correctly this time

There are two ways Windows code runs on Apple silicon, and they differ in exactly
the property an optimizer needs.

**Architecture R — x86-64 Wine under Rosetta.** The host process is x86-64.
Measured:

```
$ clang -dynamiclib -arch arm64 -o libnative.dylib native.c
$ arch -x86_64 ./loader
dlopen failed: ... (mach-o file, but is an incompatible architecture
(have 'arm64', need 'x86_64'))
```

No arm64 module can join it, and the sampler reports `Rosetta JIT` addresses with
no route back to the guest instruction (`docs/13`, Experiment 2). Under this
architecture, an optimization layer has to stay in x86-64 space.

**Architecture N — native arm64 Wine with FEX.** The host process is **arm64**;
the Windows program's x86-64 code is JIT output inside it, reached through Wine's
ARM64EC provider. Three properties follow, and all three are measured or
source-verified in `docs/11` §3:

| property | consequence |
| --- | --- |
| the host process is arm64 | native arm64 code can be loaded into it |
| Wine's own modules are ARM64X, and ntdll loads third-party ARM64EC DLLs into an x86-64 guest | a native module can be reachable *from guest code* |
| FEX writes `/tmp/perf-<pid>.map` with host	o guest block addresses | the guest can be *named*, which Rosetta never allowed |

So the Phase 3 conclusion was architecture-specific. The barrier was never "macOS
cannot mix", it was "Rosetta cannot mix", and choosing a runtime is choosing
whether the barrier exists.

**What did not change.** Being allowed to generate code is not the same as being
useful. Measured on one real program (`docs/14`): FEX is **0.97×** Rosetta,
native arm64 7-Zip is **1.57×** faster than the same binary under FEX, and the
one specialization Porte can currently apply recovers **1.09×**. The headroom is
real; it is also much smaller than the distance to native.

---

## 2. Strategy A — frontend

| | |
| --- | --- |
| Differentiation | none. Highball implements the same shape with a larger CC0 database, renderer selection and launcher integration (`docs/11` §1). |
| Feasibility | high — already built here |
| Performance potential | none (it is a launcher) |
| Complexity | low, but perpetual (engine drift, store sign-in changes, anti-cheat lists) |
| Licensing | clean |
| Long-term value | low: the layer is commoditised and the maintenance is someone else's race |

Verdict: **stop expanding.** Keep what exists because it works and users need it,
but it is not the product.

---

## 3. Strategy B — independent nativeization engine

What exists: a Unicorn-based x86-64 machine with 15 Win32 stubs, a profiler that
finds a hot reducible loop, a lifter to ARM64 machine code, a differential
oracle, and a persistent cache (`docs/90-opaque-pe-experiment.md`).

| | |
| --- | --- |
| Differentiation | genuine: nobody audited does guarded, profile-driven specialization of an opaque Windows binary with differential proof |
| Feasibility | **low** for real applications |
| Performance potential | measured in `docs/13` §6: 1.3×–19.5× behind native ARM64, depending on the loop |
| Complexity | very high: to run an unmodified application it must implement enough of `user32`, `gdi32`, `comctl32`, `ole32`, `shell32`, `comdlg32`, `advapi32`, `msvcrt`, SEH, TLS, thread scheduling and the window server bridge — Wine's PE modules, which are decades of work, against 15 stubs today |
| Maintenance | highest of the three |
| Licensing | clean (all original) |
| Long-term value | high *if* the API gap could be closed, which it cannot be inside one project's budget |

Verdict: **keep as research, do not present as a product path.** The engine is
the only original asset; it is also three orders of magnitude from running a real
program.

---

## 4. Strategy C — optimization layer beside a runtime

Three variants, not one:

| variant | mechanism | status |
| --- | --- | --- |
| C1: native code injection at run time | replace hot functions with ARM64/ARM64EC inside the Wine process | **impossible under Rosetta, possible under native arm64 Wine** (§1). Not built: needs an ARM64EC code generator and a guest-call path; the mechanism itself is proven by MetalSharp loading third-party ARM64EC DLLs into an x86-64 guest |
| C2: ahead-of-time x86-64 PE rewriting | rewrite the guest binary, keep it x86-64, let the translator produce better code | possible in principle; FEX now gives a profiling signal (§3.3 of `docs/11`), Rosetta does not; still competes with a translator on its strongest ground |
| C3: semantics specialization | remove compatibility work the program provably does not need — currently the x86 memory-ordering emulation, selected per program and verified | **built and measured**: 1.09× wall and CPU on 7-Zip against the unmodified runtime, byte-identical output, guard refuses the multi-threaded case (`docs/13` Exp. 6, `docs/14`) |

| | |
| --- | --- |
| Differentiation | C1 genuine but unbuilt; C2 research; C3 real but modest, and it is Porte's oracle (verification) more than its codegen that makes it safe |
| Feasibility | C1 medium (ARM64EC toolchain exists, not on this host yet); C2 unknown; C3 done |
| Performance potential | C1 would be large (the guest's own code dominates a CPU-bound run); C2 unknown and likely small; C3 measured 1.09× |
| Complexity | C1 high; C2 high; C3 medium |
| Licensing | clean: FEX is MIT, Wine LGPL, and neither is modified |

Verdict: **C1 is no longer dead but is a compiler project; C2 still has no proven
win; C3 works today and is the only variant with a measured number.**

---

## 5. Comparison

| criterion | A frontend | B engine | C1 inject | C2 rewrite | C3 semantics |
| --- | --- | --- | --- | --- | --- |
| Genuine differentiation | 1/5 | 4/5 | 4/5 | 3/5 | 3/5 |
| Engineering feasibility | 5/5 | 1/5 | 2/5 | 2/5 | 5/5 (built) |
| Measured performance | 0 | 1.6–9.9× vs *its own interpreter* | not built | none | **1.09× vs the unmodified runtime** |
| Complexity (higher = worse) | low | very high | high | high | medium |
| Maintenance burden | medium | very high | high | high | medium: tracks FEX config, which is public API |
| Licensing risk | none | none | none | none | none (no FEX code modified) |
| Long-term value | low | high if the API gap closed | high if the toolchain lands | medium | medium: real but bounded |

A still loses. B still cannot run a real program. C3 is the only column with a
verified number, and its ceiling is the memory-ordering cost it removes — 9% on
this workload, not 50%. C1 is now the only path to a large number, and it is a
compiler engineering programme, not a phase.

---

## 6. What would have to be true for B to win

These were the falsifiable conditions. Phase 4 settled two of them:

1. **A native ARM64 Wine exists on macOS.** ~~Not available.~~ **True as of this
   phase** — MetalSharp Wine 4.0 (Wine 11.20 + FEX) and CodeWeavers' ARM64
   preview. Measured here, not taken on trust (`docs/14`). The optimizer can emit
   into a native process, and FEX's perf map gives it the guest addresses to
   target.
2. **Porte implements the Win32 surface for a *narrow* class of programs.** Still
   the only version of B that fits one project; still unbuilt (15 stubs).
3. **A measurement shows rewriting beats the translator by a reproducible
   margin.** Still absent. C3 does not need it: it changes what the translator
   must emulate, not how well it translates.

What condition 1 does *not* provide is speed. The same workload runs 0.97× as
fast under FEX as under Rosetta, and 1.57× slower than native arm64. Any claim
that Porte should exist as a *performance* layer now rests on a number it has to
produce itself — and 1.09× is the first one.

---

## 7. Recommendation

1. **Stop building frontend features.** The GUI, bundles, Finder integration and
   MSI path stay as they are; they are support, not the product.
2. **Ship the specialization, not the fantasy.** `porte runtime specialize`
   measures a program, refuses when two threads overlap, proves the change on a
   real workload, and stores a record a launcher can read. That is a complete,
   honest feature with a measured 1.09×. Present it as what it is: the
   compatibility cost a program does not need, removed.
3. **Target native ARM64 Wine, not Rosetta.** The specialization above is
   FEX-specific; architecture R offers no lever of this kind at all.
4. **Treat C1 as the next research milestone, not the next feature.** The
   mechanism exists (ARM64EC modules load into an x86-64 guest). What is missing
   is a generator: lifting a hot guest function to ARM64EC on this host needs a
   Windows-targeting toolchain Porte does not have yet, and the honest next step
   is to establish it before promising anything.
5. **Integrate rather than duplicate** for everything compatibility-related:
   Highball's CC0 recipes/database are licence-clean to consume; FEX is MIT and
   accepts patches; Wine fixes belong upstream. MetalSharp's FEX fork is *not*
   upstream, which makes it a moving target — anything Porte builds on it should
   be a small, re-measurable layer, never a fork.

If the next milestone cannot beat 1.09× on a second, different program, the
correct outcome is to contribute the measurement and specialization work
upstream and stop claiming an execution platform.

---

## 8. Go / no-go criteria for the next milestone

Phase 4's gate is met: a real third-party program runs measurably faster through
Porte than through the same runtime without Porte (`docs/13` §5, `docs/14`). The
next milestone is the one that decides whether that is a product or a one-off.

**Milestone: a specialization that beats the unmodified backend on a second,
different program, and survives a hostile test.** Go only if all of these hold:

| # | criterion | measurement | bar |
| --- | --- | --- | --- |
| 1 | A second real program (different vendor, different workload shape) is specialized | same job format, `porte runtime specialize` | record accepted |
| 2 | The win reproduces | 5 interleaved repeats, median and min | ≥ 1.03× wall **and** CPU, both programs |
| 3 | Correctness holds beyond one run | output byte-identical over ≥ 20 inputs or invocations, plus the integrity check | zero divergence |
| 4 | The guard refuses unsafe cases | a multi-threaded mode of the same program | zero candidates proposed |
| 5 | The specialization is not silently lost | record loads from the cache, keyed by program digest | present after restart |
| 6 | No FEX fork | the runtime is unmodified MetalSharp or CrossOver | no local patches |

No-go if criterion 2 fails on the second program: a one-program result is a
measurement, not a capability, and Porte should contribute the instrument and the
finding upstream rather than ship a launcher with a tuning knob.

The current state against these criteria is in `docs/13` §7: criterion 1 is **not
met** (7-Zip only), criteria 2–5 are met for 7-Zip, criterion 6 is met.
