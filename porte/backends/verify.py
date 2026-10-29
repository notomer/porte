"""End-to-end verification of the runtime backends against real windows.

This module answers the only question that matters for a compatibility layer:
did a genuine Windows program actually run, show a window, accept input, touch
files, and exit? It does that by launching the real macOS application bundle and
then driving the guest from *inside* Windows with `build/win32/porte_probe.exe`
(SendMessage/BM_CLICK/WM_CHAR/WM_COMMAND/GetOpenFileName). Nothing here trusts
a screenshot or a self-report from the guest: every check reads a control's text
back or waits for a real process to exit.

The macOS side adds one independent observation: `build/win32/winlist` asks the
WindowServer which windows the guest process owns, so a window that exists only
inside Wine's imagination would not pass.

All of it is used by `porte runtime verify` and by tests/test_wine_backend.py.
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .. import util
from . import wine as wine_backend

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build" / "win32"
GUI_EXE = BUILD / "porte_gui.exe"
PROBE_EXE = BUILD / "porte_probe.exe"
BENCH_EXE = BUILD / "porte_bench.exe"
WINLIST = BUILD / "winlist"
THIRD_PARTY = ROOT / "build" / "third_party"


class VerificationError(RuntimeError):
    pass


def fixtures_ready() -> bool:
    return all(path.exists() for path in (GUI_EXE, PROBE_EXE, WINLIST))


def require_fixtures() -> None:
    if not fixtures_ready():
        raise VerificationError(
            "Windows fixtures are not built; run ./fixtures/win32/build.sh")


# ---- small process helpers --------------------------------------------

def _ps() -> list[tuple[int, str]]:
    proc = subprocess.run(["ps", "-axo", "pid=,command="],
                          capture_output=True, text=True)
    rows: list[tuple[int, str]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_text, _, command = line.partition(" ")
        try:
            rows.append((int(pid_text), command.strip()))
        except ValueError:
            continue
    return rows


def terminate_stale(marker: str) -> int:
    """Stop a launcher left over from an earlier run of the same bundle."""
    stopped = 0
    for pid, command in _ps():
        if marker in command:
            try:
                os.kill(pid, signal.SIGTERM)
                stopped += 1
            except OSError:
                pass
    if stopped:
        time.sleep(1.0)
    return stopped


def find_process(needle: str) -> int | None:
    for pid, command in _ps():
        if needle in command:
            return pid
    return None


def wait_for_process(needle: str, timeout: float = 45.0) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        pid = find_process(needle)
        if pid:
            return pid
        time.sleep(0.25)
    return None


def wait_for_exit(needle: str, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if find_process(needle) is None:
            return True
        time.sleep(0.25)
    return False


def run_records(state_dir: Path) -> set[Path]:
    logs = Path(state_dir) / "logs"
    return set(logs.glob("run-*.json")) if logs.exists() else set()


def wait_for_run_record(state_dir: Path, before: set[Path],
                        timeout: float = 60.0) -> dict | None:
    """Wait for a run record written after `before` was sampled."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        fresh = run_records(state_dir) - before
        if fresh:
            try:
                return util.read_json(sorted(fresh)[-1])
            except ValueError:
                pass
        time.sleep(0.25)
    return None


def windows_frames(pid: int) -> list[dict]:
    if not WINLIST.exists():
        return []
    proc = subprocess.run([str(WINLIST), str(pid), "--all", "--json"],
                          capture_output=True, text=True)
    if proc.returncode not in (0,):
        return []
    try:
        return json.loads(proc.stdout or "[]")
    except ValueError:
        return []


