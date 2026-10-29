"""Newline-delimited JSON bridge between the native macOS UI and the engine.

Protocol 1, one JSON object per line. The UI writes requests on stdin and reads
events on stdout:

    → {"id": "1", "op": "analyze", "args": {"path": "/tmp/Setup.exe"}}
    ← {"id": "1", "event": "state", "state": "analyzing", "message": "…"}
    ← {"id": "1", "event": "result", "data": {…}}
    ← {"id": "1", "event": "error",  "code": "unsupported", "message": "…"}

Rules that keep the protocol trustworthy:

* Only protocol documents are written to stdout. The engine's own prints are
  redirected to stderr at start-up, and child processes' output is captured.
* Every request is answered exactly once with `result` or `error`. Progress is
  reported as `state`, `log`, or `progress` events on the same id.
* The bridge never asks a question. Destructive or execution operations require
  explicit `consent: true` in the request; the UI is responsible for asking the
  user and explaining what will run.
* Operations are serialised. `cancel` is handled by the reader thread
  immediately: it stops the current operation where it checks, and terminates a
  running guest child process.

The Python modules this calls are the same ones the CLI uses; this file contains
no installation logic of its own.
"""
from __future__ import annotations

import base64
import collections
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

PROTOCOL = 1
STATE_ANALYZING = "analyzing"
STATE_PREPARING = "preparing"
STATE_INSTALLING = "installing"
STATE_LAUNCHING = "launching"
STATE_RUNNING = "running"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_UNSUPPORTED = "unsupported"


class Cancelled(RuntimeError):
    pass


