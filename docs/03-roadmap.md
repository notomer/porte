# Roadmap: from the fixture to Windows applications and games

The prototype deliberately stops at the smallest experiment that could refute
the hypothesis. This is the order in which the remaining work should be done,
with the gate that must pass before moving on.

## Phase 1 — done: mechanism and evidence (this repository)

- Profiler: boundary records, PC sampling, data facts, symbolication, block heat.
- Specializer: facts → plan → guarded ARM64 module with generic fallback.
- Oracle: three arms, output/return/ledger equality, guard cases, mutation test.
- Cache: content- and environment-addressed artifacts with field-level
  invalidation, smoke re-verification on load.
- Result: 14.55× over the fairest native baseline, 26.68× over translation, on a
  workload with a guard-checkable invariant; 1.79× for host-effect batching;
  all guard-violating cases unchanged.

**Gate passed:** specialization is measurable against a strong baseline,
correct on the corpus, rejected when buggy, and reusable.

## Phase 2 — a real Windows binary (Experiment 1 on PE)

**Status: partially delivered without Wine/FEX.** The smallest genuine slice —
load a stripped PE, run it from its entry point, find a hot natural loop,
translate it to ARM64 with profile-pinned memory accesses and guards, prove it
over a corpus, cache it — is implemented in `porte/pe/` and reported in
[`90-opaque-pe-experiment.md`](90-opaque-pe-experiment.md). It does not cover
Win32 API fusion, multi-loop images, or a production translator baseline; those
remain below.

The performance objective changed after that experiment: the question is no
longer "can a hot loop be specialized" but "how far is Windows-binary execution
from native macOS". The audit, the minimum experiment and the measured answer
are in [`04-native-performance-plan.md`](04-native-performance-plan.md) and
[`05-native-baseline.md`](05-native-baseline.md).

Two viable hosts:

1. **Linux arm64 + Wine + FEX** (best-supported FEX path): profile a real PE,
   then specialize a DLL boundary. This is the cleanest way to get FEX's IR,
   thunk and code-cache instrumentation without maintaining a macOS fork.
2. **macOS + MetalSharp-Wine's FEX fork** (native arm64 Wine, no Rosetta): the
   only way to validate the macOS-specific assumptions (W^X, 16K pages, `x18`,
   `__PAGEZERO`, artifact signing). Requires building the fork and adding an
   injection point for a native module.

Work items:

- Replace the Mach-O/Rosetta adapter with a translator adapter interface:
  `profile(binary)`, `module_image(binary)`, `load_native_module(path)`.
- Use a legally redistributable, deterministic Win32 console program with a hot
  loop and a narrow API surface (Experiment 1/2 in the brief). No AAA titles.
- Keep the same oracle shape: baseline PE output, native generic build, candidate
  module; compare stdout, exit status, and an API effect ledger captured at the
  thunk layer (files opened, registry keys read, timing calls, GDI objects).
- First candidate transforms for real Win32:
  - a hot DLL function with observed argument invariants (as in this report);
  - a repeated `ReadFile`/`WriteFile` sequence batched at the thunk boundary;
  - a per-frame `GetTickCount64`-style query, which must **not** be hoisted if
    the program observes the value (the oracle should reject that transform —
    use it as a negative control).

**Gate:** a PE workload where the specialization is ≥5% faster than the best
generic native build **and** the API effect ledger is identical over a corpus
that includes guard violations.

## Application integration track (new, in progress)

Status and evidence: [`06-app-integration.md`](06-app-integration.md) for bundles,
[`07-package-importer.md`](07-package-importer.md) for package formats.

| milestone | state |
| --- | --- |
| A — native app packaging | **Done** for images whose imports are in the engine's host stubs: ARM64 launcher, `Info.plist`, stable bundle id, PE icon or generated icon, PE hash in metadata |
| B — Finder and Dock | **Done**: installed in `~/Applications`, launches by `open`/Finder, registered in Launch Services and Spotlight, own Dock identity, no Porte manager |
| C — native window integration | **Not started**: the launcher window is a supervisor console; there is no Win32→AppKit window bridge, menus, clipboard, dialogs, notifications or accessibility metadata |
| D — transparent optimization | **Partial**: launcher → shared runtime → engine path works, with per-app state, run records and a profile cache; the specialization/verification pipeline is not yet wired to bundles |
| E — seamless importing | **Not started**: installation is `porte app import`; no Porte document handler, Open With/drag-and-drop, watched folder or Quick Action, so an `.msi` is not opened by double-clicking it in Finder |
| G — package formats | **Done** for `.msi` and `.msix`/`.appx`: database + cabinet install into an isolated environment, block-map validation, upgrade/repair/uninstall, automatic bundles for discovered executables; code custom actions, services, drivers and WinRT entry points are refused or reported |
| F — games | **Blocked** on the execution engine: no D3D backend, no shader translation, no guest threads, no window/input path |

