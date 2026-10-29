"""Runtime backend registry and selection policy.

Two backends exist and they answer the same three questions (analyze, run,
install). Selection is explicit:

* `auto` prefers Porte's own engine when the engine can handle the image, and
  falls back to Wine when it cannot. That keeps every Phase 1/2 behaviour
  identical while making real Windows GUI programs runnable.
* `engine` / `wine` force one backend; the caller gets an explanation when it is
  unavailable instead of a silent fallback to the other one.

No backend can claim an application is runnable when it is not: the analysis of
each backend is kept separately and reported separately.
"""
from __future__ import annotations

from pathlib import Path

from .base import (ENGINE, WINE, BackendError, BackendStatus,
                   BackendUnavailable, rosetta_available, summary_line)
from . import engine as engine_backend
from . import wine as wine_backend

BACKEND_IDS = (ENGINE, WINE)
AUTO = "auto"


def backends() -> dict[str, object]:
    return {ENGINE: engine_backend.backend(), WINE: wine_backend.backend()}


def statuses() -> list[BackendStatus]:
    return [backend.status() for backend in backends().values()]


def get(name: str = AUTO):
    if name in (None, "", AUTO):
        raise ValueError("auto is a policy, not a backend")
    if name not in BACKEND_IDS:
        raise BackendError(f"unknown runtime backend {name!r}; "
                           f"expected one of {', '.join(BACKEND_IDS)}")
    return backends()[name]


def describe() -> list[dict]:
    return [status.to_dict() for status in statuses()]


def _candidates(path: Path, preferred: str | None) -> list[tuple[object, dict]]:
    """Per-backend analyses in preference order."""
    order: list[str]
    if preferred in BACKEND_IDS:
        order = [str(preferred)]
    elif preferred in (None, "", AUTO):
        order = [ENGINE, WINE]
    else:
        raise BackendError(f"unknown runtime backend {preferred!r}")
    results: list[tuple[object, dict]] = []
    for name in order:
        backend = backends()[name]
        try:
            results.append((backend, backend.analyze(path)))
        except BackendUnavailable as exc:
            results.append((backend, {
                "backend": name, "supported": False, "run_supported": False,
                "install_supported": False, "blockers": [str(exc)],
                "warnings": [], "notes": [], "reason": str(exc),
            }))
        except Exception as exc:  # a broken image must not break the registry
            results.append((backend, {
                "backend": name, "supported": False, "run_supported": False,
                "install_supported": False,
                "blockers": [f"{type(exc).__name__}: {exc}"],
                "warnings": [], "notes": [], "reason": str(exc),
            }))
    return results


def analyze(path: Path, preferred: str | None = AUTO) -> dict:
    """Every backend's verdict on one file, in preference order."""
    results = _candidates(Path(path), preferred)
    return {
        "selected": results[0][0].id if results else None,
        "backends": [dict(document, id=backend.id, name=backend.name)
                     for backend, document in results],
    }


def choose_for_install(path: Path, preferred: str | None = AUTO):
    results = _candidates(Path(path), preferred)
    for backend, document in results:
        if document.get("install_supported"):
            return backend, document
    raise BackendUnavailable(_no_backend_reason(results))


def choose_for_run(path: Path, preferred: str | None = AUTO):
    results = _candidates(Path(path), preferred)
    for backend, document in results:
        if document.get("run_supported"):
            return backend, document
    raise BackendUnavailable(_no_backend_reason(results))


def _no_backend_reason(results: list[tuple[object, dict]]) -> str:
    parts = []
    for backend, document in results:
        blockers = document.get("blockers") or []
        reason = document.get("reason") or (blockers[0] if blockers else "no support")
        parts.append(f"{backend.name}: {reason}")
    return "No available runtime can execute this image. " + " | ".join(parts)


def ensure_gui_runtime() -> None:
    """Raise with an actionable message when no GUI-capable backend exists."""
    status = wine_backend.backend().status()
    if not status.available:
        raise BackendUnavailable(
            "Windows GUI programs need a Wine runtime. " +
            "; ".join(status.reasons or ["no Wine runtime was found"]))


__all__ = [
    "ENGINE", "WINE", "AUTO", "BackendError", "BackendUnavailable",
    "BackendStatus", "backends", "statuses", "get", "describe", "analyze",
    "choose_for_install", "choose_for_run", "ensure_gui_runtime",
    "rosetta_available", "summary_line", "engine_backend", "wine_backend",
]
