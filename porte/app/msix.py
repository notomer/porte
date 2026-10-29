"""MSIX / APPX packages: extract, validate, install, bundle.

An MSIX is a ZIP with `AppxManifest.xml`, a content-type map and a block map. It
is not an installer in the Windows Installer sense: there is no script, no
sequence and no registry table; the package declares files and the platform
registers them for the current user. Porte does the parts that have meaning
here: it checks the container against the block map, extracts the payload into
an isolated environment, resolves the declared applications, and generates a
normal `.app` for each executable the engine can actually run.

What is not done, and is reported instead of hidden: Authenticode signing is not
verified, WinRT/`Windows.*` APIs are not implemented, and package identity
(application, publisher, capabilities) has no macOS equivalent. A package whose
entry point needs those APIs is extracted and listed, but no bundle is created.
"""
from __future__ import annotations

import base64
import hashlib
import shutil
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .. import util
from . import bundle, environment
from .install import inspect_pe

NS = {
    "f": "http://schemas.microsoft.com/appx/manifest/foundation/windows10",
    "uap": "http://schemas.microsoft.com/appx/manifest/uap/windows10",
}
BLOCK = 64 * 1024
MAX_ENTRIES = 20000
MAX_UNCOMPRESSED = 2 << 30


class PackageError(RuntimeError):
    def __init__(self, message: str, blockers: list[str] | None = None):
        super().__init__(message)
        self.blockers = blockers or [message]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _manifest(zf: zipfile.ZipFile) -> ET.Element:
    try:
        raw = zf.read("AppxManifest.xml")
    except KeyError as exc:
        raise PackageError("package has no AppxManifest.xml",
                           ["no AppxManifest.xml"]) from exc
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise PackageError(f"AppxManifest.xml is not well formed: {exc}",
                           [f"manifest parse error: {exc}"]) from exc
    if _local(root.tag) != "Package":
        raise PackageError("AppxManifest.xml has no Package root",
                           ["manifest root is not <Package>"])
    return root


def _identity(root: ET.Element) -> dict:
    node = next((c for c in root if _local(c.tag) == "Identity"), None)
    if node is None:
        return {}
    return {
        "name": node.get("Name", ""),
        "publisher": node.get("Publisher", ""),
        "version": node.get("Version", ""),
        "architecture": node.get("ProcessorArchitecture", ""),
        "resource_id": node.get("ResourceId", ""),
    }


def _applications(root: ET.Element) -> list[dict]:
    apps = []
    applications = next((c for c in root if _local(c.tag) == "Applications"), None)
    if applications is None:
        return apps
    for node in applications:
        if _local(node.tag) != "Application":
            continue
        exe = node.get("Executable", "")
        visual = next((c for c in node if _local(c.tag) == "VisualElements"), None)
        apps.append({
            "id": node.get("Id", ""),
            "executable": exe.replace("\\", "/"),
            "display_name": visual.get("DisplayName", "") if visual is not None else "",
            "entry_point": node.get("EntryPoint", ""),
        })
    return apps


def _capabilities(root: ET.Element) -> list[str]:
    caps: set[str] = set()
    for node in root.iter():
        if _local(node.tag) == "Capability" and node.get("Name"):
            caps.add(node.get("Name"))
        if _local(node.tag) == "CapabilityChoice" and node.get("Name"):
            caps.add(node.get("Name"))
    return sorted(caps)


def _block_map(zf: zipfile.ZipFile, names: set[str]) -> dict:
    """Validate every file against AppxBlockMap.xml.

    Returns a summary; raises when the container and the block map disagree, so
    a truncated or tampered package cannot install silently.
    """
    if "AppxBlockMap.xml" not in names:
        return {"present": False, "files_checked": 0}
    try:
        root = ET.fromstring(zf.read("AppxBlockMap.xml"))
    except ET.ParseError as exc:
        raise PackageError(f"AppxBlockMap.xml is not well formed: {exc}",
                           [f"block map parse error: {exc}"]) from exc
    checked = 0
    for file_node in root:
        if _local(file_node.tag) != "File":
            continue
        name = file_node.get("Name", "")
        if name not in names:
            raise PackageError(f"block map lists {name}, which is not in the package",
                               [f"missing payload: {name}"])
        data = zf.read(name)
        expected = int(file_node.get("Size", len(data)))
        if expected != len(data):
            raise PackageError(f"{name}: block map size {expected} != {len(data)}",
                               [f"size mismatch: {name}"])
        blocks = [c for c in file_node if _local(c.tag) == "Block"]
        if not blocks:
            continue
        # Each Block carries its own SHA-256 of that 64 KiB chunk.
        for index, block in enumerate(blocks):
            chunk = data[index * BLOCK:(index + 1) * BLOCK]
            want = base64.b64decode(block.get("Hash", ""))
            if hashlib.sha256(chunk).digest() != want:
                raise PackageError(f"{name}: block {index} hash mismatch",
                                   [f"corrupt payload: {name}"])
        checked += 1
    return {"present": True, "files_checked": checked}


