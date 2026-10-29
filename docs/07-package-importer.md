# The package importer: .exe, .msi, .msix/.appx

Requirement this document reports against:

> Porte must recognize `.exe`, `.msi`, `.msix` and `.appx` packages, determine
> whether each is portable or requires installation, execute supported
> installation workflows, discover installed applications, and generate
> independent macOS `.app` bundles automatically. Users should subsequently
> launch those applications from `/Applications`, Spotlight or the Dock without
> interacting with Porte.

That sentence is now the definition of the importer's job. This document states
what of it is implemented, what is measured, and what is refused and why.

## 1. What each format does

| format | Porte behaviour | state |
| --- | --- | --- |
| `.exe` | static import analysis; if every import is served by a host stub, bundle it and run it. If it looks like an installer (user32/ole32/msi.dll imports), refuse with the missing names | implemented before this phase; unchanged |
| `.msi` | parse the database, extract embedded or external cabinets, install file/registry/shortcut tables into an isolated environment, generate bundles for the executables the engine can run | **implemented in this phase** |
| `.msix`, `.appx` | validate the ZIP container against `AppxBlockMap.xml`, read `AppxManifest.xml`, extract into an environment, generate bundles for declared applications the engine can run | **implemented in this phase** |
| `.bat`, `.cmd` | refused: no `cmd.exe` interpreter | not implemented (was optional) |

`porte app import` is the single entry point; the suffix selects the path.

## 2. MSI: what is actually executed

Windows Installer is a database plus a script. Porte implements the database
half natively and refuses the parts of the script half it cannot honour.

The brief suggested routing actions through a Windows Installer–compatible
engine such as Wine's `msiexec`. That was not used here: this host has no Wine
(`which wine` is empty), and handing the package to `msiexec` would execute
vendor custom actions as opaque Windows code inside a second compatibility
layer, which is exactly what this project's evidence model forbids. Reading the
tables natively keeps every write attributable and testable. A Wine-backed
executor remains the path for packages whose custom actions are load-bearing;
it is listed as future work, not claimed.

### 2.1 Reading the package

- `porte/app/msi/cfb.py` — Compound File Binary reader: header, DIFAT/FAT,
  directory tree, mini-stream for small streams, cycle and bounds checks.
- `porte/app/msi/database.py` — table streams (`_Columns`, `_Tables`,
  `_StringPool`, `_StringData`), MSI's mixed string encodings (short/long name
  split, code-page and 0x8000-flagged MIME encodings), integer and stream
  columns, `_SummaryInformation`.
- `porte/app/msi/cab.py` — cabinets: uncompressed folders and MSZIP (2-byte
  `CK` signature per block plus a 32 KiB sliding dictionary). LZX, Quantum and
  any unknown method are refused with the compression name, not decoded
  incorrectly.

The fixtures are authored with `wixl`/`msibuild` from msitools, so the reader is
checked against an independent implementation rather than against its own
writer.

### 2.2 Planning

`load_plan` turns tables into a decision-free plan: product identity
(`ProductName`, `ProductVersion`, `Manufacturer`, `ProductCode`, `UpgradeCode`),
the resolved `Directory` tree, the `File` list with sequence and component, the
`Registry` and `Shortcut` rows. Standard folder properties map into the
environment's Windows layout:

| MSI property | environment path |
| --- | --- |
| `TARGETDIR` | `/` |
| `ProgramFilesFolder`, `ProgramFiles64Folder` | `Program Files` |
| `LocalAppDataFolder` | `AppData/Local` |
| `AppDataFolder` | `AppData/Roaming` |
| `WindowsFolder`, `SystemFolder` | `Windows`, `Windows/System32` |
| `ProgramMenuFolder` | `ProgramData/Microsoft/Windows/Start Menu/Programs` |

A component without a directory, or a directory outside the mapped tree, is
resolved as far as the tables allow; `safe_join` rejects `..`, absolute parts and
control characters, so a hostile package cannot write outside its environment.

### 2.3 Refusals, before writing anything

| condition | reason given |
| --- | --- |
| custom action of type 1/2/17/18/37/38/50/53/54/63/64 | "N custom action(s) execute code Porte cannot run: …" |
| `ServiceInstall` / `ServiceControl` rows | "package installs Windows services, which have no supported equivalent in this environment" |
| no `File` and no `Registry` rows | package installs nothing |
| LZX/Quantum/unknown cabinet compression | named compression method |

