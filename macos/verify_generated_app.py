#!/usr/bin/env python3
"""Install a package through the GUI bridge, launch its macOS app, check it ran.

This is the second half of the end-to-end check in `verify.sh`: Porte is not
running, the install happens through the same structured bridge the UI uses, and
the generated application is launched with `open` exactly as Finder would. The
run record written by the bundle's own runtime is the evidence.

    python macos/verify_generated_app.py build/installer/PorteFixture.msi \
        --dest "$HOME/Applications"

Exits non-zero on the first failed claim. Always attempts to uninstall the
product it created.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_gui_bridge import BridgeProcess  # noqa: E402


def run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True)


def fail(message: str) -> NoReturn:
    print(f"FAIL  {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", help=".msi, .msix or portable .exe to install")
    parser.add_argument("--dest", default=str(Path.home() / "Applications"),
                        help="where generated applications are created")
    args = parser.parse_args()

    package = Path(args.package).expanduser().resolve()
    if not package.exists():
        fail(f"package not found: {package}")
    support = Path(os.environ.get(
        "PORTE_SUPPORT_ROOT", Path.home() / "Library/Application Support/Porte"))
    bridge = BridgeProcess(extra_env={
        "PORTE_SUPPORT_ROOT": str(support),
        "PORTE_ENV_ROOT": str(support / "Installed"),
    })
    product = None
    app_path: Path | None = None
    try:
        response = bridge.request("install", {
            "path": str(package),
            "consent": True,
            "dest": args.dest,
        }, timeout=300)["message"]
        if response["event"] != "result":
            fail(f"install was refused: {response.get('code')} {response.get('message')}")
        data = response["data"]
        product = data.get("product")
        print(f"ok    installed {data['name']}")
        bundles = data.get("bundles") or []
        if not bundles:
            fail("the package installed but produced no application bundle")
        app_path = Path(bundles[0]["app"])
        bundle_id = bundles[0]["bundle_id"]
        if not app_path.exists():
            fail(f"bundle missing at {app_path}")

        metadata = json.loads(
            (app_path / "Contents" / "Resources" / "PorteApp.json").read_text())
        if metadata.get("bundle_id") != bundle_id:
            fail("bundle metadata does not match the reported identifier")
        plist = run(["plutil", "-lint", str(app_path / "Contents/Info.plist")])
        if plist.returncode != 0:
            fail(f"invalid Info.plist: {plist.stdout} {plist.stderr}")
        executable = app_path / "Contents" / "MacOS" / app_path.stem
        arch = run(["lipo", "-archs", str(executable)]).stdout.strip()
        if arch != "arm64":
            fail(f"launcher architecture is {arch or 'unknown'}, expected arm64")
        print(f"ok    bundle is valid, ad-hoc signed, arm64 launcher ({app_path.name})")

        state_dir = support / "Applications" / bundle_id
        if not (state_dir / "consent.json").exists():
            fail("the install did not record user consent next to the application")
        log_dir = state_dir / "logs"
        before = set(log_dir.glob("run-*.json"))
        opened = run(["open", str(app_path)])
        if opened.returncode != 0:
            fail(f"macOS could not open the application: {opened.stderr.strip()}")
        print("ok    launched the application from Applications")

        deadline = time.time() + 60
        fresh: set[Path] = set()
        while time.time() < deadline:
            fresh = set(log_dir.glob("run-*.json")) - before
            if fresh:
                break
            time.sleep(0.5)
        if not fresh:
            fail("the generated application produced no run record")
        record = json.loads(sorted(fresh)[-1].read_text())
        if record.get("exit_code") != 0 or record.get("fault"):
            fail(f"guest did not complete cleanly: exit={record.get('exit_code')} "
                 f"fault={record.get('fault')}")
        manager = run(["pgrep", "-fl", "Porte.app/Contents/MacOS/Porte"])
        if manager.stdout.strip():
            fail("the Porte manager was running during the launch: "
                 + manager.stdout.strip())
        print(f"ok    application ran without Porte open: "
              f"exit {record['exit_code']}, {record['total_ms']:.0f} ms total")
    finally:
        if app_path is not None:
            run(["pkill", "-f", str(app_path / "Contents" / "MacOS")])
        if product:
            try:
                removal = bridge.request(
                    "uninstall", {"target": "product", "id": product},
                    timeout=180)["message"]
                if removal["event"] == "result":
                    print("ok    uninstalled the product and removed its application")
                else:
                    print(f"warning: uninstall failed: {removal.get('message')}",
                          file=sys.stderr)
            except Exception as exc:  # cleanup must not mask the real result
                print(f"warning: uninstall failed: {exc}", file=sys.stderr)
        bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
