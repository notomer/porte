"""Isolated per-product Windows environments.

An installed Windows program does not get to write into the user's home the way
it would on Windows. Each product gets its own directory tree, virtual registry
and manifest under Porte's Application Support. That is the smallest honest
version of "per-application compatibility environment": files and registry
writes are visible to Porte's engine and to future installers of the same
product, and nothing else.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

from .. import util
from .bundle import slug

ROOT = Path.home() / "Library" / "Application Support" / "Porte" / "Installed"
ENV_ROOT_VAR = "PORTE_ENV_ROOT"


def root() -> Path:
    """Installation root; the fixture verifier points it at a temporary tree."""
    override = os.environ.get(ENV_ROOT_VAR)
    return Path(override) if override else ROOT


class EnvironmentError(RuntimeError):
    pass


def product_key(name: str, identity: str) -> str:
    digest = util.sha256_text(identity)[:8]
    return f"{slug(name)[:40]}-{digest}"


def product_dir(key: str) -> Path:
    return root() / key


def windows_dir(key: str) -> Path:
    return product_dir(key) / "windows"


def manifest_path(key: str) -> Path:
    return product_dir(key) / "install.json"


def read_manifest(key: str) -> dict | None:
    path = manifest_path(key)
    if not path.exists():
        return None
    try:
        return util.read_json(path)
    except ValueError:
        return None


def write_manifest(key: str, doc: dict) -> None:
    util.write_json(manifest_path(key), doc)


def list_products() -> list[dict]:
    base = root()
    if not base.exists():
        return []
    out = []
    for path in sorted(base.iterdir()):
        if not path.is_dir():
            continue
        doc = read_manifest(path.name)
        if doc:
            out.append(doc)
    return out


def parse_registry_value(raw: str) -> dict:
    """MSI encodes typed registry values with a leading marker."""
    if raw.startswith("#") and raw[1:].lstrip("-").isdigit():
        return {"type": "integer", "data": int(raw[1:])}
    if raw.startswith("#%"):
        return {"type": "expand-string", "data": raw[2:]}
    if raw.startswith("#x"):
        return {"type": "binary", "data": raw[2:]}
    return {"type": "string", "data": raw}


REGISTRY_ROOTS = {
    0: "HKEY_CLASSES_ROOT",
    1: "HKEY_CURRENT_USER",
    2: "HKEY_LOCAL_MACHINE",
    3: "HKEY_USERS",
    4: "HKEY_CURRENT_CONFIG",
}


def registry_document(rows: list[dict]) -> dict:
    """Virtual registry: {hive: {key: {name: value}}}."""
    doc: dict[str, dict[str, dict[str, dict]]] = {}
    for row in rows:
        hive = REGISTRY_ROOTS.get(row.get("Root"), f"HKEY_{row.get('Root')}")
        key = (row.get("Key") or "").replace("\\\\", "\\").strip("\\")
        name = row.get("Name") or "@"
        doc.setdefault(hive, {}).setdefault(key, {})[name] = \
            parse_registry_value(row.get("Value") or "")
    return doc


def safe_join(root: Path, parts: list[str]) -> Path:
    """Join path components without allowing an escape from root."""
    current = root
    for part in parts:
        cleaned = part.strip().replace("\\", "/")
        for piece in cleaned.split("/"):
            if piece in ("", "."):
                continue
            if piece == "..":
                raise EnvironmentError(f"path escapes the environment: {parts}")
            if re.search(r"[\x00-\x1f]", piece):
                raise EnvironmentError(f"invalid path component {piece!r}")
            current = current / piece
    resolved = current.resolve()
    if root.resolve() not in (resolved, *resolved.parents):
        raise EnvironmentError(f"path escapes the environment: {parts}")
    return current


def fresh_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def remove_product(key: str) -> dict:
    directory = product_dir(key)
    if not directory.exists():
        raise EnvironmentError(f"no installed product {key}")
    manifest = read_manifest(key) or {}
    shutil.rmtree(directory)
    return {"product": key, "removed": str(directory),
            "bundles": manifest.get("bundles", [])}


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
