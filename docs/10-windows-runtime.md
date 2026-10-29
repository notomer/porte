# Running real Windows programs

Status: implemented and verified on this host (Apple silicon, macOS 27).
`results/runtime_verification.json` records the last full run:
25 checks for the Win32 fixture, 16 for Notepad++, 18 for 7-Zip's MSI.

This document answers one question and records the evidence for it: can an
unmodified, third-party Windows x86-64 GUI program be used on this Mac through
Porte?

---

## 1. Where execution used to stop

The path is Finder → `Porte.app` → Swift `AppModel` → `porte/gui/bridge.py` →
runtime backend → guest process → macOS window server. Porte's own engine
implements a CPU and a small, measured subset of the Windows API (process
creation, file/stdout, a few kernel32 calls). It has no `user32`, no `gdi32`, no
window manager and no message loop, by design and by measurement.

So the blocker was never the macOS plumbing. It was the guest half:

| layer | state before this phase | consequence |
| --- | --- | --- |
| Launch Services → Porte | worked | file delivered, analyzed |
| PE parsing, hashing, icon extraction | worked | correct identity |
| generated `.app` launch | worked | launcher started |
| **Win32 API surface (`user32`, `gdi32`, `comctl32`, `comdlg32`)** | **absent** | **no window could be created** |
| **x86-64 code execution at usable speed** | engine CPU only | UI-thread code unservable |

That is not a bug to patch: a Win32 window manager, GDI, the common controls,
OLE, the registry-driven shell and the installer engine are years of surface
area. Writing another one would have been the wrong move.

## 2. The decision

Porte now has two backends behind one interface (`porte/backends/`):

- `engine` — the original measured CPU/API implementation. Keep it: it is what
  the specialization work optimizes.
- `wine` — a real Windows compatibility runtime that Porte **discovers** on the
  machine (`~/Library/Application Support/Porte/Runtimes`, `/Applications/Wine*.app`,
  `PATH`, plus a user-chosen path). Porte never downloads, bundles or
  redistributes a runtime, so the license question stays the user's own Wine
  install (LGPL-2.1-or-later for WineHQ builds; CrossOver is proprietary).

Why the macOS Wine build: WineHQ's macOS binaries are x86-64 Mach-O and execute
through Rosetta 2, which is present and fast on Apple silicon. The alternatives
do not apply here:

- **FEX** is a Linux userspace emulator; it cannot be dropped into a macOS
  process, and Wine-for-macOS does not use it.
- **ARM64EC** is Windows-on-ARM's ABI; it has no macOS equivalent.
- **CrossOver's** Apple silicon build is proprietary and cannot be redistributed;
  Porte can still use it if the user has installed it, because discovery is by
  behaviour, not by vendor.

Selection is explicit. `auto` prefers the engine when the engine can actually
run the image, otherwise the runtime that can; `engine` and `wine` force one
backend and explain why it is unavailable instead of silently switching. A
backend that can analyze an image but not execute it no longer hides the one
that can.

## 3. What a Windows program gets

`porte/backends/wine.py`:

- **Analysis**: machine type, subsystem, imported DLLs, and which imports the
  runtime itself provides (read from its own library tree, not a hard-coded
  list), plus blockers for DLLs that must sit next to the image.
- **Execution**: the guest runs as a real child process in its own prefix under
  `~/Library/Application Support/Porte/Bottles/<key>/`. Wine owns the window,
  the message loop, keyboard/mouse, file dialogs and the exit status. Output is
  captured through files, not pipes, because Wine's helper processes inherit
  descriptors and never close a pipe.
- **Installation**: portable trees are copied into a bundle; `.msi` is handed to
  the runtime's own `msiexec /i`, then installed products are read from the
  prefix's registry (`Uninstall` keys) and turned into bundles. Extraction is
  never reported as installation.
- **macOS side**: the generated bundle records the runtime, the bottle and the
  PE hash; its launcher refuses to run if any of them is missing or changed, and
  it asks for consent before the first execution.

## 4. Evidence

Run with `porte runtime verify-e2e` (fixture + third-party), and by hand through
Finder-equivalent Launch Services opens.

| what | observation |
| --- | --- |
| Runtime | `Wine Staging 11.20` (`wine-11.20 (Staging)`), `Mach-O 64-bit executable x86_64`, Rosetta 2 present |
| Guest 1 | Notepad++ 8.9.8.1 portable, original `notepad++.exe` (x86-64), 8.5 MB |
| Guest 2 | 7-Zip 26.05 (x64), original `7zFM.exe`, installed from the vendor MSI by `msiexec` |
| Window | `winlist` (WindowServer): Notepad++ 1016×698 on screen, 7-Zip 1288×779 on screen |
| Input | macOS keystrokes delivered to the guest window; `Ctrl+S` saved; the document on disk changed from `original line` to `LAUNCHED-FROM-APPLICATIONS-OKoriginal line` |
| Win32 conformance | probe: 19 checks (menu, edit, button click → label change, `WM_CHAR` typing, clipboard copy/paste) |
| MSI | `msiexec` status 0, product registered, `7-Zip 26.05 (x64 edition).app` created in `~/Applications`, bundle launched `7zFM.exe` with a real window |
| Relaunch | the generated `.app` starts the guest with no Porte window; the manager was not running |
| Finder path | `.exe` and `.msi` are associated with `~/Applications/Porte.app`; `open file.exe` / `open file.msi` reach Porte, are analyzed (`exe-portable`, `msi`), and install through the same code path the GUI uses |

No screenshot is included: this session has no Screen Recording permission
(`screencapture` fails with "could not create image from display"), so window
evidence comes from the WindowServer's own window list, from the guest's Win32
message checks, and from files the guest wrote and saved.

## 5. Limits, stated as limits

- **A runtime must already exist.** Porte locates one and validates it; setup
  says so instead of pretending to install something it does not ship.
- **x86-64 through Rosetta.** This is real Windows code, but not native ARM64:
  speed is Rosetta-class, not native. Rosetta's support horizon is why the cache
  and profiler stay translator-agnostic.
- **MSI importer vs. runtime installer.** Porte's own MSI reader supports
  uncompressed and MSZIP cabinets only. A package whose payload needs LZX or
  Quantum (7-Zip's, for example) is now reported as such during analysis, and
  `auto` routes the install to `msiexec` in the runtime instead of failing
  halfway. The importer itself still refuses those packages.
- **The common file dialog is not driven by the probe.** Wine 11.20 implements
  `GetOpenFileName` with its own modal loop; driving it from another process can
  wedge the whole prefix (later processes stop starting). The probe therefore
  drives it only with `--dialog`, and the verification report records the check
  as skipped with that reason. File open/save was verified instead through the
  real Notepad++ window and real macOS keystrokes.
- **No D3D/game claim.** No game, no anti-cheat, no driver, no GPU path was
  tested. The frame benchmark remains the only graphics measurement.
- **Not sandboxed.** Each application has its own prefix and its own state
  directory; that is isolation of files, not a security boundary.