def wait_for_window(pid: int, min_width: int = 320,
                    timeout: float = 45.0) -> dict | None:
    """Wait for the WindowServer to show the guest's main window.

    Wine owns several helper windows (a 500x500 desktop window, full-width
    strips); the guest's own window is the biggest one, and an on-screen window
    is preferred over one the WindowServer has created but not presented.
    """
    deadline = time.time() + timeout
    best: dict | None = None
    while time.time() < deadline:
        frames = [frame for frame in windows_frames(pid)
                  if frame.get("width", 0) >= min_width
                  and frame.get("height", 0) >= 200]
        if frames:
            frames.sort(key=lambda frame: (bool(frame.get("onscreen")),
                                           frame["width"] * frame["height"]),
                        reverse=True)
            best = frames[0]
            if best.get("onscreen"):
                return best
        if best is not None and time.time() > deadline - timeout + 2.0:
            return best
        time.sleep(0.25)
    return best


# ---- bundle helpers ----------------------------------------------------

def bundle_state_dir(app: Path) -> tuple[str, Path, dict]:
    with open(Path(app) / "Contents" / "Info.plist", "rb") as handle:
        info = plistlib.load(handle)
    bundle_id = info.get("CFBundleIdentifier", "")
    metadata = util.read_json(Path(app) / "Contents" / "Resources" /
                              "PorteApp.json")
    from ..app import bundle as bundle_mod

    return bundle_id, bundle_mod.app_state_dir(bundle_id), metadata


def seed_consent(app: Path) -> Path:
    """Record consent the way the launcher's dialog would.

    A headless verification cannot click the launcher's modal alert, so the
    consent file it writes is created directly. Everything else - the hash
    check, the runtime check, the process supervision - still runs.
    """
    bundle_id, state, metadata = bundle_state_dir(app)
    state.mkdir(parents=True, exist_ok=True)
    (state / "logs").mkdir(exist_ok=True)
    path = state / "consent.json"
    util.write_json(path, {
        "bundle_id": bundle_id,
        "pe_sha256": metadata.get("pe_sha256"),
        "accepted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "accepted_by": "verification",
    })
    return path


def run_probe(prefix: Path, *, scenario: str, title: str,
              document: str | None = None, evidence: str = "porte-evidence",
              editor: bool = False, dump: bool = False, dialog: bool = False,
              timeout: float = 300.0) -> dict:
    """Drive the guest with the Win32 probe and return its JSON log."""
    require_fixtures()
    runtime = wine_backend.select()
    log_path = Path(tempfile.mkdtemp(prefix="porte-probe-")) / "probe.json"
    command = [str(runtime.wine), str(PROBE_EXE),
               "--scenario", scenario, "--title", title,
               "--log", str(log_path), "--timeout", "60000",
               "--evidence", evidence]
    if document:
        command.extend(["--file", document])
    if editor:
        command.append("--editor")
    if dump:
        command.append("--dump")
    if dialog:
        command.append("--dialog")
    # Output goes to a file, not a pipe: Wine's helper processes inherit the
    # caller's descriptors and would hold a capture pipe open indefinitely.
    try:
        result = wine_backend.run_captured(
            command, env=wine_backend.clean_env(prefix), timeout=timeout)
    except wine_backend.BackendError as exc:
        # A probe that never returns must not leave a live prefix behind: the
        # processes it started keep the wineserver busy and every later run in
        # that environment would block too.
        wine_backend.terminate_prefix(runtime, Path(prefix))
        return {
            "scenario": scenario,
            "passed": False,
            "checks": [{"name": "probe.completed", "ok": False,
                        "skipped": False, "detail": str(exc)}],
            "probe_exit": None,
            "probe_stdout": "",
            "probe_stderr": str(exc),
        }
    document_data: dict = {}
    if log_path.exists():
        try:
            document_data = util.read_json(log_path)
        except ValueError:
            document_data = {}
    document_data["probe_exit"] = result["returncode"]
    document_data["probe_stdout"] = result["stdout"][-8000:]
    document_data["probe_stderr"] = result["stderr"][-4000:]
    return document_data


def failed_checks(report: dict) -> list[dict]:
    return [check for check in report.get("checks") or []
            if not check.get("ok")]


# ---- the fixture application ------------------------------------------

