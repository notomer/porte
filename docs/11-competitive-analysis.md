# 11 — Competitive analysis: what already exists, and what Porte actually adds

Written 2026-10-29 on the machine the rest of this repository was measured on
(Apple M2 Max, macOS 27.2, Wine Staging 11.20, Rosetta 2 present). Every claim
below is either linked to a public source that was read during this audit, or
marked as measured on this host with the command that produced it. Claims that
could not be checked are labelled as such rather than repeated.

The question this document answers is not "is Porte good?" but "is any part of
Porte's plan already solved by something that exists, and if so, which part?".

---

## 1. Highball

Source read: `github.com/gauthierpiarrette/highball` README and architecture
section, `gethighball.com`, and `github.com/gauthierpiarrette/highball-db`
(linked from the README).

### 1.1 What it is

A native macOS app plus a CLI, both thin views over `HighballKit`. It sets up
Wine, DXMT, D3DMetal and DXVK for the user, installs or connects Steam/Epic
libraries, and publishes per-game renderer verdicts. Its stated differentiator
is the **data**: an open CC0 recipe/database (`highball-db`) with provenance on
every claim (verified runs, community reports, machine-derived predictions
labelled separately).

### 1.2 Architecture, as described by the project

| part | implementation | runs where |
| --- | --- | --- |
| engine | manifest-defined bundle of Wine + runtime dylibs + renderer overlays under `engines/<id>`, pinned and SHA-256 verified | macOS |
| environment | `WINEPREFIX` plus `bottle.json` | macOS |
| recipes | declarative JSON steps (`installer`, `registry`, `winetricks`, `sync`, `renderer`, `pin`, `copy`, `file`, `note`) | data |
| renderers | WineD3D / DXMT / D3DMetal / DXVK selected per launch via `WINEDLLPATH_PREPEND` | macOS |
| x86-64 execution | Wine (x86-64) under **Rosetta 2** | macOS |
| 32-bit apps | Wine's WoW64 | macOS |
| compatibility data | CC0 JSON in `highball-db` | data |

### 1.3 The seven questions, answered

1. **What does it implement?** Orchestration: engine install/verify, bottle
   lifecycle, recipe application, renderer switching, store/launcher setup,
   compatibility verdicts, reporting.
2. **Does it do its own binary translation?** No. There is no translator in the
   project description or architecture section. x86-64 execution is Wine's,
   under Rosetta.
3. **Which runtimes does it depend on?** Wine (several builds: Gcenx upstream
   builds, the Sikarugir runtime, and a Wine 11 build from the CrossOver 26.3
   tree), DXMT, DXVK, D3DMetal (Apple, licence-gated, never redistributed), and
   Rosetta 2.
4. **Does it do application-specific ARM64 optimization?** No. It selects a
   renderer and a Wine build per application; that is configuration selection,
   not code generation.
5. **How does it handle x86 memory ordering?** It does not have to: Rosetta
   translates the process and provides x86-64 memory semantics.
6. **How do graphics translation and caching work?** Through the renderers it
   selects: D3DMetal (D3D11/12 → Metal), DXMT (D3D10/11 → Metal), DXVK
   (D3D9/10/11 → Vulkan → MoltenVK → Metal), WineD3D (OpenGL). Caching is the
   renderers' own (pipeline/shader caches), not something Highball implements.
7. **Where does measurable overhead remain?** In the layers it composes: Rosetta
   translation, Wine's Win32 implementation, the D3D→Metal path. Measured on
   this host for the CPU side in §8.

### 1.4 What this means for Porte

Everything Porte's *frontend* does — runtime discovery, prefix/bottle
management, per-app launch configuration, installer recipes, `.app` generation,
a compatibility status per app — has a counterpart in Highball (`.app`
generation being the one exception: Highball launches from a bottle, Porte wraps
into a bundle). The frontend is therefore not a differentiator; it is duplicated
work with a smaller database behind it.

---

## 2. Wine and CrossOver

