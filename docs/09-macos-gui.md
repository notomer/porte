# Native macOS application

Status: implemented and verified on this host (`Apple M2 Max, macOS 27.2`) with
the engine's supported images and installer fixtures. The application is a real
SwiftUI/AppKit utility, not a mockup: its actions call the same engine modules as
the CLI, through a structured process protocol, and it generates the same real
`.app` bundles the CLI does.

Guiding boundary: the GUI can only do what a backend can do. Two exist — the
engine, and a Windows compatibility runtime that Porte discovers on the machine
(never one it downloads or ships). The app makes that surface usable from macOS
and says honestly which backend would run a file and what neither can do. It
does not add Windows APIs, and it does not turn an unsupported installer into a
working one. See [`docs/10-windows-runtime.md`](10-windows-runtime.md) for the
runtime decision and the end-to-end evidence.

---

## 1. Layout

```
macos/
  Package.swift                       Swift package (macOS 14+)
  Sources/PorteKit/                   non-UI logic: bridge client, setup store,
                                      runtime discovery, Launch Services, models
  Sources/PorteApp/                   SwiftUI app, AppKit delegate, views
  Sources/PorteLS/                    `porte-ls`: default-handler inspection/claim
  Sources/PorteCheck/                 deterministic checks (`porte-check`)
  Resources/Info.plist                bundle identity + document types
  build.sh                            assembles build/Porte.app
  verify.sh                           end-to-end verification
  verify_generated_app.py             install → .app → launch → uninstall check

porte/gui/
  bridge.py                           newline-delimited JSON protocol (protocol 1)
  runner.py                           out-of-process portable-program run
```

The app bundle is written to `build/Porte.app` and is not committed. It embeds a
`porte-runtime.json` record pointing at the runtime it was built against.

## 2. Process architecture

```
Porte.app (SwiftUI + AppKit)
   │  JSON lines over stdin/stdout        porte/gui/bridge.py (Python)
   ├────────────────────────────────────▶  analyze · install · run · library
   │  state/progress/result/error events   · launch · uninstall · repair
   │                                       · diagnostics · clear caches
   ▼
engine modules used by the CLI as well (porte/app, porte/app/msi, porte/msix, porte/pe)

generated application bundle (unchanged): AppKit launcher → porte.app.runtime
```

Why a second front end instead of scraping `porte` output: the CLI's text is for
people; it changes and it is not typed. Protocol 1 has one JSON object per line,
request ids, typed errors and explicit states. Engine prints are redirected to
stderr, and the Swift client treats a non-JSON line as a protocol violation, so
the UI cannot silently misread a human message as data.

### Requests

```json
{"id":"r1","op":"analyze","args":{"path":"/Users/me/Setup.exe"}}
```

Ops: `hello`, `analyze`, `install`, `run`, `library`, `launch`, `uninstall`,
`repair`, `diagnostics`, `clear_caches`, `cancel`, `shutdown`.

### Responses

```json
{"id":"r1","event":"state","state":"installing","message":"Installing…"}
{"id":"r1","event":"result","data":{…}}
{"id":"r1","event":"error","code":"unsupported","message":"…"}
```

States: `analyzing`, `preparing`, `installing`, `launching`, `running`,
`completed`, `failed`, `unsupported`. The Swift `WorkflowState` maps these
one-to-one; `bundling` is folded into `installing`.

Every execution or installation requires `consent: true` in the request. The UI
is responsible for asking; the bridge refuses otherwise, so no code path can run
an untrusted binary without a user decision.

### Cancellation

`cancel` is read on a separate thread and stops the current operation where it
checks. A portable program run happens in its own child process
(`porte/gui/runner.py`), so cancelling terminates one process rather than
corrupting the manager. Installing is not interrupted mid-write; the sheet
disables Cancel while the engine is applying files.

## 3. First-run setup

Opened as the root view until setup is complete:

1. **Compatibility Runtime.** Validates the runtime the app was built against,
   or one the user selects: interpreter executable, package root, engine modules,
   `clang`, `iconutil`/`sips`, and a writable support directory. If something is
   missing, the primary action is **Choose Runtime…**, which probes the choice by
   asking that interpreter to import `porte` and describe its own paths. There is
   no download and no administrator prompt, and the screen says so. Porte does
   not yet ship a signed runtime distribution; pretending to install one would be
   the fake progress this project avoids.
