"""Porte's own experimental execution engine, as a runtime backend.

This backend is a thin adapter: the analysis, installation and execution code is
exactly what Phase 1 and 2 shipped (`porte.app.install`, `porte.pe.machine`,
`porte.app.runtime`). Nothing here changes behaviour; it only exposes the engine
through the same interface the Wine backend implements, so the GUI and the CLI
can offer either one without branching on strings.

Known scope: the engine emulates a fixed set of Win32 calls and has no general
user32/gdi32 window bridge. It is the right backend for the images it was built
for and it reports everything else as unsupported.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from .base import ENGINE, BackendStatus, run_command

NAME = "Porte engine"


class EngineBackend:
    id = ENGINE
    name = NAME

    def __init__(self) -> None:
        self._status: BackendStatus | None = None

    # ---- status --------------------------------------------------------
    def status(self) -> BackendStatus:
        if self._status is not None:
            return self._status
        reasons: list[str] = []
        versions: list[str] = []
        modules = {}
        for module in ("pefile", "unicorn", "capstone"):
            try:
                imported = __import__(module)
                modules[module] = getattr(imported, "__version__", "unknown")
                versions.append(f"{module} {modules[module]}")
            except Exception as exc:
                modules[module] = None
                reasons.append(f"engine module {module} is not importable: {exc}")
        self._status = BackendStatus(
            id=ENGINE,
            name=NAME,
            available=not reasons,
            version=", ".join(versions) or None,
            path=str(Path(__file__).resolve().parents[2]),
            usable_for_gui=False,
            reasons=reasons,
            warnings=["the engine implements a fixed Win32 subset; most "
                      "third-party applications need the Wine backend"],
            license="Porte's own code",
            redistributable=True,
            requires_rosetta=False,
            rosetta_available=True,
            details={"modules": modules,
                     "executes_gui": False,
                     "implements_gui_api": False},
        )
        return self._status

    # ---- analysis ------------------------------------------------------
    def analyze(self, path: Path) -> dict:
        path = Path(path)
        from ..app import install

        facts = install.inspect_pe(path)
        # The engine executes images whose imports it serves, through
        # `porte.gui.runner`. Saying so here is what lets the caller pick the
        # backend on capability instead of assuming one; a program the engine
        # can read but not run must not be advertised as runnable.
        runnable = bool(facts["supported"])
        facts.update({
            "backend": ENGINE,
            "backend_name": NAME,
            "run_supported": runnable,
            # An installer-shaped image is a program to run, not a package to
            # bundle: its own setup UI decides what gets installed.
            "install_supported": runnable and not facts["installer_like"],
            "reason": ("the engine has host stubs for every import this image "
                       "uses" if facts["supported"] else
                       "the engine has no stub for " +
                       ", ".join(facts["missing_stubs"][:8])),
        })
        return facts

    # ---- execution -----------------------------------------------------
    def run(self, path: Path, arguments: list[str] | None = None,
            timeout: float = 600.0, env: dict | None = None) -> dict:
        command = [sys.executable, "-m", "porte.gui.runner", str(Path(path))]
        command.extend(str(a) for a in (arguments or []))
        child_env = dict(env or {})
        started = time.perf_counter()
        proc = run_command(command, env=child_env, timeout=timeout)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        document = None
        for line in reversed(proc.stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    document = json.loads(line)
                    break
                except ValueError:
                    continue
        if document is None:
            raise RuntimeError(
                f"the engine produced no result (exit status {proc.returncode})")
        if document.get("error"):
            raise RuntimeError(str(document["error"]))
        document.setdefault("backend", ENGINE)
        document["process_ms"] = elapsed_ms
        document["stderr"] = proc.stderr[-4000:]
        document["exit_status"] = proc.returncode
        return document

    # ---- installation --------------------------------------------------
    def install(self, path: Path, *, name: str | None = None,
                dest: Path | None = None, arguments: list[str] | None = None,
                force: bool = False, include_dlls: bool = True,
                accept: bool = True) -> dict:
        from ..app import install

        result = install.install(Path(path), name=name, dest=dest,
                                 arguments=arguments, accept=accept,
                                 force=force, include_dlls=include_dlls)
        result["backend"] = ENGINE
        return result


def available() -> bool:
    return EngineBackend().status().available


def backend() -> EngineBackend:
    return EngineBackend()
