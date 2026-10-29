"""The opaque-PE pipeline: profile, specialize, prove, cache, run.

Every input is a PE path plus decimal command-line arguments. There is no
symbol lookup, no source path and no per-fixture branch. If a program's hot loop
cannot be lifted, the pipeline says so and baseline execution continues.

Trust model: nothing is cached until the differential oracle has compared
baseline and specialized execution, from the image entry point, over a corpus
that includes boundary sizes. A cached artifact is re-checked cheaply (region
layout, artifact hash, smoke cases) and demoted to interpretation on any doubt.
"""
from __future__ import annotations

import hashlib
import json
import resource
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .. import util
from .dispatch import TraceDispatcher
from .lifter import UnsupportedInstruction, translate
from .machine import Machine
from .profiler import facts_of, profile_program

LIFTER_VERSION = "opaque-1"
MANIFEST_SCHEMA = 1
CACHE_ROOT = util.BUILD / "pe_cache"

# Generic workload vector: decimal sizes handed to *any* program. Nothing here
# is derived from a fixture's source or symbols.
CORPUS = ["0", "1", "2", "3", "7", "16", "63", "64", "65", "127", "255",
          "256", "257", "511", "1023", "1024", "1025", "2047", "2048", "4096",
          "65536", "1000000"]
SMOKE = ["1024", "1"]
PROFILE_ARGS = ["1024"]


@dataclass
class Observation:
    writes: str
    exit_code: int | None
    fault: str | None
    heap_digest: str
    image_digest: str
    api: list
    wall_ns: int
    dispatches: int = 0
    stats: dict | None = None

    def to_dict(self) -> dict:
        return {
            "writes": self.writes,
            "exit_code": self.exit_code,
            "fault": self.fault,
            "heap_digest": self.heap_digest,
            "image_digest": self.image_digest,
            "api": self.api,
            "wall_ns": self.wall_ns,
            "dispatches": self.dispatches,
            "stats": self.stats,
        }


def observe(result) -> Observation:
    return Observation(
        writes=result.writes.decode("utf-8", "replace"),
        exit_code=result.exit_code,
        fault=result.fault,
        heap_digest=result.heap_digest,
        image_digest=result.image_digest,
        api=[{"call": a["call"], "args": a["args"], "ret": a["ret"]}
             for a in result.api_log],
        wall_ns=result.wall_ns,
        dispatches=result.dispatches,
        stats=getattr(result, "stats", None),
    )


def comparable(obs: Observation) -> dict:
    return {
        "writes": obs.writes,
        "exit_code": obs.exit_code,
        "fault": obs.fault,
        "heap_digest": obs.heap_digest,
        "image_digest": obs.image_digest,
        "api": obs.api,
    }


def run_baseline(pe_path: Path, argv: list[str], profile_blocks: bool = False) -> Observation:
    machine = Machine(Path(pe_path))
    return observe(machine.run_entry(argv, profile_blocks=profile_blocks))


def run_with_artifact(pe_path: Path, artifact: dict, argv: list[str]) -> Observation:
    machine = Machine(Path(pe_path))
    dispatcher = TraceDispatcher(Path(artifact["dylib"]), artifact["head"],
                                 artifact.get("regions"))
    machine.set_dispatcher(dispatcher)
    result = machine.run_entry(argv)
    obs = observe(result)
    obs.stats = dispatcher.stats()
    return obs