2. **File Associations (optional).** Calls the documented
   `NSWorkspace.setDefaultApplication(at:toOpen:)` for the two content types and
   reads the result back. If macOS declines, the app explains the Finder
   Get Info → Open with → Change All route. It never edits the Launch Services
   database directly.
3. **Continue.** Persists `setup.json` and shows the library.

State and logs:

```
~/Library/Application Support/Porte/GUI/
  setup.json        completed timestamp, validated runtime, association outcome
  runtime.json      a runtime the user selected (optional)
  events.jsonl      diagnostics: launch, open_received, analysis, install, errors
```

On later launches the app validates the runtime and skips setup unless the
recorded runtime is missing or broken. A missing runtime is reported in
`events.jsonl` as `runtime_error` and returns the user to setup; it never presents
a library that cannot work.

## 4. Finder and Launch Services

`Info.plist` declares:

| format | content type | declaration | handler rank |
| --- | --- | --- | --- |
| `.exe` | `com.microsoft.windows-executable` | system-declared; Porte references it | Alternate |
| `.msi` | `com.notomer.porte.windows-msi` | `UTImportedTypeDeclarations` (Microsoft defines the format; Porte recognises it) | Alternate |

`LSHandlerRank` is Alternate deliberately: installing Porte does not seize the
format from another handler. macOS resolves documents to Porte when it is the
chosen handler, which can be the default because it is the only claimant, because
the user picked it in setup, or because they used Open With.

Delivery paths all end in `NSApplicationDelegate.application(_:open:)`:

