# macOS application integration: generated app bundles

Status: proof of concept, verified on this host. It packages a Windows executable
that the current Porte engine can actually run — and says so when it cannot. It
is not a claim of general Windows compatibility, and it is not a notarized
distribution mechanism.

What works today: an `.exe` is installed as a real `.app` in `~/Applications`,
launched from Finder/Spotlight/`open` with its own Dock identity and window, runs
through the Porte engine without any visible Porte manager, keeps its
configuration and run history across launches, and dies cleanly when quit. Two
bundles are installed on this machine as the demonstration:
`Porte Frame Demo` (Metal-backed frame workload) and `Porte CRC Demo` (console
program).

---

## 1. Two layers, one runtime

The product is split so the macOS surface and the execution engine can evolve
independently.

```
Porte Frame Demo.app
├── Contents/MacOS/Porte Frame Demo        ARM64 launcher (AppKit, compiled per bundle)
├── Contents/Info.plist                    bundle identity
├── Contents/Resources/PorteApp.json       PE hash, profile, arguments, runtime pointer
├── Contents/Resources/AppIcon.icns        extracted from the PE or generated
└── Contents/Resources/windows/*.exe       the Windows image (and sibling DLLs)

            launcher (AppKit)  ──spawn──▶  shared Porte runtime (Python)
            identity, window,              PE loading, Win32 stubs, translation,
            consent, supervision           graphics, profiling, cache

            mutable state: ~/Library/Application Support/Porte/Applications/<id>/
              config.json  consent.json  cache/profile.json  logs/run-*.json  state.json
```

The launcher is a real Mach-O executable inside `Contents/MacOS`, which is what
gives the application its own Launch Services identity. The Windows program is
never executed inside the launcher process and never appears as `python` or as
Porte in the Dock. The runtime is spawned as a child process and is not
registered as an application (verified: while the frame workload ran,
`lsappinfo list` showed only the generated app).

The bundle is immutable at runtime: after several launches,
`find … -exec shasum -a 256` over every file inside the bundle was unchanged and
`codesign --verify --deep --strict` still passed. All writes go to the per-app
state directory.

### 1.1 Metadata

`Contents/Resources/PorteApp.json` (schema 1) records:

- `bundle_id`, `name`, `version`, `profile` (`console` or `metal-frame`)
- `pe` (relative path inside `Resources/`), `pe_sha256`, `pe_size`,
  `pe_display_path` (the original path, for the consent dialog)
- `arguments` — the argv handed to the image
- `icon` — whether the artwork came from the PE or was generated
- `runtime` — absolute `python`, `pythonpath`, `root`, `module`
- `created_at`, `bundled_files`

The runtime refuses to start if the image's SHA-256 does not match this record.

## 2. Bundle generation

`porte/app/bundle.py` builds the layout, writes `Info.plist` and the metadata,
copies the image and its sibling DLLs, compiles `launcher.m` with clang
(`-arch arm64 -mmacosx-version-min=13.0 -framework Cocoa`), builds the icon,
ad-hoc signs the bundle, and registers it with Launch Services.

- **Identity**: `com.notomer.porte.windows.<name-slug>-<first 8 hex of PE sha256>`.
  Reinstalling the same image keeps the identifier (and thus the Dock position
  and Launch Services record); replacing the image produces a new identifier.
  That is the honest trade-off until a version/update registry exists.
- **Icon**: the PE's `RT_GROUP_ICON`/`RT_ICON` is decoded (PNG-compressed icons
  directly; 32/24-bit DIB icons in Python) and the largest frame is expanded into
  a full `.iconset` and compiled with `iconutil`. Images without an icon get a
  generated identity icon rather than the generic application icon.
- **Signing**: ad-hoc (`codesign --sign -`). Gatekeeper still rejects the bundle
  (`spctl -a -t exec` → `rejected`) because it is not notarized. Porte does not
  bypass that; a real distribution would need a Developer ID and notarization.