# --------------------------------------------------------------------------
# Specialization
# --------------------------------------------------------------------------
def build_artifact(pe_path: Path, outdir: Path, pin: bool = True,
                   mutate: str | None = None,
                   profile_args: list[str] | None = None) -> dict:
    """Profile the image and emit an ARM64 trace for its hottest loop."""
    profile_args = profile_args or PROFILE_ARGS
    doc = profile_program(Path(pe_path), profile_args)
    candidate = doc.get("candidate")
    if candidate is None:
        return {"ok": False, "reason": "no reducible hot loop found",
                "profile": {k: v for k, v in doc.items() if k != "sites"}}
    pinned = {}
    if pin:
        for addr, site in doc["sites"].items():
            k = site.pinned_region()
            if k is not None:
                pinned[addr] = k
    try:
        trace = translate(candidate.instructions, candidate.head, candidate.end,
                          pinned=pinned, mutate=mutate,
                          block_starts=candidate.block_starts)
    except UnsupportedInstruction as exc:
        return {"ok": False, "reason": f"loop not liftable: {exc}",
                "candidate": candidate.to_dict()}

    util.ensure_dir(outdir)
    c_path = outdir / "trace.c"
    c_path.write_text(trace.c_source)
    lib = outdir / ("lib_trace_arm64.dylib" if not mutate else "lib_mutant_arm64.dylib")
    cmd = ["clang", "-O3", "-arch", "arm64", "-dynamiclib", "-fPIC",
           "-Wall", "-Wextra", "-Wno-unused-label", "-Wno-unused-function",
           "-o", str(lib), str(c_path)]
    t_compile = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    clang_seconds = time.perf_counter() - t_compile
    if proc.returncode != 0:
        return {"ok": False, "reason": "code generation failed",
                "compiler": proc.stderr[-4000:]}

    machine = Machine(Path(pe_path))
    regions = [(r.guest, r.size) for r in machine.native_regions()]
    return {
        "ok": True,
        "profile_args": list(profile_args),
        "head": candidate.head,
        "back_edge": candidate.back_edge,
        "end": candidate.end,
        "dylib": str(lib),
        "c_source": str(c_path),
        "c_sha256": hashlib.sha256(trace.c_source.encode()).hexdigest(),
        "dylib_sha256": util.sha256_file(lib),
        "instructions": trace.instructions,
        "memory_ops": trace.memory_ops,
        "pinned_sites": trace.pinned_sites,
        "regions": regions,
        "candidate": candidate.to_dict(),
        "facts": facts_of(doc),
        "mutate": mutate,
        "clang_seconds": clang_seconds,
    }


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------
def verify_cases(pe_path: Path, artifact: dict, cases: list[str]) -> dict:
    results = []
    failures = 0
    for argv in cases:
        base = run_baseline(pe_path, [argv])
        spec = run_with_artifact(pe_path, artifact, [argv])
        equal = comparable(base) == comparable(spec)
        results.append({
            "argv": argv,
            "match": equal,
            "baseline": base.to_dict(),
            "specialized": spec.to_dict(),
        })
        if not equal:
            failures += 1
    return {
        "cases": cases,
        "passed": failures == 0,
        "failures": failures,
        "results": results,
    }


def diff_summary(case: dict) -> str:
    b, s = case["baseline"], case["specialized"]
    fields = []
    for key in ("writes", "exit_code", "fault", "heap_digest", "image_digest", "api"):
        if b.get(key) != s.get(key):
            fields.append(f"{key}: base={b.get(key)!r} spec={s.get(key)!r}")
    return "; ".join(fields) or "no observable difference"


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------
def image_sha(pe_path: Path) -> str:
    return util.sha256_file(Path(pe_path))


def cache_env(pe_path: Path, facts: dict, variant: str) -> dict:
    return {
        "schema": MANIFEST_SCHEMA,
        "lifter": LIFTER_VERSION,
        "image_sha256": image_sha(pe_path),
        "loop": {
            "head": facts.get("head"),
            "end": facts.get("end"),
            "bytes_sha256": facts.get("bytes_sha256"),
            "sites": facts.get("sites"),
        },
        "variant": variant,
        "host": util.host_info(),
        "toolchain": util.toolchain_info(),
    }


def key_of(env: dict) -> str:
    return util.digest_json(env)[:24]


def cache_path(key: str) -> Path:
    return CACHE_ROOT / key


def store(env: dict, artifact: dict, verification: dict, bench: dict | None = None) -> dict:
    """Persist an admitted artifact under its key, with its own copy.

    The artifact is copied into the key directory so a later build that reuses
    a working directory cannot silently replace the shared object an older
    manifest points at.
    """
    import shutil

    key = key_of(env)
    root = util.ensure_dir(cache_path(key))
    cached_dylib = root / "trace.dylib"
    shutil.copyfile(artifact["dylib"], cached_dylib)
    cached_c = root / "trace.c"
    shutil.copyfile(artifact["c_source"], cached_c)
    entry = {k: v for k, v in artifact.items()
             if k not in ("facts", "dylib", "c_source")}
    entry["dylib"] = str(cached_dylib)
    entry["c_source"] = str(cached_c)
    entry["dylib_sha256"] = util.sha256_file(cached_dylib)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "key": key,
        "env": env,
        "artifact": entry,
        "verification": {k: v for k, v in verification.items() if k != "results"},
        "bench": bench or {},
    }
    util.write_json(root / "manifest.json", manifest)
    return manifest


def find_cached(pe_path: Path, variant: str = "pinned") -> dict | None:
    """Look up any manifest for this image whose environment still holds."""
    if not CACHE_ROOT.exists():
        return None
    current = {"image_sha256": image_sha(pe_path), "variant": variant}
    hits = []
    for manifest_path in CACHE_ROOT.glob("*/manifest.json"):
        try:
            data = util.read_json(manifest_path)
        except Exception:
            continue
        env = data.get("env", {})
        if env.get("image_sha256") != current["image_sha256"]:
            continue
        if env.get("variant") != variant:
            continue
        if env.get("lifter") != LIFTER_VERSION:
            continue
        dylib = Path(data["artifact"]["dylib"])
        if not dylib.exists():
            continue
        if util.sha256_file(dylib) != data["artifact"]["dylib_sha256"]:
            continue
        hits.append((manifest_path.stat().st_mtime, data))
    if not hits:
        return None
    return sorted(hits, key=lambda kv: -kv[0])[0][1]