Design constraints already honoured: the launcher owns the macOS identity and
never loads the Windows image; bundles are immutable at runtime and ad-hoc
signed; per-app state lives in Application Support; missing runtime produces a
recovery prompt; Gatekeeper is not bypassed; unsupported images are refused with
the missing imports named. Installers are never executed as opaque programs:
MSI tables are read and applied by Porte, MSIX payloads are validated against
their block map, and anything that would require running vendor code is refused.

## Phase 3 — in-process specialization at the ARM64EC boundary

- Compile candidate modules as ARM64/ARM64EC DLLs loadable by an ARM64 Wine
  process (MetalSharp already ships ARM64X hybrids).
- Implement the call-boundary variant: a fused native routine that replaces a
  guest→host sequence, with the ledger comparison as the admission test.
- Measure transition cost directly (thunk counters in FEX), so the fusion claim
  rests on real numbers rather than a synthetic sink.
- Add a runtime guard registry: when an assumption's guard fails repeatedly, the
  artifact is demoted and the profile is scheduled for refresh.

**Gate:** fused path beats the unfused path on a real thunk cost, with identical
ledgers; a deliberately unsafe fusion (hoisting an observable clock read) is
rejected by the oracle.

## Phase 4 — graphical applications (Experiment 3)

- Keep graphics translation as an integrated baseline: D3D9/10/11 → DXVK-macOS,
  D3D12 → VKD3D-Proton, or Mullion/DXMT, all on MoltenVK. Porte does not build a
  driver.
- Metrics become frame-time percentiles (p50/p95/p99) and hitch counts, not
  kernel time alone. Compilation and verification must happen outside the frame
  budget or be amortized across launches via the cache.
- Candidate specializations for games: hot simulation/physics modules, asset
  decode paths, per-frame API sequences (constant-buffer updates, draw-call
  state blocks). Shader/pipeline caching is already provided by the graphics
  stack and is not claimed as Porte's contribution.
- Anti-cheat and DRM are out of scope; a title that depends on them is not a
  target, and no bypass will be implemented.

**Gate:** a DRM-free game or graphical demo where an admitted specialization
improves p95 frame time without changing rendered output beyond an explicitly
declared tolerance (pixel comparison with a documented threshold).

## Phase 5 — the macOS user experience

A SwiftUI app only after the technical claims survive Phase 3:

- Import EXE → Analyze → Run → Profile → Optimize → Verify → Launch optimized.
- The UI must distinguish, per module:
  - translated generic code (Rosetta/FEX),
  - verified native specialization with active guards,
  - fallback paths taken at run time,
  - compatibility libraries still required (Wine, DXVK/MoltenVK, VC++ runtime),
  - unsupported features and known risks.
- Never label a runtime-dependent bundle a "native port". The vocabulary is
  "specialized module with fallback" unless the entire dependency graph is
  native.
- Show measured numbers from the cache manifest, including the verification
  corpus size and the date, and offer a "re-verify now" action.

## Cross-cutting work

- **Generalize the oracle.** More mutants (guard removal, window widening,
  effect reordering, dropped entries), property-based input generation, and
  concurrency cases once threads are in scope.
- **Guard minimization.** Current guards are coarse (full min/max pass). Sampling
  or incremental guards could cut the fallback cost that hurts tiny workloads.
- **Precision of the cache key.** Host memory size and CPU count are coarse
  proxies; a real deployment should key on microarchitecture features that
  affect codegen (LSE, SVE, cache line size) rather than the machine model.
- **Signing and JIT.** If specialization ever happens on the user's machine
  rather than in a build step, a signing helper or the `MAP_JIT` entitlement is
  required; plan for it before promising on-device compilation.
- **Instrumentation cost.** The 8–11% profiling overhead is acceptable for a
  one-off analysis but not for continuous profiling; the boundary channel should
  become the steady-state source, with sampling as an occasional audit.

## Kill criteria

Stop or redirect if any of these hold after Phase 2/3:

- No specialization beats the best generic native build by ≥5% on any real
  workload, or wins only by weakening observable behaviour.
- Guards cost more than the specialization saves across the realistic workload
  distribution.
- Verification cannot be made cheap enough to run on every launch or every
  environment change, and users disable it.
- The only available baselines (FEX/Rosetta) cannot load a native replacement
  module without patching the translator beyond reasonable maintenance.

In that case the honest outcome is the negative result plus the oracle: a
differential testing harness for Windows-on-ARM translation is useful on its own.