def inspect_package(path: Path) -> dict:
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    if not zipfile.is_zipfile(path):
        raise PackageError(f"{path.name} is not a ZIP container",
                           ["not a ZIP container"])
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        names = {i.filename for i in infos}
        blockers = []
        warnings = []
        if len(infos) > MAX_ENTRIES:
            blockers.append(f"{len(infos)} entries exceeds the {MAX_ENTRIES} limit")
        total = sum(i.file_size for i in infos)
        if total > MAX_UNCOMPRESSED:
            blockers.append(f"{total} uncompressed bytes exceeds the 2 GiB limit")
        for info in infos:
            target = Path(info.filename)
            if target.is_absolute() or ".." in target.parts:
                blockers.append(f"entry escapes the package: {info.filename}")
        if "AppxSignature.p7x" not in names:
            warnings.append("package is not signed (AppxSignature.p7x is absent)")
        warnings.append("Authenticode signature is not verified on this host")
        root = _manifest(zf)
        identity = _identity(root)
        apps = _applications(root)
        blocks = _block_map(zf, names)
        missing = [a["executable"] for a in apps
                   if a["executable"] and a["executable"] not in names]
        for name in missing:
            blockers.append(f"declared executable is missing: {name}")
        arch = identity.get("architecture", "")
        if arch.lower() in ("arm", "arm64", "neutral"):
            pass  # neutral is fine; arm-only packages contain no x86 images
        if arch.lower() == "arm64" and any(
                a["executable"].lower().endswith(".exe") for a in apps):
            blockers.append("package targets ARM64 Windows; Porte executes x86")
        entries = [{"name": i.filename, "size": i.file_size} for i in infos
                   if not i.filename.endswith("/")]
        return {
            "format": path.suffix.lower().lstrip("."),
            "identity": identity,
            "applications": apps,
            "capabilities": _capabilities(root),
            "files": entries,
            "block_map": blocks,
            "supported": not blockers,
            "blockers": blockers,
            "warnings": warnings,
        }


def default_environment_key(identity: dict, path: Path) -> str:
    seed = identity.get("name") or path.stem
    return environment.product_key(seed, identity.get("publisher") or str(path))


