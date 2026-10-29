"""Component C: the differential correctness oracle.

Three arms are compared for every corpus case:

  baseline    x86-64 generic module, executed through Rosetta
  native      arm64 generic module, executed natively
  candidate   arm64 specialization, executed natively

A candidate is admitted only when, for every case, the return value, the
output digest, the host-effect ledger and the exit status agree, and the
fast path was actually taken exactly where the profile said it should be.
Cases that must fall back are part of the corpus, so a guard that silently
stops guarding is a failure, not a speedup.
"""
from __future__ import annotations

from pathlib import Path

from . import build, profile, util

CORPUS = list(profile.WORKLOADS.keys())

# Whether the specialization is expected to take its fast path.
EXPECT_FAST = {
    "rank_small_range": True,
    "rank_wide": False,
    "rank_unaligned": True,   # counting sort does not require alignment
    "rank_tiny": False,
    "audit_basic": True,
    "audit_interleaved": False,
}


def _ledger_bytes(path: Path) -> bytes:
    return path.read_bytes() if path.exists() else b""


def corpus_descriptor() -> dict:
    return {
        name: {"kernel": meta["kernel"], "n": meta["n"],
               "q": meta.get("q", 0), "expect_fast": EXPECT_FAST[name]}
        for name, meta in profile.WORKLOADS.items()
    }


def verify_candidate(spec_dylib: Path, corpus: list[str] | None = None,
                     workdir: Path | None = None) -> dict:
    corpus = corpus or CORPUS
    workdir = util.ensure_dir(workdir or (util.BUILD / "oracle"))
    arms = {
        "baseline": ("x86_64", build.paths_for("x86_64")["generic"]),
        "native": ("arm64", build.paths_for("arm64")["generic"]),
        "candidate": ("arm64", Path(spec_dylib)),
    }
    cases = []
    passed = True
    for wl in corpus:
        rows = {}
        ledgers = {}
        for arm, (arch, kernel) in arms.items():
            ledger = workdir / f"{wl}.{arm}.ledger"
            if ledger.exists():
                ledger.unlink()
            run = profile.run_app(arch, kernel, wl, ledger=ledger, repeat_warm=0)
            rows[arm] = run
            ledgers[arm] = _ledger_bytes(ledger)
        base, native, cand = rows["baseline"], rows["native"], rows["candidate"]
        checks = {
            "return_matches_baseline": cand.ret == base.ret,
            "return_matches_native": cand.ret == native.ret,
            "output_matches_baseline": cand.out_digest == base.out_digest,
            "output_matches_native": cand.out_digest == native.out_digest,
            "ledger_matches_baseline": ledgers["candidate"] == ledgers["baseline"],
            "ledger_matches_native": ledgers["candidate"] == ledgers["native"],
            "native_matches_baseline": native.ret == base.ret
            and native.out_digest == base.out_digest
            and ledgers["native"] == ledgers["baseline"],
        }
        expect_fast = EXPECT_FAST[wl]
        if expect_fast:
            checks["fast_path_taken"] = cand.fast >= 1 and cand.fallback == 0
        else:
            checks["fallback_taken"] = cand.fallback >= 1
        ok = all(checks.values())
        passed = passed and ok
        cases.append({
            "workload": wl,
            "kernel": profile.WORKLOADS[wl]["kernel"],
            "expected_fast": expect_fast,
            "checks": checks,
            "ok": ok,
            "candidate": {
                "impl": cand.impl, "fast": cand.fast, "fallback": cand.fallback,
                "ret": cand.ret, "out_digest": cand.out_digest,
            },
            "baseline": {"ret": base.ret, "out_digest": base.out_digest,
                         "ledger_bytes": len(ledgers["baseline"])},
            "native": {"ret": native.ret, "out_digest": native.out_digest,
                       "ledger_bytes": len(ledgers["native"])},
            "candidate_ledger_bytes": len(ledgers["candidate"]),
        })

    return {
        "schema": 1,
        "passed": passed,
        "cases": cases,
        "corpus": corpus,
        "corpus_digest": util.digest_json(corpus_descriptor()),
        "candidate_dylib": str(spec_dylib),
        "candidate_sha256": util.sha256_file(Path(spec_dylib)),
        "host": util.host_info(),
        "when": util.toolchain_info(),
    }