def invalidate(pe_path: Path) -> int:
    removed = 0
    if not CACHE_ROOT.exists():
        return 0
    sha = image_sha(pe_path)
    for manifest_path in CACHE_ROOT.glob("*/manifest.json"):
        data = util.read_json(manifest_path)
        if data.get("env", {}).get("image_sha256") == sha:
            for p in manifest_path.parent.iterdir():
                p.unlink()
            manifest_path.parent.rmdir()
            removed += 1
    return removed


# --------------------------------------------------------------------------
# Top level
# --------------------------------------------------------------------------
def optimize(pe_path: Path, profile_args: list[str] | None = None,
             corpus: list[str] | None = None, force: bool = False) -> dict:
    """Produce a verified, cached specialization or explain why not."""
    pe_path = Path(pe_path)
    corpus = corpus or CORPUS
    if not force:
        cached = find_cached(pe_path)
        if cached:
            return {"cached": True, "manifest": cached}
    t0 = time.perf_counter()
    outdir = util.ensure_dir(util.BUILD / "pe" / pe_path.stem)
    artifact = build_artifact(pe_path, outdir, profile_args=profile_args)
    if not artifact.get("ok"):
        return {"cached": False, "ok": False, **artifact}
    artifact["build_seconds"] = time.perf_counter() - t0

    verification = verify_cases(pe_path, artifact, corpus)
    if not verification["passed"]:
        first = next(c for c in verification["results"] if not c["match"])
        return {
            "cached": False,
            "ok": False,
            "reason": "differential oracle rejected the specialization",
            "first_failure": {"argv": first["argv"], "diff": diff_summary(first)},
            "artifact": artifact,
            "verification": verification,
        }
    env = cache_env(pe_path, artifact["facts"], "pinned")
    manifest = store(env, artifact, verification)
    return {"cached": False, "ok": True, "manifest": manifest,
            "artifact": artifact, "verification": verification,
            "seconds": time.perf_counter() - t0}


def load_and_check(pe_path: Path, smoke: list[str] | None = None) -> dict | None:
    """Cheap reuse: artifact integrity, region layout, and a smoke corpus."""
    manifest = find_cached(Path(pe_path))
    if manifest is None:
        return None
    artifact = manifest["artifact"]
    machine = Machine(Path(pe_path))
    regions = [(r.guest, r.size) for r in machine.native_regions()]
    recorded = [(int(a), int(b)) for a, b in artifact.get("regions") or []]
    if recorded and recorded != regions:
        return None
    t0 = time.perf_counter()
    verification = verify_cases(pe_path, artifact, smoke or SMOKE)
    elapsed = time.perf_counter() - t0
    if not verification["passed"]:
        return None
    return {"manifest": manifest, "smoke": verification, "check_seconds": elapsed}


def selftest(pe_path: Path, corpus: list[str] | None = None,
             profile_args: list[str] | None = None) -> dict:
    """Can the oracle fail? Build a deliberately wrong trace and check."""
    pe_path = Path(pe_path)
    corpus = corpus or ["1", "64", "1024"]
    outdir = util.ensure_dir(util.BUILD / "pe" / (pe_path.stem + "_mutant"))
    mutant = build_artifact(pe_path, outdir, pin=True, mutate="perturb_load",
                            profile_args=profile_args)
    if not mutant.get("ok"):
        return {"mutant_built": False, "reason": mutant.get("reason")}
    verification = verify_cases(pe_path, mutant, corpus)
    failures = [c["argv"] for c in verification["results"] if not c["match"]]
    return {
        "mutant_built": True,
        "mutation": "one loaded value bit-flipped in the emitted trace",
        "cases": corpus,
        "rejected": not verification["passed"],
        "failing_cases": failures,
        "first_diff": next((diff_summary(c) for c in verification["results"]
                            if not c["match"]), None),
    }


