# Repository, packaging and releases

## Name and identifiers

| item | value |
| --- | --- |
| product name | Porte |
| tagline | Your Windows apps. At home on Mac. |
| CLI | `porte` (deprecated alias: `forge`) |
| Python package | `porte` |
| generated bundle ids | `com.notomer.porte.windows.<app>-<digest>` |
| macOS display name of generated apps | the imported program's name |
| repository | `github.com/notomer/porte` |

The project was previously named **Forge**. The rename touched the Python
package, CLI, generated bundle identifiers, fixtures, build scripts and
documentation. Committed measurements in `results/` and `profiles/` were produced
under the old name and are left as recorded; they are evidence, not branding.
The `forge` console script remains as a warning alias for one release cycle.

There is no automatic migration of Forge-era installations. Applications
created under `com.forge.windows.*` and products under
`~/Library/Application Support/Forge/` are left untouched; `porte app list`
will not show them, and `porte` will not reuse their state. Remove them with
`forge app remove <name>` (alias) or by hand, then re-import the package.

Not renamed: third-party project names in the prior-art and roadmap documents,
and the C ABI of the research fixtures beyond the `porte_` prefix (the fixture
contract, kernel entry points and probe are all consistently `porte_*`).

## Layout

```
porte/                 Python package: profiler, specializer, oracle, cache, CLI
porte/app/             bundle generator, AppKit launcher, runtime, MSI/MSIX importers
porte/app/msi/         CFB reader, MSI table reader, CAB reader, install engine
porte/gui/             JSON bridge and out-of-process runner used by the macOS app
macos/                 SwiftUI app, Launch Services tooling, checks, end-to-end verifier
porte/frame/           frame-path experiment (Windows PE vs native Metal)
porte/pe/              opaque PE lifter, profiler, dispatcher, pipeline
runtime/               C probe and the shared kernel/host contract
fixtures/              workloads, Windows PEs, installer packages, verifiers
tests/                 fast unit checks (no GUI, no long benchmarks)
docs/                  prior art, architecture, reports, limits, this file
profiles/              committed profile documents for the reported runs
results/               committed measurement artifacts
build/                 generated output (git-ignored)
```

## Continuous integration

`.github/workflows/ci.yml` runs on `macos-15` (Apple Silicon) for pushes to
`main` and every pull request. The retired `macos-14` image is no longer used:

1. syntax check (`compileall`) over the package, importer fixtures and tests;
2. both CLI entry points (`porte`, and the deprecated `forge` alias);
3. `unittest` suite — identifiers, path safety, CLI, MSI/MSIX parsing, and the
   GUI bridge protocol (consent, analysis, a real MSI install, uninstall);
4. `fixtures/installer/build.sh` + `fixtures/installer/verify.py` — the end-to-end
   importer check (tables, cabinet, registry, upgrade, repair, refusals, MSIX
   block map, bundle generation, headless launch, uninstall);
5. `porte build --force` + `porte selftest` — the engine fixtures and the oracle
   mutation check;
6. `macos/build.sh` + `porte-check` — build the SwiftUI app and run the Swift
   checks (bridge handshake, setup state, Info.plist document types, file-open
   queue);
7. `macos/verify.sh` — the native end-to-end pass: Launch Services registration,
   file-open delivery to a running and a stopped app, missing-runtime recovery,
   install to a real `.app`, launch from Applications without the manager, and
   uninstall.

The workflow installs `msitools` and `mingw-w64` with Homebrew because the MSI
and PE fixtures are authored with reference toolchains rather than by Porte's own
writers. Frame-path measurements (`porte frame`) and the full experiment are not
run in CI: they need window-server Metal access and take minutes, and their
numbers are only meaningful on a labelled host. They remain manual, with results
committed deliberately.

## Release checklist

1. `main` is green in CI.
2. `porte selftest` and `fixtures/installer/verify.py` pass locally on the
   release host.
3. `pyproject.toml` version is bumped; `CHANGELOG`-style notes live in the GitHub
   release description.
4. `THIRD_PARTY.md` is re-read: no dependency added since the last release without
   a licensing note.
5. No generated artifacts are committed (`build/`, EXEs, MSIs, MSIXs, `.app`).
6. Generated bundles and `Porte.app` are ad-hoc signed only; the release notes
   must not imply notarization or Gatekeeper approval. `Porte.app` embeds a
   `porte-runtime.json` pointer to the build host's runtime, so it is a developer
   build, not a redistributable installer.
7. If a binary distribution includes Unicorn, the GPLv2 obligations in
   `THIRD_PARTY.md` apply; a source-only GitHub release does not trigger them.

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
./fixtures/installer/build.sh && .venv/bin/python fixtures/installer/verify.py
./macos/build.sh && ./macos/verify.sh
```

`verify.py` points `PORTE_ENV_ROOT` and `PORTE_SUPPORT_ROOT` at a temporary tree,
so it does not touch installed products or `~/Applications`. Those overrides exist
for tests and sandboxing; normal installs use
`~/Library/Application Support/Porte/`.
