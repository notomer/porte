"""Install an MSI package into a Porte environment.

Scope, stated up front: this is a native subset of Windows Installer, not
msiexec. It executes the file, registry and shortcut tables of a package into an
isolated per-product directory. It does not run arbitrary custom actions, does
not start services or install drivers, and does not evaluate the full condition
and sequence language. Packages that need those features are refused with the
reason, before anything is written.
"""
from __future__ import annotations

import hashlib
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from ... import util
from .. import bundle, environment
from ..install import inspect_pe
from .cab import CabError, Cabinet
from .database import MSIDatabase, MSIError

# Custom action types that execute code Porte cannot run natively.
CODE_ACTION_TYPES = {1, 2, 17, 18, 37, 38, 50, 53, 54, 63, 64}

STANDARD_DIRS = {
    "TARGETDIR": "",
    "ProgramFilesFolder": "Program Files",
    "ProgramFiles64Folder": "Program Files",
    "ProgramFiles(x86)Folder": "Program Files (x86)",
    "CommonFilesFolder": "Program Files/Common Files",
    "CommonFiles64Folder": "Program Files/Common Files",
    "LocalAppDataFolder": "AppData/Local",
    "AppDataFolder": "AppData/Roaming",
    "CommonAppDataFolder": "ProgramData",
    "WindowsFolder": "Windows",
    "SystemFolder": "Windows/System32",
    "System64Folder": "Windows/System32",
    "ProgramMenuFolder": "ProgramData/Microsoft/Windows/Start Menu/Programs",
    "DesktopFolder": "Desktop",
    "FontsFolder": "Windows/Fonts",
}

class UnsupportedPackage(RuntimeError):
    def __init__(self, message: str, blockers: list[str]):
        super().__init__(message)
        self.blockers = blockers


def long_name(value: str) -> str:
    """'SHORT|Long Name' -> 'Long Name'; plain names unchanged."""
    if not value:
        return ""
    if "|" in value:
        value = value.split("|", 1)[1]
    return value.rstrip(".")


def _dir_paths(db: MSIDatabase) -> dict[str, list[str]]:
    rows = {row["Directory"]: row for row in db.rows("Directory")}
    paths: dict[str, list[str]] = {}

    def resolve(ident: str, seen: set[str]) -> list[str]:
        if ident in paths:
            return paths[ident]
        if ident in seen:
            raise MSIError(f"directory cycle at {ident}")
        if ident in STANDARD_DIRS:
            root = STANDARD_DIRS[ident]
            paths[ident] = root.split("/") if root else []
            return paths[ident]
        row = rows.get(ident)
        if row is None:
            paths[ident] = []
            return []
        seen.add(ident)
        parent = resolve(row.get("Directory_Parent") or "TARGETDIR", seen)
        name = long_name(row.get("DefaultDir") or ident)
        parts = parent + ([name] if name and name not in (".", "SourceDir") else [])
        paths[ident] = parts
        return parts

    for ident in rows:
        resolve(ident, set())
    return paths


def _media_for_sequence(db: MSIDatabase, sequence: int) -> dict | None:
    media = sorted(db.rows("Media"), key=lambda r: r.get("LastSequence", 0))
    for row in media:
        if sequence <= row.get("LastSequence", 0):
            return row
    return media[-1] if media else None


