"""Generate and manage macOS application bundles for Windows executables.

A bundle is self-describing: the Windows image, its icon, a metadata document and
a freshly compiled ARM64 launcher live inside it; all mutable state lives in
`~/Library/Application Support/Porte/Applications/<bundle id>/`. Nothing is
written into the bundle after it is built, so signing it (or shipping it from a
read-only location) stays valid.

The launcher is compiled per bundle from `launcher.m`. It is a real Mach-O
executable inside `Contents/MacOS`, which is what gives the application its own
Dock identity: the Windows program never runs as `python` or as Porte.
"""
from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import util

SUPPORT_ROOT = Path.home() / "Library" / "Application Support" / "Porte"
APPS_ROOT = SUPPORT_ROOT / "Applications"
SUPPORT_ROOT_VAR = "PORTE_SUPPORT_ROOT"
LAUNCHER_SOURCE = Path(__file__).with_name("launcher.m")
LSREGISTER = Path(
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister")
SCHEMA = 1


def support_root() -> Path:
    override = os.environ.get(SUPPORT_ROOT_VAR)
    return Path(override) if override else SUPPORT_ROOT


def app_state_dir(bundle_id: str) -> Path:
    return support_root() / "Applications" / bundle_id


def slug(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return cleaned or "windows-app"


def default_bundle_id(pe_path: Path, name: str, identity: str = "") -> str:
    """Stable id from the image, plus the product it was installed as.

    The digest alone is not enough: two packages can carry the same executable
    and must not share one application state directory.
    """
    digest = util.sha256_file(pe_path)
    if identity:
        digest = util.sha256_text(digest + "\0" + identity)
    return f"com.notomer.porte.windows.{slug(name)[:32]}-{digest[:8]}"


def _exe_name(name: str) -> str:
    return re.sub(r"[/:]", "-", name).strip() or "PorteApp"


def runtime_interpreter() -> tuple[Path, list[str]]:
    """Choose an interpreter that stays a CLI process inside the app.

    A venv created by Homebrew Python points at the framework's `bin/python3.14`
    through symlinks. Launching the venv path from a GUI process makes CPython
    re-exec the framework's `Python.app`, which is a second application bundle;
    resolving the symlink avoids that. The import path is recorded explicitly
    because the resolved interpreter no longer derives it from the venv.
    """
    exe = Path(sys.executable)
    try:
        exe = exe.resolve()
    except OSError:
        pass
    pythonpath = [str(util.ROOT)]
    for entry in sys.path:
        if ("site-packages" in entry or "dist-packages" in entry) and entry not in pythonpath:
            pythonpath.append(entry)
    return exe, pythonpath


def _cmd(cmd: list[str], why: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{why}: {' '.join(cmd[:3])}… -> "
                           f"{proc.stderr.strip()[:600] or proc.stdout.strip()[:600]}")
    return proc


@dataclass
class AppSpec:
    pe_path: Path
    name: str
    bundle_id: str
    arguments: list[str] = field(default_factory=list)
    profile: str = "console"
    version: str = "1.0.0"
    category: str = "public.app-category.utilities"


def profile_for_imports(import_names: set[str]) -> str:
    return "metal-frame" if "PresentFrame" in import_names else "console"


def icon_category(profile: str) -> str:
    if profile in ("metal-frame", "game"):
        return "public.app-category.games"
    return "public.app-category.utilities"


def _write_plist(path: Path, spec: AppSpec, exe_name: str) -> None:
    doc = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleExecutable": exe_name,
        "CFBundleIconFile": "AppIcon",
        "CFBundleIdentifier": spec.bundle_id,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": spec.name,
        "CFBundleDisplayName": spec.name,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": spec.version,
        "CFBundleVersion": spec.version,
        "LSApplicationCategoryType": spec.category,
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
        "NSSupportsAutomaticGraphicsSwitching": True,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump(doc, f, sort_keys=True)


def _write_metadata(path: Path, spec: AppSpec, *, pe_relative: str,
                    runtime_root: Path, python: Path, pythonpath: list[str],
                    icon: dict, extra: dict, runtime: dict | None = None,
                    pe_location: str = "bundle") -> None:
    runtime_doc = {
        "kind": "engine",
        "python": str(python),
        "pythonpath": pythonpath,
        "root": str(runtime_root),
        "module": "porte.app.runtime",
    }
    runtime_doc.update(runtime or {})
    doc = {
        "schema": SCHEMA,
        "bundle_id": spec.bundle_id,
        "name": spec.name,
        "version": spec.version,
        "profile": spec.profile,
        "arguments": spec.arguments,
        "pe": pe_relative,
        "pe_location": pe_location,
        "pe_display_path": extra.get("pe_display_path", str(spec.pe_path)),
        "pe_sha256": util.sha256_file(spec.pe_path),
        "pe_size": spec.pe_path.stat().st_size,
        "icon": icon,
        "runtime": runtime_doc,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "porte_schema": SCHEMA,
        "bundled_files": extra.get("bundled_files", []),
    }
    util.write_json(path, doc)


def build_app(spec: AppSpec, *, dest_dir: Path | None = None,
              runtime_root: Path | None = None, python: Path | None = None,
              force: bool = False, bundled_files: list[Path] | None = None,
              pe_display_path: str | None = None,
              runtime: dict | None = None,
              pe_location: str = "bundle",
              pe_override: str | None = None) -> dict:
    dest_dir = Path(dest_dir) if dest_dir else Path.home() / "Applications"
    runtime_root = Path(runtime_root) if runtime_root else util.ROOT
    default_python, pythonpath = runtime_interpreter()
    python = Path(python) if python else default_python
    dest_dir.mkdir(parents=True, exist_ok=True)
    app_path = dest_dir / f"{_exe_name(spec.name)}.app"
    if app_path.exists():
        if not force:
            raise FileExistsError(f"{app_path} already exists (use --force)")
        shutil.rmtree(app_path)

    contents = app_path / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    windows = resources / "windows"
    for path in (macos, windows):
        path.mkdir(parents=True, exist_ok=True)

    if pe_location not in ("bundle", "prefix"):
        raise ValueError(f"unknown pe_location {pe_location!r}")
    if pe_location == "prefix":
        # An installed product keeps its files in its own Windows prefix; the
        # bundle holds identity, icon, launcher and the recorded Windows path.
        if not pe_override:
            raise ValueError("pe_location='prefix' needs the Windows path")
        pe_relative = pe_override
        copied: list[str] = []
    else:
        pe_relative = Path("windows") / spec.pe_path.name
        shutil.copy2(spec.pe_path, resources / pe_relative)
        copied = [str(pe_relative)]
    for extra in bundled_files or []:
        src = Path(extra)
        if not src.exists():
            raise FileNotFoundError(f"bundled file missing: {src}")
        target = windows / src.name
        if target.exists():
            raise FileExistsError(f"duplicate bundled file {src.name}")
        if src.is_dir():
            shutil.copytree(src, target)
        else:
            shutil.copy2(src, target)
        copied.append(str(Path("windows") / src.name))

    exe_name = _exe_name(spec.name)
    launcher = macos / exe_name
    _cmd(["clang", "-O2", "-fobjc-arc", "-arch", "arm64",
          "-mmacosx-version-min=13.0", "-framework", "Cocoa",
          "-o", str(launcher), str(LAUNCHER_SOURCE)],
         "launcher compilation failed")

    from . import icon as icon_mod

    tmp = util.BUILD / "app_icon" / spec.bundle_id
    icon_info = icon_mod.build_icon(spec.pe_path, resources / "AppIcon.icns", tmp)
    shutil.rmtree(tmp, ignore_errors=True)

    _write_metadata(resources / "PorteApp.json", spec,
                    pe_relative=str(pe_relative), runtime_root=runtime_root,
                    python=python, pythonpath=pythonpath, icon=icon_info,
                    extra={"pe_display_path": pe_display_path or str(spec.pe_path),
                           "bundled_files": copied},
                    runtime=runtime, pe_location=pe_location)
    _write_plist(contents / "Info.plist", spec, exe_name)

    # Ad-hoc signature: arm64 executables must be signed to run, and a stable
    # signature makes the bundle's identity reproducible across rebuilds.
    sign = subprocess.run(["codesign", "--force", "--sign", "-", str(app_path)],
                          capture_output=True, text=True)
    signed = sign.returncode == 0

    state = app_state_dir(spec.bundle_id)
    state.mkdir(parents=True, exist_ok=True)
    config_path = state / "config.json"
    if not config_path.exists():
        util.write_json(config_path, {
            "bundle_id": spec.bundle_id,
            "name": spec.name,
            "arguments": spec.arguments,
            "profile": spec.profile,
            "pe_sha256": util.sha256_file(spec.pe_path),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
    (state / "logs").mkdir(exist_ok=True)
    (state / "cache").mkdir(exist_ok=True)

    registered = register(app_path)
    return {
        "app": str(app_path),
        "bundle_id": spec.bundle_id,
        "launcher": str(launcher),
        "launcher_arch": launcher_arch(launcher),
        "signed": signed,
        "registered": registered,
        "icon": icon_info,
        "pe": str(spec.pe_path),
        "pe_sha256": util.sha256_file(spec.pe_path),
        "state_dir": str(state),
        "profile": spec.profile,
        "arguments": spec.arguments,
        "runtime_kind": (runtime or {}).get("kind", "engine"),
        "pe_location": pe_location,
    }


def launcher_arch(launcher: Path) -> str:
    proc = subprocess.run(["lipo", "-archs", str(launcher)],
                          capture_output=True, text=True)
    return proc.stdout.strip() or "unknown"


def register(app_path: Path) -> bool:
    if not LSREGISTER.exists():
        return False
    proc = subprocess.run([str(LSREGISTER), "-f", str(app_path)],
                          capture_output=True, text=True)
    return proc.returncode == 0


def unregister(app_path: Path) -> bool:
    if not LSREGISTER.exists():
        return False
    proc = subprocess.run([str(LSREGISTER), "-u", str(app_path)],
                          capture_output=True, text=True)
    return proc.returncode == 0


def inspect_app(app_path: Path) -> dict:
    app_path = Path(app_path)
    info_path = app_path / "Contents" / "Info.plist"
    meta_path = app_path / "Contents" / "Resources" / "PorteApp.json"
    if not info_path.exists():
        raise FileNotFoundError(f"not an application bundle: {app_path}")
    with open(info_path, "rb") as f:
        info = plistlib.load(f)
    meta = util.read_json(meta_path) if meta_path.exists() else {}
    exe = app_path / "Contents" / "MacOS" / info.get("CFBundleExecutable", "")
    state = app_state_dir(info.get("CFBundleIdentifier", ""))
    signed = subprocess.run(["codesign", "-dv", str(app_path)],
                            capture_output=True, text=True)
    return {
        "app": str(app_path),
        "bundle_id": info.get("CFBundleIdentifier"),
        "name": info.get("CFBundleName"),
        "executable": str(exe),
        "launcher_arch": launcher_arch(exe) if exe.exists() else "missing",
        "signed": "code object is not signed" not in signed.stderr,
        "icon": (app_path / "Contents" / "Resources" / "AppIcon.icns").exists(),
        "pe": meta.get("pe"),
        "pe_location": meta.get("pe_location") or "bundle",
        "pe_sha256": meta.get("pe_sha256"),
        "runtime": meta.get("runtime"),
        "runtime_kind": (meta.get("runtime") or {}).get("kind"),
        "profile": meta.get("profile"),
        "arguments": meta.get("arguments"),
        "state_dir": str(state),
        "config": str(state / "config.json") if (state / "config.json").exists() else None,
        "runs": len(list((state / "logs").glob("run-*.json"))) if (state / "logs").exists() else 0,
    }


def remove_app(app_path: Path, purge: bool = False) -> dict:
    app_path = Path(app_path)
    info_path = app_path / "Contents" / "Info.plist"
    if not info_path.exists():
        raise FileNotFoundError(f"not an application bundle: {app_path}")
    with open(info_path, "rb") as f:
        info = plistlib.load(f)
    bundle_id = info.get("CFBundleIdentifier", "")
    unregister(app_path)
    shutil.rmtree(app_path)
    removed_state = False
    state = app_state_dir(bundle_id)
    if purge and state.exists():
        shutil.rmtree(state)
        removed_state = True
    return {"app": str(app_path), "bundle_id": bundle_id,
            "state_removed": removed_state, "state_dir": str(state)}


def find_app(name_or_id: str) -> Path:
    """Resolve an installed bundle by bundle id, app path or display name."""
    candidate = Path(name_or_id).expanduser()
    if candidate.exists() and candidate.suffix == ".app":
        return candidate
    for base in (Path.home() / "Applications", Path("/Applications")):
        if not base.exists():
            continue
        for app in base.glob("*.app"):
            try:
                info = inspect_app(app)
            except Exception:
                continue
            if name_or_id in (info["bundle_id"], info["name"], app.stem):
                return app
    raise FileNotFoundError(f"no installed application matches {name_or_id!r}")
