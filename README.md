# Porte

[![CI](https://github.com/notomer/porte/actions/workflows/ci.yml/badge.svg)](https://github.com/notomer/porte/actions/workflows/ci.yml)

**Your Windows apps. At home on Mac.**

Porte is an experimental Windows application compatibility and binary
nativeization platform for Apple Silicon. Its long-term goal is to let people
install and launch Windows applications and games as first-class macOS
applications, while progressively optimizing execution toward native ARM64 and
Metal performance.

The intended experience is:

```
Windows EXE/MSI  →  Porte  →  macOS application  →  Launch from Applications,
                                                    Spotlight, or the Dock
```

It is **not** a Wine repackaging or a universal EXE compatibility layer, and it
does not yet deliver that experience for arbitrary software. What exists is the
measurement and admission machinery around one specialization, the evidence that
the machinery works, the negative results, and a proof-of-concept application
layer that turns supported Windows executables and packages into ordinary macOS
`.app` bundles.

> Porte was previously named **Forge**. Committed research artifacts (`results/`,
> `profiles/`) were produced under the old name and keep their original values;
> only the project, package and CLI identifiers were renamed.

## Project status

| area | state | evidence |
| --- | --- | --- |
| Guarded native specialization with a differential oracle | **Implemented, measured** on the fixture workloads | `results/experiment.json`, `docs/02-experiment-report.md` |
| Opaque Windows PE profiling and specialization | **Implemented, measured** on five stripped PEs | `docs/90-opaque-pe-experiment.md` |
| Frame-path baseline vs native ARM64 + Metal | **Measured**: parity only when the GPU is the limiter | `docs/05-native-baseline.md` |
| macOS `.app` generation (launcher, Dock identity, icon, state) | **Implemented, verified** on this host | `docs/06-app-integration.md` |
| Native macOS application (SwiftUI, Finder and Open With) | **Implemented, verified** end to end | `docs/09-macos-gui.md` |
| Package importing: `.msi`, `.msix`/`.appx`, portable `.exe` | **Implemented, verified** end to end | `docs/07-package-importer.md` |
| Real Windows x86-64 GUI programs (via a runtime Porte discovers) | **Implemented, verified** with 7-Zip 26.05 and Notepad++ 8.9.8.1 | `results/runtime_verification.json`, `docs/10-windows-runtime.md` |
| D3D, anti-cheat, games | **Roadmap only** — not implemented or tested | `docs/03-roadmap.md` |
| Near-native CPU-bound performance | **Not achieved**; the gap is measured and attributed | `docs/04-native-performance-plan.md`, `docs/05-native-baseline.md` |

No near-native performance claim is made beyond the GPU-bound case, and no
compatibility claim is made beyond the executables whose imports the engine
serves.

## Direction after the competitive audit

Porte's frontend (installs, prefixes, MSI, `.app` bundles, Finder integration)
is not a differentiator: [Highball](https://github.com/gauthierpiarrette/highball)
implements the same shape on top of Wine, DXMT and D3DMetal with an open CC0
compatibility database. Porte's only unclaimed asset is its guarded,
profile-driven nativeization with a differential oracle.

Measurements in this repository bound what that asset can do on macOS today:

- A Rosetta-translated process **cannot load arm64 code** (verified with
  `dlopen`), so native code cannot be injected into a Wine process.
- Profiling a genuine third-party Windows program under Wine shows ≈49% of
  executing samples inside Rosetta's runtime, but the guest's hot code is
  reported only as anonymous `Rosetta JIT` addresses — it cannot be mapped back
  to the program's functions.
- Porte's own generated ARM64 code, verified byte-exact against an interpreter,
  is still 1.3×–19.5× slower than a native build on the same workloads.

So Porte does not ship a faster execution path today. The next milestone is the
narrow one that can still be won: run one real Windows **console** program in the
engine and require byte-identical output plus at least Wine parity before
claiming anything. Details, including the go/no-go bar:
[`docs/11-competitive-analysis.md`](docs/11-competitive-analysis.md),
[`docs/12-technical-direction.md`](docs/12-technical-direction.md),
[`docs/13-nativeization-experiment.md`](docs/13-nativeization-experiment.md).

Read first:

- [`docs/00-prior-art.md`](docs/00-prior-art.md) — what FEX, MetalSharp, Ferrum,
  Rosetta, ARM64EC/Hangover, Elevator, WYTIWYG/Polynima, DynamoRIO and the
  validation literature already do, the competitor matrix, and the falsifiable
  hypotheses.
- [`docs/01-architecture.md`](docs/01-architecture.md) — components, the macOS
  process-boundary constraint, and the risk register.
- [`docs/02-experiment-report.md`](docs/02-experiment-report.md) — measured
  results, threats to validity, negative results.
- [`docs/03-roadmap.md`](docs/03-roadmap.md) — PE, Win32 API fusion, games, UI.
- [`docs/04-native-performance-plan.md`](docs/04-native-performance-plan.md) —
  the near-native performance objective, the audit that constrains it, and the
  minimum experiment that can measure the gap.
- [`docs/05-native-baseline.md`](docs/05-native-baseline.md) — measured gap
  between Windows-binary execution and native ARM64 Metal, layer by layer.
- [`docs/06-app-integration.md`](docs/06-app-integration.md) — generated macOS
  `.app` bundles: architecture, verified Finder/Dock behaviour, and what is
  still missing.
- [`docs/07-package-importer.md`](docs/07-package-importer.md) — `.exe`, `.msi`
  and `.msix`/`.appx` importing: the MSI database and cabinet path, MSIX block
  map validation, upgrades, repair, refusals, and the measured end-to-end run.
- [`docs/09-macos-gui.md`](docs/09-macos-gui.md) — the native macOS app:
  architecture, the JSON bridge protocol, Finder/Launch Services integration,
  setup, what is verified, and what still depends on the Windows engine.
- [`docs/10-windows-runtime.md`](docs/10-windows-runtime.md) — where execution
  used to stop, why the runtime backend is a discovered Wine distribution, and
  the exact evidence for a real Windows application running with a real window.
- [`docs/90-opaque-pe-experiment.md`](docs/90-opaque-pe-experiment.md) — prior
  phase: stripped Windows PEs executed and nativeized from the binary alone.

## Where the project stands: measured, not assumed

The current objective is near-native macOS performance for supported Windows
games. The first frame-path measurement (`docs/05-native-baseline.md`) says:
on a GPU-bound frame the Windows PE through Porte is within 1% of the native
ARM64 + Metal build, because 1.6 ms of translated CPU work hides inside 8.5 ms
of GPU work; on a CPU-bound frame the same PE is **~110–120× slower** than native
and ~60× slower than Rosetta 2 on the same workload, and essentially all of that
is guest instruction execution at 400–540 MIPS, not the API or Metal boundary.

No near-native claim is made beyond the GPU-bound case.

```bash
./fixtures/frame/build.sh
.venv/bin/python -m porte.cli frame --repeats 3   # -> results/frame_baseline.json
```

## macOS application bundles and Windows installers

A supported Windows executable can be installed as a real `.app` in
`~/Applications`: its own ARM64 launcher, `Info.plist`, bundle identifier and
icon (extracted from the PE when it has one), a Finder/Spotlight identity, a
Dock icon, and no visible Porte manager. The launcher owns the macOS lifecycle
and supervises the shared Porte runtime, which executes the image.

```bash
.venv/bin/python -m porte.cli app install build/frame/frame_win.exe \
    --name "Porte Frame Demo" --args 300 8192 1 --accept
open -a "Porte Frame Demo"
.venv/bin/python -m porte.cli app list
```

`porte app import` takes a package and picks the right path from the suffix:

| input | behaviour |
| --- | --- |
| `.exe` | portable image: import analysis, bundle, run. Installer-like EXEs (user32/ole32/msi.dll) are refused with the missing imports |
| `.msi` | installed by the runtime's own Windows Installer (`msiexec /i`), then the installed programs are discovered and bundled. With no runtime present, Porte reads the database itself and applies the file, registry and shortcut tables into an isolated per-product environment |
| `.msix`, `.appx` | ZIP validated against `AppxBlockMap.xml`, manifest parsed, payload extracted, declared applications bundled |
| `.bat`, `.cmd` | refused: Porte has no `cmd.exe` |

```bash
./fixtures/installer/build.sh
.venv/bin/python fixtures/installer/verify.py             # 33 end-to-end checks
.venv/bin/python -m porte.cli app import build/installer/PorteFixture.msi --accept
.venv/bin/python -m porte.cli app products
open -a "Porte Installer Fixture"
.venv/bin/python -m porte.cli app uninstall "Porte Installer Fixture"
```

Verified on this host: valid bundle, launches from Finder/`open`, independent
Dock identity (`lsappinfo`), window shown without Porte UI, clean quit with no
orphan runtime, configuration and run history persisted, and bundle contents
unchanged by runs. For packages: the MSI fixture installs a 3-file product, its
registry value lands in the virtual registry, the GUI executable is reported as
unsupported instead of bundled, the console executable becomes an app, upgrades
from 1.0.0 to 1.1.0 replace the old environment, a package with a code custom
action is refused, a tampered MSIX is rejected on a block hash, and uninstall
removes product, apps and state.

Two execution backends sit behind that description. The engine path packages
what the engine can run and is not general Windows compatibility (no user32, no
window manager, no `msiexec` script engine). The runtime path — a Wine
distribution Porte discovers on the machine, never one it ships — executes
unmodified Windows x86-64 programs, including GUI programs with real windows and
real keyboard input, and installs `.msi` packages through the runtime's own
Windows Installer. Neither path claims D3D, anti-cheat, drivers, services or
Authenticode. Details: [`docs/06-app-integration.md`](docs/06-app-integration.md),
[`docs/07-package-importer.md`](docs/07-package-importer.md),
[`docs/10-windows-runtime.md`](docs/10-windows-runtime.md).

## Native macOS application

Porte also ships a native SwiftUI application, so the product is usable without
the terminal. `./macos/build.sh` assembles `build/Porte.app`; on first launch it
validates the runtime, optionally makes itself the handler for `.exe` and `.msi`
through the documented Launch Services API, and then routes Finder double-clicks,
Open With, Dock drops, in-window drops and File ▸ Open to the existing engine.

The app talks to the engine through a structured newline-delimited JSON protocol
(`porte/gui/bridge.py`), not by parsing terminal output, and it reports typed
states: analyzing, preparing, installing, launching, running, completed, failed,
unsupported. Installs create the same real `.app` bundles as the CLI; the
resulting app launches from Applications without any Porte window.

```bash
./macos/build.sh
open build/Porte.app
./macos/verify.sh          # build, checks, and drive the real app end to end
```

Verified on this host: a stopped app receives an MSI handed over by macOS and
reaches the installation analysis; a running app receives an EXE and analyzes it;
`open file.exe` reaches Porte when it is the default handler; a package installs
into a valid arm64 `.app` that runs without the manager; a missing runtime sends
the app back to setup instead of showing a broken library. An `.msi` double-clicked
in Finder is installed by `msiexec` in the runtime whenever one is present —
including packages Porte's own reader cannot parse — and the installed program
becomes a launchable app. What neither path can do is make Windows APIs that no
runtime implements exist: installer-shaped EXEs outside the host stubs are
reported as unsupported, and a program that needs drivers, services, anti-cheat
or kernel extensions fails inside the runtime with its own status rather than
being reported as working. Details, protocol and the manual gesture checklist:
[`docs/09-macos-gui.md`](docs/09-macos-gui.md).

## Phase 2 result: opaque Windows PE

Five independently compiled, symbol-stripped Windows x86-64 executables with no
exports are loaded from `AddressOfEntryPoint`, profiled, and specialized from
machine instructions plus runtime observations only. No source, no symbols, no
kernel names, no fixture branch in the optimizer. One of them (`crc.exe`) was
built after the pipeline was frozen and was admitted without fixture-specific
changes.

| program | baseline PE | generic trace | pinned trace | speedup | native ARM64 reference |
| --- | ---: | ---: | ---: | ---: | ---: |
| blob, n=65,536 | 2.076 ms | 0.482 ms | **0.405 ms** | **5.13×** | 0.011 ms |
| lookup, n=65,536 | 2.451 ms | 0.544 ms | **0.554 ms** | **4.42×** | 0.256 ms |
| sieve, n=1,000,000 | 58.542 ms | 6.619 ms | **6.005 ms** | **9.75×** | 1.261 ms |
| crc, n=262,144 | 1.642 ms | 1.131 ms | **1.041 ms** | **1.58×** | 0.776 ms |
| fault, n=16 | 0.096 ms | 0.170 ms | 0.142 ms | 0.68× (negative) | — |

- 22-case corpus per image (boundaries, repeats, faults): byte-exact on stdout,
  exit code, fault, API ledger, heap and image digests.
- The oracle rejects a trace with one loaded value corrupted; a changed memory
  layout demotes to interpretation with identical output; a one-byte image
  change misses the cache.
- Cold pipeline 1.6–10.2 s (mostly profiling), cache smoke 19–62 ms.
- The baseline is a fast interpreter, not Rosetta/FEX, so these are not
  comparable to production translation speeds. The native reference is 2.8–42×
  faster than the specialized path.

```bash
.venv/bin/python fixtures/opaque/gen.py --clean
.venv/bin/python -m porte.cli pe optimize fixtures/opaque/out/blob.exe --workload 65536
.venv/bin/python -m porte.cli pe verify   fixtures/opaque/out/blob.exe
.venv/bin/python -m porte.cli pe experiment --repeats 7
```

Details, limits and the audit of the Phase 1 claims: `docs/90-opaque-pe-experiment.md`.
These are compiler experiments on single kernels. They are **not** evidence of
native game performance: the frame-path baseline in
`docs/05-native-baseline.md` shows a 100× CPU-side gap on a CPU-bound frame and
parity only where the GPU is the limiter.

## Result (M2 Max, macOS 27.2, 7 interleaved fresh-process runs)

| workload | translated x86-64 | native generic (libc qsort) | native generic (in-module sort) | **nativeized** |
| --- | ---: | ---: | ---: | ---: |
| rank, values in [0,4095] | 132.49 ms | 92.13 ms | 72.26 ms | **4.97 ms** |
| audit, 1 host call/record | 36.30 ms | 17.05 ms | 17.02 ms | **9.53 ms** |

- **14.55×** over the fairest native baseline, **26.68×** over translation.
- Guard-violating inputs (`rank_wide`, `rank_tiny`, `audit_interleaved`) fall
  back and produce byte-identical output; the tiny case is within wall-clock noise.
- 2,000,000 host effects fused into 15,625 transitions with a byte-identical
  ledger.
- Oracle rejects a known-buggy module (`porte selftest`); cache load is 350 µs,
  full cold pipeline is ~3.6 s.
- Raw numbers, profiles and verdicts: `results/experiment.json`.

**What "nativeized" means here:** one module is native ARM64 with runtime guards
and a generic fallback. The rest of the application is still whatever the
baseline was. The UI-facing vocabulary in the roadmap forbids calling this a
fully native port.

## Install

Requirements: macOS on Apple Silicon, Xcode command line tools (clang, objdump,
nm, atos) plus a Swift 6 toolchain to build the macOS app, Python 3.10+, Rosetta
installed (the translated baseline runs x86-64 Mach-O images through it).

```bash
python3 -m venv .venv
.venv/bin/pip install -e .      # porte, plus the deprecated `forge` alias
.venv/bin/porte --help
```

The engine needs [Unicorn](https://www.unicorn-engine.org/) (GPLv2) and
[Capstone](https://www.capstone-engine.org/) (BSD-3); see
[`THIRD_PARTY.md`](THIRD_PARTY.md) for the license audit and what that means if
you redistribute binaries.

## Quickstart

```bash
porte build                 # fixture images for x86_64 and arm64
porte experiment --repeats 7
porte selftest              # the oracle must reject the mutant
porte cache list
```

The experiment prints a summary and writes `results/experiment.json`,
`profiles/*.json`, `results/oracle/*` and a cache entry under `build/cache/`.

### CLI

| command | purpose |
| --- | --- |
| `porte build [--force]` | compile fixture images for both architectures |
| `porte analyze <binary>` | basic-block inventory, indirect transfers, hashes |
| `porte profile [--workload X]` | boundary + PC-sampled profile of the baseline |
| `porte specialize` | profiles → guarded ARM64 module (generated C + dylib) |
| `porte verify --dylib P` | differential oracle over the six-case corpus |
| `porte run --arm baseline\|native\|spec --workload X` | one arm, one run |
| `porte bench --dylib P [--repeats N]` | four-arm measurement with p10/p90 |
| `porte experiment` | the whole pipeline, cached and reported |
| `porte warm` | reload a cached artifact, re-check its key, smoke-verify |
| `porte selftest` | mutation test: prove the oracle can fail |
| `porte cache list\|show` | inspect verified artifacts and their keys |
| `porte pe analyze\|optimize\|verify\|run\|bench\|selftest\|warm\|experiment <exe>` | opaque PE pipeline (prior phase) |
| `porte frame [--repeats N]` | Windows-vs-native frame gap, per layer |
| `porte app install\|list\|inspect\|launch\|remove` | generate and manage macOS application bundles |
| `porte app import <pkg> [--accept]` | install `.exe`, `.msi`, `.msix`/`.appx`; bundle discovered apps |
| `porte app package-info <pkg>` | inspect an MSI/MSIX without installing |
| `porte app products` | installed Windows products and their environments |
| `porte app repair\|uninstall <product>` | verify and re-extract, or remove product + apps |
| `porte runtime verify` | report the compatibility runtime Porte found and whether it is usable |
| `porte runtime verify-e2e` | drive a Win32 fixture, Notepad++ and 7-Zip's MSI end to end, writing `results/runtime_verification.json` |
| `porte runtime profile <exe>` | sample a real Windows program under the runtime and report how much time is guest code, runtime, Rosetta or host (`results/wine_profile.json`) |

## How it works (30 seconds)

1. **Profile** the x86-64 module through Rosetta: boundary call records, sampled
   guest PCs symbolicated per image, and an instrumented pass that reports data
   facts such as the observed value window. Overhead is measured, not assumed.
2. **Specialize**: facts become a plan with an explicit assumption ledger. The
   generator emits C for the hot module, guarded by those assumptions, linked
   against the generic module as its fallback.
3. **Verify**: three arms (translated, native generic, candidate) over a corpus
   that includes inputs violating each guard. Compare return values, output
   digests, ordered host-effect ledgers and the path taken. A known-buggy mutant
   must be rejected before anything is cached.
4. **Cache** under a key covering application hash, module hash, profile digest,
   corpus digest, transform source hash, host, OS build and toolchain. Any change
   invalidates; a smoke verification runs on load.

## Layout

```
porte/            profiler, specializer, oracle, cache, benchmarks, CLI
runtime/          PC sampler + boundary probe; shared observable contract
fixtures/app/     deterministic workload driver (same source, both arches)
fixtures/kernel/  generic baseline (libc qsort) and the in-module-sort control
fixtures/host/    host effect sink with single and batched entry points
porte/app/        bundle generator, AppKit launcher, runtime, MSI/MSIX importers
porte/gui/        structured bridge and out-of-process runner for the macOS app
macos/            SwiftUI application, Launch Services tooling, checks, verifier
fixtures/frame/   Windows PE + native + Rosetta frame benchmark, one Metal scene
fixtures/installer/  MSI/MSIX fixtures, payloads, end-to-end verifier
docs/             prior art, architecture, reports, baselines, app integration
profiles/         committed profile documents for the reported run
results/          committed experiment JSON, oracle ledgers, mutation artifacts
build/            generated images, specializations, cache (git-ignored)
```

## Scope and honesty

- The baseline is **Rosetta on Mach-O**, not a Windows PE. This host has no
  Wine/FEX runtime, and Rosetta cannot enter native code inside a translated
  process, so the seam used here is the module boundary — the same seam a PE
  port would use in an ARM64 Wine + FEX process. See the architecture doc.
- The specialization is generated from a **source-level module contract**, not
  lifted from an arbitrary binary. A general lifter is future work; the pipeline
  around it is what this repository tests.
- Equivalence is **evidenced**, never proven: guards, a corpus, and a sensitivity
  test. Any untested input that violates an unguarded assumption remains a risk.
- No DRM/anti-cheat interaction, no universal compatibility claim, no graphics
  driver. Games are roadmap, not results.
- Rosetta's full support ends with macOS 27, which is why the profiler and cache
  are translator-agnostic and the PE path targets FEX rather than Rosetta.

## Contributing and security

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — build, test, and the rules for changes
  to measured behaviour.
- [`SECURITY.md`](SECURITY.md) — threat model, what is trusted today, and how to
  report a vulnerability privately.
- [`docs/08-packaging.md`](docs/08-packaging.md) — repository layout, CI, and
  release checks.

## License

Porte's original source code is released under the MIT License; see
[`LICENSE`](LICENSE). Third-party components keep their own licenses — the
engine links Unicorn (GPLv2), so **binary distributions that include Unicorn are
subject to GPLv2 obligations**. [`THIRD_PARTY.md`](THIRD_PARTY.md) records the
audit, notices and distribution constraints.