def install(path: Path, *, accept: bool = False, force: bool = False,
            dest: Path | None = None, runtime_root: Path | None = None,
            generate_bundles: bool = True) -> dict:
    path = Path(path).expanduser().resolve()
    report = inspect_package(path)
    if report["blockers"]:
        raise PackageError("this MSIX cannot be installed:\n  - "
                           + "\n  - ".join(report["blockers"]),
                           report["blockers"])
    identity = report["identity"]
    if not identity.get("name"):
        raise PackageError("identity is missing from the manifest",
                           ["no package identity"])
    key = default_environment_key(identity, path)
    if not accept:
        print(f"MSIX package: {identity.get('name')} {identity.get('version')}")
        print(f"  publisher: {identity.get('publisher')}")
        print(f"  applications: {len(report['applications'])}")
        print(f"  files: {len(report['files'])}, "
              f"block map: {'yes' if report['block_map']['present'] else 'no'}")
        print("  signature: unverified (Authenticode is not checked on this host)")
        try:
            reply = input("Extract and install this package? [y/N] ")
        except EOFError as exc:
            raise PermissionError("no interactive terminal; re-run with --accept") from exc
        if reply.strip().lower() not in ("y", "yes"):
            raise PermissionError("installation declined")

    root = environment.product_dir(key)
    if root.exists() and not force:
        raise FileExistsError(f"{identity.get('name')} is already installed at {root}")
    environment.fresh_dir(root)
    windows = environment.windows_dir(key)
    windows.mkdir(parents=True, exist_ok=True)
    installed = []
    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                parts = Path(info.filename).parts
                target = environment.safe_join(windows, list(parts))
                target.parent.mkdir(parents=True, exist_ok=True)
                data = zf.read(info)
                target.write_bytes(data)
                installed.append({"path": "/".join(parts), "size": len(data),
                                  "sha256": hashlib.sha256(data).hexdigest()})
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise

    from .bundle import AppSpec

    bundles, launchable, skipped = [], [], []
    try:
        for app in (report["applications"] if generate_bundles else []):
            exe = app["executable"]
            if not exe:
                continue
            target = windows / exe
            if not target.exists():
                skipped.append({"file": exe, "missing": ["file not in package"]})
                continue
            facts = inspect_pe(target)
            if not facts["supported"]:
                skipped.append({"file": exe, "missing": facts["missing_stubs"]})
                continue
            label = app.get("display_name") or app.get("id") or target.stem
            if label.startswith("ms-resource:"):
                label = target.stem
            spec = AppSpec(
                pe_path=target,
                name=label,
                bundle_id=bundle.default_bundle_id(
                    target, label,
                    identity=f"{identity.get('name')}/{identity.get('publisher')}"),
                arguments=[],
                profile=facts["profile"],
                category=bundle.icon_category(facts["profile"]),
            )
            result = bundle.build_app(spec, dest_dir=dest,
                                      runtime_root=runtime_root, force=force,
                                      pe_display_path=str(target))
            from .install import _record_consent

            _record_consent(spec.bundle_id, facts, "msix import")
            bundles.append({"app": result["app"],
                            "bundle_id": result["bundle_id"], "exe": exe})
            launchable.append(exe)
    except Exception:
        for built in bundles:
            shutil.rmtree(built["app"], ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)
        raise

    manifest = {
        "kind": "msix",
        "product_key": key,
        "name": identity.get("name"),
        "version": identity.get("version"),
        "publisher": identity.get("publisher"),
        "architecture": identity.get("architecture"),
        "package": str(path),
        "package_sha256": util.sha256_file(path),
        "installed_at": environment.now(),
        "windows": str(windows),
        "files": installed,
        "applications": report["applications"],
        "capabilities": report["capabilities"],
        "bundles": bundles,
        "launchable": launchable,
        "skipped_executables": skipped,
        "warnings": report["warnings"],
    }
    environment.write_manifest(key, manifest)
    return {"product": key, "name": identity.get("name"),
            "version": identity.get("version"), "environment": str(root),
            "files": len(installed), "bundles": bundles,
            "launchable": launchable, "skipped_executables": skipped,
            "warnings": report["warnings"]}


def uninstall(product_key: str) -> dict:
    manifest = environment.read_manifest(product_key)
    if manifest is None:
        raise environment.EnvironmentError(f"no installed product {product_key}")
    removed = []
    for entry in manifest.get("bundles", []):
        try:
            bundle.remove_app(Path(entry["app"]), purge=True)
            removed.append(entry["app"])
        except FileNotFoundError:
            continue
    result = environment.remove_product(product_key)
    result["apps_removed"] = removed
    return result


def repair(product_key: str) -> dict:
    manifest = environment.read_manifest(product_key)
    if manifest is None:
        raise environment.EnvironmentError(f"no installed product {product_key}")
    windows = Path(manifest["windows"])
    damaged = [entry["path"] for entry in manifest.get("files", [])
               if not (windows / entry["path"]).exists()
               or util.sha256_file(windows / entry["path"]) != entry["sha256"]]
    package = Path(manifest.get("package", ""))
    reinstall = False
    if damaged and package.exists() and \
            util.sha256_file(package) == manifest.get("package_sha256"):
        install(package, accept=True, force=True, generate_bundles=False)
        repaired = environment.read_manifest(product_key) or {}
        for field_name in ("bundles", "launchable", "skipped_executables"):
            repaired[field_name] = manifest.get(field_name, [])
        repaired["installed_at"] = manifest.get("installed_at")
        repaired["repaired_at"] = environment.now()
        environment.write_manifest(product_key, repaired)
        reinstall = True
    return {"product": product_key, "checked": len(manifest.get("files", [])),
            "damaged": damaged, "reinstalled": reinstall,
            "package_available": package.exists()}
