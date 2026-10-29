"""Fast checks that do not need a window server or a built fixture tree.

The end-to-end importer check lives in `fixtures/installer/verify.py`; this file
covers the pieces that can be asserted in milliseconds so a pull request fails
early and precisely.
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from porte.app import bundle, environment
from porte.app.msi import install as msi_install
from porte.app.msi.cab import CabError, Cabinet
from porte.cli import main

ROOT = Path(__file__).resolve().parents[1]
MSI = ROOT / "build" / "installer" / "PorteFixture.msi"
MSIX = ROOT / "build" / "installer" / "PorteFixture.msix"
TAMPERED = ROOT / "build" / "installer" / "PorteFixture-tampered.msix"


class IdentifierTests(unittest.TestCase):
    def test_bundle_id_uses_product_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "app.exe"
            image.write_bytes(b"MZ" + b"\0" * 64)
            first = bundle.default_bundle_id(image, "Example App")
            again = bundle.default_bundle_id(image, "Example App")
            second = bundle.default_bundle_id(image, "Example App", identity="A")
            third = bundle.default_bundle_id(image, "Example App", identity="B")
        self.assertTrue(first.startswith("com.notomer.porte.windows."))
        self.assertEqual(first, again)
        self.assertNotEqual(second, third)

    def test_same_image_two_products_get_different_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "app.exe"
            image.write_bytes(b"MZ" + b"\0" * 64)
            a = bundle.default_bundle_id(image, "App", identity="msi-1")
            b = bundle.default_bundle_id(image, "App", identity="msix-2")
        self.assertNotEqual(a, b)

    def test_environment_rejects_escaping_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for parts in (["..", "etc"], ["a", ".", "..", "b"]):
                with self.assertRaises(environment.EnvironmentError):
                    environment.safe_join(root, parts)

    def test_absolute_part_stays_inside_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = environment.safe_join(root, ["/absolute"])
        self.assertEqual(path, root / "absolute")

    def test_environment_keeps_nested_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = environment.safe_join(root, ["AppData", "Local", "x.exe"])
        self.assertEqual(path.name, "x.exe")
        self.assertIn("AppData", str(path))


class CliTests(unittest.TestCase):
    def test_help_works_for_both_entry_points(self):
        for argv in (["--help"], ["app", "--help"]):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    main(argv)
            self.assertEqual(ctx.exception.code, 0)

    def test_legacy_alias_delegates(self):
        from porte.cli import legacy_main

        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as ctx:
                legacy_main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("previous name", err.getvalue())


class CabinetTests(unittest.TestCase):
    def test_non_cabinet_is_refused(self):
        with self.assertRaises(CabError):
            Cabinet(b"not a cabinet at all", "bad.cab")


@unittest.skipUnless(MSI.exists(), "run ./fixtures/installer/build.sh first")
class MsiParsingTests(unittest.TestCase):
    def test_plan_has_files_and_identity(self):
        info = msi_install.inspect_package(MSI)
        self.assertEqual(info["name"], "Porte Installer Fixture")
        names = {entry["name"] for entry in info["files"]}
        self.assertIn("PorteFixture.exe", names)
        self.assertIn("PorteFixtureGui.exe", names)
        self.assertEqual(info["registry_values"], 1)
        self.assertEqual(info["shortcuts"], 1)
        self.assertTrue(info["supported"], info["blockers"])

    def test_custom_action_package_is_refused(self):
        refused = ROOT / "build" / "installer" / "PorteFixture-customaction.msi"
        if not refused.exists():
            self.skipTest("custom-action fixture not built")
        info = msi_install.inspect_package(refused)
        self.assertFalse(info["supported"])
        self.assertTrue(any("custom action" in b for b in info["blockers"]))

    def test_malformed_package_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.msi"
            broken.write_bytes(b"\xd0\xcf\x11\xe0" + b"\0" * 100)
            with self.assertRaises(Exception):
                msi_install.inspect_package(broken)


@unittest.skipUnless(MSIX.exists(), "run ./fixtures/installer/build.sh first")
class MsixParsingTests(unittest.TestCase):
    def test_manifest_and_block_map(self):
        from porte.app import msix

        info = msix.inspect_package(MSIX)
        self.assertEqual(info["identity"]["name"], "Porte.MsiFixture")
        self.assertEqual(len(info["applications"]), 1)
        self.assertTrue(info["block_map"]["present"])
        self.assertTrue(info["supported"], info["blockers"])
        self.assertTrue(any("not signed" in w for w in info["warnings"]))

    def test_tampered_package_is_refused(self):
        from porte.app import msix

        if not TAMPERED.exists():
            self.skipTest("tampered fixture not built")
        with self.assertRaises(msix.PackageError):
            msix.inspect_package(TAMPERED)


class MetadataTests(unittest.TestCase):
    def test_pyproject_names_the_package(self):
        text = (ROOT / "pyproject.toml").read_text()
        self.assertIn('name = "porte"', text)
        self.assertIn('porte = "porte.cli:main"', text)

    def test_license_and_notice_exist(self):
        self.assertIn("MIT License", (ROOT / "LICENSE").read_text())
        self.assertIn("GPLv2", (ROOT / "THIRD_PARTY.md").read_text())


if __name__ == "__main__":
    unittest.main()
