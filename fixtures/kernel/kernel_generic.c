/* Generic (baseline) implementation of the fixture module.
 *
 * This is the module a translator would see and translate as-is. The
 * instrumented build (-DPORTE_PROFILE) additionally reports data-distribution
 * facts that a translated run cannot observe without instrumentation.
 */
#include "porte_probe.h"
#include "host.h"
#include "kernel_contract.h"

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define API __attribute__((visibility("default")))

static int cmp_i32(const void *a, const void *b) {
    int32_t x = *(const int32_t *)a;
    int32_t y = *(const int32_t *)b;
    return (x > y) - (x < y);
}

API int64_t porte_kernel_rank_generic(const int32_t *params, const int32_t *vals,
                                      int64_t n, int32_t *out, int64_t q) {
    if (n <= 0 || q < 0) return -1;
    int32_t *scratch = (int32_t *)malloc((size_t)n * sizeof(int32_t));
    if (!scratch) return -2;
    memcpy(scratch, vals, (size_t)n * sizeof(int32_t));

#if defined(PORTE_PROFILE)
    /* Observation pass. Compiled only into the profiling build; its cost is
     * measured against the plain build and reported. */
    {
        int32_t mn = vals[0], mx = vals[0];
        for (int64_t i = 1; i < n; i++) {
            if (vals[i] < mn) mn = vals[i];
            if (vals[i] > mx) mx = vals[i];
        }
        porte_probe_fact_minmax(PORTE_KERNEL_RANK, mn, mx, n);
    }
#endif

    qsort(scratch, (size_t)n, sizeof(int32_t), cmp_i32);
    int64_t r = porte_rank_result(scratch, n, params, out, q);
    free(scratch);
    return r;
}

API int64_t porte_kernel_audit_generic(const int32_t *params,
                                       const int32_t *recs, int64_t n,
                                       int32_t *out) {
    if (n < 0) return -1;
    int32_t strict = params[1];
    uint64_t acc = 0;
    for (int64_t i = 0; i < n; i++) {
        int32_t v = recs[i];
        int32_t key = porte_audit_key(v);
        int32_t val = porte_audit_val(v, strict);
        host_emit(0x41554449, key, val); /* one host transition per record */
        out[i] = val;
        acc += (uint64_t)(uint32_t)key;
    }
    return (int64_t)acc;
}

API int64_t porte_kernel_rank(const int32_t *params, const int32_t *vals,
                              int64_t n, int32_t *out, int64_t q) {
    return porte_kernel_rank_generic(params, vals, n, out, q);
}

API int64_t porte_kernel_audit(const int32_t *params, const int32_t *recs,
                               int64_t n, int32_t *out) {
    return porte_kernel_audit_generic(params, recs, n, out);
}

API const char *porte_kernel_impl(void) { return "generic"; }
API uint64_t porte_kernel_caps(void) { return 0; }
API int64_t porte_kernel_counters(int which) { (void)which; return 0; }
