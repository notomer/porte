/* The observable contract of the fixture module.
 *
 * Everything in this header is compiled into BOTH the generic module and any
 * Porte-generated specialization, from the same source text. A specialization
 * may change how work is scheduled, but it may not re-implement the contract:
 * that keeps the differential oracle focused on the transformation itself
 * rather than on transcription differences between two hand-written copies.
 */
#ifndef PORTE_KERNEL_CONTRACT_H
#define PORTE_KERNEL_CONTRACT_H

#include <stdint.h>

#define PORTE_KERNEL_RANK  1
#define PORTE_KERNEL_AUDIT 2

/* --- rank kernel ------------------------------------------------------- */
/* sorted[0..n) is ascending; output is q deterministic probes of it. */

static inline void porte_rank_queries(const int32_t *sorted, int64_t n,
                                      const int32_t *params, int32_t *out,
                                      int64_t q) {
    uint64_t base = (uint64_t)(uint32_t)params[0];
    for (int64_t i = 0; i < q; i++) {
        uint64_t idx = ((uint64_t)i * 2654435761u + base) % (uint64_t)n;
        out[i] = sorted[idx];
    }
}

static inline int64_t porte_rank_result(const int32_t *sorted, int64_t n,
                                        const int32_t *params, int32_t *out,
                                        int64_t q) {
    uint32_t h = 0;
    for (int64_t i = 0; i < n; i++) h = (h ^ (uint32_t)sorted[i]) * 16777619u;
    porte_rank_queries(sorted, n, params, out, q);
    return (int64_t)((uint64_t)h ^ (uint64_t)n);
}

/* --- audit kernel ------------------------------------------------------ */
/* Per-record transformation and the host effect it produces are defined once
 * here, so a fused implementation produces byte-identical effects. */

static inline int32_t porte_audit_key(int32_t v) {
    return (int32_t)((uint32_t)v * 2654435761u);
}

static inline int32_t porte_audit_val(int32_t v, int32_t strict) {
    int32_t val = v ^ (v >> 7);
    if (strict && (val & 1)) val = -val;
    return val;
}

#endif /* PORTE_KERNEL_CONTRACT_H */
