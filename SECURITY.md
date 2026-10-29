# Security policy

## Supported versions

Porte is an experimental prototype with no released version. Security fixes land
on `main`; there are no maintained release branches yet.

## Reporting a vulnerability

Please do not open a public issue for a vulnerability.

- Preferred: GitHub's private vulnerability reporting on
  [`notomer/porte`](https://github.com/notomer/porte/security/advisories/new).
- Alternative: email `notomerkhan@gmail.com` with a description and, if
  possible, a minimal reproduction.

You can expect an acknowledgement within a few days. This is a single-maintainer
project, so timelines are best effort; fixes are published with a note in the
release history.

## What Porte executes, and what that means

Porte runs untrusted Windows binaries inside a CPU emulator and translates their
API calls through host stubs implemented in Python. It is **not** a sandbox and
does not claim to make Windows malware safe:

- The guest can call the host stubs that exist today (`WriteFile`,
  `GetStdHandle`, timing, `VirtualAlloc`, `PresentFrame`, …). File writes go to
  captured output, not arbitrary paths, but a future stub could widen that.
- Generated macOS bundles contain the original Windows image unchanged and run
  it through the same engine. A bundle is only as safe as the image it carries;
  consent on install is a speed bump, not a security boundary.
- Bundles are ad-hoc signed, not notarized. Gatekeeper rejection is expected and
  is not bypassed by Porte.
- MSI and MSIX importers verify what they can (embedded cabinet structure, MSIX
  block-map hashes, path traversal, size limits) and **do not** verify
  Authenticode signatures. An unsigned or maliciously signed package installs
  files into an isolated per-product directory; no vendor code is executed
  during install.
- Custom actions that execute code are refused by default. `--allow-custom-actions`
  downgrades the refusal to a warning and still does not execute the action; it
  exists for research on packages with irrelevant actions.
- The research fixtures (`porte build`, `porte experiment`, `porte frame`) build
  and run local code with your user's privileges. Do not run them on a machine
  where compiling arbitrary C fixtures is unacceptable.

## Threat model for the importer

Trusted: the local user, the repository's own fixtures, the toolchain installed
by the user (clang, msitools, mingw-w64).

Untrusted: any `.exe`, `.msi`, `.msix`/`.appx` the user imports. The importer
protects the filesystem boundary (path traversal, absolute paths, oversized
archives, hash mismatches) and the install environment boundary (per-product
directories, no writes outside them). It does not protect against the guest
misbehaving once launched with the stubs it is given.

## Known non-issues

- Gatekeeper rejecting generated bundles.
- `spctl` refusing ad-hoc signatures.
- macOS asking for confirmation the first time a generated app runs.
- The emulator producing a fault instead of executing an unsupported
  instruction: the fault is reported, not hidden.
