"""End-to-end experiment driver.

profile -> specialize -> verify -> cache -> measure, with the cost of every
stage recorded so the cold path and the warm path can be compared honestly.
"""
from __future__ import annotations

import time
from pathlib import Path

from . import bench, build, cache, macho, profile, specialize, util, verify

PROFILE_WORKLOADS = ["rank_small_range", "audit_basic"]
BENCH_WORKLOADS = list(profile.WORKLOADS.keys())


def _analyze(path: Path) -> dict:
    blks = macho.blocks(path)
    return {
        "path": str(path),
        "sha256": util.sha256_file(path),
        "arch": macho.arch_of(path),
        "blocks": len(blks),
        "instructions": sum(b.ninstr for b in blks),
        "indirect_blocks": sum(1 for b in blks if b.ends_indirect),
        "hot_candidates": [
            {"function": b.func, "start": hex(b.start), "instructions": b.ninstr}
            for b in sorted(blks, key=lambda b: -b.ninstr)[:10]
        ],
    }


def run(repeats: int = 7, force_build: bool = False,
        arch: str = "arm64") -> dict:
    started = time.perf_counter_ns()

    t = time.perf_counter_ns()
    build.build_all(force=force_build)
    build_ns = time.perf_counter_ns() - t

    paths = build.paths_for("x86_64")
    analysis = _analyze(paths["generic"])

    # --- cold: profile ---------------------------------------------------
    t = time.perf_counter_ns()
    profiles = {wl: profile.profile_workload(wl) for wl in PROFILE_WORKLOADS}
    profile_ns = time.perf_counter_ns() - t
    for wl, prof in profiles.items():
        util.write_json(util.PROFILES / f"{wl}.json", prof)

    plans = specialize.plan_from_profiles(profiles)
    if not plans:
        raise RuntimeError("profiles did not justify any specialization")

    # --- cold: compile ---------------------------------------------------
    t = time.perf_counter_ns()
    staging = util.BUILD / "spec" / "staging"
    artifacts = specialize.compile_plans(plans, arch, staging)
    compile_ns = time.perf_counter_ns() - t

    # --- cold: verify ----------------------------------------------------
    t = time.perf_counter_ns()
    verification = verify.verify_candidate(Path(artifacts["dylib"]))
    verify_ns = time.perf_counter_ns() - t

    if not verification["passed"]:
        util.write_json(util.RESULTS / "experiment.json", {
            "verdict": "rejected", "analysis": analysis,
            "profiles": profiles, "artifacts": artifacts,
            "verification": verification,
        })
        raise RuntimeError("candidate rejected by the differential oracle")

    # The oracle must be able to fail: a known-buggy module has to be rejected
    # before any artifact is admitted to the cache.
    mutation = verify.mutation_test()
    if not mutation["oracle_rejected"]:
        raise RuntimeError("differential oracle accepted a known-buggy module")

    # --- measure ---------------------------------------------------------
    t = time.perf_counter_ns()
    bench_results = bench.run_bench(Path(artifacts["dylib"]), BENCH_WORKLOADS,
                                    repeats=repeats)
    bench_ns = time.perf_counter_ns() - t

    # --- cache -----------------------------------------------------------
    env = cache.environment(
        arch=arch,
        app_sha=util.sha256_file(build.paths_for(arch)["app"]),
        module_sha=util.sha256_file(build.paths_for(arch)["generic"]),
        profile=profiles["rank_small_range"],
        corpus=verify.corpus_descriptor(),
        transform={**artifacts["plan"],
                   "source_sha256": artifacts["source_sha256"]},
    )
    key = cache.key_of(env)
    cache.store(env, artifacts, {"passed": verification["passed"],
                                 "cases": len(verification["cases"])},
                {w: {"speedup_vs_native_generic": r["speedup_vs_native_generic"],
                     "speedup_vs_translated_baseline":
                         r["speedup_vs_translated_baseline"]}
                 for w, r in bench_results["workloads"].items()})

    # --- warm ------------------------------------------------------------
    t = time.perf_counter_ns()
    loaded = cache.load(key)
    load_ns = time.perf_counter_ns() - t
    t = time.perf_counter_ns()
    smoke = verify.smoke(Path(loaded["artifacts"]["dylib"]))
    smoke_ns = time.perf_counter_ns() - t

    # --- cold start / translation overhead -------------------------------
    cold_start = {
        "translated_x86_64": profile.measure_cold_start("x86_64"),
        "native_arm64_control": profile.measure_cold_start("arm64"),
    }

    # --- invalidation demonstration -------------------------------------
    mutated_os = dict(env)
    mutated_os["host"] = dict(env["host"])
    mutated_os["host"]["os_build"] = (env["host"]["os_build"] or "") + "-sim"
    mutated_module = dict(env)
    mutated_module["module_sha256"] = "0" * 64

    result = {
        "schema": 1,
        "verdict": "admitted" if smoke["passed"] else "admitted-unverified",
        "arch": arch,
        "host": util.host_info(),
        "toolchain": util.toolchain_info(),
        "analysis": analysis,
        "profiles": {
            wl: {
                "runtime": p["runtime"],
                "sampling": p["sampling"],
                "boundary": p["boundary"],
                "facts": p["facts"],
                "hottest_functions": p["hottest_functions"][:6],
                "hottest_blocks": p["hottest_blocks"][:6],
                "transition_accounting": p["transition_accounting"],
            }
            for wl, p in profiles.items()
        },
        "plans": [p.to_dict() for p in plans],
        "artifacts": artifacts,
        "verification": verification,
        "oracle_mutation": {k: v for k, v in mutation.items() if k != "verdict"},
        "cold": {"build_ns": build_ns, "profile_ns": profile_ns,
                 "compile_ns": compile_ns, "verify_ns": verify_ns,
                 "bench_ns": bench_ns},
        "warm": {"cache_load_ns": load_ns, "smoke_verify_ns": smoke_ns,
                 "smoke_passed": smoke["passed"]},
        "cold_start": cold_start,
        "cache": {
            "key": key,
            "environment": env,
            "reasons_on_os_change": cache.invalidation_reasons(env, mutated_os),
            "reasons_on_module_change": cache.invalidation_reasons(env, mutated_module),
        },
        "bench": bench_results,
        "total_ns": time.perf_counter_ns() - started,
    }
    util.write_json(util.RESULTS / "experiment.json", result)
    return result