def verify_gui_fixture(dest: Path | None = None, *, keep: bool = False,
                       timeout: float = 240.0) -> dict:
    """Install the Win32 fixture as a .app, launch it, and drive its window."""
    require_fixtures()
    dest_dir = Path(dest) if dest else Path(tempfile.mkdtemp(prefix="porte-apps-"))
    backend = wine_backend.backend()
    # A throwaway environment per run: a stale wineserver from a previous run
    # must never be able to poison this one.
    bottle_key = f"verify-{uuid.uuid4().hex[:10]}"
    # The build directory also holds the probe, the benchmark and the window
    # lister; only the fixture itself belongs in this bundle.
    result = backend.install(GUI_EXE, name="Porte Win32 Fixture",
                             dest=dest_dir, force=True, include_siblings=False,
                             bottle_key=bottle_key)
    app = Path(result["app"])
    prefix = Path(result["bottle"])
    state = Path(result["state_dir"])
    seed_consent(app)
    document = Path(tempfile.mkdtemp(prefix="porte-doc-")) / "document.txt"
    document.write_text("porte-evidence\n", encoding="utf-8")

    terminate_stale(f"{app}/Contents/MacOS/")
    before = run_records(state)
    report: dict = {
        "app": str(app),
        "bundle_id": result["bundle_id"],
        "bottle": str(prefix),
        "wine_version": result.get("wine_version"),
        "checks": [],
    }
    opened = subprocess.run(["open", str(app)], capture_output=True, text=True)
    if opened.returncode != 0:
        raise VerificationError(f"macOS refused to open {app}: {opened.stderr}")
    guest_marker = f"{app}/Contents/Resources/windows/{GUI_EXE.name}"
    guest_pid = wait_for_process(guest_marker, timeout=90)
    report["checks"].append({
        "name": "bundle.launch.spawns_guest", "ok": guest_pid is not None,
        "detail": f"pid {guest_pid} running {guest_marker}"})
    if guest_pid is None:
        raise VerificationError("the generated application did not start a guest")

    frame = wait_for_window(guest_pid, min_width=400, timeout=60)
    report["checks"].append({
        "name": "window.on_screen",
        "ok": frame is not None,
        "detail": (f"{frame['width']:.0f}x{frame['height']:.0f}"
                   if frame else "the WindowServer listed no window for the guest")})

    probe = run_probe(prefix, scenario="fixture", title="Porte Win32 Fixture",
                      document=str(document), evidence="porte-evidence",
                      timeout=timeout)
    report["probe"] = probe
    checks = probe.get("checks") or []
    report["checks"].extend(checks)
    # The common-dialog leg is opt-in (see probes --dialog), so the saved-file
    # check only applies when that leg actually ran.
    if any(check["name"] == "dialog.exists" for check in checks):
        saved = document.read_text(encoding="utf-8") if document.exists() else ""
        report["checks"].append({
            "name": "file.saved_contents",
            "ok": saved.strip() == "porte-roundtrip",
            "detail": f"document now contains {saved.strip()!r}"})

    record = wait_for_run_record(state, before, timeout=90)
    report["checks"].append({
        "name": "record.written", "ok": record is not None,
        "detail": f"{len(run_records(state))} record(s)"})
    if record:
        report["record"] = record
        report["checks"].append({
            "name": "record.exit_zero",
            "ok": record.get("exit_code") == 0 and not record.get("fault"),
            "detail": f"exit={record.get('exit_code')} backend={record.get('backend')}"})
    report["checks"].append({
        "name": "bundle.exits_after_close",
        "ok": wait_for_exit(f"{app}/Contents/MacOS/", timeout=60),
        "detail": "launcher process gone"})
    report["checks"].append({
        "name": "manager.not_required",
        "ok": find_process("/build/Porte.app/Contents/MacOS/Porte") is None,
        "detail": "the Porte manager is not running"})
    report["passed"] = not [c for c in report["checks"] if not c["ok"]]
    report["failures"] = [c for c in report["checks"] if not c["ok"]]
    if not keep and report["passed"]:
        from ..app import bundle as bundle_mod

        wine_backend.terminate_prefix(backend.runtime, prefix)
        bundle_mod.remove_app(app, purge=True)
        shutil.rmtree(prefix, ignore_errors=True)
        report["cleaned"] = not app.exists()
    return report


