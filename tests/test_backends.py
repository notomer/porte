"""Tests for the runtime backend layer that do not need a Wine install.

They cover the registry, the per-backend analysis contract, the Wine registry
parser (with the exact syntax Wine writes, including section timestamps and
typed values), path mapping inside a prefix, and the bridge's backend-aware
`hello`/`analyze`/`runtime` operations.

A Wine-dependent integration test lives in tests/test_wine_backend.py and is
skipped, with a reason, when no runtime is present.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAME_EXE = ROOT / "build" / "frame" / "frame_win.exe"
GUI_EXE = ROOT / "build" / "win32" / "porte_gui.exe"


class BackendRegistryTests(unittest.TestCase):
    def test_registry_reports_both_backends(self):
        from porte.backends import describe

        rows = {row["id"]: row for row in describe()}
        self.assertEqual(set(rows), {"engine", "wine"})
        for row in rows.values():
            self.assertIn("available", row)
            self.assertIn("license", row)
            self.assertIn("reasons", row)
            self.assertTrue(row["license"])

    def test_engine_backend_is_available_and_never_claims_gui(self):
        from porte.backends import engine

        status = engine.backend().status()
        self.assertTrue(status.available)
        self.assertFalse(status.usable_for_gui)

    def test_wine_backend_reports_rosetta_requirement(self):
        from porte.backends import wine

        status = wine.backend().status()
        self.assertTrue(status.requires_rosetta)
        self.assertIn("LGPL", status.license)
        if not status.available:
            self.assertTrue(status.reasons, "an unavailable runtime must say why")

    def test_unknown_backend_is_rejected(self):
        from porte.backends import BackendError, get

        with self.assertRaises(BackendError):
            get("dosbox")


@unittest.skipUnless(FRAME_EXE.exists(), "run ./fixtures/frame/build.sh first")
class CrossBackendAnalysisTests(unittest.TestCase):
    def test_engine_only_image_prefers_the_engine(self):
        from porte.backends import analyze

        document = analyze(FRAME_EXE)
        rows = {row["id"]: row for row in document["backends"]}
        self.assertTrue(rows["engine"]["supported"])
        # portegpu.dll is not a Windows DLL and is not next to the image, so a
        # real Wine runtime could not load it either.
        self.assertFalse(rows["wine"]["supported"])
        self.assertIn("portegpu.dll", " ".join(rows["wine"]["blockers"]))

    def test_forced_backend_is_respected(self):
        from porte.backends import analyze

        document = analyze(FRAME_EXE, preferred="wine")
        self.assertEqual(len(document["backends"]), 1)
        self.assertEqual(document["backends"][0]["id"], "wine")


@unittest.skipUnless(GUI_EXE.exists(), "run ./fixtures/win32/build.sh first")
class GuiImageAnalysisTests(unittest.TestCase):
    def test_gui_image_is_understood_by_the_engine_as_unrunnable(self):
        from porte.backends import engine

        document = engine.backend().analyze(GUI_EXE)
        self.assertFalse(document["supported"])
        self.assertTrue(document["missing_stubs"])
        self.assertIn("CreateWindowExA", document["missing_stubs"])

    def test_wine_analysis_describes_a_real_gui_program(self):
        from porte.backends import wine

        document = wine.backend().analyze(GUI_EXE)
        if not wine.backend().status().available:
            self.assertFalse(document["supported"])
            self.assertTrue(document["blockers"])
            return
        self.assertTrue(document["supported"], document["blockers"])
        self.assertTrue(document["is_gui"])
        self.assertEqual(document["machine"], "x86-64")
        self.assertEqual(document["subsystem"], "windows")
        self.assertEqual(document["missing_dlls"], [])


class WineRegistryTests(unittest.TestCase):
    """The parser must read what Wine actually writes, not a tidy format."""

    REGISTRY = (
        "WINE REGISTRY Version 2\n"
        ";; All keys relative to \\\\Machine\n"
        "\n"
        "[Software\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Uninstall\\\\{AAAA}] 1793306368\n"
        "#time=1dd67e597de2c36\n"
        '"DisplayName"="7-Zip 26.05 (x64 edition)"\n'
        '"DisplayVersion"="26.05.00.0"\n'
        '"InstallLocation"="C:\\\\Program Files\\\\7-Zip\\\\"\n'
        '"UninstallString"="MsiExec.exe /I{23170F69-40C1-2702-2605-000001000000}"\n'
        '"EstimatedSize"=dword:00001234\n'
        "\n"
        "[Software\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Uninstall\\\\{BBBB}] 1793306369\n"
        '"DisplayName"=str(2):"Typed Name"\n'
        '"NoDisplayName"=str(2):"ignored"\n'
    )

    def setUp(self):
        from porte.backends import wine

        self.wine = wine
        self.tmp = tempfile.TemporaryDirectory()
        self.prefix = Path(self.tmp.name)
        (self.prefix / "system.reg").write_text(self.REGISTRY, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_registry_reads_timestamps_and_types(self):
        registry = self.wine.parse_registry(self.prefix / "system.reg")
        key = ("Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\"
               "{AAAA}")
        self.assertIn(key, registry)
        values = registry[key]
        self.assertEqual(values["DisplayName"], "7-Zip 26.05 (x64 edition)")
        self.assertEqual(values["EstimatedSize"], "4660")
        self.assertEqual(values["InstallLocation"], "C:\\Program Files\\7-Zip\\")

    def test_installed_programs_finds_products_and_product_codes(self):
        programs = self.wine.installed_programs(self.prefix)
        names = {program["name"] for program in programs}
        self.assertIn("7-Zip 26.05 (x64 edition)", names)
        self.assertIn("Typed Name", names)
        seven = next(p for p in programs if p["name"].startswith("7-Zip"))
        self.assertEqual(seven["product_code"],
                         "{23170F69-40C1-2702-2605-000001000000}")
        self.assertEqual(seven["publisher"] if "publisher" in seven else None,
                         None)

    def test_dos_paths_map_into_the_prefix(self):
        (self.prefix / "dosdevices").mkdir()
        (self.prefix / "drive_c").mkdir()
        try:
            os.symlink("../drive_c", self.prefix / "dosdevices" / "c:")
        except OSError as exc:  # pragma: no cover - unusual filesystem
            self.skipTest(f"cannot create dosdevices symlink: {exc}")
        target = self.wine.dos_to_unix(self.prefix,
                                       "C:\\Program Files\\7-Zip\\7zFM.exe")
        self.assertIsNotNone(target)
        self.assertEqual(target.name, "7zFM.exe")
        self.assertIn("drive_c", str(target))
        self.assertEqual(self.wine.dos_to_unix(self.prefix, "/tmp/x.exe"), None)


class DiscoveryTests(unittest.TestCase):
    def _fake_runtime(self, base: Path) -> Path:
        root = base / "Wine Test.app" / "Contents" / "Resources" / "wine"
        (root / "bin").mkdir(parents=True)
        wine = root / "bin" / "wine"
        wine.write_text("#!/bin/sh\necho wine-9.9 (Test)\n")
        wine.chmod(0o755)
        return root

    def test_which_wine_and_root_are_derived_from_the_binary(self):
        from porte.backends import base as base_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = self._fake_runtime(Path(tmp))
            binary = base_mod.which_wine(root)
            self.assertEqual(binary, root / "bin" / "wine")
            self.assertEqual(base_mod.wine_distribution_root(binary), root)

    def test_discovery_and_version_parsing(self):
        from porte.backends import wine

        with tempfile.TemporaryDirectory() as tmp:
            root = self._fake_runtime(Path(tmp))
            original = wine.candidate_roots
            wine.candidate_roots = lambda: [root]
            try:
                found = wine.discover()
            finally:
                wine.candidate_roots = original
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].version, "9.9")
            self.assertEqual(found[0].kind, "wine")
            self.assertTrue(found[0].redistributable)


class BridgeBackendTests(unittest.TestCase):
    """The bridge must expose both backends and stay protocol-pure."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        env["PORTE_SUPPORT_ROOT"] = str(base / "support")
        env["PORTE_ENV_ROOT"] = str(base / "installed")
        env["PORTE_BOTTLE_ROOT"] = str(base / "bottles")
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "porte.gui.bridge"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env, cwd=str(ROOT))
        cls.ready = json.loads(cls.proc.stdout.readline())
        cls.counter = 0

    @classmethod
    def tearDownClass(cls):
        cls.proc.stdin.close()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.tmp.cleanup()

    def request(self, op: str, args: dict | None = None) -> dict:
        type(self).counter += 1
        request_id = f"b{self.counter}"
        self.proc.stdin.write(json.dumps({"id": request_id, "op": op,
                                          "args": args or {}}) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            self.assertTrue(line, "the bridge stopped answering")
            message = json.loads(line)
            if message.get("id") == request_id and \
                    message.get("event") in ("result", "error"):
                return message

    def test_hello_lists_backends_and_capabilities(self):
        data = self.request("hello")["data"]
        ids = {row["id"] for row in data["backends"]}
        self.assertEqual(ids, {"engine", "wine"})
        self.assertIn("run_gui", data["capabilities"])
        self.assertFalse(data["capabilities"]["downloads_runtime"])

    def test_runtime_status_and_engine_verification(self):
        data = self.request("runtime", {"action": "status"})["data"]
        self.assertEqual({row["id"] for row in data["backends"]},
                         {"engine", "wine"})
        self.assertIn("rosetta", data)
        verify = self.request("runtime",
                              {"action": "verify", "backend": "engine"})
        self.assertEqual(verify["event"], "result", verify)
        self.assertTrue(verify["data"]["checks"][0]["ok"])

    @unittest.skipUnless(GUI_EXE.exists(), "run ./fixtures/win32/build.sh first")
    def test_gui_image_analysis_names_the_runtime(self):
        data = self.request("analyze", {"path": str(GUI_EXE)})["data"]
        self.assertTrue(data["supported"], data.get("blockers"))
        self.assertEqual(len(data["backends"]), 2)
        ids = {row["id"] for row in data["backends"]}
        self.assertEqual(ids, {"engine", "wine"})
        wine_row = next(row for row in data["backends"] if row["id"] == "wine")
        if wine_row["available"]:
            self.assertEqual(data["runtime"], "wine")
        self.assertIn("install", data["actions"])

    def test_run_without_consent_is_refused_even_for_wine(self):
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as handle:
            handle.write(b"MZ" + b"\0" * 200)
            path = handle.name
        try:
            message = self.request("run", {"path": path, "runtime": "wine"})
            self.assertEqual(message["event"], "error")
            self.assertEqual(message["code"], "consent_required")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