- **Wine** implements the Windows API (PE modules) over POSIX. On macOS the
  published builds (Gcenx, WineHQ packages) are x86-64 Mach-O processes that
  macOS runs under Rosetta. Wine 11.20 was measured here; `msiexec`, `user32`,
  `gdi32`, WoW64 and the Mac driver are all present.
- **CrossOver** is CodeWeavers' commercial Wine with support, a patched tree and
  D3DMetal integration. Its licence does not permit redistribution of the
  proprietary parts; the Wine tree it derives from is LGPL.
- Neither implements x86-64 → ARM64 translation. Both rely on Rosetta on Apple
  silicon.

Porte's Wine backend is a thin, honest client of that runtime: discovery,
prefix creation, `msiexec`, and launch. It adds no execution capability that
Wine does not have.

---

## 3. FEX-Emu

Source read: `github.com/FEX-Emu/FEX` and `fex-emu.com`.

FEX is a **userspace x86 and x86-64 emulator for AArch64 Linux**. It JITs guest
code through an IR, and its system-call translation layer targets the Linux
kernel ABI. It is not a macOS product: the Linux syscall layer, the host
libraries it expects, and its process model do not exist on macOS. Portability
to Darwin would be a port of the syscall/host layer, not a configuration change,
and Apple's Rosetta already occupies that role for whole processes.

Consequence for Porte: FEX cannot be dropped into a macOS build to obtain
translation, and its techniques (IR + JIT) are the same family as Porte's
prototype engine, not a shortcut past it.

---

## 4. Hangover and ARM64EC

- **ARM64EC** is Microsoft's ABI for running x86-64 code inside an ARM64
  Windows process (call thunks, `xtajit` emulation on Windows). It is a Windows
  feature: it depends on Windows' loader, its exception model and its ntdll.
- **Hangover** applies that idea to Wine *on Windows on ARM*, so 32-bit and
  64-bit Windows programs run in an ARM64 Wine.

Neither is available on macOS. Apple silicon macOS has no ARM64EC loader, no
`xtajit`, and no mixed-architecture process: the Rosetta probe in
`docs/13-nativeization-experiment.md` shows an x86-64 process cannot even
`dlopen` an arm64 dylib. Any plan that assumes "ARM64EC-style thunking inside a
Rosetta Wine process" is therefore wrong on this platform, not merely hard.

---

## 5. Apple Game Porting Toolkit, D3DMetal, DXMT

- **GPTK / D3DMetal** (Apple): Direct3D 11/12 → Metal, hosted by a Wine
  environment, licence-gated, redistributable only under Apple's terms.
- **DXMT** (3Shain, MIT/LGPL): Direct3D 10/11 → Metal, used by Porte-adjacent
  tools and by Highball.
- **DXVK** (Zlib): D3D9/10/11 → Vulkan; on macOS through MoltenVK.

These are the GPU half of the problem, and they are active, well-funded
projects. A single developer duplicating them is not a plan. They also confirm
the pattern that works on macOS: **translate at an API boundary to a native
framework**, rather than emulate a CPU.

---

## 6. Binary recompilation, static and dynamic

| project | approach | host | usable on macOS ARM64 for Win32? |
| --- | --- | --- | --- |
| Rosetta 2 | whole-process AOT+JIT x86-64 → ARM64, closed | macOS | yes, already does it |
| FEX-Emu | userspace JIT, IR, Linux syscalls | AArch64 Linux | no |
| qemu-user / box64 | userspace emulation, library wrapping | Linux | no |
| static recompilers (e.g. console decomp projects) | offline x86/MIPS → C/ARM64 rebuild per binary | any | not applicable to Win32 apps at scale |
| Wine + ARM64EC (Windows) | API layer native, guest emulated by OS | Windows on ARM | no |

Porte's Phase-2 engine is in the last family but ahead of time and per-loop,
with an oracle. Nothing in this table does *guarded, profile-driven,
application-specific specialization of an opaque Win32 binary with differential
verification*. That is the only technical space in this audit that is not
already occupied — and §8 of `docs/12-technical-direction.md` explains why it is
currently unusable for real applications.