@dataclass
class Plan:
    name: str
    version: str
    manufacturer: str
    product_code: str
    upgrade_code: str
    key: str
    directories: dict[str, list[str]] = field(default_factory=dict)
    files: list[dict] = field(default_factory=list)
    registry: list[dict] = field(default_factory=list)
    shortcuts: list[dict] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def load_plan(msi_path: Path) -> tuple[MSIDatabase, Plan]:
    db = MSIDatabase(Path(msi_path))
    props = db.properties()
    name = props.get("ProductName") or Path(msi_path).stem
    product_code = props.get("ProductCode", "")
    plan = Plan(
        name=name,
        version=props.get("ProductVersion", ""),
        manufacturer=props.get("Manufacturer", ""),
        product_code=product_code,
        upgrade_code=props.get("UpgradeCode", ""),
        key=environment.product_key(name, product_code or str(msi_path)),
        directories=_dir_paths(db),
    )
    plan.warnings.append(
        "installation executes the file/registry/shortcut tables only; "
        "conditions, feature selection and install sequences are not evaluated")

    components = {row["Component"]: row for row in db.rows("Component")}
    for row in db.rows("FeatureComponents"):
        if row["Component_"] not in components:
            continue
        directory = components[row["Component_"]].get("Directory_", "TARGETDIR")
        plan.directories.setdefault(directory, plan.directories.get(directory, []))
    for row in db.rows("File"):
        plan.files.append({
            "id": row["File"],
            "component": row["Component_"],
            "name": long_name(row["FileName"]),
            "size": row.get("FileSize", 0),
            "sequence": row.get("Sequence", 0),
            "attributes": row.get("Attributes", 0),
            "directory": components.get(row["Component_"], {}).get(
                "Directory_", "TARGETDIR"),
        })
    plan.files.sort(key=lambda f: f["sequence"])
    plan.registry = db.rows("Registry")
    plan.shortcuts = db.rows("Shortcut")

    actions = db.rows("CustomAction")
    code_actions = [a for a in actions
                    if (a.get("Type", 0) & 0x3F) in CODE_ACTION_TYPES]
    if code_actions:
        plan.blockers.append(
            f"{len(code_actions)} custom action(s) execute code Porte cannot run: "
            + ", ".join(sorted({a['Action'] for a in code_actions})[:6]))
    if db.rows("ServiceInstall") or db.rows("ServiceControl"):
        plan.blockers.append("package installs Windows services, which have no "
                             "supported equivalent in this environment")
    # Cabinet compression is part of whether this package can be applied at all,
    # so it is reported here rather than discovered halfway through extracting.
    # A package whose payload needs LZX/Quantum is refused by the importer and
    # can still be installed by the Windows Installer inside the Wine runtime.
    try:
        cabinets = _cab_streams(db, msi_path)
    except (MSIError, OSError) as exc:
        plan.blockers.append(f"package payload is unavailable: {exc}")
        cabinets = {}
    for cab_name, data in cabinets.items():
        try:
            cabinet = Cabinet(data, cab_name)
        except CabError as exc:
            plan.blockers.append(str(exc))
            continue
        plan.blockers.extend(cabinet.unsupported_folders())
    if not plan.files and not plan.registry:
        plan.blockers.append("package installs no files and no registry values")
    ignored = len(actions) - len(code_actions)
    if ignored:
        plan.warnings.append(f"{ignored} property/custom table action(s) ignored")
    return db, plan


def _cab_streams(db: MSIDatabase, msi_path: Path) -> dict[str, bytes]:
    """Embedded cabinets, plus external ones next to the package."""
    cabs: dict[str, bytes] = {}
    for row in db.rows("Media"):
        cabinet = (row.get("Cabinet") or "").strip()
        if not cabinet:
            continue
        if cabinet.startswith("#"):
            name = cabinet[1:]
            if name not in db.streams:
                raise MSIError(f"embedded cabinet {name} is missing")
            cabs[name] = db.streams[name]
        else:
            path = Path(msi_path).parent / cabinet
            if not path.exists():
                raise MSIError(f"external cabinet not found: {path}")
            cabs[cabinet] = path.read_bytes()
    return cabs


def _file_payloads(db: MSIDatabase, plan: Plan, msi_path: Path) -> dict[str, bytes]:
    cabs = _cab_streams(db, msi_path)
    extracted: dict[str, bytes] = {}
    for name, data in cabs.items():
        cab = Cabinet(data, name)
        try:
            extracted.update(cab.extract())
        except CabError as exc:
            raise UnsupportedPackage(str(exc), [str(exc)]) from exc
    payloads: dict[str, bytes] = {}
    for entry in plan.files:
        row = _media_for_sequence(db, entry["sequence"])
        if row is None:
            raise UnsupportedPackage("package has no media table", ["no media table"])
        cabinet = (row.get("Cabinet") or "").strip()
        if cabinet:
            key = cabinet[1:] if cabinet.startswith("#") else cabinet
            if entry["name"] not in extracted:
                raise MSIError(f"{entry['name']} is not in cabinet {key}")
            payloads[entry["id"]] = extracted[entry["name"]]
        else:
            source = Path(row.get("Source") or ".")
            base = source if source.is_absolute() else Path(msi_path).parent / source
            candidate = base / entry["name"]
            if not candidate.exists():
                raise MSIError(f"uncompressed source file missing: {candidate}")
            payloads[entry["id"]] = candidate.read_bytes()
    return payloads