- **Runtime discovery**: the bundle records the shared runtime it was built
  against. If that runtime is missing at launch, the launcher shows a recovery
  alert naming the expected path instead of failing silently.

## 3. Installation policy

`porte/app/install.py` refuses what the engine cannot run, before a bundle exists.

- Static inspection lists imports and compares them with the host stubs the
  engine actually implements (15 entry points: command line, std handles,
  `WriteFile`, `ExitProcess`, timing, `VirtualAlloc`/`VirtualFree`,
  `FlushFileBuffers`, `memset`/`memcpy`/`memmove`, `PresentFrame`).
- Any import without a stub → `NotImplementedError` naming the missing
  functions. Images whose imports look like installers/user32/D3D are called out
  as such; **no Windows installer is executed as a program**. An installer that
  is a package (`.msi`, `.msix`, `.appx`) is instead read by Porte and applied
  through the importer in [`07-package-importer.md`](07-package-importer.md).
- Ordinal imports are refused.
- Sibling `.dll` files are copied into the bundle. There is no deeper dependency
  discovery yet; this is documented, not assumed.
- Consent is required before packaging: an interactive confirmation, or
  `--accept` after reviewing the printed imports. The acceptance (image hash,
  timestamp, method) is written to `consent.json`; the launcher also has its own
  first-run consent dialog for bundles whose state directory does not exist yet
  (for example after copying the `.app` to another Mac).

## 4. Lifecycle

The launcher is a normal AppKit application:

- Regular activation policy, its own window and menu (About, Hide, Quit ⌘Q,
  Edit/Copy), Dock icon, Spotlight metadata.
- Window autosave, activation on launch, termination when the window closes.
- The runtime's stdout/stderr is streamed into the window, so console programs
  show their output and the frame workload shows its measured frame time.
- On quit, the launcher sends `SIGTERM` to the runtime, waits up to 2 s, then
  `SIGKILL`s it. Verified with a 200,000-frame run: quit left no runtime process.
- When the guest exits, the window stays with the exit status; for a game this
  is the natural "program ended" moment, and ⌘Q closes the application.

No Porte control window, tray icon, or manager process is involved at any point.

## 5. Verified behaviours

All of the following were executed on this host (`Apple M2 Max, macOS 27.2`),
against the installed bundles.

| # | Claim | Evidence |
| --- | --- | --- |
| 1 | Valid bundle, appears as an application | `plutil -lint Info.plist` → OK; `mdls` → `kMDItemContentType = com.apple.application-bundle`, `kMDItemKind = Application`; `lsregister -dump` → `platform: native`, `slices: arm64`, identifier `com.notomer.porte.windows.porte-frame-demo-e4f59ec6`; `mdfind` by bundle id returns the bundle |
| 2 | Launches by double-click / open, from `~/Applications` and `/Applications` | `open -a "Porte Frame Demo"` starts the bundle; process is `…/Contents/MacOS/Porte Frame Demo`. A copy of the CRC bundle in `/Applications` launched by path (`open "/Applications/Porte CRC Demo.app"`), reported pid 5316 with the same bundle id, and quit cleanly. Launch Services shows the app in front |
| 3 | Independent Dock identity | `lsappinfo info -only bundleID,name -pid <launcher>` → `"Porte Frame Demo"`, bundle id as above; the runtime child is not a registered application |
| 4 | No visible Porte manager | Only the launcher process and its runtime child exist; no Porte process, no Porte window. The launcher window is the application's own (640×392, layer 0, verified with `CGWindowListCopyWindowInfo`) |
| 5 | Terminates and relaunches | `osascript -e 'tell application id "…" to quit'` → no launcher or runtime processes; relaunch works and increments the run counter (1 → 5 across reinstall + launches) |
| 6 | Configuration and cache persist | `config.json`, `consent.json`, `cache/profile.json`, `logs/run-*.json`, `state.json` under `~/Library/Application Support/Porte/Applications/<id>/`; second launch logs `profile cache: hit` and reuses the recorded imports |
| 7 | Real Windows execution | `Porte CRC Demo` prints `262144 00000000a382316d` (24 bytes, sha256 `b87c41322d4e9d17…`) with API calls `GetCommandLineA:1, GetStdHandle:1, WriteFile:1`, exit 0. `Porte Frame Demo` executes 300 `PresentFrame` calls, 299 frames, 1.380 ms median frame, 0.084 ms GPU, exit 0 |

