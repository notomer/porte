"""Wine runtime backend.

Architecture decision (rationale and sources in docs/10-windows-runtime.md): on
macOS 26/27 Apple silicon the only runtime that executes genuine Windows x86-64
GUI code today is an x86-64 Wine distribution running under Rosetta 2. Wine's
ARM64EC work targets Windows on ARM, FEX is Linux-only, and CrossOver's native
Apple silicon build is proprietary and cannot be redistributed. Porte therefore
*discovers* a Wine distribution that is already installed on the machine and
never bundles or downloads one.

What this backend does:

* discovery and versioning of Wine distributions, including a user-supplied
  override, with the license and Rosetta requirement reported honestly;
* static analysis of any Windows PE (machine type, subsystem, imports that a
  Wine distribution will not resolve on its own);
* execution of the guest program as a real child process inside a Wine prefix,
  with its real window, real keyboard input and real exit status;
* portable-EXE installation into a macOS application bundle whose launcher
  starts Wine, so the app relaunches from Finder with no Porte manager;
* MSI installation by driving the runtime's Windows Installer (`msiexec /i`),
  then discovering the installed programs from the prefix's registry and
  generating launchable bundles for them. Extraction is not installation, and
  this module never reports success unless msiexec succeeded.

Everything a Windows program writes stays inside its own prefix under
`~/Library/Application Support/Porte/Bottles/`.
"""
from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pefile

from .. import util
from .base import (WINE, BackendError, BackendStatus, BackendUnavailable,
                   host_arch, rosetta_available, run_command,
                   wine_distribution_root, which_wine)

NAME = "Wine"

MACHINE_NAMES = {0x8664: "x86-64", 0x14C: "x86", 0xAA64: "arm64", 0x1C4: "arm"}
SUBSYSTEM_NAMES = {1: "native", 2: "windows", 3: "console", 5: "efi"}

#: Windows API sets a Wine distribution provides itself. An import from any
#: other DLL has to sit next to the executable, or the loader will fail.
#
# The list is deliberately about *resolution*, not about behaviour: Wine may
# implement a given function incorrectly, but it will still load the image.
SYSTEM_DLLS = {
    "advapi32.dll", "bcrypt.dll", "cabinet.dll", "cfgmgr32.dll", "comctl32.dll",
    "comdlg32.dll", "crypt32.dll", "d3d11.dll", "d3d12.dll", "d3d9.dll",
    "dbghelp.dll", "dnsapi.dll", "dwmapi.dll", "dxgi.dll", "gdi32.dll",
    "gdiplus.dll", "iphlpapi.dll", "kernel32.dll", "kernelbase.dll",
    "mpr.dll", "mscoree.dll", "msi.dll", "msimg32.dll", "msvcrt.dll",
    "ncrypt.dll", "netapi32.dll", "ntdll.dll", "ole32.dll", "oleacc.dll",
    "oleaut32.dll", "opengl32.dll", "powrprof.dll", "propsys.dll",
    "psapi.dll", "rpcrt4.dll", "secur32.dll", "setupapi.dll", "shell32.dll",
    "shcore.dll", "shlwapi.dll", "user32.dll", "userenv.dll", "uxtheme.dll",
    "version.dll", "winhttp.dll", "wininet.dll", "winmm.dll", "wintrust.dll",
    "ws2_32.dll", "wtsapi32.dll", "ucrtbase.dll", "vcruntime140.dll",
    "vcruntime140_1.dll", "msvcp140.dll", "msacm32.dll", "avifil32.dll",
    "combase.dll", "windowscodecs.dll", "dwrite.dll", "dxva2.dll",
    "hid.dll", "imm32.dll", "mapi32.dll", "odbc32.dll", "ole2.dll",
    "winspool.drv", "api-ms-win-core-console-l1-1-0.dll",
}

DEFAULT_SEARCH = [
    "/Applications/Wine Stable.app/Contents/Resources/wine",
    "/Applications/Wine Staging.app/Contents/Resources/wine",
    "/Applications/Wine Devel.app/Contents/Resources/wine",
    "/Applications/Wine.app/Contents/Resources/wine",
    "/opt/wine-stable/bin/wine",
    "/opt/wine-staging/bin/wine",
    "/usr/local/bin/wine",
    "/opt/homebrew/bin/wine",
    "/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wine",
]

PREFIX_ENV = "PORTE_WINE_BIN"
ROOT_ENV = "PORTE_WINE_ROOT"
BOTTLES_ENV = "PORTE_BOTTLE_ROOT"