# ---- third-party applications -----------------------------------------

def verify_portable_gui(exe: Path, title: str, dest: Path | None = None,
                        *, keep: bool = False, accuracy_note: str = "",
                        timeout: float = 240.0) -> dict:
    """Install a third-party portable program and drive its real window."""
    require_fixtures()
    exe = Path(exe)
    if not exe.exists():
        raise VerificationError(f"missing third-party image {exe}")
    dest_dir = Path(dest) if dest else Path(tempfile.mkdtemp(prefix="porte-apps-"))
    backend = wine_backend.backend()
    result = backend.install(exe, name=title, dest=dest_dir, force=True,
                             include_dlls=True,
                             bottle_key=f"verify-{uuid.uuid4().hex[:10]}")
    app = Path(result["app"])
    prefix = Path(result["bottle"])
    state = Path(result["state_dir"])
    seed_consent(app)

    terminate_stale(f"{app}/Contents/MacOS/")
    before = run_records(state)
    report: dict = {
        "app": str(app),
        "bundle_id": result["bundle_id"],
        "bottle": str(prefix),
        "wine_version": result.get("wine_version"),
        "exe": str(exe),
        "note": accuracy_note,
        "checks": [],
    }
    opened = subprocess.run(["open", str(app)], capture_output=True, text=True)
    if opened.returncode != 0:
        raise VerificationError(f"macOS refused to open {app}: {opened.stderr}")
    guest_marker = f"{app}/Contents/Resources/windows/{exe.name}"
    guest_pid = wait_for_process(guest_marker, timeout=120)
    report["checks"].append({
        "name": "bundle.launch.spawns_guest", "ok": guest_pid is not None,
        "detail": f"pid {guest_pid}"})
    if guest_pid is None:
        raise VerificationError(f"{title} did not start")
    frame = wait_for_window(guest_pid, min_width=400, timeout=90)
    report["checks"].append({
        "name": "window.on_screen", "ok": frame is not None,
        "detail": (f"{frame['width']:.0f}x{frame['height']:.0f} "
                   f"({'on screen' if frame and frame.get('onscreen') else 'created'})"
                   if frame else "no window")})
    probe = run_probe(prefix, scenario="app", title=title, editor=True,
                      timeout=timeout)
    report["probe"] = probe
    report["checks"].extend(probe.get("checks") or [])
    record = wait_for_run_record(state, before, timeout=90)
    report["checks"].append({
        "name": "record.written", "ok": record is not None,
        "detail": "guest exit recorded"})
    if record:
        report["record"] = record
        report["checks"].append({
            "name": "record.exit_zero",
            "ok": record.get("exit_code") == 0,
            "detail": f"exit={record.get('exit_code')}"})
    report["checks"].append({
        "name": "bundle.exits_after_close",
        "ok": wait_for_exit(f"{app}/Contents/MacOS/", timeout=60),
        "detail": "launcher process gone"})
    report["passed"] = not [c for c in report["checks"] if not c["ok"]]
    report["failures"] = [c for c in report["checks"] if not c["ok"]]
    if not keep and report["passed"]:
        from ..app import bundle as bundle_mod

        wine_backend.terminate_prefix(backend.runtime, prefix)
        bundle_mod.remove_app(app, purge=True)
        shutil.rmtree(prefix, ignore_errors=True)
        report["cleaned"] = not app.exists()
    return report


