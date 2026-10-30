# 11 — Competitive analysis: what already exists, and what Porte actually adds

Written 2026-10-29 on the machine the rest of this repository was measured on
(Apple M2 Max, macOS 27.2, Wine Staging 11.20 under Rosetta 2, and MetalSharp
Wine 4.0 — Wine 11.20 built natively for arm64 with a FEX fork). Every claim
below is either linked to a public source that was read during this audit, or
marked as measured on this host with the command that produced it. Claims that
could not be checked are labelled as such rather than repeated.

**Revision note (Phase 4).** The first edition of this document concluded that
x86-64 to ARM64 translation did not exist for Wine on macOS. That was wrong, and
the error mattered: it removed the only architecture in which a native
optimization layer could exist. §2 and §3 are rewritten from the projects'
sources and from a runtime installed and measured here; §10 is rewritten to
follow the new evidence. The parts that were right — Highball duplicates the
frontend, nobody does per-application native code generation — are unchanged.

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

- **Wine** implements the Windows API (PE modules) over POSIX. The widely used
  macOS builds (Gcenx, WineHQ packages) are x86-64 Mach-O processes that macOS
  runs under Rosetta. Wine 11.20 in that configuration was measured here;
  `msiexec`, `user32`, `gdi32`, WoW64 and the Mac driver are all present.
- **CrossOver** is CodeWeavers' commercial Wine with support, a patched tree and
  D3DMetal integration. Its licence does not permit redistribution of the
  proprietary parts; the Wine tree it derives from is LGPL. CodeWeavers shipped
  a **native ARM64 preview build in July 2026** (announced as "the right to bear
  ARM64 on Mac"): Wine itself compiled for aarch64, with x86-64 Windows programs
  run by a macOS-compatible build of FEX. That is the commercial proof that the
  native-ARM64 architecture is not hypothetical.
- So the split that matters is not "Wine or no Wine", it is **which process
  architecture Wine runs in**: x86-64 under Rosetta (no native code may join it)
  or native arm64 with FEX (native code may join it, §3.2).

Porte's Wine backend is a thin, honest client of that runtime: discovery,
prefix creation, `msiexec`, and launch. It adds no execution capability that
Wine does not have.

---

## 3. Native ARM64 Wine with FEX on macOS

Source read: `github.com/metalsharp/MetalSharp-Wine` (README, `docs/STATUS.md`,
`docs/PERFORMANCE.md`, `patches/`), the CodeWeavers ARM64 preview announcement,
and the installed runtime itself. Everything in §3.2 and §3.3 was measured on
this host with MetalSharp Wine 4.0, not quoted.

### 3.1 What FEX is, and what it needed to become

FEX-Emu is a userspace x86 and x86-64 **JIT translator** for AArch64: it decodes
guest x86 into an IR, optimises it, and emits ARM64. Its upstream target is
AArch64 **Linux**, including the syscall layer — which is why "FEX on macOS" was
correctly dismissed for years as a port of the host layer rather than a
configuration change.

That port now exists. MetalSharp's FEX fork (FEX-2610 + 22 commits, not
upstreamed) is built for macOS and linked into Wine's ARM64EC provider
(`libarm64ecfex.dll`); i386 uses `libwow64fex.dll` instead. The Mac-specific
work is in `docs/STATUS.md` and is architectural, not cosmetic: x18/TEB
preservation across syscalls, a 4 GB `PAGEZERO` and the cross-architecture
entitlement, 16K-page W^X handling, emulated stores into W+X pages, and
`KUSER_SHARED_DATA` relocation with the load rebased at JIT time.

### 3.2 How ARM64 code gets into a translated process

On Windows ARM64 a 64-bit x86 process does not exist as such: the loader starts
an ARM64 process and **ARM64EC** is how x86-64 code is called inside it. Wine
implements that shape: its own PE modules are built as ARM64X (one image with
arm64ec and aarch64 entry points), x86-64 guests run through FEX, and an
x86-64 Windows program is an ARM64 process whose guest code is JIT output.

Consequence, and this is the whole architectural finding of this phase: in a
**native arm64** Wine process, native arm64 code and the translated guest share
one address space, and the loader will load ARM64EC/ARM64X modules into it.
MetalSharp already does exactly that for its own DLLs, and ships a third-party
ARM64EC DLL (`lib/wfdxcompat/.../wfdx-launchers-v1.dll`) that ntdll loads into
the x86-64 Rockstar launcher — proof that the mechanism accepts code that is not
part of Wine.