def parse_version(text: str) -> tuple[int, ...]:
    parts = []
    for piece in (text or "").split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def find_upgrade(plan: Plan) -> dict | None:
    """An installed product sharing this package's UpgradeCode."""
    if not plan.upgrade_code:
        return None
    for product in environment.list_products():
        if product.get("upgrade_code") == plan.upgrade_code:
            return product
    return None


def inspect_package(msi_path: Path) -> dict:
    _, plan = load_plan(msi_path)
    return {
        "format": "msi",
        "name": plan.name,
        "version": plan.version,
        "manufacturer": plan.manufacturer,
        "product_code": plan.product_code,
        "upgrade_code": plan.upgrade_code,
        "product_key": plan.key,
        "files": [{"name": f["name"], "size": f["size"],
                   "directory": "/".join(plan.directories.get(f["directory"], []))}
                  for f in plan.files],
        "registry_values": len(plan.registry),
        "shortcuts": len(plan.shortcuts),
        "supported": not plan.blockers,
        "blockers": plan.blockers,
        "warnings": plan.warnings,
    }


def install(msi_path: Path, *, accept: bool = False, force: bool = False,
            ignore_custom_actions: bool = False, generate_bundles: bool = True,
            dest: Path | None = None, runtime_root: Path | None = None) -> dict:
    msi_path = Path(msi_path).expanduser().resolve()
    if not msi_path.exists():
        raise FileNotFoundError(msi_path)
    db, plan = load_plan(msi_path)
    blockers = list(plan.blockers)
    if ignore_custom_actions:
        blockers = [b for b in blockers if "custom action" not in b]
    if blockers:
        raise UnsupportedPackage(
            "this MSI needs features Porte cannot provide:\n  - "
            + "\n  - ".join(blockers), blockers)

    if not accept:
        print(f"Windows Installer package: {plan.name} {plan.version}")
        print(f"  product code: {plan.product_code or '(none)'}")
        print(f"  installs: {len(plan.files)} file(s), "
              f"{len(plan.registry)} registry value(s), "
              f"{len(plan.shortcuts)} shortcut(s)")
        print(f"  environment: {environment.windows_dir(plan.key)}")
        if plan.warnings:
            print("  notes: " + "; ".join(plan.warnings))
        try:
            reply = input("Install this package through Porte? [y/N] ")
        except EOFError as exc:
            raise PermissionError(
                "no interactive terminal; re-run with --accept after reviewing "
                "the package summary") from exc
        if reply.strip().lower() not in ("y", "yes"):
            raise PermissionError("installation declined")

    payloads = _file_payloads(db, plan, msi_path)
    root = environment.product_dir(plan.key)
    previous = find_upgrade(plan)
    upgrade_from = None
    if previous is not None:
        old_dir = environment.product_dir(previous["product_key"])
        if old_dir.exists() and old_dir.resolve() != root.resolve():
            if force:
                upgrade_from = previous
            elif parse_version(plan.version) >= parse_version(
                    previous.get("version", "")):
                upgrade_from = previous
            else:
                raise FileExistsError(
                    f"{previous['name']} {previous['version']} is installed; "
                    f"refusing to downgrade to {plan.version} (use --force)")
    if root.exists() and not force and (upgrade_from is None or
                                        upgrade_from["product_key"] == plan.key):
        raise FileExistsError(
            f"{plan.name} is already installed at {root} (use --force to replace)")
    environment.fresh_dir(root)
    windows = environment.windows_dir(plan.key)
    windows.mkdir(parents=True, exist_ok=True)
    installed_files = []
    try:
        for entry in plan.files:
            data = payloads[entry["id"]]
            parts = plan.directories.get(entry["directory"], [])
            target = environment.safe_join(windows, parts + [entry["name"]])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            installed_files.append({
                "path": str(target.relative_to(windows)),
                "size": len(data),
                "sha256": digest,
                "component": entry["component"],
            })
            if entry["size"] and entry["size"] != len(data):
                raise MSIError(f"{entry['name']}: expected {entry['size']} bytes, "
                               f"extracted {len(data)}")
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise

    registry = environment.registry_document(plan.registry)
    util.write_json(root / "registry.json", registry)
    util.write_json(root / "shortcuts.json", plan.shortcuts)

    if upgrade_from is not None:
        # Old bundles and the old environment go first: a same-named application
        # would otherwise collide with the new bundle.
        for entry in upgrade_from.get("bundles", []):
            try:
                bundle.remove_app(Path(entry["app"]), purge=True)
            except FileNotFoundError:
                continue
        environment.remove_product(upgrade_from["product_key"])

    bundles = []
    launchable = []
    skipped = []
    try:
        if generate_bundles:
            from ..bundle import AppSpec

            exes = [f for f in installed_files
                    if f["path"].lower().endswith(".exe")]
            supported: list[tuple[dict, dict, Path]] = []
            for entry in exes:
                path = windows / entry["path"]
                facts = inspect_pe(path)
                if facts["supported"]:
                    supported.append((entry, facts, path))
                else:
                    skipped.append({"file": entry["path"],
                                    "missing": facts["missing_stubs"]})
            for entry, facts, path in supported:
                app_name = (plan.name if len(supported) == 1
                            else f"{plan.name} ({path.stem})")
                spec = AppSpec(
                    pe_path=path,
                    name=app_name,
                    bundle_id=bundle.default_bundle_id(
                    path, app_name, identity=plan.product_code or plan.key),
                    arguments=[],
                    profile=facts["profile"],
                    category=bundle.icon_category(facts["profile"]),
                )
                result = bundle.build_app(
                    spec, dest_dir=dest, runtime_root=runtime_root, force=force,
                    pe_display_path=str(path))
                from ..install import _record_consent

                _record_consent(spec.bundle_id, facts, "msi import")
                bundles.append({"app": result["app"],
                                "bundle_id": result["bundle_id"],
                                "exe": entry["path"]})
                launchable.append(entry["path"])
    except Exception:
        for built in bundles:
            shutil.rmtree(built["app"], ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)
        raise

    manifest = {
        "kind": "msi",
        "product_key": plan.key,
        "name": plan.name,
        "version": plan.version,
        "manufacturer": plan.manufacturer,
        "product_code": plan.product_code,
        "upgrade_code": plan.upgrade_code,
        "package": str(msi_path),
        "package_sha256": util.sha256_file(msi_path),
        "installed_at": environment.now(),
        "windows": str(windows),
        "files": installed_files,
        "registry_values": len(plan.registry),
        "shortcuts": len(plan.shortcuts),
        "bundles": bundles,
        "launchable": launchable,
        "skipped_executables": skipped,
        "warnings": plan.warnings,
        "upgraded_from": ({k: upgrade_from.get(k) for k in
                           ("product_key", "name", "version")}
                          if upgrade_from else None),
    }
    environment.write_manifest(plan.key, manifest)
    return {
        "product": plan.key,
        "upgraded_from": manifest["upgraded_from"],
        "name": plan.name,
        "version": plan.version,
        "environment": str(root),
        "files": len(installed_files),
        "registry_values": len(plan.registry),
        "bundles": bundles,
        "launchable": launchable,
        "skipped_executables": skipped,
        "warnings": plan.warnings,
    }