def smoke(spec_dylib: Path, workdir: Path | None = None) -> dict:
    """Cheap re-verification used when loading a cached artifact."""
    return verify_candidate(spec_dylib,
                            corpus=["rank_small_range", "audit_basic", "rank_tiny"],
                            workdir=workdir)


MUTANT_RANK = """
/* Deliberately broken specialization: prefix sums start at 1, so the sorted\n
 * permutation is shifted by one slot. Used only to prove the oracle fails. */

#include "kernel_contract.h"
#include <stdint.h>
#include <stdlib.h>

#define API __attribute__((visibility("default")))

extern int64_t porte_kernel_rank_generic(const int32_t *, const int32_t *,
                                         int64_t, int32_t *, int64_t);

extern int64_t porte_kernel_audit_generic(const int32_t *, const int32_t *,
                                          int64_t, int32_t *);
extern int host_has_batch(void);

API int64_t porte_kernel_rank(const int32_t *params, const int32_t *vals,
                              int64_t n, int32_t *out, int64_t q) {
    if (n < 1024 || q < 0) return porte_kernel_rank_generic(params, vals, n, out, q);
    int32_t lo = INT32_MAX, hi = INT32_MIN;
    for (int64_t i = 0; i < n; i++) {
        int32_t v = vals[i];
        lo = v < lo ? v : lo;
        hi = v > hi ? v : hi;
    }
    int64_t s = (int64_t)hi - (int64_t)lo + 1;
    if (lo < 0 || hi > 4095 || s > 4096)
        return porte_kernel_rank_generic(params, vals, n, out, q);
    uint32_t *count = (uint32_t *)calloc((size_t)s, sizeof(uint32_t));
    int32_t *scratch = (int32_t *)malloc((size_t)n * sizeof(int32_t));
    if (!count || !scratch) { free(count); free(scratch);
        return porte_kernel_rank_generic(params, vals, n, out, q); }
    for (int64_t i = 0; i < n; i++) count[(uint32_t)(vals[i] - lo)]++;
    uint32_t running = 1;  /* the injected bug */
    for (int64_t c = 0; c < s; c++) { uint32_t t = count[c]; count[c] = running; running += t; }
    for (int64_t i = 0; i < n; i++)
        scratch[count[(uint32_t)(vals[i] - lo)]++] = vals[i];
    int64_t r = porte_rank_result(scratch, n, params, out, q);
    free(count); free(scratch);
    return r;
}

API int64_t porte_kernel_audit(const int32_t *params, const int32_t *recs,
                               int64_t n, int32_t *out) {
    return porte_kernel_audit_generic(params, recs, n, out);
}
API const char *porte_kernel_impl(void) { return "mutant:rank-off-by-one"; }
API uint64_t porte_kernel_caps(void) { return 1; }
API int64_t porte_kernel_counters(int which) { (void)which; return 0; }
"""


def mutation_test(workdir: Path | None = None) -> dict:
    """The oracle must reject a specialization with a known semantic bug.

    A test suite that cannot fail is not evidence. This compiles a module with
    an off-by-one in the prefix sums and asserts the differential verdict is
    negative, reporting which checks caught it.
    """
    from . import build

    workdir = util.ensure_dir(workdir or (util.BUILD / "mutation"))
    c_path = workdir / "mutant.c"
    c_path.write_text(MUTANT_RANK)
    dylib = workdir / "libporte_kernel_mutant_arm64.dylib"
    build.build_specialization(c_path, dylib, "arm64",
                               build.paths_for("arm64")["generic"])
    verdict = verify_candidate(dylib, corpus=["rank_small_range", "rank_unaligned"],
                               workdir=workdir)
    caught = []
    for case in verdict["cases"]:
        caught.extend(k for k, v in case["checks"].items() if not v)
    return {
        "mutant": "rank-counting with prefix sums starting at 1",
        "dylib": str(dylib),
        "oracle_rejected": not verdict["passed"],
        "failed_checks": sorted(set(caught)),
        "verdict": verdict,
    }
