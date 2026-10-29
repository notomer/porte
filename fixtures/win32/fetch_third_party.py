#!/usr/bin/env python3
"""Fetch the third-party Windows applications used by the compatibility tests.

Nothing here is redistributed by Porte and nothing is fetched silently: this is
an explicit, checksum-pinned download of a specific published release, run by a
person (or by CI) that asked for it. The versions and hashes are pinned so a
vendor release cannot change what the tests measured.

    python fixtures/win32/fetch_third_party.py            download what is missing
    python fixtures/win32/fetch_third_party.py --check    report what is present

Files land in build/third_party/ (git-ignored).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEST = ROOT / "build" / "third_party"

ARTIFACTS = [
    {
        "name": "Notepad++ 8.9.8.1 (portable, x64)",
        "file": "npp.8.9.8.1.portable.x64.zip",
        "url": ("https://github.com/notepad-plus-plus/notepad-plus-plus/"
                "releases/download/v8.9.8.1/npp.8.9.8.1.portable.x64.zip"),
        "sha256": "beddf5548e75c97930d1575ba1c7d113e6e2b33d0a200310e734ca08e225f414",
        "size": 8250001,
        "extract": "npp",
        "kind": "portable",
        "license": "GPL-3.0-or-later",
    },
    {
        "name": "7-Zip 26.05 (x64 MSI)",
        "file": "7z2605-x64.msi",
        "url": "https://www.7-zip.org/a/7z2605-x64.msi",
        "sha256": "3fe4ab99fa15629c652944309a2b2bc6e41fc933fc6b63f5df1ebcf3aea68263",
        "size": 2008576,
        "extract": None,
        "kind": "msi",
        "license": "LGPL-2.1-or-later (7-Zip)",
    },
]


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


def status(artifact: dict) -> dict:
    path = DEST / artifact["file"]
    present = path.exists()
    correct = present and digest(path) == artifact["sha256"]
    extra = DEST / artifact["extract"] if artifact.get("extract") else None
    return {
        "name": artifact["name"],
        "file": str(path),
        "present": present,
        "verified": correct,
        "size": path.stat().st_size if present else 0,
        "extracted": bool(extra and extra.exists()),
        "license": artifact["license"],
    }


def fetch(artifact: dict) -> dict:
    DEST.mkdir(parents=True, exist_ok=True)
    path = DEST / artifact["file"]
    if path.exists() and digest(path) == artifact["sha256"]:
        print(f"ok      {artifact['file']} already present and verified")
    else:
        print(f"fetch   {artifact['url']}")
        temporary = path.with_suffix(path.suffix + ".part")
        with urllib.request.urlopen(artifact["url"], timeout=300) as response, \
                open(temporary, "wb") as handle:
            while True:
                block = response.read(1 << 20)
                if not block:
                    break
                handle.write(block)
        actual = digest(temporary)
        if actual != artifact["sha256"]:
            temporary.unlink()
            raise SystemExit(
                f"checksum mismatch for {artifact['file']}: {actual} != "
                f"{artifact['sha256']}")
        os.replace(temporary, path)
        print(f"ok      {artifact['file']} downloaded and verified")
    if artifact.get("extract"):
        target = DEST / artifact["extract"]
        if not target.exists():
            with zipfile.ZipFile(path) as archive:
                archive.extractall(target)
            print(f"ok      extracted to {target}")
    return status(artifact)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch third-party Windows applications for tests")
    parser.add_argument("--check", action="store_true",
                        help="only report what is present")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    rows = []
    for artifact in ARTIFACTS:
        if args.check:
            rows.append(status(artifact))
        else:
            rows.append(fetch(artifact))
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        state = ("verified" if row["verified"] else
                 ("present, checksum differs" if row["present"] else "missing"))
        print(f"{row['name']:34s} {state:26s} {row['license']}")
    return 0 if all(row["verified"] for row in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