---

## 7. Feature comparison

| capability | Porte | Highball | CrossOver | Wine (raw) |
| --- | --- | --- | --- | --- |
| Runs Windows x86-64 GUI programs | yes (Wine + Rosetta) | yes | yes | yes |
| MSI installation | yes (`msiexec`) | via recipes | yes | manual |
| macOS `.app` per Windows program | yes | no | no | no |
| Finder association / double-click | yes | no | partial | no |
| Compatibility database | small, local | CC0, large, versioned | vendor | none |
| Renderer selection (DXMT/D3DMetal/DXVK) | no | yes | yes | manual |
| Steam/Epic integration | no | yes | partial | no |
| Own binary translator | prototype (engine) | no | no | no |
| Per-application ARM64 code generation | prototype, offline | no | no | no |
| Differential correctness oracle for generated code | yes | n/a | n/a | n/a |
| Upstream patch pipeline | no | yes (`spike/`) | yes | n/a |

Two rows matter. Porte's *unique* rows are the translation research and the
oracle. Its *duplicated* rows are everything a user touches.

---

## 8. Where the overhead actually is, measured here

Command: `porte runtime profile <7z.exe> --arg=a --arg=-t7z --arg=-mx=9
--arg=-mmt=off ... --seconds 8` (`porte/backends/profile.py`), 28,739 samples of
a genuine third-party Windows x86-64 program under Wine 11.20.

| layer | share of samples |
| --- | ---: |
| unattributed system ranges (threads waiting in `mach_msg`) | 56.3% |
| Rosetta runtime routines (translation/emulation helpers) | 22.3% |
| translated guest x86-64 (LZMA inside `7z.exe` + Wine's PE modules) | 21.3% |

Of the samples that are *executing* rather than waiting, about **49% are inside
Rosetta's own runtime** and 51% in translated guest code. The sampler cannot
split "translated guest" into the application and Wine's Win32 modules: Rosetta
reports JIT addresses, not the originating instruction addresses. That is not a
tooling gap to be fixed later; it is how translated code is exposed.

So: the overhead a nativeization engine would have to remove is real and large
(≈half of executing time), but it is inside a process whose code Porte cannot
replace or even address-map.

---

## 9. Licensing

| component | licence | consequence |
| --- | --- | --- |
| Porte | MIT | GPL-3 code cannot be copied in |
| Highball app/CLI/Kit | GPL-3.0 | read for ideas only; no code reuse without a deliberate GPL-3 relicense of Porte |
| highball-db recipes/database | CC0-1.0 | reusable as data, with attribution practice |
| Wine | LGPL-2.1+ | Porte links nothing into it; launches it |
| DXMT | MIT / LGPL | usable if Porte ever selects renderers |
| D3DMetal (GPTK) | Apple licence | never redistribute |

A future integration with Highball's *data* is licence-clean. Reusing its *code*
is not, unless Porte adopts GPL-3 — a decision with product consequences that
should be made deliberately, not by copying a file.

---

## 10. Conclusion of the audit

1. Highball has already built the launcher, engine management and data layer
   that Porte spent its last two phases adjacent to. Porte's frontend duplicates
   it with less data and fewer renderers.
2. No audited project does per-application native code generation for Windows
   binaries on macOS. Porte's only unclaimed territory is its engine and its
   oracle.
3. That territory is boxed in by the platform: x86-64 Windows code on macOS is
   Rosetta-translated, and translated processes cannot host arm64 code
   (measured, §13). A "native acceleration layer next to Wine" cannot exist in
   the form usually imagined.
4. The honest choice is therefore between (a) building an engine complete enough
   to replace Wine for some class of programs, (b) contributing to the layer that
   actually runs the code, or (c) stopping. `docs/12-technical-direction.md`
   makes that call with numbers.
