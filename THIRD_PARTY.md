# Third-party components and licenses

Porte's own source is MIT (`LICENSE`). That does **not** relicense anything it
depends on or runs. This file records the audit performed before choosing MIT,
and the obligations that remain.

## Runtime dependencies (imported by Porte)

| component | version used | license | role | redistribution notes |
| --- | --- | --- | --- | --- |
| [Unicorn](https://github.com/unicorn-engine/unicorn) | 2.1.x | **GPLv2** | x86-64 CPU emulation for the PE engine | PyPI metadata says "BSD License" in its classifier, but the project and its distribution describe GPLv2. Treat it as GPLv2. A binary distribution that bundles Unicorn is a distribution of a GPLv2 work: you must ship the corresponding source (including Porte's modifications, if any) and the GPLv2 text, and may not add restrictions beyond it. |
| [Capstone](https://github.com/capstone-engine/capstone) | 5.0.x | BSD-3-Clause | disassembly / instruction decoding | keep copyright notice and license text |
| [pefile](https://github.com/erocarrera/pefile) | 2024.x | MIT | PE parsing | keep copyright notice and license text |

Consequences:

- **Source distribution of Porte:** MIT applies to first-party code; installing
  dependencies from PyPI obtains them under their own licenses. No conflict.
- **Binary/self-contained distribution:** because Unicorn is linked into the
  same process, the combined distribution must satisfy GPLv2. Any future
  notarized `.app`, DMG or Homebrew cask that includes Unicorn therefore needs a
  GPLv2-compatible release of the combined work (source offer, license texts,
  no extra restrictions). A permissively licensed product build would require a
  replacement for Unicorn or a separate-process architecture, and that is a
  product decision, not a relicense.
- This repository does not bundle Unicorn binaries; `pip install -e .` resolves
  it locally.

## Build-time-only tools (not distributed with Porte)

| component | license | use |
| --- | --- | --- |
| [msitools](https://gitlab.gnome.org/GNOME/msitools) (`wixl`, `msibuild`, `msiinfo`) | LGPL-2.1-or-later | authors the MSI fixtures and independently validates the reader; never linked into Porte |
| MinGW-w64 (`x86_64-w64-mingw32-gcc`) | GPL/LGPL with runtime exception, plus public-domain headers | compiles the Windows fixture PEs; the resulting fixture binaries are not distributed as part of Porte |
| Apple clang / Xcode command line tools | Apple license | compiles fixtures, launchers and specialized modules |
| Rosetta 2 | Apple license | provides the translated baseline used in measurements; not redistributed |

## Referenced but not included

The prior-art and roadmap documents discuss FEX, Wine, DXVK, VKD3D-Proton,
MoltenVK, DXMT/Mullion, MetalSharp, Ferrum, Elevator, DynamoRIO, ARM64EC and
Apple's Game Porting Toolkit. **No code from those projects is vendored or
linked in this repository.** Their licenses (GPL/LGPL, MIT, BSD, Apple's GPTK
terms) constrain any future integration:

- Wine and FEX are LGPL-2.1+; DXVK, VKD3D-Proton and MoltenVK are zlib/MIT-style;
  MetalSharp and Ferrum are MIT/Apache-2.0 — verify at integration time, not from
  this list.
- Apple's Game Porting Toolkit is not open source and must not be redistributed;
  it may only be used as permitted by its license.
- Shipping any of these inside a Porte distribution means honoring their terms
  and adding their notices here. Do not relicense third-party code.

## Fixtures and test data

`fixtures/` sources are original to this project (MIT). Generated artifacts
(`build/`, EXEs, MSIs, MSIXs, dylibs) are outputs of the toolchain and are not
committed to the repository.

## Reporting a licensing concern

Open an issue titled `licensing:` or use the private channel in `SECURITY.md` if
the concern is sensitive. Remove or replace the component in question rather
than adding a restriction to first-party code.
