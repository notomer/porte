"""Runtime backend contract.

Porte has two kinds of execution backend and they are not the same thing:

* the experimental `engine` executes Windows PE code inside its own emulator and
  implements a small, explicit set of Win32 calls. It is Porte's own code, it is
  fully self-contained, and it can run the fixtures it was built for.
* the `wine` backend drives an external Wine distribution. It provides the real
  Win32 user32/gdi32/comdlg32/ole32 surface, real windowing, real installers,
  and therefore real third-party applications.

A backend answers three questions about a file: can it be analyzed, run and
installed; and it can run and install. Nothing above this layer needs to know
which backend is in use, and no backend is allowed to pretend to succeed: an
unavailable runtime reports itself unavailable and the caller explains why.

Backends never download or install a runtime on their own. Discovery only looks
at paths the user already has (plus an explicit override), because silently
fetching and executing a compatibility layer is exactly the kind of thing Porte
must not do.
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import sys
from pathlib import Path

ENGINE = "engine"
WINE = "wine"

#: Raised when a backend cannot do what was asked, with a reason a user can act
#: on rather than a traceback.
class BackendError(RuntimeError):
    pass


class BackendUnavailable(BackendError):
    pass


@dataclasses.dataclass
class BackendStatus:
    id: str
    name: str
    available: bool
    version: str | None
    path: str | None
    usable_for_gui: bool
    reasons: list[str] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)
    license: str = ""
    redistributable: bool = True
    requires_rosetta: bool = False
    rosetta_available: bool = False
    details: dict = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def rosetta_available() -> bool:
    """True when this Mac can execute x86-64 code (Apple silicon only)."""
    if os.uname().machine != "arm64":
        return True
    for path in ("/usr/libexec/rosetta/oahd",):
        if Path(path).exists():
            return True
    probe = subprocess.run(["arch", "-x86_64", "/usr/bin/true"],
                           capture_output=True)
    return probe.returncode == 0


def host_arch() -> str:
    return os.uname().machine


def which_wine(root: Path) -> Path | None:
    """Find `wine` inside a Wine distribution or application bundle."""
    root = Path(root)
    for relative in ("bin/wine", "bin/wine64", "wine/bin/wine",
                     "Contents/Resources/wine/bin/wine",
                     "Contents/Resources/wine/bin/wine64"):
        candidate = root / relative
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def wine_distribution_root(binary: Path) -> Path:
    """The directory that holds bin/, lib/ and share/ for a wine binary."""
    binary = Path(binary)
    if binary.parent.name == "bin":
        return binary.parent.parent
    return binary.parent


def run_command(command: list[str], *, env: dict | None = None,
                timeout: float = 120.0, cwd: Path | None = None,
                input_text: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, capture_output=True, text=True,
                              timeout=timeout, env=env,
                              cwd=str(cwd) if cwd else None,
                              input=input_text)
    except subprocess.TimeoutExpired as exc:
        raise BackendError(
            f"{Path(command[0]).name} did not finish within {timeout:.0f}s") from exc
    except FileNotFoundError as exc:
        raise BackendUnavailable(f"{command[0]} is not executable") from exc


def find_executable(name: str) -> str | None:
    return shutil.which(name)


def python_runtime() -> tuple[Path, list[str]]:
    """Interpreter + import path a generated bundle uses to reach Porte."""
    from ..app import bundle

    return bundle.runtime_interpreter()


def summary_line(status: BackendStatus) -> str:
    if not status.available:
        return f"{status.name}: unavailable — {'; '.join(status.reasons) or 'not found'}"
    return f"{status.name}: {status.version or 'unknown version'} at {status.path}"


__all__ = [
    "ENGINE", "WINE", "BackendError", "BackendUnavailable", "BackendStatus",
    "rosetta_available", "host_arch", "which_wine", "wine_distribution_root",
    "run_command", "find_executable", "python_runtime", "summary_line",
    "sys",
]
