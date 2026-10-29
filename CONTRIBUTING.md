# Contributing to Porte

Porte is an experimental research prototype, not a compatibility product. The
most useful contributions are measurements, negative results, and fixes that keep
the evidence honest.

## Set up

```bash
git clone https://github.com/notomer/porte.git
cd porte
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/porte --help
```

You need macOS on Apple Silicon, Xcode command line tools (clang, objdump, nm,
atos) and Rosetta 2 for the translated baseline. The package fixtures also need
`msitools` and `mingw-w64`:

```bash
brew install msitools mingw-w64
```

## Checks before a pull request

Run these; CI runs the same set.

```bash
.venv/bin/python -m compileall -q porte fixtures/installer
.venv/bin/python -m unittest discover -s tests -v
./fixtures/installer/build.sh
.venv/bin/python fixtures/installer/verify.py
.venv/bin/porte build --force
.venv/bin/porte selftest
```

`verify.py` is the end-to-end check for the importer: MSI database and cabinet
extraction, registry table, upgrade, repair, custom-action refusal, MSIX block
map validation, bundle generation, headless launch and uninstall. It must print
`all checks passed`.

`porte selftest` proves the differential oracle rejects a known-buggy module. A
change that makes it pass while the mutant is admitted is a regression, not an
improvement.

## Rules for measured behaviour

- **Do not edit committed results to match new code.** `results/` and `profiles/`
  are evidence for a specific run on a specific host. Re-run the experiment and
  commit new numbers with the host and date, or leave the old ones alone.
- **Do not weaken the oracle.** No skipped corpus cases, no relaxed comparisons,
  no widened tolerances to make a candidate pass. If a case is wrong, fix the
  case and say why in the pull request.
- **Do not claim more than the measurement.** The vocabulary is precise: a
  "specialized module with fallback" is not a "native port"; parity on a
  GPU-bound frame is not parity on a CPU-bound frame.
- **New behaviour needs a check that can fail.** For engine, importer or bundle
  changes, add an assertion to the closest verifier (`fixtures/installer/verify.py`,
  `porte selftest`, or a new test under `tests/`).

## Code layout

| path | contents |
| --- | --- |
| `porte/` | profiler, specializer, oracle, cache, CLI, engine |
| `porte/app/` | bundle generator, AppKit launcher, in-bundle runtime, MSI/MSIX importers |
| `porte/pe/` | opaque PE lifter, profiler, dispatcher, pipeline |
| `runtime/` | C probe and the shared kernel/host contract |
| `fixtures/` | workloads, Windows PEs, installer packages, verifiers |
| `docs/` | prior art, architecture, experiment reports, limits |

Keep compiler and lifter logic stable unless the change is the point of the pull
request; unrelated rewrites make the evidence hard to review.

## Style

- Python 3.10+, standard library first, type hints where they clarify an
  interface.
- Docstrings state what a module guarantees and what it refuses, not how it was
  written.
- No new runtime dependency without a licensing note in `THIRD_PARTY.md`.
- Tests and verifiers print what they checked, not just pass/fail.

## Commits and pull requests

- One logical change per commit; the message says why, not just what.
- Describe the verification you ran, including the exact commands.
- Note any limitation you introduced or discovered.
- Do not commit `build/`, `.venv/`, generated EXEs/MSIs/MSIXs, or app bundles.

## Security

Do not report vulnerabilities in a public issue. See [`SECURITY.md`](SECURITY.md).