def warm(key: str | None = None) -> dict:
    """Reload a verified artifact in a fresh process and re-establish its key.

    This is the cross-launch path: recompute the environment from the saved
    profile and the current binaries, compare it with the stored manifest,
    then smoke-verify the artifact before using it.
    """
    stored = cache.load(key) if key else cache.latest()
    if not stored:
        return {"found": False}
    env = stored["env"]
    profile_path = util.PROFILES / f"{env.get('profile_workload', 'rank_small_range')}.json"
    if not profile_path.exists():
        profile_path = util.PROFILES / "rank_small_range.json"
    prof = util.read_json(profile_path)
    transform = {
        "transform": env["transform"],
        "version": env["transform_version"],
        "source_sha256": env["transform_source_sha256"],
        "porte_version": env["porte_version"],
    }
    current = cache.environment(
        arch=env["arch"],
        app_sha=util.sha256_file(build.paths_for(env["arch"])["app"]),
        module_sha=util.sha256_file(build.paths_for(env["arch"])["generic"]),
        profile=prof, corpus=verify.corpus_descriptor(), transform=transform)
    reasons = cache.invalidation_reasons(env, current)
    key_now = cache.key_of(current)
    source_ok = util.sha256_file(Path(stored["artifacts"]["source"])) == \
        env["transform_source_sha256"]
    report = {
        "found": True, "stored_key": stored["key"], "recomputed_key": key_now,
        "key_matches": key_now == stored["key"], "reasons": reasons,
        "source_hash_matches": source_ok,
        "artifact": stored["artifacts"]["dylib"],
    }
    if report["key_matches"] and source_ok:
        report["smoke"] = verify.smoke(Path(report["artifact"]))
        report["smoke_passed"] = report["smoke"]["passed"]
        report["bench"] = bench.bench_workload("rank_small_range",
                                               Path(report["artifact"]), repeats=3)
    return report


def summary_text(result: dict) -> str:
    lines = []
    lines.append(f"verdict: {result['verdict']}")
    lines.append(f"cache key: {result['cache']['key']}")
    lines.append("")
    for wl, r in result["bench"]["workloads"].items():
        lines.append(
            f"{wl:18s} spec/native-generic {r['speedup_vs_native_generic']:6.2f}x   "
            f"spec/translated {r['speedup_vs_translated_baseline']:6.2f}x   "
            f"fast={r['summary']['nativeized']['fast']} "
            f"fallback={r['summary']['nativeized']['fallback']}")
    lines.append("")
    c = result["cold"]
    w = result["warm"]
    lines.append(f"cold: profile {util.human_ns(c['profile_ns'])}, "
                 f"compile {util.human_ns(c['compile_ns'])}, "
                 f"verify {util.human_ns(c['verify_ns'])}")
    lines.append(f"warm: cache load {util.human_ns(w['cache_load_ns'])}, "
                 f"smoke verify {util.human_ns(w['smoke_verify_ns'])}")
    return "\n".join(lines)
