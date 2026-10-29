#!/usr/bin/env python3
"""End-to-end check of the package importer.

Builds the fixtures, then:
  1. inspects the MSI database and its embedded cabinet,
  2. installs it into a temporary environment and a temporary app directory,
  3. runs the generated application headless through the real runtime,
  4. upgrades it with a package that shares the UpgradeCode,
  5. refuses a package that declares a code custom action,
  6. installs an MSIX whose block map matches, and refuses a tampered one,
  7. uninstalls both products and checks nothing was left behind.

Every assertion prints PASS or raises; the exit code is the result.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

BUILD = ROOT / "build" / "installer"
TREE = ROOT / "fixtures" / "installer" / "tree"
MESSAGE = b"PorteFixture: extracted by Porte, running as a Windows image\n"


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise SystemExit(f"FAIL: {label}{(': ' + detail) if detail else ''}")
    print(f"PASS  {label}{(': ' + detail) if detail else ''}")


def build() -> None:
    proc = subprocess.run([str(ROOT / "fixtures" / "installer" / "build.sh")],
                          capture_output=True, text=True)
    check("fixtures build", proc.returncode == 0,
          (proc.stderr or proc.stdout).strip().splitlines()[-1] if
          (proc.stderr or proc.stdout).strip() else "")


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from porte import util
    from porte.app import bundle, environment, msix
    from porte.app.msi import install as msi_install

    build()
    work = Path(tempfile.mkdtemp(prefix="porte-installer-"))
    env_root = work / "installed"
    support = work / "support"
    apps = work / "Applications"
    os.environ[environment.ENV_ROOT_VAR] = str(env_root)
    os.environ[bundle.SUPPORT_ROOT_VAR] = str(support)
    msi = BUILD / "PorteFixture.msi"
    msi_next = BUILD / "PorteFixture-1.1.0.msi"
    refused = BUILD / "PorteFixture-customaction.msi"
    package = BUILD / "PorteFixture.msix"
    tampered = BUILD / "PorteFixture-tampered.msix"
    try:
        info = msi_install.inspect_package(msi)
        names = {f["name"] for f in info["files"]}
        check("MSI metadata", info["name"] == "Porte Installer Fixture"
              and info["version"] == "1.0.0", info["product_code"])
        check("MSI file table", names == {"PorteFixture.exe",
                                          "PorteFixtureGui.exe", "readme.txt"},
              f"{len(names)} files")
        check("MSI registry/shortcut tables",
              info["registry_values"] == 1 and info["shortcuts"] == 1)
        check("MSI supported", info["supported"], str(info["blockers"]))

        first = msi_install.install(msi, accept=True, dest=apps)
        windows = Path(first["environment"]) / "windows"
        payload = windows / "AppData/Local/PorteFixture/PorteFixture.exe"
        check("cabinet extraction", util.sha256_file(payload) ==
              util.sha256_file(TREE / "PorteFixture.exe"))
        check("uncompressed payload", (windows / "AppData/Local/PorteFixture/"
                                       "readme.txt").read_bytes() ==
              b"Porte installer fixture\n")
        registry = util.read_json(Path(first["environment"]) / "registry.json")
        value = registry.get("HKEY_CURRENT_USER", {}).get(
            "Software\\PorteFixture", {}).get("Installed")
        check("registry table applied", value == {"type": "integer", "data": 1},
              json.dumps(value))
        check("one executable bundled", len(first["bundles"]) == 1,
              Path(first["bundles"][0]["app"]).name)
        check("GUI executable reported, not invented",
              [s["file"] for s in first["skipped_executables"]] ==
              ["AppData/Local/PorteFixture/PorteFixtureGui.exe"]
              and "MessageBoxA" in first["skipped_executables"][0]["missing"])

        app = Path(first["bundles"][0]["app"])
        check("bundle is a real application bundle",
              (app / "Contents" / "Info.plist").exists()
              and (app / "Contents" / "MacOS" / app.stem).exists())
        run = subprocess.run([sys.executable, "-m", "porte.app.runtime", str(app)],
                             capture_output=True, text=True, cwd=str(ROOT))
        check("runtime exit code", run.returncode == 0, run.stderr.strip()[:200])
        check("Windows image produced its output", MESSAGE.decode().strip()
              in run.stdout, " | ".join(run.stdout.strip().splitlines()[-2:]))
        state = bundle.app_state_dir(first["bundles"][0]["bundle_id"])
        logs = sorted((state / "logs").glob("run-*.json"))
        check("run recorded", bool(logs))
        record = util.read_json(logs[-1])
        check("guest output hash", record["output_sha256"] ==
              util.sha256_text(MESSAGE.decode()))
        check("workspace warp", str(env_root) in str(Path(first["environment"])))

        damaged_path = windows / "AppData/Local/PorteFixture/readme.txt"
        damaged_path.unlink()
        repaired = msi_install.repair(first["product"])
        check("repair detects damage", repaired["damaged"] ==
              ["AppData/Local/PorteFixture/readme.txt"],
              json.dumps(repaired["damaged"]))
        check("repair re-extracts the file", damaged_path.exists())
        after = environment.read_manifest(first["product"]) or {}
        check("repair keeps the application",
              len(after.get("bundles", [])) == 1 and app.exists())

        second = msi_install.install(msi_next, accept=True, dest=apps)
        check("upgrade recorded", (second["upgraded_from"] or {}).get("version")
              == "1.0.0", json.dumps(second["upgraded_from"]))
        check("old environment removed",
              not (env_root / first["product"]).exists())
        check("upgraded app relaunches", subprocess.run(
            [sys.executable, "-m", "porte.app.runtime",
             second["bundles"][0]["app"]],
            capture_output=True, text=True, cwd=str(ROOT)).returncode == 0)

        try:
            msi_install.install(refused, accept=True, dest=apps)
        except msi_install.UnsupportedPackage as exc:
            check("custom action refused",
                  any("FixturePost" in b for b in exc.blockers))
        else:
            raise SystemExit("FAIL: custom action package was installed")

        msix_info = msix.inspect_package(package)
        check("MSIX manifest parsed",
              msix_info["identity"]["name"] == "Porte.MsiFixture"
              and len(msix_info["applications"]) == 1)
        check("MSIX block map validated",
              msix_info["block_map"]["present"]
              and msix_info["block_map"]["files_checked"] == 3)
        check("MSIX signature reported as unverified",
              any("not signed" in w for w in msix_info["warnings"])
              and msix_info["supported"])
        try:
            msix.inspect_package(tampered)
        except msix.PackageError as exc:
            check("tampered MSIX refused", "hash mismatch" in str(exc))
        else:
            raise SystemExit("FAIL: tampered MSIX accepted")

        third = msix.install(package, accept=True, dest=apps)
        check("MSIX installed", third["launchable"] == ["PorteFixture.exe"],
              json.dumps(third["bundles"]))
        check("same image, distinct products, distinct state",
              third["bundles"][0]["bundle_id"] !=
              second["bundles"][0]["bundle_id"],
              third["bundles"][0]["bundle_id"])
        check("MSIX app runs", subprocess.run(
            [sys.executable, "-m", "porte.app.runtime", third["bundles"][0]["app"]],
            capture_output=True, text=True, cwd=str(ROOT)).returncode == 0)

        msi_install.uninstall(second["product"])
        msix.uninstall(third["product"])
        check("uninstall removed products",
              environment.list_products() == [])
        check("uninstall removed applications",
              sorted(p.name for p in apps.glob("*.app")) == [],
              str(list(apps.glob("*.app"))))
        check("uninstall removed state",
              not Path(second["bundles"][0]["app"]).exists()
              and not state.exists())
        print("\nall checks passed")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)
        for product in (env_root.iterdir() if env_root.exists() else []):
            shutil.rmtree(product, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