def uninstall(product_key: str) -> dict:
    manifest = environment.read_manifest(product_key)
    if manifest is None:
        raise environment.EnvironmentError(f"no installed product {product_key}")
    removed_apps = []
    for entry in manifest.get("bundles", []):
        try:
            bundle.remove_app(Path(entry["app"]), purge=True)
            removed_apps.append(entry["app"])
        except FileNotFoundError:
            continue
    result = environment.remove_product(product_key)
    result["apps_removed"] = removed_apps
    return result


def repair(product_key: str) -> dict:
    manifest = environment.read_manifest(product_key)
    if manifest is None:
        raise environment.EnvironmentError(f"no installed product {product_key}")
    windows = Path(manifest["windows"])
    missing = []
    for entry in manifest.get("files", []):
        path = windows / entry["path"]
        if not path.exists() or util.sha256_file(path) != entry["sha256"]:
            missing.append(entry["path"])
    package = Path(manifest.get("package", ""))
    reinstall = False
    if missing and package.exists() and \
            util.sha256_file(package) == manifest.get("package_sha256"):
        reinstall = True
        install(package, accept=True, force=True, generate_bundles=False)
        # The re-extract rewrites the manifest without bundle generation, so the
        # existing applications are carried over explicitly.
        repaired = environment.read_manifest(product_key) or {}
        for field_name in ("bundles", "launchable", "skipped_executables"):
            repaired[field_name] = manifest.get(field_name, [])
        repaired["installed_at"] = manifest.get("installed_at")
        repaired["repaired_at"] = environment.now()
        environment.write_manifest(product_key, repaired)
    return {"product": product_key, "checked": len(manifest.get("files", [])),
            "damaged": missing, "reinstalled": reinstall,
            "package_available": package.exists()}