- double-click in Finder (default handler),
- right-click → Open With → Porte,
- drag onto the Dock icon,
- drag into the library window (SwiftUI `dropDestination`),
- File ▸ Open… (the app's Open panel).

The delegate hands URLs to `AppModel`, which:

- filters to `.exe`, `.msi`, `.msix`, `.appx` and explains anything else,
- suppresses a duplicate delivery of the same path within two seconds
  (`FileOpenQueue`; verified by `porte-check`),
- queues files that arrive before setup/window restoration is finished and
  processes them afterwards,
- queues a second file opened while another workflow sheet is open, so each
  delivered file gets its own analysis instead of replacing one.

`LSMultipleInstancesProhibited` keeps one manager instance. The generated
applications are separate bundles with their own identities and are unaffected.

## 5. Workflow and library

Opening a file shows a sheet with the engine's analysis: kind, compatibility,
size, SHA-256, blockers, package facts, and a technical disclosure. Actions
appear only when the engine reports they are possible:

- **Install as App** — portable `.exe`, `.msi`, or `.msix`; writes a real bundle
  into the configured destination (default `~/Applications`).
- **Run** — portable console images only; executes in the child process and shows
  real output, exit status and fault. Images that need a window are installed
  instead, because the engine cannot host their UI in the manager.
- **Replace Existing** — after `already_installed`, which removes the old bundle
  and environment.
- Unsupported files show the reason and a Finder reveal, never an install button.

The library is a native grid from `porte app` state: icon, name, readiness, and a
context menu with Open, Show in Finder, Details, Repair (when the app belongs to
an installed product) and Uninstall. Search appears once there are at least six
applications. Launching uses `NSWorkspace`; the generated app then runs with no
Porte manager involved.

## 6. Settings

A standard Settings scene (`⌘,`) with four small tabs:

- **General** — installation destination; the effective default handler per type
  with a Make Default action; re-run setup.
- **Compatibility** — runtime readiness (engine, clang, icon toolchain, storage),
  version, interpreter; profile policy is described, not exposed as a fake
  toggle. Advanced shows the package root, support folder and diagnostics.
- **Performance** — real cache size and a cache clear that deletes only
  regenerable per-application profiles; renderer description without switches the
  engine cannot honour; optional diagnostics with copy.
- **About** — version, repository, licenses, runtime path, non-affiliation notice.

There is no startup-at-login, update channel, or graphics preference because none
of those exist in the engine yet.

## 7. Security and isolation

- Imported binaries are untrusted. Installation and execution require explicit
  consent twice over: the UI names the file and destination, and the bridge
  refuses any install/run without `consent: true`.
- The generated bundle is ad-hoc signed; Gatekeeper still rejects it until the
  user allows it. Porte does not bypass Gatekeeper, does not disable SIP, and
  does not inject code into other processes.
- Each Windows product keeps its files, virtual registry and shortcuts in its own
  `Application Support/Porte/Installed/<product>` tree; generated apps keep
  configuration, consent, cache and run logs under
  `Application Support/Porte/Applications/<bundle id>`.
- This is **not** a sandbox. The engine runs as a normal user process and the
  guest can reach files the user can reach through the APIs the engine
  implements. No sandbox entitlement or VM boundary is claimed.

## 8. Verified behavior

`./macos/verify.sh` runs all of this against the real app; `--no-gui` stops after
the deterministic checks. Results on this host:

| claim | evidence |
| --- | --- |
| App builds as a valid arm64 bundle | `plutil -lint`, `codesign --verify --strict`, `lipo -archs` = arm64 |
| Launch Services knows the app and both document types | `porte-ls find com.notomer.porte`, `CFBundleDocumentTypes` assertions |
| Launches, validates the runtime, stops at setup when no setup state exists | `events.jsonl`: `launch`, `runtime_ready`, no analysis |
| A running app receives an EXE from Launch Services and analyzes it | `open -a Porte.app <exe>` → `open_received`, `analysis_result` kind `exe-portable`; one instance |
| A stopped app launches from an MSI document request | `open -a Porte.app <msi>` → cold `launch`, `open_received`, `analysis_result` kind `msi` |
| The double-click path reaches Porte when it is the default handler | `porte-ls status exe` reports `is_porte`, then `open <exe>` → `open_received` |
| Missing runtime returns to setup and is reported | copied bundle without runtime → `runtime_error`, no `runtime_ready` |
| Install produces a real `.app` in Applications | bundle exists, `Info.plist` valid, launcher arm64, metadata matches |
| The generated app runs without Porte open | `open` it, wait for the bundle's own `run-*.json`, exit 0, no manager process |
| Uninstall removes product and application | generated bundle gone |
| Duplicate open suppression | `porte-check` `FileOpenQueue` check |
| Protocol purity (no CLI text parsed) | bridge tests assert stdout is JSON-only |

Swift logic checks (`porte-check`) cover JSON decoding, bridge events, the open
queue, workflow state mapping, typed analysis/library/diagnostics payloads,
runtime validation, setup persistence, the Info.plist contract, and a live bridge
handshake plus `hello`/`library` round trip. Python tests cover the protocol,
consent enforcement, a real MSI install through the bridge, library reporting and
uninstall; the existing 15 tests still pass.

What was **not** automated, and therefore not claimed as machine-verified:

- a literal physical double-click with the mouse, and dragging onto the Dock icon
  or window. These use the same Launch Services and `dropDestination` paths that
  were exercised, but the gesture itself needs GUI automation or a person;
- light/dark appearance and VoiceOver. The UI uses only system colors, materials,
  standard controls and SF Symbols, and every icon-only control has an
  accessibility label; that is the design, not a screenshot claim;
- clicking through setup and the install button in the UI. The state machine and
  bridge actions they invoke are tested, and the setup gate is verified through
  the running app, but the click itself is manual.

Manual pass (about two minutes):

1. Remove `~/Library/Application Support/Porte/GUI/setup.json`.
2. `open build/Porte.app`; confirm Welcome, runtime ready, Continue.
3. In Finder, choose an `.exe` and Get Info → Open with → Porte → Change All, or
   press Make Porte the Default in setup.
4. Double-click the file; confirm the sheet names it, shows compatibility and
   offers Install as App (or the unsupported explanation).
5. Install; open `~/Applications/<Name>.app` from Finder; confirm the Windows
   program window appears with no Porte manager.
6. Drag another `.exe` onto the Porte Dock icon, then into the window; both should
   show the same analysis sheet.

## 9. Boundaries and future work

- **No runtime bootstrap.** Setup locates and validates a runtime; it cannot
  download, sign or install one, because Porte has no runtime distribution yet.
- **No general Windows compatibility.** No user32/gdi32 window bridge, no D3D, no
  threads/TLS, no `msiexec` script engine, no services or drivers, no Authenticode
  verification. MSIs with custom actions or unsupported features are refused by
  the importer, and the UI reports that refusal.
- **Installers are packages, not programs.** An installer-shaped `.exe` is
  analyzed and reported as unsupported instead of being executed; `.msi`,
  `.msix` and `.appx` are applied by the importer that now backs the GUI.
- **Not notarized.** Ad-hoc signing only; first launch may require the user to
  allow the app in System Settings ▸ Privacy & Security.
- **No privileged installation.** `/Applications` requires the user's approval;
  `~/Applications` is the default and needs none.
- **GUI test depth.** There is no XCUITest suite; UI behavior is verified through
  the app's own diagnostics log and the shared code paths. Adding XCUITest is the
  obvious next step for gesture-level automation.
