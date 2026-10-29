## What changed

<!-- One or two sentences. Say why, not only what. -->

## Area

- [ ] Engine / nativeization
- [ ] macOS application bundles
- [ ] Package importer (`.exe`, `.msi`, `.msix`/`.appx`)
- [ ] Graphics / frame path
- [ ] Measurement, docs or tooling

## Verification

<!-- Exact commands you ran and their result. A build succeeding is not a test. -->

```bash
.venv/bin/python -m compileall -q porte fixtures/installer tests
.venv/bin/python -m unittest discover -s tests -v
./fixtures/installer/build.sh && .venv/bin/python fixtures/installer/verify.py
.venv/bin/porte build --force && .venv/bin/porte selftest
```

Result:

## Evidence and honesty

- [ ] New behaviour is covered by a check that can fail.
- [ ] Committed results under `results/` or `profiles/` were not edited to match
      new code; new measurements are labelled with host and date.
- [ ] The change does not weaken the differential oracle, skip corpus cases, or
      widen tolerances to make a candidate pass.
- [ ] Documentation distinguishes implemented behaviour from roadmap work.
- [ ] No generated binaries (`build/`, EXE/MSI/MSIX, `.app`, `.venv`) are included.

## Limitations

<!-- What this does not do, and what you could not test. Say "not tested" plainly. -->