def verify_msi(package: Path, *, dest: Path | None = None, keep: bool = False,
               launch_title: str | None = None,
               cli_exe: str | None = None,
               cli_args: list[str] | None = None) -> dict:
    """Install a real third-party MSI through the runtime's Windows Installer."""
    package = Path(package)
    if not package.exists():
        raise VerificationError(f"missing package {package}")
    dest_dir = Path(dest) if dest else Path(tempfile.mkdtemp(prefix="porte-apps-"))
    backend = wine_backend.backend()
    result = backend.install_msi(package, dest=dest_dir, force=True)
    prefix = Path(result["environment"])
    report: dict = {
        "product": result["product"],
        "product_name": result["product_name"],
        "environment": str(prefix),
        "msiexec_status": result["msiexec_status"],
        "log": result.get("log"),
        "programs": result.get("programs"),
        "bundles": [b["app"] for b in result["bundles"]],
        "checks": [],
    }
    report["checks"].append({
        "name": "msiexec.succeeded", "ok": result["msiexec_status"] in (0, 3010),
        "detail": f"status {result['msiexec_status']}"})
    report["checks"].append({
        "name": "product.registered", "ok": bool(result["programs"]),
        "detail": ", ".join(p["name"] for p in result.get("programs") or [])})
    report["checks"].append({
        "name": "bundle.created", "ok": bool(result["bundles"]),
        "detail": f"{len(result['bundles'])} bundle(s)"})

    if cli_exe:
        runtime = wine_backend.select()
        cli_path = wine_backend.dos_to_unix(prefix, cli_exe)
        if cli_path and cli_path.exists():
            cli_result = wine_backend.run_captured(
                [str(runtime.wine), cli_exe] + [str(a) for a in (cli_args or [])],
                env=wine_backend.clean_env(prefix), timeout=300)
            report["cli"] = {"exit": cli_result["returncode"],
                             "stdout": cli_result["stdout"][-4000:],
                             "stderr": cli_result["stderr"][-2000:]}
            report["checks"].append({
                "name": "installed_console_program.runs",
                "ok": cli_result["returncode"] == 0,
                "detail": f"exit {cli_result['returncode']}: "
                          f"{(cli_result['stdout'] or '').strip().splitlines()[:2]}"})
        else:
            report["checks"].append({
                "name": "installed_console_program.runs", "ok": False,
                "detail": f"{cli_exe} was not installed"})

    if launch_title and result["bundles"]:
        app = Path(result["bundles"][0]["app"])
        seed_consent(app)
        terminate_stale(f"{app}/Contents/MacOS/")
        opened = subprocess.run(["open", str(app)], capture_output=True,
                                text=True)
        report["checks"].append({
            "name": "bundle.launches", "ok": opened.returncode == 0,
            "detail": opened.stderr.strip() or str(app)})
        # The exe stays inside the product prefix; find it through the manifest.
        metadata = util.read_json(app / "Contents" / "Resources" / "PorteApp.json")
        guest_pid = wait_for_process(metadata.get("pe", "").split("\\")[-1],
                                     timeout=120)
        report["checks"].append({
            "name": "installed_app.spawns_guest", "ok": guest_pid is not None,
            "detail": f"pid {guest_pid}"})
        if guest_pid:
            frame = wait_for_window(guest_pid, min_width=300, timeout=90)
            report["checks"].append({
                "name": "installed_app.window", "ok": frame is not None,
                "detail": (f"{frame['width']:.0f}x{frame['height']:.0f}"
                           if frame else "no window")})
            probe = run_probe(prefix, scenario="app",
                              title=launch_title, timeout=240)
            report["probe"] = probe
            report["checks"].extend(probe.get("checks") or [])
            report["checks"].append({
                "name": "installed_app.exits_cleanly",
                "ok": wait_for_exit(f"{app}/Contents/MacOS/", timeout=90),
                "detail": "launcher exited"})

    report["passed"] = not [c for c in report["checks"] if not c["ok"]]
    report["failures"] = [c for c in report["checks"] if not c["ok"]]
    if not keep and report["passed"]:
        backend.uninstall_msi(result["product"])
        report["cleaned"] = not prefix.exists()
    return report


def collect(report: dict) -> dict:
    """Flatten a verification report into a machine-readable summary."""
    return {
        "passed": report.get("passed", False),
        "checks": len(report.get("checks") or []),
        "failures": report.get("failures"),
        "probe": (report.get("probe") or {}).get("passed"),
        "probe_checks": len((report.get("probe") or {}).get("checks") or []),
    }
