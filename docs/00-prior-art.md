# Prior art, novelty, and the hypothesis under test

Status: written 2026-10-28, after a literature and product survey. Claims about
third-party systems are cited; claims about Porte refer to the code in this
repository and the measurements in `results/experiment.json`.

## 1. What already exists

### x86-64 → ARM64 translation engines

**Rosetta 2 (Apple).** A hybrid translator: ahead-of-time translation of the
bulk of an x86-64 Mach-O image plus dynamic translation for code discovered at
run time, with TSO support backed by Apple Silicon hardware. It is the baseline
used in this repository, and it is going away: Apple states that Rosetta is
available on Apple silicon through macOS 27, with reduced functionality from
macOS 28 ([Apple support 102527](https://support.apple.com/en-us/102527)).
Rosetta exposes no public interface for per-application specialization, code
replacement, or artifact verification, and cannot mix native ARM64 code into an
x86-64 process.

**FEX-Emu (Linux).** A user-mode x86/x86-64 emulator for AArch64 Linux with a
custom IR and ARM64 JIT, thunking for host libraries, ARM64EC/WoW64 support, and
an experimental persistent JIT code cache (`FEX_DISKCACHE`, FEX 2609, September
2026, [Phoronix](https://www.phoronix.com/news/FEX-2609-Released)). The cache
persists *translated blocks keyed by guest code*; it does not admit a
specialized implementation only after proving behavioural equivalence, and it
does not model per-application data-distribution invariants. Upstream FEX
targets Linux; macOS support exists only in forks.

**MetalSharp-Wine (macOS).** Wine 11.20 built as a native arm64 application,
with a FEX fork translating x86-64 Windows code in-process through ARM64EC, no
Rosetta, x86 TSO emulated in software, and macOS-specific fixes for `x18`/TEB,
the 4 GB `__PAGEZERO`, 16K pages, W^X and the cross-architecture entitlement
([README](https://github.com/metalsharp/MetalSharp-Wine)). Graphics go through
Mullion/DXMT, DXVK-macOS or VKD3D-Proton. This is the closest shipping system
to "Windows games on Apple Silicon", and it establishes that native arm64 Wine +
FEX on macOS is real engineering, not speculation.

**Ferrum (announced).** FEX on Apple Silicon: x86-64 Windows code translated to
ARM64, Wine underneath for Win32, DXVK for graphics
([project page](https://pyrosoft.pro/ferrum/),
[repository](https://github.com/PyroSoftPro/Ferrum)). Same product category as
MetalSharp; no published verification or specialization layer.

**Hangover.** Wine WoW64 with emulator DLLs on arm64/ppc64le, now using Wine's
ARM64EC support plus FEX to run Win64 applications
([repo](https://github.com/andrerh/hangover),
[Phoronix](https://www.phoronix.com/news/Hangover-9.20-Win64-On-ARM64)). It
emulates only the application, not the Wine installation, but specializes
nothing per application.

**ARM64EC.** Microsoft's ABI for x86-64/ARM64 interoperation inside one Windows
process, with per-call thunks at the boundary. Wine implements ARM64EC and
ARM64X hybrids. It makes *mixing* possible; it does not decide *what* to
specialize, nor verify the result.

**Microsoft Prism (Windows on Arm).** AOT plus dynamic translation, in the same
family as Rosetta; closed and unexposed to per-application optimization.

### Static and profile-guided recompilation

**Elevator (2026).** Deterministic, heuristics-free whole-binary x86-64 →
AArch64 translation using superset disassembly and LLVM-backed instruction
tiles; output is a standalone binary, at the cost of code-size expansion and
performance comparable to QEMU user-mode
([arXiv 2605.08419](https://arxiv.org/html/2605.08419v2)). Its selling point is
that the *actual deployed code* can be tested and signed. It does not perform
run-time admission, per-host invalidation, or data-distribution specialization.

**WYTIWYG (2024), Polynima (2024), Janus (CGO'19), BinRec (2020).** Static or
static-plus-dynamic lifting along paths identified by profiling, with fallback
to dynamic translation for unseen control flow (discussed in Elevator's related
work; Janus: <https://www.cl.cam.ac.uk/~tmj32/papers/docs/zhou19-cgo.pdf>). They
specialize *control flow*, not observed data invariants, and they publish
binaries rather than per-machine admitted artifacts.

**Dynamo/PBO and DynamoRIO.** The canonical dynamic-BTI line: hot trace
formation, superblock optimization, and profile-guided code layout inside a
running process. The techniques are directly relevant, but the artifacts die
with the process and nothing is proven equivalent to an unoptimized baseline.

**Differential validation of binary lifters.** Dasgupta et al., PLDI 2020
(<https://sushant94.me/publications/20pldi.pdf>) test lifters by differential
execution. That is the right methodology; Porte applies it at the level of
*admitted specializations*, and additionally tests the oracle itself with a
known-buggy mutant.

### Graphics and compatibility layers

D3DMetal/Game Porting Toolkit, DXMT/Mullion, DXVK-macOS, VKD3D-Proton and
MoltenVK translate graphics APIs to Metal. They are complementary: Porte treats
the graphics stack as an integrated baseline and does not attempt a driver.

## 2. Competitor matrix

| System | Translation | Persistence | Per-app specialization | Equivalence evidence | API boundary | macOS/Apple Silicon |
| --- | --- | --- | --- | --- | --- | --- |
| Rosetta 2 | AOT + JIT | opaque AOT cache | no | none public | none | yes (through macOS 27) |
| FEX-Emu | IR + ARM64 JIT | JIT block cache (2609) | no (code-keyed cache) | none | thunks, ARM64EC | fork only |
| MetalSharp-Wine | FEX in-process | FEX cache | no | none | Wine + ARM64EC | yes, no Rosetta |
| Ferrum | FEX in-process | FEX cache | no | none | Wine | yes |
| Hangover | WoW64 + FEX | emulator DLL | no | none | Wine | Linux arm64 |
| ARM64EC | ABI thunks | n/a | no | ABI contract only | per-call thunk | Windows |
| Elevator | static whole-binary | the output binary | no | deterministic translation, testable output | ABI translation | cross-ISA |
| WYTIWYG/Polynima/Janus | profile-guided static lifting | binary | control-flow paths | fallback on unseen paths | n/a | research |
| DynamoRIO/PBO | dynamic traces | none | hot traces, in-run | none | n/a | n/a |
| **Porte (this repo)** | profile → guarded ARM64 module | **verified artifacts keyed by exe/env/toolchain/corpus** | **data invariants + host-call fusion** | **differential oracle over state, output and effect ledger, mutation-tested** | **effect-ledger fusion with fallback** | **M2 Max, measured** |

## 3. What is *not* novel here

To keep the claim honest, the following are explicitly **not** claimed as
contributions:

- JIT or AOT translation of x86-64 to ARM64 (Rosetta, FEX, Elevator, Prism).
- Persisting translated code to disk (FEX 2609; Rosetta's AOT cache).
- Profile-guided optimization, hot-block discovery, trace formation (Dynamo,
  DynamoRIO, PGO in general).
- Forwarding Win32 calls to host implementations one at a time (Wine, Hangover,
  ARM64EC thunks).
- Translating D3D/OpenGL/Vulkan to Metal (GPTK, DXMT, DXVK-macOS, VKD3D).
- The individual transforms used in the prototype: counting sort under a value
  window and batched host effects are textbook.

## 4. The narrow capability this project targets

**Proof-carrying, assumption-invalidated module specialization for translated
Windows binaries.**

A specialization is a native ARM64 implementation of a hot module (or of a
guest→host call sequence) that is:

1. **Derived from execution facts** that a translator cannot see: the observed
   value window, argument stability, transition counts, effect sequences.
2. **Accompanied by an explicit assumption ledger.** Every fact that the fast
   path depends on is either (a) checked by a runtime guard, or (b) discharged
   by construction against a shared behavioural contract.
3. **Admitted only by an oracle that compares observable behaviour** — return
   values, output digests, and an ordered ledger of host effects — against both
   the translated baseline and the generic native build, over a corpus that
   *includes inputs that violate each guard*.
4. **Admitted only if the oracle itself is demonstrably sensitive**, proven by a
   mutation test that injects a known semantic bug and requires rejection.
5. **Cached under a key that includes** the application and module hashes, the
   profile digest, the transform source hash, the host, the OS build, the
   toolchain and the verification corpus; any change invalidates the artifact.
6. **Always fallback-capable**: the generic module is linked in and is the
   failure path, so an invalid assumption costs time, never results.

None of the surveyed systems combines per-application execution facts,
assumption-guarded native replacement that crosses the guest/host boundary,
effect-level differential admission with a mutation-tested oracle, and
environment-keyed invalidation. FEX caches translations; Elevator publishes a
translation; Dynamo optimizes a process; Wine forwards calls. Porte is about
*which native code is allowed to run, and what evidence permits it*.

## 5. Falsifiable hypotheses

**H1 (primary).** For a workload whose hot module has a data-distribution
invariant that is stable across runs, cheap to guard, and invisible to a generic
translator or a generic native compiler, an admitted specialization reduces
steady-state kernel time by at least 5% versus the best generic native build of
the same module, and by at least 5% versus the translated baseline, with
identical observable behaviour on the full corpus including guard-violating
inputs.

*Refuted if:* the gain is < 5%, or exists only versus a weak baseline (for
example libc `qsort` with an out-of-line comparator), or requires weakening
observable semantics, or the guard costs more than it saves.

**H2 (boundary fusion).** When host transitions dominate a module's time, fusing
a run of effect-equivalent calls into one transition reduces kernel time
measurably while preserving the ordered effect ledger.

*Refuted if:* transitions are not dominant, or the fused ledger differs, or the
fused path is not faster on real transition costs.

**H3 (reuse).** A verified artifact can be reloaded in well under the cost of
re-deriving it, and re-verification at load time can be made cheap enough to be
practical (target: < 1 s on this corpus), with no correctness loss.

*Refuted if:* reload requires re-running the full pipeline, or load-time
re-verification dominates the workload.

## 6. What this repository's experiment shows

- H1: **supported on the fixture.** Counting-sort specialization inside an
  observed `[0,4095]` window is **14.55×** faster than a fair in-module
  quicksort control and **18.55×** faster than the libc-`qsort` generic build;
  **26.68×** versus the translated x86-64 baseline. Guard-violating inputs
  (`rank_wide`, `rank_tiny`) fall back and match baseline exactly, with
  wall-clock within noise on the tiny case.
- H2: **supported, modestly.** Batching 2,000,000 host effects into 15,625
  transitions is **1.79×** faster than the generic native module (3.81× versus
  translated), with byte-identical ledgers. The fixture's host call is a lock
  plus an append, far cheaper than a real Win32 transition, so this is a lower
  bound.
- H3: **supported with a caveat.** Cache load is ~350 µs; smoke re-verification
  is ~0.48 s, against ~3.6 s for the full cold pipeline. Full re-verification is
  still the dominant warm-path cost, which is why the smoke/full distinction
  exists. The key is stable across runs (`porte warm` recomputes it and reports
  field-level invalidation reasons), so the artifact is genuinely reusable.
- **Negative results** are recorded rather than hidden: the first transform
  attempted (aliasing-driven hoisting) was already performed by clang and was
  dropped; an 8× unrolled LCG benchmark made x86-64-under-Rosetta look 6× faster
  than native ARM64 purely because the x86 backend composed the affine
  recurrence (see `docs/02-experiment-report.md`); the tiny-input case is slower
  with the specialization due to guard cost.

## 7. Unverified name

The brief mentions "Hadron". Searches on 2026-10-28 for a binary-translation
project by that name returned nothing matching the description (only unrelated
uses of "hadron"). It is therefore **not** characterized here. If "Hadron"
refers to a non-public or differently spelled project, its feature set should be
checked before this document's novelty claim is relied on.