def rollback_check(pe_path: Path, artifact: dict, argv: str,
                   heap_size: int = 4 << 20) -> dict:
    """A machine with a different memory layout must demote, not mis-run."""
    pe_path = Path(pe_path)
    machine = Machine(pe_path, heap_size=heap_size)
    dispatcher = TraceDispatcher(Path(artifact["dylib"]), artifact["head"],
                                 artifact.get("regions"))
    machine.set_dispatcher(dispatcher)
    spec = observe(machine.run_entry([argv]))
    base_machine = Machine(pe_path, heap_size=heap_size)
    base = observe(base_machine.run_entry([argv]))
    fields = ("writes", "exit_code", "fault", "image_digest")
    return {
        "heap_size": heap_size,
        "demoted": dispatcher.stats()["demotions"] > 0,
        "region_mismatch": dispatcher.stats()["region_mismatch"],
        "output_matches": all(getattr(spec, f) == getattr(base, f) for f in fields),
    }


def invalidation_check(pe_path: Path, workdir: Path | None = None) -> dict:
    """A one-byte change to the image must miss the cache."""
    pe_path = Path(pe_path)
    data = bytearray(pe_path.read_bytes())
    # Flip a byte inside the read-only data section, leaving code untouched.
    from .image import PEImage

    img = PEImage.load(pe_path)
    written = False
    for s in img.sections:
        if s.name == ".rdata" and s.raw_size > 64:
            data[s.raw_ptr + s.raw_size // 2] ^= 0x01
            written = True
            break
    if not written:
        return {"tested": False, "reason": "no .rdata to modify"}
    workdir = util.ensure_dir(workdir or (util.BUILD / "pe" / "invalidate"))
    copy = workdir / (pe_path.stem + "_modified.exe")
    copy.write_bytes(bytes(data))
    return {
        "tested": True,
        "original_sha256": image_sha(pe_path),
        "modified_sha256": image_sha(copy),
        "original_hit": find_cached(pe_path) is not None,
        "modified_hit": find_cached(copy) is not None,
        "modified_exe": str(copy),
    }


def run_case(pe_path: Path, argv: list[str], manifest: dict | None) -> Observation:
    if manifest is None:
        return run_baseline(pe_path, argv)
    return run_with_artifact(pe_path, manifest["artifact"], argv)


def bench(pe_path: Path, manifest: dict, argv: list[str], repeats: int = 7) -> dict:
    """Interleaved fresh-process runs of each arm.

    Arms:
      baseline    - the PE under the emulator, no Porte involvement
      translated  - a lifted trace with generic memory handling (no pins)
      specialized - the cached artifact (profile-pinned accesses)
      native      - optional native ARM64 reference built by the fixture side
    """
    pe_path = Path(pe_path)
    arms: dict[str, list[float]] = {}

    generic = build_artifact(
        pe_path, util.ensure_dir(util.BUILD / "pe" / (pe_path.stem + "_generic")),
        pin=False, profile_args=manifest["artifact"].get("profile_args"))
    if generic.get("ok"):
        generic["regions"] = manifest["artifact"]["regions"]

    samples = {name: [] for name in ("baseline", "translated", "specialized")}
    for _ in range(repeats):
        # Interleave so thermal drift and background load hit all arms alike.
        for arm in ("baseline", "translated", "specialized"):
            if arm == "baseline":
                obs = run_baseline(pe_path, argv)
            elif arm == "translated":
                if not generic.get("ok"):
                    continue
                obs = run_with_artifact(pe_path, generic, argv)
            else:
                obs = run_with_artifact(pe_path, manifest["artifact"], argv)
            samples[arm].append(obs.wall_ns / 1e6)

    result = {
        "argv": argv,
        "repeats": repeats,
        "arms_ms": {k: {"median": _median(v), "min": min(v) if v else None,
                        "samples": v} for k, v in samples.items()},
    }
    base = result["arms_ms"]["baseline"]["median"]
    for arm in ("translated", "specialized"):
        med = result["arms_ms"][arm]["median"]
        result["arms_ms"][arm]["speedup_vs_baseline"] = base / med if med else None
    stale = manifest["artifact"].get("dylib_sha256") != util.sha256_file(
        Path(manifest["artifact"]["dylib"]))
    result["artifact_unchanged"] = not stale
    return result


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


def describe(pe_path: Path) -> dict:
    """Static inventory: sections, imports, entry point, no symbol names."""
    from .image import PEImage

    img = PEImage.load(Path(pe_path))
    return {
        "image": img.path.name,
        "sha256": util.sha256_file(Path(pe_path)),
        "bytes": Path(pe_path).stat().st_size,
        "image_base": hex(img.image_base),
        "entry_rva": hex(img.entry_rva),
        "sections": [{"name": s.name, "va": hex(s.va), "raw": s.raw_size}
                     for s in img.sections],
        "imports": sorted({f"{i.dll}!{i.name}" for i in img.imports if i.name}),
        "exports": sorted(img.exports),
        "relocations": len(img.relocations),
        "stripped_exports": len(img.exports) == 0,
    }
