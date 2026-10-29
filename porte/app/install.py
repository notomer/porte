"""Installation policy for turning a Windows executable into a macOS app.

The engine decides what it can execute; this module decides what may be
installed. It refuses early and explains why, rather than producing a bundle
that will fail on first launch.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .. import util
from ..pe.image import PEImage
from ..pe.machine import Machine
from . import bundle
from .bundle import AppSpec

DEFAULT_ARGS = {
    "console": [],
    "metal-frame": ["300", "8192", "1"],
}

INSTALLER_HINTS = {
    "shell32.dll", "advapi32.dll", "ole32.dll", "msi.dll", "user32.dll",
    "gdi32.dll", "comctl32.dll", "setupapi.dll", "dwmapi.dll",
}


def _stub_names() -> set[str]:
    from ..pe.gpu import GpuMachine

    names: set[str] = set()
    for cls in (Machine, GpuMachine):
        for attr in dir(cls):
            if attr.startswith("api_"):
                names.add(attr[4:])
    return names


def inspect_pe(pe_path: Path) -> dict:
    """Static facts used to decide whether an image is installable."""
    image = PEImage.load(Path(pe_path))
    imports = [(imp.dll, imp.name, imp.ordinal) for imp in image.imports]
    stubs = _stub_names()
    missing = sorted({name or f"ordinal:{ordinal}" for _, name, ordinal in imports
                      if not name or name not in stubs})
    names = {name for _, name, _ in imports if name}
    profile = bundle.profile_for_imports(names)
    dlls = sorted({dll.lower() for dll, _, _ in imports})
    gui_or_installer = bool(set(dlls) & INSTALLER_HINTS)
    return {
        "path": str(pe_path),
        "sha256": util.sha256_file(pe_path),
        "size": Path(pe_path).stat().st_size,
        "entry_rva": image.entry_rva,
        "imports": [{"dll": dll, "name": name, "ordinal": ordinal}
                    for dll, name, ordinal in imports],
        "import_names": sorted(names),
        "missing_stubs": missing,
        "supported": not missing,
        "installer_like": gui_or_installer,
        "profile": profile,
        "sections": [{"name": s.name, "vsize": s.vsize,
                      "raw_size": s.raw_size} for s in image.sections],
    }


def _ask_consent(pe_path: Path, facts: dict) -> None:
    print(f"Windows executable: {pe_path}")
    print(f"  sha256:   {facts['sha256']}")
    print(f"  imports:  {', '.join(facts['import_names']) or 'none'}")
    print(f"  profile:  {facts['profile']}")
    print("\nThis will run the program through Porte's compatibility layer.")
    print("It is not native macOS software and is not made safe by packaging.")
    try:
        reply = input("Install and allow this executable to run? [y/N] ")
    except EOFError as exc:
        raise PermissionError(
            "no interactive terminal; re-run with --accept once you have reviewed "
            "the imports above") from exc
    if reply.strip().lower() not in ("y", "yes"):
        raise PermissionError("installation declined")


def _record_consent(bundle_id: str, facts: dict, how: str) -> Path:
    state = bundle.app_state_dir(bundle_id)
    state.mkdir(parents=True, exist_ok=True)
    path = state / "consent.json"
    util.write_json(path, {
        "bundle_id": bundle_id,
        "pe_sha256": facts["sha256"],
        "accepted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "accepted_by": how,
    })
    return path


def _pe_display_path(pe_path: Path) -> str:
    try:
        return str(pe_path.resolve().relative_to(Path.home()))
    except ValueError:
        return str(pe_path.resolve())


def install(pe_path: Path, *, name: str | None = None,
            bundle_id: str | None = None, dest: Path | None = None,
            arguments: list[str] | None = None, accept: bool = False,
            force: bool = False, include_dlls: bool = True,
            runtime_root: Path | None = None) -> dict:
    pe_path = Path(pe_path).expanduser().resolve()
    if not pe_path.exists():
        raise FileNotFoundError(pe_path)
    if pe_path.suffix.lower() != ".exe":
        raise ValueError(f"not a Windows executable: {pe_path.name}")

    facts = inspect_pe(pe_path)
    if not facts["supported"]:
        hint = ("This program needs a GUI/installer Windows API surface that Porte "
                "does not provide yet. Porte cannot run it, so no bundle was "
                "created. Installers are never run as opaque programs: an .msi or "
                ".msix package is applied by `porte app import`, which reads its "
                "tables or payload itself."
                if facts["installer_like"] else
                "Porte cannot run this program yet, so no bundle was created.")
        raise NotImplementedError(
            f"{hint}\nMissing host stubs: {', '.join(facts['missing_stubs'])}")

    display = name or pe_path.stem
    spec = AppSpec(
        pe_path=pe_path,
        name=display,
        bundle_id=bundle_id or bundle.default_bundle_id(pe_path, display),
        arguments=list(arguments) if arguments else list(DEFAULT_ARGS[facts["profile"]]),
        profile=facts["profile"],
        category=bundle.icon_category(facts["profile"]),
    )

    if accept:
        consent_how = "install --accept"
    else:
        _ask_consent(pe_path, facts)
        consent_how = "install prompt"

    siblings: list[Path] = []
    if include_dlls:
        siblings = sorted(p for p in pe_path.parent.iterdir()
                          if p.is_file() and p.suffix.lower() == ".dll")

    result = bundle.build_app(spec, dest_dir=dest, runtime_root=runtime_root,
                              force=force, bundled_files=siblings,
                              pe_display_path=_pe_display_path(pe_path))
    result["consent"] = str(_record_consent(spec.bundle_id, facts, consent_how))
    result["pe_facts"] = {
        "imports": facts["import_names"],
        "profile": facts["profile"],
        "installer_like": facts["installer_like"],
    }
    return result


def launch(app_path: Path) -> dict:
    app = bundle.find_app(str(app_path))
    proc = subprocess.run(["open", "-n", str(app)], capture_output=True, text=True)
    return {"app": str(app), "opened": proc.returncode == 0,
            "error": proc.stderr.strip() or None}