Bundle contents were unchanged by these runs; `codesign --verify --deep --strict`
passes after them, and `spctl` still rejects the bundle as unnotarized (expected,
documented above).

## 6. Start-up and steady-state overhead

The launcher measures three phases separately, which is what the performance
objective requires: launcher start-up, interpreter start, and guest execution.

Latest recorded run of `Porte Frame Demo` (from `logs/run-*.json`; phase
timings vary by roughly ±10 ms between launches):

| phase | ms |
| --- | ---: |
| launcher (AppKit, window, menu, consent check) | 122.0 |
| launcher → runtime (process start, interpreter import) | 55.8 |
| image verification + profile cache lookup | 0.6 |
| guest execution (300 PresentFrame calls) | 424.0 |

- The launcher contributes ~122 ms once per launch; it does no work per frame.
- Steady state is unchanged by packaging: 1.380 ms median frame inside the app
  versus 1.44 ms for the same image in the benchmark harness, and GPU time
  matches (0.084 ms).
- `Porte CRC Demo`: launcher 139.7 ms, interpreter 53.6 ms, guest 6.1 ms,
  24 bytes of output, exit 0.

The interpreter start (~55 ms) is Python importing the engine. It is part of the
"compatibility initialisation" budget, not the launcher's, and is recorded
separately so future work can target it without guessing.

## 7. What is not implemented

These are product goals from the brief that this proof of concept does **not**
deliver, in the order they matter:

- **Native window integration (Milestone C).** The launcher owns a log window;
  a Windows program cannot create its own window yet. There is no Win32→AppKit
  window bridge, no menus/context menus/clipboard/notifications/file dialogs, no
  accessibility metadata. The launcher window is a supervisor console, not a
  transformed Windows UI.
- **Seamless importing (Milestone E).** Installation is `porte app install` or
  `porte app import <package>`. There is no Porte.app document handler, no
  Open With/drag-and-drop association for `.msi`/`.msix`, no watched import
  folder, and no Finder Quick Action. The `.app` itself has no document types.
- **Games (Milestone F).** No D3D backend, no shader translation, no controller
  or fullscreen handling; the Metal workload is an offscreen frame benchmark.
- **General Windows compatibility.** No threads/TLS, no user32/gdi32, no file
  dialogs. The virtual registry exists for installed products but guest code
  cannot query it through `advapi32`. Only images whose imports are all in the
  15 host stubs can be bundled.
- **Distribution.** Ad-hoc signature only: Gatekeeper rejects the bundle until
  the user explicitly allows it. No notarization, no hardened runtime, no
  sandbox, no update mechanism.
- **Portable export.** Bundles reference the shared runtime by absolute path and
  are not self-contained; there is no export that bundles the runtime.
- **Multi-instance policy.** `open -n` can start more than one instance; the
  default Finder double-click activates the existing one.

## 8. Reproduce

```bash
# build the Windows images the engine can run
./fixtures/frame/build.sh
.venv/bin/python fixtures/opaque/gen.py --clean

# package a Metal-backed image and a console image
.venv/bin/python -m porte.cli app install build/frame/frame_win.exe \
    --name "Porte Frame Demo" --args 300 8192 1 --accept
.venv/bin/python -m porte.cli app install fixtures/opaque/out/crc.exe \
    --name "Porte CRC Demo" --args 262144 --accept

open -a "Porte Frame Demo"
.venv/bin/python -m porte.cli app list
.venv/bin/python -m porte.cli app inspect "Porte Frame Demo"
.venv/bin/python -m porte.cli app remove "Porte CRC Demo" --purge
```

Run records: `~/Library/Application Support/Porte/Applications/<bundle id>/logs/`.