class RequestError(RuntimeError):
    """An error the UI can present, with a stable machine-readable code."""

    def __init__(self, code: str, message: str, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.data = data or {}


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("porte")
    except Exception:
        return "0.1.0"


def _path(value) -> Path:
    if not isinstance(value, str) or not value:
        raise RequestError("invalid_request", "a file path is required")
    return Path(value).expanduser()


def _suffix(path: Path) -> str:
    return path.suffix.lower()


def _encode_icon(pe_path: Path) -> str | None:
    try:
        from ..app.icon import extract_pe_icon_png

        found = extract_pe_icon_png(pe_path)
    except Exception:
        return None
    if not found:
        return None
    png, _size = found
    return base64.b64encode(png).decode("ascii")


def _default_dest() -> Path:
    return Path.home() / "Applications"


class Bridge:
    def __init__(self, stdin=None, stdout=None):
        # A private duplicate of the original stdout carries the protocol;
        # fd 1 itself is pointed at stderr so stray prints cannot corrupt it.
        if stdout is None:
            self._out = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
            os.dup2(2, 1)
        else:
            self._out = stdout
        self._in = stdin or sys.stdin
        self._write_lock = threading.Lock()
        self._queue: queue.Queue[dict | None] = queue.Queue()
        self._token: threading.Event | None = None
        self._active_op: str | None = None
        self._started = time.time()

    # ---- transport ----------------------------------------------------
    def send(self, doc: dict) -> None:
        line = json.dumps(doc, sort_keys=True)
        with self._write_lock:
            try:
                self._out.write(line + "\n")
                self._out.flush()
            except (BrokenPipeError, ValueError):
                pass

    def _emit(self, request_id, event: str, **fields) -> None:
        self.send({"id": request_id, "event": event, **fields})

    def _read_loop(self) -> None:
        try:
            for raw in self._in:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    request = json.loads(raw)
                except ValueError:
                    self.send({"event": "protocol_error",
                               "message": "request is not valid JSON"})
                    continue
                if not isinstance(request, dict) or "op" not in request:
                    self.send({"event": "protocol_error",
                               "message": "request must be an object with op"})
                    continue
                if request.get("op") == "cancel":
                    if self._token is not None:
                        self._token.set()
                    self.send({"id": request.get("id"), "event": "result",
                               "data": {"cancel_requested": True,
                                        "active": self._active_op}})
                    continue
                if "id" not in request:
                    self.send({"event": "protocol_error",
                               "message": "request must carry an id"})
                    continue
                self._queue.put(request)
        finally:
            self._queue.put(None)

    def _handle(self, request: dict) -> None:
        request_id = request.get("id")
        op = request.get("op")
        args = request.get("args") if isinstance(request.get("args"), dict) else {}
        token = threading.Event()
        self._token = token
        self._active_op = op
        try:
            handler = getattr(self, f"op_{op}", None)
            if handler is None:
                raise RequestError("unknown_op", f"unknown operation {op!r}")
            data = handler(args, request_id, token)
            if data is None:
                data = {}
            self._emit(request_id, "result", data=data)
        except Cancelled:
            self._emit(request_id, "error", code="cancelled",
                       message="The operation was cancelled.")
        except RequestError as exc:
            self._emit(request_id, "error", code=exc.code, message=str(exc),
                       data=exc.data)
        except FileNotFoundError as exc:
            self._emit(request_id, "error", code="not_found",
                       message=f"File not found: {exc}")
        except FileExistsError as exc:
            self._emit(request_id, "error", code="already_installed",
                       message=str(exc))
        except PermissionError as exc:
            self._emit(request_id, "error", code="declined", message=str(exc))
        except NotImplementedError as exc:
            self._emit(request_id, "error", code="unsupported", message=str(exc))
        except Exception as exc:  # anything else is a bug: log it, tell the UI
            from ..app.msi.install import MSIError, UnsupportedPackage

            if isinstance(exc, UnsupportedPackage):
                # The package itself is the limit, not Porte's code path.
                self._emit(request_id, "error", code="unsupported",
                           message=str(exc))
            elif isinstance(exc, MSIError):
                self._emit(request_id, "error", code="invalid_package",
                           message=f"Porte could not apply this package: {exc}")
            else:
                traceback.print_exc(file=sys.stderr)
                self._emit(request_id, "error", code="internal",
                           message=f"{type(exc).__name__}: {exc}")
        finally:
            self._token = None
            self._active_op = None

    def run(self) -> int:
        from .. import util

        try:
            support_root = Path(os.environ.get("PORTE_SUPPORT_ROOT") or
                                (Path.home() / "Library" / "Application Support"
                                 / "Porte"))
            support_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"warning: support directory unavailable: {exc}",
                  file=sys.stderr)
        reader = threading.Thread(target=self._read_loop, daemon=True)
        reader.start()
        self.send({
            "event": "ready",
            "protocol": PROTOCOL,
            "version": _version(),
            "python": sys.executable,
            "root": str(util.ROOT),
            "pid": os.getpid(),
        })
        while True:
            request = self._queue.get()
            if request is None:
                break
            self._handle(request)
        return 0

    # ---- operations ---------------------------------------------------
    def op_hello(self, args, request_id, token) -> dict:
        from .. import util
        from ..app import bundle
        from ..backends import describe

        health = self._health()
        backends_doc = describe()
        wine_available = any(entry["id"] == "wine" and entry["available"]
                             for entry in backends_doc)
        return {
            "protocol": PROTOCOL,
            "schema": 1,
            "version": _version(),
            "python": sys.executable,
            "root": str(util.ROOT),
            "support_root": str(bundle.support_root()),
            "apps_root": str(_default_dest()),
            "started_at": self._started,
            "health": health,
            "capabilities": {
                "install_exe": True,
                "install_msi": True,
                "install_msix": True,
                "run_console": health["engine"],
                "run_frame": False,
                "run_gui": wine_available,
                "install_msi_via_runtime": wine_available,
                "generate_bundles": health["clang"] and health["iconutil"],
                "uninstall": True,
                "repair": True,
                "launch": True,
                "bundle_installers": True,
                "msi_custom_actions": False,
                "authenticode": False,
                "downloads_runtime": False,
            },
            "backends": backends_doc,
            "host": util.host_info(),
        }

    @staticmethod
    def _health() -> dict:
        from ..app import bundle

        problems: list[str] = []
        modules = {}
        for name in ("pefile", "unicorn", "capstone"):
            try:
                __import__(name)
                modules[name] = True
            except Exception:
                modules[name] = False
                problems.append(f"engine module {name} is not importable")
        engine = "porte.pe.machine" in sys.modules
        if not engine:
            try:
                from ..pe import machine  # noqa: F401

                engine = True
            except Exception as exc:
                problems.append(f"engine unavailable: {exc}")
        clang = shutil.which("clang")
        iconutil = shutil.which("iconutil")
        sips = shutil.which("sips")
        if not clang:
            problems.append("clang is required to build application launchers")
        if not iconutil or not sips:
            problems.append("iconutil and sips are required to build icons")
        if not bundle.LAUNCHER_SOURCE.exists():
            problems.append("launcher.m is missing from the Porte package")
        writable = False
        try:
            root = bundle.support_root()
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            writable = True
        except OSError:
            problems.append("the Porte support directory is not writable")
        return {
            "engine": engine,
            "modules": modules,
            "clang": bool(clang),
            "iconutil": bool(iconutil and sips),
            "writable": writable,
            "launcher_source": bundle.LAUNCHER_SOURCE.exists(),
            "problems": problems,
            "ok": not problems,
            "support_root": str(bundle.support_root()),
        }

    def _scan_roots(self, dest: Path | None = None) -> list[Path]:
        roots = [Path.home() / "Applications", Path("/Applications")]
        if dest is not None:
            roots.insert(0, Path(dest).expanduser())
        unique: list[Path] = []
        for root in roots:
            if root not in unique:
                unique.append(root)
        return unique

    def _installed_bundles(self, dest: Path | None = None) -> list[dict]:
        from ..app import bundle

        rows: list[dict] = []
        seen: set[str] = set()
        for base in self._scan_roots(dest):
            if not base.exists():
                continue
            for app in sorted(base.glob("*.app")):
                try:
                    info = bundle.inspect_app(app)
                except Exception:
                    continue
                bundle_id = info.get("bundle_id") or ""
                if not bundle_id.startswith("com.notomer.porte.windows."):
                    continue
                if bundle_id in seen:
                    continue
                seen.add(bundle_id)
                rows.append(info)
        return rows

    @staticmethod
    def _bundle_status(info: dict) -> tuple[str, str | None]:
        runtime = info.get("runtime") or {}
        executable = info.get("executable") or ""
        if not executable or not Path(executable).exists():
            return "damaged", "the launcher is missing from the bundle"
        if info.get("launcher_arch") != "arm64":
            return "damaged", ("the launcher is built for "
                               f"{info.get('launcher_arch')}, not arm64")
        if runtime.get("kind") == "wine":
            wine = runtime.get("wine")
            if not wine or not Path(wine).exists():
                return "runtime-missing", (
                    "the Wine runtime recorded for this application is gone")
            bottle = runtime.get("bottle")
            if not bottle or not Path(bottle).exists():
                return "runtime-missing", (
                    "the compatibility environment for this application is gone")
            return "ready", None
        python = runtime.get("python")
        if python and not Path(python).exists():
            return "runtime-missing", "the Porte runtime recorded for this app has moved"
        root = runtime.get("root")
        if root and not Path(root).exists():
            return "runtime-missing", "the Porte runtime recorded for this app is missing"
        return "ready", None

    def op_library(self, args, request_id, token) -> dict:
        from ..app import environment

        dest = Path(args["dest"]).expanduser() if args.get("dest") else None
        products = environment.list_products()
        product_by_app: dict[str, dict] = {}
        for product in products:
            for entry in product.get("bundles") or []:
                product_by_app[str(entry.get("app"))] = product

        applications = []
        for info in self._installed_bundles(dest):
            status, problem = self._bundle_status(info)
            product = product_by_app.get(info.get("app") or "")
            state_dir = Path(info.get("state_dir") or "")
            last_run = None
            state_file = state_dir / "state.json"
            if state_file.exists():
                try:
                    from .. import util

                    last_run = util.read_json(state_file).get("last_run")
                except Exception:
                    last_run = None
            applications.append({
                "id": info.get("bundle_id"),
                "bundle_id": info.get("bundle_id"),
                "name": info.get("name") or Path(info.get("app", "")).stem,
                "path": info.get("app"),
                "profile": info.get("profile"),
                "runs": info.get("runs", 0),
                "status": status,
                "problem": problem,
                "state_dir": info.get("state_dir"),
                "last_run": last_run,
                "product_key": product.get("product_key") if product else None,
                "arguments": info.get("arguments") or [],
            })

        product_rows = []
        for product in products:
            product_rows.append({
                "product_key": product.get("product_key"),
                "name": product.get("name"),
                "version": product.get("version"),
                "kind": product.get("kind"),
                "manufacturer": product.get("manufacturer") or product.get("publisher"),
                "installed_at": product.get("installed_at"),
                "environment": product.get("windows") or product.get("environment"),
                "files": len(product.get("files") or []),
                "registry_values": product.get("registry_values", 0),
                "bundles": product.get("bundles") or [],
                "skipped_executables": product.get("skipped_executables") or [],
                "warnings": product.get("warnings") or [],
                "package": product.get("package"),
            })
        return {
            "applications": applications,
            "products": product_rows,
            "roots": {"applications": str(dest or _default_dest()),
                      "scanned": [str(p) for p in self._scan_roots(dest)]},
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def op_analyze(self, args, request_id, token) -> dict:
        path = _path(args.get("path"))
        if not path.exists():
            raise RequestError("not_found", f"{path} does not exist.")
        if path.is_dir():
            raise RequestError("invalid_request", f"{path.name} is a folder, not a file.")
        self._emit(request_id, "state", state=STATE_ANALYZING,
                   message=f"Inspecting {path.name}…")
        report = self._analyze(path, token)
        self._emit(request_id, "state",
                   state=STATE_COMPLETED if report["supported"] else STATE_UNSUPPORTED,
                   message=report["summary"])
        return report

    def _analyze(self, path: Path, token: threading.Event) -> dict:
        suffix = _suffix(path)
        base = {
            "path": str(path),
            "name": path.stem,
            "suffix": suffix,
            "size": path.stat().st_size,
            "sha256": None,
            "kind": "unsupported",
            "supported": False,
            "install_supported": False,
            "run_supported": False,
            "profile": None,
            "blockers": [],
            "warnings": [],
            "imports": [],
            "missing_stubs": [],
            "notes": [],
            "icon_png": None,
            "installed": None,
            "package": None,
            "actions": [],
            "summary": "Unsupported file.",
        }
        try:
            from .. import util

            base["sha256"] = util.sha256_file(path)
        except OSError as exc:
            raise RequestError("not_readable", f"{path.name} cannot be read: {exc}")
        if token.is_set():
            raise Cancelled()

        installed = self._find_installed_by_sha(base["sha256"])
        if installed:
            base["installed"] = installed

        if suffix == ".exe":
            report = self._analyze_exe(path, base, installed)
            return report

        if suffix == ".msi":
            from ..app.msi import install as msi_install

            try:
                info = msi_install.inspect_package(path)
            except Exception as exc:
                base.update(kind="msi-invalid",
                            notes=[f"Porte could not read this package: {exc}"],
                            summary="Unreadable Windows Installer package.")
                return base
            importer_supported = bool(info.get("supported"))
            from ..backends import wine as wine_backend

            wine_status = wine_backend.backend().status()
            supported = importer_supported or wine_status.available
            base.update(
                kind="msi",
                name=info.get("name") or path.stem,
                supported=supported,
                install_supported=supported,
                runtime="engine" if importer_supported else
                        ("wine" if wine_status.available else None),
                runtime_name="Porte importer" if importer_supported else
                             ("Wine" if wine_status.available else None),
                blockers=list(info.get("blockers") or [])
                if not supported else [],
                warnings=list(info.get("warnings") or []),
                package={
                    "format": "msi",
                    "version": info.get("version"),
                    "manufacturer": info.get("manufacturer"),
                    "product_code": info.get("product_code"),
                    "files": len(info.get("files") or []),
                    "registry_values": info.get("registry_values", 0),
                    "shortcuts": info.get("shortcuts", 0),
                },
                actions=["install"] if supported else [],
                summary=(f"Windows Installer package “{info.get('name')}” "
                         f"{info.get('version') or ''}".strip()),
            )
            if not supported:
                base["summary"] = "Windows Installer package with unsupported features."
            elif not importer_supported:
                base["summary"] = (f"Windows Installer package “{info.get('name')}” "
                                   f"{info.get('version') or ''}".strip())
                base["notes"].append(
                    "Porte's own importer cannot apply this package, so the "
                    "installation runs the package through the Windows Installer "
                    "inside the Wine runtime: the vendor's own install actions "
                    "execute instead of Porte extracting files.")
                base["warnings"].extend(
                    "Porte importer: " + blocker
                    for blocker in (info.get("blockers") or [])[:3])
                if wine_status.version:
                    base["runtime_version"] = wine_status.version
            if installed:
                base["notes"].append(
                    f"Already installed as “{installed['name']}”.")
            return base

        if suffix in (".msix", ".appx"):
            from ..app import msix

            try:
                info = msix.inspect_package(path)
            except Exception as exc:
                base.update(kind="msix-invalid",
                            notes=[f"Porte could not read this package: {exc}"],
                            summary="Unreadable MSIX package.")
                return base
            identity = info.get("identity") or {}
            supported = bool(info.get("supported"))
            base.update(
                kind="msix",
                name=identity.get("name") or path.stem,
                supported=supported,
                install_supported=supported,
                blockers=list(info.get("blockers") or []),
                warnings=list(info.get("warnings") or []),
                package={
                    "format": "msix",
                    "version": identity.get("version"),
                    "publisher": identity.get("publisher"),
                    "applications": [a.get("display_name") or a.get("id")
                                     for a in info.get("applications") or []],
                    "files": len(info.get("files") or []),
                },
                actions=["install"] if supported else [],
                summary=f"MSIX package “{identity.get('name')}”",
            )
            return base

        if suffix in (".bat", ".cmd"):
            base.update(kind="batch", notes=[
                "Porte does not implement cmd.exe, so batch files cannot run.",
                "Package the program the script starts as a Windows PE instead."])
            base["summary"] = "Batch files are not supported."
            return base

        base["notes"] = [
            "Porte opens Windows executables (.exe) and installer packages "
            "(.msi, .msix, .appx).",
        ]
        base["summary"] = f"{suffix or 'This file'} is not a Windows program."
        return base

    @staticmethod
    def _backend_row(document: dict, available: bool = False) -> dict:
        return {
            "id": document.get("backend") or document.get("id"),
            "name": document.get("backend_name") or document.get("name"),
            "available": bool(document.get("available", available)),
            "supported": bool(document.get("supported")),
            "run_supported": bool(document.get("run_supported")),
            "install_supported": bool(document.get("install_supported")),
            "reason": document.get("reason"),
            "version": document.get("runtime_version"),
            "blockers": list(document.get("blockers") or []),
            "warnings": list(document.get("warnings") or []),
        }

    def _analyze_exe(self, path: Path, base: dict, installed: dict | None) -> dict:
        """Analyze an executable against every runtime backend.

        The two backends are asked separately and their answers are kept
        separate: the engine may say "no stub for user32" while Wine says the
        program is entirely ordinary. The report names which runtime would
        execute the file, and never merges one backend's limits into the
        other's verdict.
        """
        from ..backends import engine as engine_backend
        from ..backends import wine as wine_backend

        engine_doc: dict
        wine_doc: dict
        try:
            engine_doc = engine_backend.backend().analyze(path)
        except Exception as exc:
            engine_doc = {"backend": "engine", "backend_name": "Porte engine",
                          "supported": False, "run_supported": False,
                          "install_supported": False, "import_names": [],
                          "missing_stubs": [], "profile": None,
                          "installer_like": False,
                          "blockers": [f"{type(exc).__name__}: {exc}"],
                          "warnings": [], "notes": [],
                          "reason": f"{type(exc).__name__}: {exc}"}
        try:
            wine_doc = wine_backend.backend().analyze(path)
        except Exception as exc:
            wine_doc = {"backend": "wine", "backend_name": "Wine",
                        "supported": False, "run_supported": False,
                        "install_supported": False, "dlls": [],
                        "missing_dlls": [], "is_gui": True,
                        "blockers": [f"{type(exc).__name__}: {exc}"],
                        "warnings": [], "notes": [],
                        "reason": f"{type(exc).__name__}: {exc}"}

        engine_ok = bool(engine_doc.get("supported"))
        wine_ok = bool(wine_doc.get("supported"))
        engine_run = bool(engine_doc.get("run_supported"))
        wine_run = bool(wine_doc.get("run_supported"))
        # The engine treats any user32 import as installer-shaped; the Wine
        # backend has a precise test (setup/MSI APIs or a setup-like name), so
        # the backend that would actually run the program decides.
        installer_like = bool(
            wine_doc.get("installer_like") if (wine_ok and not engine_ok)
            else engine_doc.get("installer_like"))
        wine_status = wine_backend.backend().status()

        # Selection follows capability, not preference: an engine that can read
        # an image but cannot execute it must not hide a runtime that can.
        if engine_ok and engine_run:
            selected = "engine"
        elif wine_ok:
            selected = "wine"
        elif engine_ok:
            selected = "engine"
        else:
            selected = None
        selected_doc = {"engine": engine_doc, "wine": wine_doc}.get(selected, {})
        engine_status = engine_backend.backend().status()
        base["backends"] = [
            self._backend_row(engine_doc, engine_status.available),
            self._backend_row(wine_doc, wine_status.available),
        ]
        base["runtime"] = selected
        base["runtime_name"] = ("Porte engine" if selected == "engine" else
                                ("Wine" if selected == "wine" else None))
        base["runtime_version"] = (wine_status.version
                                   if selected == "wine" else None)
        base["profile"] = engine_doc.get("profile") or \
            ("wine-gui" if wine_doc.get("is_gui") else "wine-console")
        base["imports"] = (engine_doc.get("import_names")
                           if engine_ok else (wine_doc.get("dlls") or []))
        base["missing_stubs"] = list(engine_doc.get("missing_stubs") or [])
        base["icon_png"] = _encode_icon(path)
        base["notes"] = list(wine_doc.get("notes") or [])
        base["warnings"] = list(wine_doc.get("warnings") or [])

        if engine_ok:
            base["supported"] = True
            base["install_supported"] = not installer_like or bool(
                engine_doc.get("install_supported"))
            base["run_supported"] = bool(selected_doc.get("run_supported"))
        elif wine_ok:
            base["supported"] = True
            # A Windows installer is a program, not a package: running it starts
            # its own setup UI, which Porte cannot track. Bundling it would be
            # wrong, so it is runnable but not installable as an app.
            base["install_supported"] = not installer_like
            base["run_supported"] = True
        else:
            base["supported"] = False
            base["install_supported"] = False
            base["run_supported"] = False

        if base["supported"]:
            base["kind"] = "exe-portable"
            actions = []
            if base["install_supported"]:
                actions.append("install")
            if base["run_supported"]:
                actions.append("run")
            base["actions"] = actions
            if selected == "wine":
                base["summary"] = (
                    "Windows application. It runs in the Wine compatibility "
                    "runtime and can be installed as a macOS application."
                    if base["install_supported"] else
                    "Windows installer. Running it starts the vendor's own setup "
                    "program inside the compatibility runtime.")
                base["notes"].append(
                    f"Executed by the Wine runtime "
                    f"{wine_status.version or ''}".strip() +
                    ("; the Porte engine has no Win32 window surface for it."
                     if not engine_ok else "."))
                if installer_like:
                    base["notes"].append(
                        "Programs this installer adds stay inside its own Windows "
                        "environment; Porte does not track or uninstall them. Use "
                        "the vendor's .msi when one is available for a tracked "
                        "installation.")
            else:
                base["summary"] = (
                    "Portable Windows application. It can be run or installed as "
                    "a macOS application.")
            if base["missing_stubs"]:
                base["warnings"].append(
                    "The Porte engine has no stub for: " +
                    ", ".join(base["missing_stubs"][:8]) +
                    " (this does not limit the Wine runtime)")
        else:
            if installer_like:
                base["kind"] = "exe-installer"
                base["summary"] = "A Windows installer that no runtime can execute here."
            else:
                base["kind"] = "exe-unsupported"
                base["summary"] = "Windows executable no available runtime can run."
            reasons = [row["reason"] for row in base["backends"] if row["reason"]]
            base["blockers"] = (list(wine_doc.get("blockers") or []) or
                                list(engine_doc.get("blockers") or []) or
                                reasons)
            if not wine_status.available and wine_status.reasons:
                base["notes"].append(wine_status.reasons[0])

        if installed:
            base["notes"].append(f"Already installed as “{installed['name']}”.")
        return base

    def _find_installed_by_sha(self, sha: str | None) -> dict | None:
        if not sha:
            return None
        from ..app import environment

        for product in environment.list_products():
            if product.get("package_sha256") == sha:
                return {"product_key": product.get("product_key"),
                        "name": product.get("name"),
                        "version": product.get("version"),
                        "kind": product.get("kind")}
        for info in self._installed_bundles():
            metadata = Path(info.get("app", "")) / "Contents" / "Resources" / "PorteApp.json"
            if not metadata.exists():
                continue
            try:
                from .. import util

                doc = util.read_json(metadata)
            except Exception:
                continue
            if doc.get("pe_sha256") == sha:
                return {"bundle_id": info.get("bundle_id"),
                        "name": info.get("name"), "kind": "exe"}
        return None

    def op_install(self, args, request_id, token) -> dict:
        path = _path(args.get("path"))
        if not path.exists():
            raise RequestError("not_found", f"{path} does not exist.")
        if not args.get("consent"):
            raise RequestError(
                "consent_required",
                "Installing a Windows program requires explicit confirmation.")
        force = bool(args.get("force"))
        dest = Path(args["dest"]).expanduser() if args.get("dest") else None
        name = args.get("name") or None
        if dest is not None:
            dest.mkdir(parents=True, exist_ok=True)

        report = self._analyze(path, token)
        if not report["install_supported"]:
            raise RequestError("unsupported", report["summary"],
                               {"report": report})
        self._emit(request_id, "state", state=STATE_PREPARING,
                   message="Preparing the compatibility environment…")
        self._emit(request_id, "progress", fraction=None,
                   message="Applying package contents")
        if token.is_set():
            raise Cancelled()
        suffix = _suffix(path)
        self._emit(request_id, "state", state=STATE_INSTALLING,
                   message="Installing…")
        started = time.perf_counter()
        preferred = args.get("runtime") or "auto"
        backend_used = report.get("runtime")
        if suffix == ".exe":
            from ..backends import choose_for_install

            backend, document = choose_for_install(path, preferred)
            backend_used = backend.id
            self._emit(request_id, "log",
                       message=f"Using the {backend.name} runtime")
            kwargs = dict(name=name, dest=dest, accept=True, force=force,
                          arguments=args.get("arguments") or None,
                          include_dlls=not args.get("no_dlls", False))
            if backend.id == "wine":
                kwargs["include_siblings"] = not args.get("no_siblings", False)
            result = backend.install(path, **kwargs)
            bundles = [{"app": result["app"],
                        "bundle_id": result["bundle_id"],
                        "name": report["name"] or path.stem}]
            product = None
            environment_path = result.get("bottle")
            skipped: list = []
            warnings: list = []
        elif suffix == ".msi":
            from ..app.msi import install as msi_install
            from ..backends import wine as wine_backend

            use_wine = preferred == "wine" or (
                preferred in ("auto", None, "") and
                report.get("runtime") != "engine")
            if use_wine:
                backend_used = "wine"
                self._emit(request_id, "log",
                           message="Running the package through the Windows "
                                   "Installer inside the Wine runtime")
                result = wine_backend.backend().install_msi(
                    path, dest=dest, force=force, accept=True)
            else:
                backend_used = "engine"
                result = msi_install.install(
                    path, accept=True, force=force, dest=dest,
                    ignore_custom_actions=bool(args.get("allow_custom_actions")))
            bundles = [{"app": b["app"], "bundle_id": b.get("bundle_id"),
                        "name": Path(b["app"]).stem} for b in result["bundles"]]
            product = result["product"]
            environment_path = result["environment"]
            skipped = result.get("skipped_executables") or []
            warnings = result.get("warnings") or []
        elif suffix in (".msix", ".appx"):
            from ..app import msix

            result = msix.install(path, accept=True, force=force, dest=dest)
            bundles = [{"app": b["app"], "bundle_id": b.get("bundle_id"),
                        "name": Path(b["app"]).stem} for b in result["bundles"]]
            product = result["product"]
            environment_path = result["environment"]
            skipped = result.get("skipped_executables") or []
            warnings = result.get("warnings") or []
        else:
            raise RequestError("unsupported",
                               f"{suffix or path.name} is not a supported package.")
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        for bundle_entry in bundles:
            self._emit(request_id, "log",
                       message=f"Created {bundle_entry['app']}")
        self._emit(request_id, "state", state=STATE_COMPLETED,
                   message=("Installed" if bundles else
                            "Package installed, but no launchable application "
                            "was found."))
        return {
            "kind": suffix.lstrip("."),
            "name": report["name"],
            "product": product,
            "environment": environment_path,
            "bundles": bundles,
            "skipped_executables": skipped,
            "warnings": warnings,
            "elapsed_ms": elapsed_ms,
            "runtime": backend_used,
            "files": (result.get("files") if isinstance(result.get("files"), int)
                      else None),
        }

    def op_run(self, args, request_id, token) -> dict:
        path = _path(args.get("path"))
        if not path.exists():
            raise RequestError("not_found", f"{path} does not exist.")
        if not args.get("consent"):
            raise RequestError(
                "consent_required",
                "Running a Windows program requires explicit confirmation.")
        report = self._analyze(path, token)
        if not report["run_supported"]:
            raise RequestError(
                "unsupported",
                ("This program cannot be run directly by the engine. Install it "
                 "as a macOS application to launch it."
                 if report["supported"] else report["summary"]),
                {"report": report})
        preferred = args.get("runtime") or "auto"
        from ..backends import choose_for_run

        backend, _document = choose_for_run(path, preferred)
        if backend.id == "wine":
            return self._run_wine(request_id, token, backend, path,
                                  list(args.get("arguments") or []), report)

        self._emit(request_id, "state", state=STATE_RUNNING,
                   message="Running in the Porte engine…")
        command = [sys.executable, "-m", "porte.gui.runner", str(path)]
        command.extend(str(a) for a in (args.get("arguments") or []))
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env)
        try:
            while proc.poll() is None:
                if token.is_set():
                    proc.kill()
                    proc.wait(timeout=5)
                    raise Cancelled()
                time.sleep(0.05)
            stdout, stderr = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
        for line in stderr.splitlines():
            if line.strip():
                self._emit(request_id, "log", message=line.rstrip())
        document = None
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    document = json.loads(line)
                    break
                except ValueError:
                    continue
        if document is None:
            raise RequestError(
                "run_failed",
                f"The engine produced no result (exit status {proc.returncode}).")
        if document.get("error"):
            raise RequestError("run_failed", str(document["error"]))
        if document.get("output"):
            self._emit(request_id, "log", message=document["output"].rstrip())
        self._emit(request_id, "state", state=STATE_COMPLETED,
                   message=("Exited with status "
                            f"{document.get('exit_code')}"
                            + (f" (guest fault: {document['fault']})"
                               if document.get("fault") else "")))
        document["profile"] = report["profile"]
        return document

    def _run_wine(self, request_id, token, backend, path: Path,
                  arguments: list[str], report: dict) -> dict:
        """Run the guest as a real process with the Wine runtime.

        The window belongs to the guest; this method only supervises it, relays
        its output, and turns cancellation into a terminated guest process.
        """
        from .. import util
        from ..backends import wine as wine_backend

        self._emit(request_id, "state", state=STATE_RUNNING,
                   message=f"Running in the Wine runtime "
                           f"{backend.runtime.version}…")
        key = f"run-{util.sha256_file(path)[:12]}"
        prefix = wine_backend.ensure_bottle(backend.runtime, key)
        command = [str(backend.runtime.wine), str(path)]
        command.extend(str(a) for a in arguments)
        started = time.perf_counter()

        # The guest's output goes to files rather than pipes: Wine keeps helper
        # processes (wineserver, services.exe) that inherit descriptors, and a
        # capture pipe would only reach EOF when they exit, which they do not.
        run_dir = Path(tempfile.mkdtemp(prefix="porte-wine-run-"))
        out_path, err_path = run_dir / "stdout", run_dir / "stderr"
        lines: list[str] = []

        def emit_new(chunk_path: Path, position: int) -> int:
            try:
                with open(chunk_path, "rb") as handle:
                    handle.seek(position)
                    data = handle.read()
            except OSError:
                return position
            if not data:
                return position
            for line in data.decode("utf-8", "replace").splitlines():
                if not line.strip():
                    continue
                lines.append(line)
                if len(lines) <= 200:
                    self._emit(request_id, "log", message=line)
            return position + len(data)

        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            proc = subprocess.Popen(command, stdout=out, stderr=err,
                                    stdin=subprocess.DEVNULL,
                                    env=wine_backend.clean_env(prefix),
                                    cwd=str(path.parent))
            positions = {out_path: 0, err_path: 0}
            try:
                while proc.poll() is None:
                    if token.is_set():
                        proc.kill()
                        proc.wait(timeout=10)
                        raise Cancelled()
                    for target in (out_path, err_path):
                        positions[target] = emit_new(target, positions[target])
                    time.sleep(0.1)
            finally:
                if proc.poll() is None:
                    proc.kill()
        for target in (out_path, err_path):
            positions[target] = emit_new(target, positions[target])
        shutil.rmtree(run_dir, ignore_errors=True)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        output = "\n".join(lines)
        self._emit(request_id, "state", state=STATE_COMPLETED,
                   message=f"Exited with status {proc.returncode}")
        return {
            "backend": "wine",
            "exit_code": proc.returncode,
            "fault": None,
            "run_ms": elapsed_ms,
            "output": output,
            "output_bytes": len(output),
            "api_calls": {},
            "profile": report.get("profile"),
            "wine_version": backend.runtime.version,
            "bottle": str(prefix),
        }

    def op_launch(self, args, request_id, token) -> dict:
        from ..app import bundle

        app = self._resolve_app(args)
        self._emit(request_id, "state", state=STATE_LAUNCHING,
                   message=f"Launching {app.stem}…")
        proc = subprocess.run(["open", str(app)], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RequestError("launch_failed",
                               proc.stderr.strip() or "macOS refused to open the app.")
        self._emit(request_id, "state", state=STATE_COMPLETED,
                   message=f"Launched {app.stem}.")
        return {"app": str(app), "bundle_id": bundle.inspect_app(app)["bundle_id"]}

    def _resolve_app(self, args) -> Path:
        from ..app import bundle

        if args.get("path"):
            candidate = Path(str(args["path"])).expanduser()
            if candidate.suffix == ".app" and candidate.exists():
                return candidate
        identifier = args.get("id") or args.get("bundle_id") or args.get("path")
        if not identifier:
            raise RequestError("invalid_request", "an application is required")
        try:
            return bundle.find_app(str(identifier))
        except FileNotFoundError:
            raise RequestError("not_found",
                               f"No installed Porte application matches {identifier!r}.")

    def op_uninstall(self, args, request_id, token) -> dict:
        target = args.get("target") or "bundle"
        identifier = args.get("id") or args.get("product") or args.get("bundle_id")
        if not identifier:
            raise RequestError("invalid_request", "a product or bundle is required")
        self._emit(request_id, "state", state=STATE_PREPARING,
                   message="Removing installed files…")
        if target == "product":
            from ..app import environment
            from ..app.msi import install as msi_install
            from ..app import msix

            product = self._find_product(str(identifier))
            manifest = environment.read_manifest(product["product_key"]) or {}
            engine = msi_install if manifest.get("kind") == "msi" else msix
            result = engine.uninstall(product["product_key"])
        else:
            from ..app import bundle

            app = self._resolve_app(args)
            result = bundle.remove_app(app, purge=True)
        self._emit(request_id, "state", state=STATE_COMPLETED,
                   message="Removed.")
        return result

    def op_repair(self, args, request_id, token) -> dict:
        from ..app import environment
        from ..app.msi import install as msi_install
        from ..app import msix

        product = self._find_product(str(args.get("product") or args.get("id") or ""))
        manifest = environment.read_manifest(product["product_key"]) or {}
        engine = msi_install if manifest.get("kind") == "msi" else msix
        self._emit(request_id, "state", state=STATE_INSTALLING,
                   message="Verifying installed files…")
        result = engine.repair(product["product_key"])
        self._emit(request_id, "state", state=STATE_COMPLETED,
                   message="Repair complete.")
        return result

    def _find_product(self, identifier: str) -> dict:
        from ..app import environment

        products = environment.list_products()
        for product in products:
            if identifier in (product.get("product_key"), product.get("name")):
                return product
        lowered = identifier.lower()
        for product in products:
            if lowered == str(product.get("name") or "").lower():
                return product
        raise RequestError("not_found",
                           f"No installed product matches {identifier!r}.")

    def op_clear_caches(self, args, request_id, token) -> dict:
        """Remove only regenerable per-application caches."""
        from ..app import bundle

        freed = 0
        cleared = 0
        apps_root = bundle.support_root() / "Applications"
        if apps_root.exists():
            for cache in apps_root.glob("*/cache"):
                if not cache.is_dir():
                    continue
                for path in cache.rglob("*"):
                    if path.is_file():
                        try:
                            freed += path.stat().st_size
                        except OSError:
                            pass
                shutil.rmtree(cache, ignore_errors=True)
                cache.mkdir(parents=True, exist_ok=True)
                cleared += 1
        self._emit(request_id, "log",
                   message=f"Cleared {cleared} cache folder(s).")
        return {"freed_bytes": freed, "caches_cleared": cleared}

    def op_diagnostics(self, args, request_id, token) -> dict:
        from .. import util
        from ..app import bundle, environment

        cache_bytes = 0
        logs = []
        apps_root = bundle.support_root() / "Applications"
        if apps_root.exists():
            for app_state in apps_root.iterdir():
                cache = app_state / "cache"
                if cache.exists():
                    for path in cache.rglob("*"):
                        if path.is_file():
                            cache_bytes += path.stat().st_size
                logs_dir = app_state / "logs"
                if logs_dir.exists():
                    for record in sorted(logs_dir.glob("run-*.json"))[-5:]:
                        try:
                            logs.append(util.read_json(record))
                        except Exception:
                            continue
        products = []
        for product in environment.list_products():
            products.append({
                "product_key": product.get("product_key"),
                "name": product.get("name"),
                "version": product.get("version"),
                "kind": product.get("kind"),
                "environment": product.get("windows"),
                "bundles": product.get("bundles") or [],
            })
        from ..backends import describe

        return {
            "protocol": PROTOCOL,
            "version": _version(),
            "python": sys.executable,
            "root": str(util.ROOT),
            "support_root": str(bundle.support_root()),
            "applications_root": str(apps_root),
            "products": products,
            "cache_bytes": cache_bytes,
            "recent_runs": logs,
            "health": self._health(),
            "backends": describe(),
            "host": util.host_info(),
            "toolchain": util.toolchain_info(),
        }

    def op_runtime(self, args, request_id, token) -> dict:
        """Runtime setup operations: status, verification, and a real smoke test.

        Porte never downloads or installs a compatibility runtime. This op
        reports what is present, verifies the pieces a user-installed runtime
        needs to have, and can execute a harmless guest command to prove the
        runtime actually starts programs.
        """
        from .. import util
        from ..backends import describe
        from ..backends import wine as wine_backend

        action = args.get("action") or "status"
        if action == "status":
            return {"backends": describe(), "host": util.host_info(),
                    "rosetta": wine_backend.rosetta_available()}

        if action == "verify":
            backend_id = args.get("backend") or "wine"
            checks: list[dict] = []
            if backend_id == "engine":
                from ..backends import engine as engine_backend

                status = engine_backend.backend().status()
                checks.append({"check": "engine modules", "ok": status.available,
                               "detail": "; ".join(status.reasons) or
                                         "pefile, unicorn and capstone import"})
                return {"backend": "engine", "checks": checks,
                        "status": status.to_dict()}

            status = wine_backend.backend().status()
            override = args.get("path") or None
            runtime = None
            try:
                runtime = wine_backend.select(override)
            except Exception as exc:
                checks.append({"check": "runtime discovered", "ok": False,
                               "detail": str(exc)})
            if runtime is not None:
                checks.append({"check": "runtime discovered", "ok": True,
                               "detail": f"{runtime.version} at {runtime.wine}"})
                checks.append({"check": "version query", "ok": True,
                               "detail": runtime.raw_version or runtime.version})
                for tool in ("wineboot", "wineserver", "msiexec"):
                    path = runtime.wine.parent / tool
                    checks.append({"check": f"{tool} present",
                                   "ok": path.exists(), "detail": str(path)})
                rosetta = wine_backend.rosetta_available()
                checks.append({"check": "Rosetta 2 available", "ok": rosetta,
                               "detail": "x86-64 Wine needs Rosetta 2 on "
                                         "Apple silicon"})
                writable = False
                bottle_root = wine_backend.bottles_root()
                try:
                    bottle_root.mkdir(parents=True, exist_ok=True)
                    probe = bottle_root / ".write-probe"
                    probe.write_text("ok", encoding="utf-8")
                    probe.unlink()
                    writable = True
                except OSError as exc:
                    checks.append({"check": "bottle storage writable",
                                   "ok": False, "detail": str(exc)})
                if writable:
                    checks.append({"check": "bottle storage writable",
                                   "ok": True, "detail": str(bottle_root)})
                if args.get("smoke"):
                    started = time.perf_counter()
                    try:
                        prefix = wine_backend.ensure_bottle(runtime, "verify")
                        proc = wine_backend.run_command(
                            [str(runtime.wine), "cmd", "/c", "echo porte-runtime-ok"],
                            env=wine_backend.clean_env(prefix), timeout=180)
                        ok = "porte-runtime-ok" in (proc.stdout or "")
                        checks.append({"check": "guest program executed",
                                       "ok": ok,
                                       "detail": (proc.stdout or proc.stderr or
                                                  "no output").strip()[:300]})
                    except Exception as exc:
                        checks.append({"check": "guest program executed",
                                       "ok": False, "detail": str(exc)})
                    checks[-1]["ms"] = round(
                        (time.perf_counter() - started) * 1000.0, 1)
            return {"backend": "wine", "checks": checks,
                    "status": status.to_dict(),
                    "passed": all(check["ok"] for check in checks)}

        raise RequestError("invalid_request",
                           f"unknown runtime action {action!r}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("--version", "-V"):
        print(f"porte.gui.bridge protocol {PROTOCOL}, version {_version()}")
        return 0
    if argv and argv[0] in ("--help", "-h"):
        print(__doc__)
        return 0
    return Bridge().run()


if __name__ == "__main__":
    raise SystemExit(main())