@dataclass
class WineRuntime:
    root: Path
    wine: Path
    version: str
    raw_version: str
    kind: str
    app: Path | None
    redistributable: bool

    @property
    def phosphor(self) -> str:  # pragma: no cover - compatibility shim
        return self.version

    def to_dict(self) -> dict:
        return {
            "root": str(self.root),
            "wine": str(self.wine),
            "version": self.version,
            "raw_version": self.raw_version,
            "kind": self.kind,
            "app": str(self.app) if self.app else None,
            "redistributable": self.redistributable,
        }


# ---- discovery ---------------------------------------------------------


def _app_bundle_for(path: Path) -> Path | None:
    for parent in Path(path).parents:
        if parent.suffix == ".app":
            return parent
    return None


def _probe(binary: Path, timeout: float = 20.0) -> str | None:
    proc = subprocess.run([str(binary), "--version"], capture_output=True,
                          text=True, timeout=timeout,
                          env=clean_env(None))
    text = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return text or None


def clean_env(prefix: Path | None) -> dict:
    """A predictable environment for Wine.

    Wine inherits whatever the caller had, and Python settings in particular
    confuse its own launchers. GUI pyc/import paths are dropped, debug channels
    are silenced, and the mono/gecko installers are disabled so no program can
    block on a modal download prompt that Porte never asked for.
    """
    keep = ("HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "SSH_AUTH_SOCK")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"
    env["WINEDEBUG"] = "-all"
    # Wine brings up MoltenVK for Vulkan; its startup banner is not useful in a
    # Porte log and would drown the guest's actual output.
    env["MVK_CONFIG_LOG_LEVEL"] = "0"
    env["WINEDLLOVERRIDES"] = "mscoree,mshtml="
    env["WINEARCH"] = "win64"
    if prefix is not None:
        env["WINEPREFIX"] = str(prefix)
    return env


def candidate_roots() -> list[Path]:
    roots: list[Path] = []
    override = os.environ.get(ROOT_ENV)
    if override:
        roots.append(Path(override).expanduser())
    binary = os.environ.get(PREFIX_ENV)
    if binary:
        roots.append(Path(binary).expanduser())
    managed = Path.home() / "Library" / "Application Support" / "Porte" / "Runtimes"
    if managed.exists():
        for entry in sorted(managed.iterdir()):
            if entry.is_dir():
                roots.append(entry)
                for nested in sorted(entry.glob("*.app")):
                    roots.append(nested)
    for text in DEFAULT_SEARCH:
        roots.append(Path(text))
    for base in (Path.home() / "Applications", Path("/Applications")):
        if base.exists():
            for app in sorted(base.glob("Wine*.app")):
                roots.append(app / "Contents" / "Resources" / "wine")
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def discover(probe: bool = True) -> list[WineRuntime]:
    """Every Wine distribution this machine already has, newest first."""
    found: list[WineRuntime] = []
    for candidate in candidate_roots():
        binary = which_wine(candidate)
        if binary is None:
            continue
        root = wine_distribution_root(binary)
        raw = _probe(binary) if probe else None
        version = "unknown"
        if raw:
            match = re.search(r"wine-([0-9][^\s]*)", raw)
            version = match.group(1) if match else raw.splitlines()[0][:40]
        kind = "crossover" if "CrossOver" in str(binary) else "wine"
        app = _app_bundle_for(binary)
        found.append(WineRuntime(root=root, wine=binary, version=version,
                                 raw_version=raw or "", kind=kind, app=app,
                                 redistributable=kind == "wine"))
    found.sort(key=lambda runtime: _version_key(runtime.version), reverse=True)
    return found


def _version_key(version: str) -> tuple:
    parts = re.findall(r"\d+", version)
    return tuple(int(p) for p in parts[:3]) or (0,)


def select(preferred: str | None = None) -> WineRuntime:
    runtimes = discover()
    if not runtimes:
        raise BackendUnavailable(
            "no Wine runtime found. Porte does not download or install one: "
            "install a Wine distribution (for example Wine Staging from the "
            "WineHQ macOS builds) and either put it in /Applications or point "
            f"{ROOT_ENV} at it, then re-run setup.")
    if preferred:
        for runtime in runtimes:
            if preferred in (str(runtime.wine), str(runtime.root),
                             runtime.version, runtime.kind):
                return runtime
        raise BackendUnavailable(f"no discovered runtime matches {preferred!r}")
    for runtime in runtimes:
        if runtime.redistributable:
            return runtime
    return runtimes[0]


# ---- bottles -----------------------------------------------------------

def bottles_root() -> Path:
    override = os.environ.get(BOTTLES_ENV)
    if override:
        return Path(override)
    from ..app import bundle

    return bundle.support_root() / "Bottles"


def bottle_dir(key: str) -> Path:
    return bottles_root() / key


def bottle_meta(key: str) -> dict | None:
    path = bottle_dir(key) / "bottle.json"
    if not path.exists():
        return None
    try:
        return util.read_json(path)
    except ValueError:
        return None


def ensure_bottle(runtime: WineRuntime, key: str,
                  timeout: float = 300.0) -> Path:
    """Create (or validate) a Wine prefix and record what created it."""
    prefix = bottle_dir(key)
    prefix.mkdir(parents=True, exist_ok=True)
    meta = bottle_meta(key)
    system_reg = prefix / "system.reg"
    if meta and system_reg.exists():
        if meta.get("wine_version") == runtime.version:
            return prefix
        meta["upgraded_from"] = meta.get("wine_version")
    started = time.perf_counter()
    result = run_captured([str(runtime.wine), "wineboot", "-u"],
                          env=clean_env(prefix), timeout=timeout)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    # wineboot returns once the session is up; the registry files are written by
    # the wineserver, so wait for them instead of assuming they are already
    # there. `wineserver -w` finishes the job if it is still flushing.
    deadline = time.time() + 60.0
    while not (prefix / "system.reg").exists() and time.time() < deadline:
        time.sleep(0.2)
    if not (prefix / "system.reg").exists():
        server = runtime.wine.parent / "wineserver"
        if server.exists():
            try:
                run_command([str(server), "-w"], env=clean_env(prefix),
                            timeout=60)
            except BackendError:
                pass
    if not (prefix / "system.reg").exists():
        raise BackendError(
            "wineboot did not produce a usable prefix: " +
            (result["stderr"].strip()[-800:] or
             result["stdout"].strip()[-800:] or "no output"))
    util.write_json(prefix / "bottle.json", {
        "key": key,
        "wine": str(runtime.wine),
        "wine_version": runtime.version,
        "wine_raw_version": runtime.raw_version,
        "runtime_kind": runtime.kind,
        "arch": "win64",
        "created_at": (meta or {}).get("created_at") or
                      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "boot_ms": elapsed_ms,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    return prefix


def run_captured(command: list[str], *, env: dict | None = None,
                 timeout: float = 900.0, cwd: Path | None = None) -> dict:
    """Run a Wine command with its output redirected to files.

    Wine keeps long-lived helpers (`wineserver`, `services.exe`, `winedevice`)
    that inherit the caller's file descriptors. Capturing through a pipe makes
    the call block until those helpers exit, which is never - so output goes to
    a file and the wait is only for the process we started.
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix="porte-run-") as tmp:
        out_path = Path(tmp) / "stdout"
        err_path = Path(tmp) / "stderr"
        try:
            with open(out_path, "wb") as out, open(err_path, "wb") as err:
                proc = subprocess.run(command, stdout=out, stderr=err,
                                      stdin=subprocess.DEVNULL, env=env,
                                      cwd=str(cwd) if cwd else None,
                                      timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise BackendError(
                f"{Path(command[1] if len(command) > 1 else command[0]).name} "
                f"did not finish within {timeout:.0f}s") from exc
        return {
            "returncode": proc.returncode,
            "stdout": out_path.read_text("utf-8", "replace"),
            "stderr": err_path.read_text("utf-8", "replace"),
        }


def terminate_prefix(runtime: WineRuntime, prefix: Path) -> None:
    """Stop every Wine process using a prefix, then leave the files alone."""
    server = runtime.wine.parent / "wineserver"
    if not server.exists():
        return
    try:
        run_command([str(server), "-k"], env=clean_env(prefix), timeout=60)
    except Exception:
        pass


def dos_to_unix(prefix: Path, windows_path: str) -> Path | None:
    """Map a Windows path inside a prefix to the file on disk."""
    text = windows_path.strip().strip('"')
    if not text:
        return None
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", text)
    if not match:
        return None
    drive, rest = match.group(1).lower(), match.group(2)
    parts = [p for p in re.split(r"[\\/]+", rest) if p]
    if drive == "z":
        return Path("/").joinpath(*parts)
    link = Path(prefix) / "dosdevices" / f"{drive}:"
    base = link.resolve() if link.exists() else Path(prefix) / f"drive_{drive}"
    return base.joinpath(*parts)


# ---- registry parsing --------------------------------------------------

def parse_registry(path: Path) -> dict[str, dict[str, str]]:
    """Read a Wine registry file.

    Wine stores the whole registry as text (`system.reg`, `user.reg`); reading
    it directly is far more reliable than parsing the output of `reg query`,
    and it needs no running wineserver.
    """
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    if not Path(path).exists():
        return sections
    header = re.compile(r"^\[(?P<key>.*)\](?:\s+\d+)?$")
    with open(path, encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.rstrip("\r\n")
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            match = header.match(line)
            if match:
                # Wine appends the key's last-write time after the section name
                # and escapes backslashes inside it.
                key = match.group("key").replace("\\\\", "\\")
                current = sections.setdefault(key, {})
                continue
            if current is None or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name = name.strip().strip('"')
            value = value.strip()
            if value.startswith("dword:"):
                value = str(int(value.split(":", 1)[1], 16))
            else:
                typed = re.match(r"^(?:str|hex)\((\d+)\):(.*)$", value)
                if typed:
                    value = typed.group(2)
                if value.startswith('"') and value.endswith('"') and len(value) >= 2:
                    value = value[1:-1]
            current[name] = value.replace("\\\\", "\\")
    return sections


UNINSTALL_ROOTS = (
    "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\",
    "Software\\Wow6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\",
)


def installed_programs(prefix: Path) -> list[dict]:
    """Products with an Uninstall entry and a display name."""
    registry: dict[str, dict[str, str]] = {}
    for filename in ("system.reg", "user.reg"):
        registry.update(parse_registry(Path(prefix) / filename))
    products: list[dict] = []
    seen: set[str] = set()
    for section, values in registry.items():
        if not any(section.startswith(root) for root in UNINSTALL_ROOTS):
            continue
        name = values.get("DisplayName")
        if not name:
            continue
        code = None
        for field in ("UninstallString", "QuietUninstallString"):
            match = re.search(r"\{[0-9A-Fa-f-]{36}\}", values.get(field) or "")
            if match:
                code = match.group(0).upper()
                break
        identity = code or section
        if identity in seen:
            continue
        seen.add(identity)
        products.append({
            "name": name,
            "version": values.get("DisplayVersion"),
            "publisher": values.get("Publisher"),
            "install_location": values.get("InstallLocation"),
            "display_icon": values.get("DisplayIcon"),
            "product_code": code,
            "registry_key": section,
            "estimated_size_kb": values.get("EstimatedSize"),
        })
    return products


# ---- PE analysis -------------------------------------------------------

def _import_dlls(path: Path) -> tuple[list[str], list[str], int, int]:
    pe = pefile.PE(str(path), fast_load=False)
    machine = pe.FILE_HEADER.Machine
    subsystem = pe.OPTIONAL_HEADER.Subsystem
    dlls: list[str] = []
    if hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
        for entry in pe.DIRECTORY_ENTRY_IMPORT:
            dlls.append(entry.dll.decode("ascii", "replace").lower())
    functions: list[str] = []
    if hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
        for entry in pe.DIRECTORY_ENTRY_IMPORT:
            for imp in entry.imports:
                if imp.name:
                    functions.append(imp.name.decode("ascii", "replace"))
    pe.close()
    return dlls, functions, machine, subsystem


_DLL_CACHE: dict[str, set[str]] = {}


def runtime_dlls(runtime: WineRuntime | None) -> set[str]:
    """Every DLL the runtime itself provides, read from its own library tree.

    A hand-written list of Windows DLLs is always out of date (Notepad++ imports
    `sensapi.dll`, which Wine has and most such lists forget), so the runtime is
    asked directly. The static list below is only a fallback for analysis
    without a runtime present.
    """
    if runtime is None:
        return set()
    key = str(runtime.root)
    if key in _DLL_CACHE:
        return _DLL_CACHE[key]
    names: set[str] = set()
    for directory in ("x86_64-windows", "i386-windows", "x86_64-unix"):
        base = Path(runtime.root) / "lib" / "wine" / directory
        if not base.exists():
            continue
        for entry in base.iterdir():
            name = entry.name.lower()
            if name.endswith(".dll"):
                names.add(name)
            elif name.endswith(".so"):
                names.add(name[:-3].replace("_", "-") + ".dll")
    _DLL_CACHE[key] = names
    return names


def has_wow64(runtime: WineRuntime | None) -> bool:
    if runtime is None:
        return False
    return (Path(runtime.root) / "lib" / "wine" / "i386-windows").exists()


def is_system_dll(name: str, runtime: WineRuntime | None = None) -> bool:
    """True when Windows (and therefore Wine) supplies the DLL itself.

    API sets are virtual names (`api-ms-win-crt-*`, `ext-ms-*`) that Wine
    resolves through its own redirection table, so they count as system DLLs
    even though no such file exists on disk.
    """
    lowered = name.lower()
    if lowered in SYSTEM_DLLS:
        return True
    if lowered in runtime_dlls(runtime):
        return True
    return (lowered.startswith("api-ms-win-")
            or lowered.startswith("ext-ms-")
            or lowered.startswith("api-ms-"))


def _sibling_dlls(path: Path) -> set[str]:
    try:
        return {p.name.lower() for p in Path(path).parent.iterdir()
                if p.is_file() and p.suffix.lower() == ".dll"}
    except OSError:
        return set()


def analyze_exe(path: Path, runtime: WineRuntime | None = None) -> dict:
    path = Path(path)
    dlls, functions, machine, subsystem = _import_dlls(path)
    siblings = _sibling_dlls(path)
    missing = sorted({dll for dll in dlls
                      if not is_system_dll(dll, runtime) and dll not in siblings})
    notes: list[str] = []
    blockers: list[str] = []
    warnings: list[str] = []

    if machine == 0x8664:
        pass
    elif machine == 0x14C:
        if has_wow64(runtime):
            warnings.append(
                "32-bit x86 image: it runs through the runtime's WoW64 layer, "
                "which Porte exercises less than the x86-64 path")
        else:
            blockers.append(
                "32-bit x86 image: this Wine distribution has no 32-bit "
                "libraries, so it cannot load the program")
    elif machine == 0xAA64:
        blockers.append(
            "ARM64 Windows image: an x86-64 Wine runtime cannot execute it "
            "(x86-64 Windows binaries are the supported case)")
    else:
        blockers.append(f"unsupported PE machine type 0x{machine:x}")

    if subsystem not in (2, 3):
        blockers.append(
            f"{SUBSYSTEM_NAMES.get(subsystem, subsystem)} subsystem image: not "
            "an application")
    if missing:
        blockers.append(
            "imports DLLs that are not part of Windows or next to the "
            "executable: " + ", ".join(missing[:8]))

    if subsystem == 2:
        notes.append("Windows GUI program: it creates its own window inside the "
                     "compatibility runtime.")
    elif subsystem == 3:
        notes.append("Windows console program: output is captured from the guest "
                     "process.")
    if "mscoree.dll" in dlls or ".NET" in " ".join(functions):
        warnings.append("managed (.NET) image: Wine's mono runtime is not "
                        "installed by Porte, so this may fail to start")
    if any(dll.startswith("d3d") or dll == "dxgi.dll" for dll in dlls):
        warnings.append("Direct3D: the runtime provides its own OpenGL/Vulkan "
                        "translation; rendering quality and speed are not "
                        "verified for this program")
    if "msi.dll" in dlls:
        warnings.append("this image drives the Windows Installer API itself")

    # What makes a program an *installer* here: it drives the Windows Installer
    # or setup APIs, or it names itself setup/install/uninstall. Importing
    # user32 alone is every GUI program, not a hint.
    installer_apis = {"msiprocessmessage", "msiinstallproduct",
                      "msiopenpackage", "setupopeninfofile",
                      "setupiteratedata", "setupinstallfrominfosection",
                      "fdicopy", "extractcab"}
    lowered_functions = {name.lower() for name in functions}
    stem = path.stem.lower()
    installer_like = bool(installer_apis & lowered_functions) or \
        stem.startswith(("setup", "install", "unins", "update")) or \
        "msiexec" in stem

    return {
        "supported": not blockers,
        "run_supported": not blockers,
        "install_supported": not blockers,
        "machine": MACHINE_NAMES.get(machine, f"0x{machine:x}"),
        "subsystem": SUBSYSTEM_NAMES.get(subsystem, str(subsystem)),
        "is_gui": subsystem == 2,
        "installer_like": installer_like,
        "dlls": dlls,
        "functions": sorted(set(functions)),
        "missing_dlls": missing,
        "blockers": blockers,
        "warnings": warnings,
        "notes": notes,
    }


def _product_key_for(path: Path, name: str) -> str:
    from ..app import environment

    return environment.product_key(name, util.sha256_file(path))


# ---- backend -----------------------------------------------------------

class WineBackend:
    id = WINE
    name = NAME

    def __init__(self, runtime: WineRuntime | None = None) -> None:
        self._runtime = runtime
        self._status: BackendStatus | None = None

    @property
    def runtime(self) -> WineRuntime:
        if self._runtime is None:
            self._runtime = select()
        return self._runtime

    # ---- status --------------------------------------------------------
    def status(self) -> BackendStatus:
        if self._status is not None:
            return self._status
        reasons: list[str] = []
        warnings: list[str] = []
        rosetta = rosetta_available()
        runtime: WineRuntime | None = None
        try:
            runtime = self.runtime
        except BackendUnavailable as exc:
            reasons.append(str(exc))
        if runtime is not None:
            if host_arch() == "arm64" and not rosetta:
                reasons.append(
                    "this Mac has no Rosetta 2, so it cannot execute the "
                    "x86-64 Wine runtime (install it with "
                    "`softwareupdate --install-rosetta`)")
            if runtime.kind == "crossover":
                warnings.append(
                    "CrossOver's Wine is detected but is proprietary: Porte "
                    "runs it for you and never copies or redistributes it")
        self._status = BackendStatus(
            id=WINE,
            name=NAME,
            available=runtime is not None and not reasons,
            version=runtime.version if runtime else None,
            path=str(runtime.wine) if runtime else None,
            usable_for_gui=runtime is not None and not reasons,
            reasons=reasons,
            warnings=warnings,
            license="LGPL-2.1-or-later (Wine); a CrossOver runtime is proprietary",
            redistributable=bool(runtime.redistributable) if runtime else True,
            requires_rosetta=True,
            rosetta_available=rosetta,
            details={
                "runtime": runtime.to_dict() if runtime else None,
                "candidates": [r.to_dict() for r in discover(probe=False)][:8],
                "bottles": str(bottles_root()),
                "host_arch": host_arch(),
                "downloads_runtime": False,
            },
        )
        return self._status

    # ---- analysis ------------------------------------------------------
    def analyze(self, path: Path) -> dict:
        path = Path(path)
        try:
            runtime = self.runtime
        except BackendUnavailable:
            runtime = None
        info = analyze_exe(path, runtime)
        status = self.status()
        if not status.available:
            info["supported"] = info["run_supported"] = False
            info["install_supported"] = False
            info["blockers"] = list(info["blockers"]) + [
                "the Wine runtime is not available: " +
                (status.reasons[0] if status.reasons else "not found")]
        info.update({
            "backend": WINE,
            "backend_name": NAME,
            "runtime_version": status.version,
            "reason": ("the runtime provides the Windows API this image uses"
                       if info["supported"] else
                       "; ".join(info["blockers"][:2])),
        })
        return info

    # ---- execution -----------------------------------------------------
    def run(self, path: Path, arguments: list[str] | None = None,
            timeout: float = 900.0, bottle: str | None = None,
            windows_path: str | None = None) -> dict:
        runtime = self.runtime
        path = Path(path)
        key = bottle or f"run-{util.sha256_file(path)[:12]}"
        prefix = ensure_bottle(runtime, key)
        target = windows_path or str(path)
        command = [str(runtime.wine), target] + [str(a) for a in (arguments or [])]
        started = time.perf_counter()
        result = run_captured(command, env=clean_env(prefix), timeout=timeout)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        # A clean guest exit is 0; wine passes the guest's status through. 53 is
        # wine's "the executable could not be started" and is reported as an
        # error rather than as a program that exited 53.
        if result["returncode"] == 53 and \
                "could not be started" in result["stderr"]:
            raise BackendError(
                "the runtime could not start the program: " +
                (result["stderr"].strip()[-600:] or "no output"))
        return {
            "backend": WINE,
            "exit_code": result["returncode"],
            "fault": None,
            "run_ms": elapsed_ms,
            "output": result["stdout"],
            "stderr": result["stderr"][-8000:],
            "api_calls": {},
            "wine_version": runtime.version,
            "bottle": str(prefix),
        }

    # ---- portable EXE installation -------------------------------------
    def install(self, path: Path, *, name: str | None = None,
                dest: Path | None = None, arguments: list[str] | None = None,
                force: bool = False, include_dlls: bool = True,
                include_siblings: bool = True,
                accept: bool = True,
                bottle_key: str | None = None) -> dict:
        from ..app import bundle as bundle_mod
        from ..app.bundle import AppSpec

        path = Path(path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        facts = self.analyze(path)
        if not facts["install_supported"]:
            raise NotImplementedError(
                "this program cannot be installed with the Wine backend: " +
                "; ".join(facts["blockers"]))
        runtime = self.runtime
        status = self.status()
        display = name or path.stem
        spec = AppSpec(
            pe_path=path,
            name=display,
            bundle_id=bundle_mod.default_bundle_id(path, display),
            arguments=list(arguments or []),
            profile="wine-gui" if facts["is_gui"] else "wine-console",
            category=bundle_mod.icon_category("game"),
        )
        # The bottle is normally derived from the bundle identity, so an
        # application keeps its settings across rebuilds. A caller that wants a
        # throwaway environment (verification) passes its own key.
        key = bottle_key or f"app-{util.sha256_text(str(spec.bundle_id))[:12]}"
        prefix = ensure_bottle(runtime, key)
        # A portable Windows program is its folder: Notepad++ needs its
        # plugins, models and configuration next to the executable, and a
        # program that silently loses them is worse than a larger bundle.
        siblings: list[Path] = []
        for entry in sorted(path.parent.iterdir()):
            if entry.resolve() == path:
                continue
            if entry.name.startswith("."):
                continue
            if include_siblings or (entry.is_file() and
                                    entry.suffix.lower() == ".dll"):
                siblings.append(entry)
        result = bundle_mod.build_app(
            spec, dest_dir=dest, force=force, bundled_files=siblings,
            pe_display_path=_display_path(path),
            runtime={
                "kind": WINE,
                "wine": str(runtime.wine),
                "wine_version": runtime.version,
                "wine_raw_version": runtime.raw_version,
                "bottle": str(prefix),
                "rosetta_required": host_arch() == "arm64",
                "redistributable": runtime.redistributable,
                "status": status.to_dict(),
            })
        result["backend"] = WINE
        result["wine_version"] = runtime.version
        result["bottle"] = str(prefix)
        result["pe_facts"] = {
            "machine": facts["machine"],
            "subsystem": facts["subsystem"],
            "dlls": len(facts["dlls"]),
        }
        return result

    # ---- MSI installation ----------------------------------------------
    def install_msi(self, path: Path, *, dest: Path | None = None,
                    force: bool = False, accept: bool = True,
                    timeout: float = 1800.0) -> dict:
        from ..app import bundle as bundle_mod
        from ..app import environment

        path = Path(path).expanduser().resolve()
        runtime = self.runtime
        name = _msi_product_name(path)
        key = _product_key_for(path, name)
        prefix = ensure_bottle(runtime, key)
        logs = environment.product_dir(key) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log_path = logs / "msiexec.log"

        command = [str(runtime.wine), "msiexec", "/i", str(path),
                   "/qn", "/norestart", "/l*v", str(log_path)]
        started = time.perf_counter()
        result = run_captured(command, env=clean_env(prefix), timeout=timeout)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        # 0 = success, 3010 = success but a reboot is pending (which Porte does
        # not perform). Anything else is a failed installation.
        if result["returncode"] not in (0, 3010):
            raise BackendError(
                f"Windows Installer failed with status "
                f"{result['returncode']}; see {log_path}")
        # The registry lives in a text file the wineserver flushes in its own
        # time; reading it immediately after msiexec can catch it mid-write.
        programs: list[dict] = []
        deadline = time.time() + 45.0
        while not programs and time.time() < deadline:
            programs = installed_programs(prefix)
            if programs:
                break
            time.sleep(0.5)
        if not programs:
            # Nothing to lose by flushing now: the installer has exited.
            terminate_prefix(runtime, prefix)
            time.sleep(1.0)
            programs = installed_programs(prefix)
        if not programs:
            raise BackendError(
                "the installer reported success but registered no program: "
                f"see {log_path}")
        bundles = self._bundles_for_programs(
            prefix, programs, dest=dest, force=force,
            package=path, product_key=key)
        environment.write_manifest(key, {
            "product_key": key,
            "name": name,
            "version": programs[0].get("version"),
            "manufacturer": programs[0].get("publisher"),
            "kind": "wine-msi",
            "runtime": WINE,
            "wine_version": runtime.version,
            "environment": str(prefix),
            "windows": str(prefix),
            "package": str(path),
            "package_sha256": util.sha256_file(path),
            "files": [],
            "registry_values": 0,
            "programs": programs,
            "bundles": [{"app": b["app"], "bundle_id": b["bundle_id"]}
                        for b in bundles],
            "skipped_executables": [],
            "warnings": [
                "the installer ran inside Wine; Windows installer actions that "
                "depend on drivers, services or kernel extensions have no "
                "macOS equivalent and were not applied",
            ],
            "install_ms": elapsed_ms,
            "installed_at": environment.now(),
        })
        return {
            "product": key,
            "environment": str(prefix),
            "product_name": name,
            "programs": programs,
            "bundles": bundles,
            "warnings": [],
            "files": 0,
            "backend": WINE,
            "msiexec_status": result["returncode"],
            "log": str(log_path),
            "elapsed_ms": elapsed_ms,
        }

    def _bundles_for_programs(self, prefix: Path, programs: list[dict], *,
                              dest: Path | None, force: bool,
                              package: Path | None = None,
                              product_key: str = "") -> list[dict]:
        from ..app import bundle as bundle_mod
        from ..app.bundle import AppSpec

        bundles: list[dict] = []
        for program in programs:
            executable = _launchable_executable(prefix, program)
            if executable is None:
                continue
            unix_exe, windows_exe = executable
            spec = AppSpec(
                pe_path=unix_exe,
                name=program["name"],
                bundle_id=bundle_mod.default_bundle_id(
                    unix_exe, program["name"],
                    identity=program.get("product_code") or
                    (util.sha256_file(package) if package else product_key)),
                arguments=[],
                profile="wine-gui",
                category=bundle_mod.icon_category("game"),
            )
            result = bundle_mod.build_app(
                spec, dest_dir=dest, force=force, pe_location="prefix",
                pe_override=windows_exe,
                runtime={
                    "kind": WINE,
                    "wine": str(self.runtime.wine),
                    "wine_version": self.runtime.version,
                    "wine_raw_version": self.runtime.raw_version,
                    "bottle": str(prefix),
                    "rosetta_required": host_arch() == "arm64",
                    "redistributable": self.runtime.redistributable,
                    "product": product_key,
                })
            result["backend"] = WINE
            result["program"] = program["name"]
            bundles.append(result)
        return bundles

    # ---- removal -------------------------------------------------------
    def uninstall_msi(self, product_key: str) -> dict:
        from ..app import bundle as bundle_mod
        from ..app import environment

        manifest = environment.read_manifest(product_key)
        if not manifest:
            raise BackendError(f"no installed product {product_key}")
        prefix = Path(manifest.get("environment") or manifest.get("windows") or "")
        runtime = self.runtime
        removed_bundles = []
        for entry in manifest.get("bundles") or []:
            app = Path(entry.get("app") or "")
            if app.exists():
                bundle_mod.remove_app(app, purge=True)
                removed_bundles.append(str(app))
        status = None
        codes = {program.get("product_code")
                 for program in manifest.get("programs") or []
                 if program.get("product_code")}
        if prefix.exists():
            for code in codes:
                result = run_captured(
                    [str(runtime.wine), "msiexec", "/x", code, "/qn",
                     "/norestart"],
                    env=clean_env(prefix), timeout=900)
                status = result["returncode"]
        removed_prefix = False
        if prefix.exists():
            shutil.rmtree(prefix, ignore_errors=True)
            removed_prefix = True
        if environment.product_dir(product_key).exists():
            shutil.rmtree(environment.product_dir(product_key),
                          ignore_errors=True)
        return {"product": product_key, "bundles": removed_bundles,
                "environment_removed": removed_prefix,
                "msiexec_status": status}

    def repair_msi(self, product_key: str) -> dict:
        from ..app import environment

        manifest = environment.read_manifest(product_key)
        if not manifest:
            raise BackendError(f"no installed product {product_key}")
        prefix = Path(manifest.get("environment") or "")
        runtime = self.runtime
        codes = [program.get("product_code")
                 for program in manifest.get("programs") or []
                 if program.get("product_code")]
        results = []
        for code in codes:
            outcome = run_captured(
                [str(runtime.wine), "msiexec", "/f", code, "/qn", "/norestart"],
                env=clean_env(prefix), timeout=900)
            results.append({"product_code": code,
                            "status": outcome["returncode"]})
        missing = [b for b in manifest.get("bundles") or []
                   if not Path(b.get("app") or "").exists()]
        return {"product": product_key, "msiexec": results,
                "missing_bundles": missing,
                "environment_exists": prefix.exists()}


# ---- helpers -----------------------------------------------------------

def _display_path(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path.home()))
    except ValueError:
        return str(Path(path).resolve())


def _msi_product_name(path: Path) -> str:
    try:
        from ..app.msi import install as msi_install

        info = msi_install.inspect_package(path)
        if info.get("name"):
            return str(info["name"])
    except Exception:
        pass
    return path.stem


def _launchable_executable(prefix: Path, program: dict) -> tuple[Path, str] | None:
    """Find the program's main executable inside its prefix.

    Order: the DisplayIcon path when it is an .exe, then the largest GUI
    executable under InstallLocation, then the largest GUI executable under
    Program Files whose name matches the product.
    """
    icon = program.get("display_icon") or ""
    icon = icon.split(",")[0].strip().strip('"')
    if icon.lower().endswith(".exe"):
        candidate = dos_to_unix(prefix, icon)
        if candidate and candidate.exists():
            return candidate, icon
    location = program.get("install_location") or ""
    roots: list[Path] = []
    if location:
        mapped = dos_to_unix(prefix, location)
        if mapped and mapped.exists():
            roots.append(mapped)
    for name in ("Program Files", "Program Files (x86)"):
        base = dos_to_unix(prefix, f"C:\\{name}")
        if base and base.exists():
            roots.append(base)
    best: tuple[int, Path, str] | None = None
    for root in roots:
        for exe in sorted(root.rglob("*.exe")):
            lower = exe.name.lower()
            if lower.startswith("unins") or lower in ("setup.exe", "install.exe"):
                continue
            try:
                _, _, machine, subsystem = _import_dlls(exe)
            except Exception:
                continue
            if machine != 0x8664 or subsystem != 2:
                continue
            size = exe.stat().st_size
            if best is None or size > best[0]:
                try:
                    relative = exe.relative_to(Path(prefix) / "drive_c")
                    windows = "C:\\" + str(relative).replace("/", "\\")
                except ValueError:
                    windows = str(exe)
                best = (size, exe, windows)
    if best:
        return best[1], best[2]
    return None


def available() -> bool:
    return WineBackend().status().available


def backend(runtime: WineRuntime | None = None) -> WineBackend:
    return WineBackend(runtime)