`--allow-custom-actions` downgrades the custom-action refusal to a warning and
proceeds with the table install; the action itself is still **not executed**, and
the manifest records that the install is partial.

### 2.4 Installing

Files are extracted from the cabinets in `Media` order, written under
`~/Library/Application Support/Porte/Installed/<product key>/windows/`, and hashed
(size and `FileSize` are checked as well). Then:

- `registry.json` — the virtual registry, hive → key → value with MSI's typed
  value markers decoded (`#n` integer, `#%` expand-string, `#x` binary).
- `shortcuts.json` — the `Shortcut` table verbatim (target, working directory,
  arguments, icon).
- `install.json` — the manifest: product key, package path and SHA-256, every
  installed file with its hash, the generated bundles, launchable executables,
  skipped executables with the imports that stopped them, upgrade history.

The environment is per product: two packages that install the same program into
the same directory still get separate trees, so an uninstall or repair of one
cannot damage the other.

### 2.5 Discovering applications and generating bundles

Every extracted `.exe` is inspected with the same static import check the
portable path uses. Each one the engine can serve becomes an ordinary bundle via
the machinery in `docs/06-app-integration.md` (ARM64 launcher, `Info.plist`,
icon, ad-hoc signature, Launch Services registration). Each one it cannot serve
is listed with the missing stubs. Nothing is bundled optimistically: a bundle
that would fail at first launch is never produced.

Bundle ids include the product identity, so two packages carrying the same
executable get separate application state.

### 2.6 Upgrade, repair, uninstall

- **Upgrade** — a package whose `UpgradeCode` matches an installed product and
  whose version is not lower replaces it: new environment first, then the old
  bundles and old environment are removed, and `upgraded_from` is recorded in the
  manifest. A lower version is refused unless `--force`.
- **Repair** — verifies every installed file against its recorded SHA-256 and,
  when the original package is still present and unchanged, re-extracts and
  re-installs it (`porte app repair <product>`). Bundles are not regenerated.
- **Uninstall** — removes the generated `.app` bundles (and their state) plus the
  environment (`porte app uninstall <product>`), leaving no product in
  `porte app products`.

## 3. MSIX / APPX

`porte/app/msix.py`:

- ZIP limits: 20,000 entries, 2 GiB uncompressed, no absolute paths, no `..`.
- `AppxManifest.xml` parsed with `ElementTree` (identity, applications,
  capabilities); a missing or unparsable manifest, a wrong root element or a
  missing declared executable is a refusal.
- `AppxBlockMap.xml` validation: per-file size and per-64 KiB SHA-256, so a
  truncated or tampered container is rejected before extraction. If a package
  has no block map, that is reported rather than assumed valid.
- `AppxSignature.p7x` is detected but **Authenticode is not verified** — the
  report says "signature unverified", and the install prompt says so too.
- ARM64-only packages are refused (the engine executes x86); `x64`, `x86` and
  `neutral` are accepted.
- Extraction preserves the package layout inside the environment, then the same
  discovery and bundle generation as MSI.

No WinRT activation, package identity, VFS or capability enforcement exists.
Packages whose entry point needs `Windows.*` APIs are extracted and reported as
skipped, with the missing stubs, exactly like an unsupported MSI executable.

## 4. Measured on this host (Apple M2 Max, macOS 27.2)

`fixtures/installer/verify.py`, 2026-10-29T03:45Z. Every line below is an
assertion in that script; it exits non-zero on the first failure.

| check | result |
| --- | --- |
| MSI database + embedded cabinet (3 files, 1 registry value, 1 shortcut) | pass |
| extracted payload SHA-256 == `tree/PorteFixture.exe` | pass |
| uncompressed payload bytes identical | pass |
| registry table applied (`HKCU\Software\PorteFixture\Installed` = integer 1) | pass |
| GUI executable discovered, reported, not bundled (`MessageBoxA` missing) | pass |
| one bundle generated for the console executable | pass |
| bundle runs headless through the real runtime, exit 0 | pass |
| guest output hash matches the expected 61-byte message | pass |
| upgrade 1.0.0 → 1.1.0 recorded, old environment removed, app relaunches | pass |
| custom-action package refused, with `FixturePost` named | pass |
| MSIX block map validated (3 files) | pass |
| unsigned MSIX reported, not silently trusted | pass |
| tampered MSIX refused ("block 0 hash mismatch") | pass |
| MSIX install + launch | pass |
| same executable in two packages gets distinct bundle ids | pass |
| uninstall removes products, apps and app state | pass |

