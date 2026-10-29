"""Protocol tests for the GUI bridge (`porte.gui.bridge`).

These are fast, real subprocess tests: they start the bridge the same way the
native app does, speak protocol 1 over stdin/stdout, and check that only
protocol documents appear on stdout. Installing and running are exercised when
the fixtures have been built; otherwise those cases are skipped explicitly
rather than silently passing.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAME_EXE = ROOT / "build" / "frame" / "frame_win.exe"
CRC_EXE = ROOT / "fixtures" / "opaque" / "out" / "crc.exe"
MSI = ROOT / "build" / "installer" / "PorteFixture.msi"


class BridgeProcess:
    """A bridge child process with request/response helpers."""

    def __init__(self, extra_env: dict | None = None):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        for key in ("PORTE_SUPPORT_ROOT", "PORTE_ENV_ROOT"):
            env.setdefault(key, str(Path(tempfile.gettempdir()) / "porte-gui-tests"))
        if extra_env:
            env.update(extra_env)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "porte.gui.bridge"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env, cwd=str(ROOT))
        self._lines: queue.Queue = queue.Queue()
        self._raw_stdout: list[str] = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self.ready = self._next(timeout=20)
        self._counter = 0

    def _read(self) -> None:
        for line in self.proc.stdout:
            self._raw_stdout.append(line)
            self._lines.put(json.loads(line))

    def _next(self, timeout: float = 30) -> dict:
        try:
            return self._lines.get(timeout=timeout)
        except queue.Empty:
            stderr = self.proc.stderr.read() if self.proc.poll() is not None else ""
            raise AssertionError(f"bridge produced no message; stderr={stderr!r}")

    def request(self, op: str, args: dict | None = None,
                timeout: float = 60) -> dict:
        self._counter += 1
        request_id = f"t{self._counter}"
        payload = {"id": request_id, "op": op, "args": args or {}}
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        events = []
        while True:
            message = self._next(timeout=timeout)
            self.assert_protocol_line(message)
            if message.get("id") != request_id:
                events.append(message)
                continue
            if message.get("event") in ("result", "error"):
                return {"message": message, "events": events}
            events.append(message)

    @staticmethod
    def assert_protocol_line(message: dict) -> None:
        assert isinstance(message, dict), message
        assert "event" in message, message

    def stdout_lines_are_json(self) -> bool:
        for line in self._raw_stdout:
            try:
                json.loads(line)
            except ValueError:
                return False
        return True

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc.stdout.close()
        self.proc.stderr.close()


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.bridge = BridgeProcess(extra_env={
            "PORTE_SUPPORT_ROOT": str(base / "support"),
            "PORTE_ENV_ROOT": str(base / "installed"),
        })

    @classmethod
    def tearDownClass(cls):
        cls.bridge.close()
        cls.tmp.cleanup()

    def test_ready_handshake(self):
        self.assertEqual(self.bridge.ready["event"], "ready")
        self.assertEqual(self.bridge.ready["protocol"], 1)
        self.assertTrue(self.bridge.ready["python"])

    def test_hello_reports_real_capabilities_and_health(self):
        response = self.bridge.request("hello")
        self.assertEqual(response["message"]["event"], "result")
        data = response["message"]["data"]
        self.assertEqual(data["protocol"], 1)
        self.assertTrue(data["health"]["engine"])
        self.assertTrue(data["health"]["writable"])
        self.assertTrue(data["capabilities"]["install_exe"])
        self.assertTrue(data["capabilities"]["install_msi"])
        self.assertFalse(data["capabilities"]["msi_custom_actions"])
        self.assertFalse(data["capabilities"]["authenticode"])
        self.assertIn("support_root", data)

    def test_stdout_carries_only_protocol_documents(self):
        self.bridge.request("library")
        self.bridge.request("diagnostics")
        self.assertTrue(self.bridge.stdout_lines_are_json(),
                        "engine output leaked onto the protocol stream")

    def test_missing_file_is_a_typed_error(self):
        response = self.bridge.request("analyze", {"path": "/nonexistent/a.exe"})
        self.assertEqual(response["message"]["event"], "error")
        self.assertEqual(response["message"]["code"], "not_found")

    def test_non_windows_file_is_reported_unsupported(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as handle:
            handle.write(b"hello, not a PE\n")
            path = handle.name
        try:
            response = self.bridge.request("analyze", {"path": path})
        finally:
            os.unlink(path)
        data = response["message"]["data"]
        self.assertFalse(data["supported"])
        self.assertEqual(data["kind"], "unsupported")
        self.assertEqual(data["actions"], [])

    def test_garbage_exe_is_reported_not_crashed(self):
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as handle:
            handle.write(b"MZ" + b"\0" * 200)
            path = handle.name
        try:
            response = self.bridge.request("analyze", {"path": path})
        finally:
            os.unlink(path)
        self.assertEqual(response["message"]["event"], "result")
        self.assertFalse(response["message"]["data"]["supported"])

    def test_install_and_run_require_consent(self):
        # Any existing file is enough: consent is checked before the image is
        # inspected, and this must hold on a checkout without built fixtures.
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as handle:
            handle.write(b"MZ" + b"\0" * 128)
            path = handle.name
        try:
            for op in ("install", "run"):
                response = self.bridge.request(op, {"path": path})
                self.assertEqual(response["message"]["event"], "error")
                self.assertEqual(response["message"]["code"], "consent_required")
        finally:
            os.unlink(path)

    def test_unknown_operation_is_typed(self):
        response = self.bridge.request("teleport", {})
        self.assertEqual(response["message"]["event"], "error")
        self.assertEqual(response["message"]["code"], "unknown_op")

    def test_library_is_safe_with_no_installs(self):
        data = self.bridge.request("library")["message"]["data"]
        self.assertIsInstance(data["applications"], list)
        self.assertIsInstance(data["products"], list)

    def test_cache_clear_reports_real_numbers(self):
        data = self.bridge.request("clear_caches")["message"]["data"]
        self.assertGreaterEqual(data["freed_bytes"], 0)
        self.assertGreaterEqual(data["caches_cleared"], 0)
        self.assertIn("freed_bytes", data)

    @unittest.skipUnless(FRAME_EXE.exists(), "run ./fixtures/frame/build.sh first")
    def test_portable_image_analyzes_with_actions(self):
        data = self.bridge.request("analyze", {"path": str(FRAME_EXE)})["message"]["data"]
        self.assertTrue(data["supported"], data.get("blockers"))
        self.assertEqual(data["kind"], "exe-portable")
        self.assertIn("install", data["actions"])
        self.assertTrue(data["icon_png"] or data["profile"])
        self.assertEqual(len(data["sha256"]), 64)

    @unittest.skipUnless(CRC_EXE.exists(), "run ./fixtures/opaque/gen.py first")
    def test_run_executes_in_a_child_and_reports_output(self):
        response = self.bridge.request(
            "run", {"path": str(CRC_EXE), "consent": True, "arguments": ["262144"]},
            timeout=120)
        message = response["message"]
        self.assertEqual(message["event"], "result", message)
        self.assertEqual(message["data"]["exit_code"], 0)
        self.assertTrue(message["data"]["output"].strip())
        self.assertIsNone(message["data"]["fault"])


@unittest.skipUnless(MSI.exists(), "run ./fixtures/installer/build.sh first")
class BridgeInstallerTests(unittest.TestCase):
    """A real MSI install through the bridge, into a temporary environment."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.dest = base / "Applications"
        self.bridge = BridgeProcess(extra_env={
            "PORTE_SUPPORT_ROOT": str(base / "support"),
            "PORTE_ENV_ROOT": str(base / "installed"),
        })

    def tearDown(self):
        self.bridge.close()
        self.tmp.cleanup()

    def test_msi_install_produces_bundle_and_library_entry(self):
        analysis = self.bridge.request("analyze", {"path": str(MSI)})["message"]
        self.assertEqual(analysis["event"], "result", analysis)
        self.assertTrue(analysis["data"]["supported"], analysis["data"]["blockers"])
        self.assertEqual(analysis["data"]["kind"], "msi")

        result = self.bridge.request("install", {
            "path": str(MSI), "consent": True, "dest": str(self.dest),
        }, timeout=180)["message"]
        self.assertEqual(result["event"], "result", result)
        data = result["data"]
        self.assertEqual(data["name"], "Porte Installer Fixture")
        self.assertEqual(len(data["bundles"]), 1)
        app = Path(data["bundles"][0]["app"])
        self.assertTrue(app.exists())
        info_plist = app / "Contents" / "Info.plist"
        self.assertTrue(info_plist.exists())
        metadata = json.loads(
            (app / "Contents" / "Resources" / "PorteApp.json").read_text())
        self.assertTrue(metadata["bundle_id"].startswith("com.notomer.porte.windows."))
        self.assertIn("runtime", metadata)
        # With a Windows runtime present the package is installed by its own
        # Windows Installer, so every detected program is launchable. Only the
        # importer fallback (no runtime on this machine) leaves executables
        # that it could not turn into applications.
        self.assertIn(data["runtime"], ("wine", "engine"))
        if data["runtime"] == "wine":
            self.assertFalse(data["skipped_executables"])
            self.assertEqual(metadata["runtime"]["kind"], "wine")
        else:
            self.assertTrue(any(data["skipped_executables"]))

        library = self.bridge.request("library", {"dest": str(self.dest)})["message"]["data"]
        names = {app["name"] for app in library["applications"]}
        self.assertIn("Porte Installer Fixture", names)
        self.assertTrue(any(p["name"] == "Porte Installer Fixture"
                            for p in library["products"]))
        entry = next(a for a in library["applications"]
                     if a["name"] == "Porte Installer Fixture")
        self.assertEqual(entry["status"], "ready")
        self.assertIsNotNone(entry["product_key"])

        removed = self.bridge.request(
            "uninstall", {"target": "product", "id": data["product"]},
            timeout=120)["message"]
        self.assertEqual(removed["event"], "result", removed)
        self.assertFalse(app.exists())
        library = self.bridge.request("library", {"dest": str(self.dest)})["message"]["data"]
        self.assertFalse(any(a["name"] == "Porte Installer Fixture"
                             for a in library["applications"]))


if __name__ == "__main__":
    unittest.main()