### 3.3 What the translator itself gives a profiler

Measured here with `FEX_LIBRARYJITNAMING=1 FEX_BLOCKJITNAMING=1`: FEX writes a
perf map, `/tmp/perf-<pid>.map`,

```
0x7ff4ade70004 690 \??\c:\program files\7-zip\7z.exe+0x61380 (0x7ff4ade70004)
```

— the host address and size of every translated block and the guest module + RVA
it came from (6,277 such entries for one 7-Zip run). That is precisely the
mapping Phase 3 recorded as unavailable under Rosetta. Its coverage is
incomplete in this build: the sampled hot loop's addresses fall outside every
named range (§`docs/13`, Experiment 5), so attribution works in principle and
not yet in practice.

### 3.4 Memory model

FEX emulates x86 TSO in software (`LRCPC`/`LSE` sequences), with separate
switches for the model as a whole and for the pieces that dominate real programs:
`FEX_TSOENABLED`, `FEX_HALFBARRIERTSOENABLED`, `FEX_VECTORTSOENABLED`,
`FEX_MEMCPYSETTSOENABLED`. A program that never runs two threads at once cannot
observe those barriers, which makes them removable for that program — the one
optimization this phase could implement and verify (`docs/13`, Experiment 6).

### 3.5 Performance, as published and as measured

The project's `docs/PERFORMANCE.md` (Apple M4, 2026-10-22) reports x86-64
through FEX at 0.94–1.01× native ARM64 on C workloads except JIT-cold paths, and
CPython at 1.67× native overall. Measured here on LZMA (`docs/14`): FEX is
**0.97×** Rosetta Wine, i.e. the native-ARM64 architecture is not by itself a
speedup, and native arm64 7-Zip is **1.57×** faster than the same program under
FEX. Whatever Porte contributes has to be measured against those two numbers.

---

## 4. Hangover and ARM64EC

- **ARM64EC** is Microsoft's ABI for running x86-64 code inside an ARM64
  Windows process (call thunks, `xtajit` emulation on Windows). It is a Windows
  feature: it depends on Windows' loader, its exception model and its ntdll.
- **Hangover** applies that idea to Wine *on Windows on ARM*, so 32-bit and
  64-bit Windows programs run in an ARM64 Wine.

Neither exists as a Windows feature on macOS, and there is no mixed-architecture
process under Rosetta: the probe in `docs/13` shows an x86-64 process cannot even
`dlopen` an arm64 dylib. But **Wine implements the ARM64EC shape itself** when it
is built for arm64 (§3.2): the host process is arm64, so "ARM64EC thunking inside
a translated process" is no longer a contradiction — it is how MetalSharp runs
Wine's own DLLs, D3D and a third-party launcher shim. The Rosetta-specific
impossibility stands; the general one does not.

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
| FEX-Emu | MIT | a fork may be modified and shipped with attribution; upstream is not macOS-ready |
| MetalSharp-Wine (Wine + FEX + patches) | LGPL-2.1+ Wine, MIT FEX, per-payload licences | usable as an installed runtime; redistribution means shipping sources/offers for the LGPL parts, and the fork is not upstream, so it is not a stable platform to build on |
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
2. Native ARM64 Wine with FEX is real on macOS (MetalSharp Wine 4.0, CodeWeavers'
   ARM64 preview), and it removes the platform barrier that Phase 3 mistook for
   a permanent one: an arm64 Wine process can host native arm64/ARM64EC modules.
3. It does not hand anyone a speedup. Measured on the same workload, FEX is 0.97×
   Rosetta, native arm64 is 1.57× faster than either, and the memory-model
   specialization in `docs/13` recovers 1.09× of the gap for a program that does
   not need x86 ordering.
4. No audited project does per-application native code generation for Windows
   binaries on macOS, and none ships a measured, verified semantic specialization
   per application. That territory is now reachable rather than blocked — and it
   is reachable **through** FEX, whose licence permits it, rather than around it.
5. The honest choice is between (a) building the specialization layer against a
   native ARM64 Wine runtime and measuring it app by app, (b) contributing the
   measurement machinery and patches upstream to FEX/Wine, or (c) stopping if the
   next milestone cannot beat 1.09×. `docs/12-technical-direction.md` makes that
   call with numbers, and `docs/14` holds the benchmark that decides it.
