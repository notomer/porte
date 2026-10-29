"""Component D: versioned optimization cache.

Artifacts are addressed by a digest over the application and module hashes,
the profile facts, the transform version, the host, the OS build, the
toolchain and the verification corpus. Every field is an invalidation key:
changing any one produces a different address, so stale code is never loaded.
Manifests are stored next to artifacts so a miss can be explained per field.
"""
from __future__ import annotations

from pathlib import Path

from . import util

SCHEMA = 3


def profile_fingerprint(profile: dict) -> dict:
    """Stable, run-independent identity of a profile.

    Raw pointer values from the boundary record change with ASLR on every run;
    including them would make the cache key unique per execution and turn the
    cache into a no-op. Only the observed shape and facts are identity.
    """
    boundary = profile.get("boundary") or {}
    return {
        "workload": profile.get("workload"),
        "facts": profile.get("facts"),
        "shape": {
            "n": boundary.get("n"),
            "q": boundary.get("q"),
            "flags": boundary.get("flags"),
            "alias_possible": boundary.get("alias_possible"),
        },
        "module_sha256": (profile.get("module") or {}).get("sha256"),
        "runtime": (profile.get("runtime") or {}).get("name"),
    }


def environment(arch: str, app_sha: str, module_sha: str, profile: dict,
                corpus: dict, transform: dict) -> dict:
    """Everything a cached specialization is allowed to depend on."""
    return {
        "schema": SCHEMA,
        "porte_version": transform.get("porte_version", "0"),
        "arch": arch,
        "application_sha256": app_sha,
        "module_sha256": module_sha,
        "profile_digest": util.digest_json(profile_fingerprint(profile)),
        "corpus_digest": util.digest_json(corpus),
        "transform": transform.get("transform"),
        "transform_version": transform.get("version"),
        "transform_source_sha256": transform.get("source_sha256"),
        "host": util.host_info(),
        "toolchain": util.toolchain_info(),
    }


def key_of(env: dict) -> str:
    return util.digest_json(env)[:32]


def store(env: dict, artifacts: dict, verification: dict, bench: dict) -> Path:
    key = key_of(env)
    root = util.ensure_dir(util.CACHE / key)
    util.write_json(root / "manifest.json",
                    {"key": key, "env": env, "artifacts": artifacts,
                     "verification": verification, "bench": bench})
    return root


def load(key: str) -> dict | None:
    root = util.CACHE / key
    manifest = root / "manifest.json"
    if not manifest.exists():
        return None
    data = util.read_json(manifest)
    dylib = Path(data.get("artifacts", {}).get("dylib", ""))
    if not dylib.exists():
        return None
    return data


def latest() -> dict | None:
    roots = sorted(util.CACHE.glob("*/manifest.json"),
                   key=lambda p: p.stat().st_mtime)
    return util.read_json(roots[-1]) if roots else None


def invalidation_reasons(stored_env: dict, current_env: dict) -> list[str]:
    reasons = []
    for field in sorted(set(stored_env) | set(current_env)):
        a, b = stored_env.get(field), current_env.get(field)
        if a != b:
            reasons.append(f"{field}: {a!r} -> {b!r}")
    return reasons