Interactive path, same day: `app import PorteFixture.msi --accept` put
`Porte Installer Fixture.app` in `~/Applications`; `open -a` launched it; the
runtime log recorded `exit_code 0`, output
`PorteFixture: extracted by Porte, running as a Windows image`,
`api_calls {GetStdHandle: 1, WriteFile: 1}`, guest time ~4.7 ms. The `lsappinfo`
identity is the bundle's own (`com.notomer.porte.windows.porte-installer-fixture-…`), not
`python` and not Porte.

Startup cost is the same as any other generated bundle (launcher ≈ 3.2–3.9 s
under load on this host, interpreter handoff 64–97 ms, guest 4–32 ms); the
importer adds one `install` step, not a per-launch cost.

## 5. What is still missing

- **No `msiexec` script engine.** Install/upgrade/uninstall *sequences*,
  conditions, feature selection, `LaunchCondition`, `RemoveFile`, `IniFile`,
  `Environment`, `MsiFileHash`, `Binary`/`Icon` tables, advertised shortcuts and
  `CustomAction` code are not executed. Those are reported as warnings or
  refusals, never approximated silently.
- **No Windows Installer service or per-machine installs.** Everything is
  per-user inside a Porte environment; `ALLUSERS` packages are not elevated.
- **No Win32 window/UI bridge** — a bundled MSI application is still a console
  process unless the engine learns user32/GDI.
- **No Authenticode** verification for MSI (`MsiDigitalSignature`) or MSIX.
- **No custom-action executor.** Packages whose usefulness depends on their
  install script (a bootstrapper that unpacks a second package, a licence check,
  a database migration) cannot be installed by Porte even with
  `--allow-custom-actions`; they install their tables and stop there.
- **No DLL registration.** `DllRegisterServer`/`regsvr32` self-registration is a
  custom action and is refused with it; the `Registry` table is written to the
  virtual registry, but the engine has no `advapi32`, so a guest cannot read it.
  COM dependencies (e.g. a redistributable installed by a separate MSI) are not
  resolved automatically; each package is installed on its own.
- **No file associations or double-click handling for packages.** Installing an
  MSI still starts from `porte app import <package>`; `.msi` is not registered as
  a document type of a Porte application. "Double-click the MSI" therefore means
  "import it", not "Finder runs Porte".
- **No `.bat`/`.cmd`**, no driver installation, no services, no guest threads,
  no D3D title installation workflows.
- Repair re-extracts from the package; it does not re-run the install script,
  because there is no install script engine.
- Shortcuts are recorded, not materialised as macOS aliases: the generated `.app`
  is the launcher, and the `Shortcut` rows remain available in the manifest for a
  future Start-menu mirror.

## 6. Reproduce

```bash
./fixtures/installer/build.sh          # wixl + mingw + the MSIX generator
.venv/bin/python fixtures/installer/verify.py

.venv/bin/python -m porte.cli app package-info build/installer/PorteFixture.msi
.venv/bin/python -m porte.cli app import build/installer/PorteFixture.msi --accept
.venv/bin/python -m porte.cli app products
open -a "Porte Installer Fixture"
.venv/bin/python -m porte.cli app repair "Porte Installer Fixture"
.venv/bin/python -m porte.cli app uninstall "Porte Installer Fixture"

.venv/bin/python -m porte.cli app import build/installer/PorteFixture.msix --accept
.venv/bin/python -m porte.cli app import build/installer/PorteFixture-customaction.msi --accept
# -> refused: 1 custom action(s) execute code Porte cannot run: FixturePost
```

`verify.py` sets `PORTE_ENV_ROOT` and `PORTE_SUPPORT_ROOT` to a temporary tree,
so it does not touch the installed products or `~/Applications`.
