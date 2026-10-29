/* A *second* generic baseline, used to remove a confound in the main
 * comparison: libc qsort() calls a comparator through a function pointer for
 * every comparison, which no compiler can inline across the libc boundary.
 * This module implements the same contract with an in-module quicksort whose
 * comparisons are inlined, so a specialization has to beat good generic code
 * rather than a library call pattern.
 */
#include "host.h"
#include "kernel_contract.h"

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define API __attribute__((visibility("default")))

static inline void swap_i32(int32_t *a, int32_t *b) {
    int32_t t = *a;
    *a = *b;
    *b = t;
}

static void insertion_sort(int32_t *a, int64_t lo, int64_t hi) {
    for (int64_t i = lo + 1; i <= hi; i++) {
        int32_t x = a[i];
        int64_t j = i - 1;
        while (j >= lo && a[j] > x) {
            a[j + 1] = a[j];
            j--;
        }
        a[j + 1] = x;
    }
}

static void quicksort_i32(int32_t *a, int64_t lo, int64_t hi) {
    while (lo < hi) {
        if (hi - lo < 24) {
            insertion_sort(a, lo, hi);
            return;
        }
        int64_t mid = lo + (hi - lo) / 2;
        if (a[mid] < a[lo]) swap_i32(&a[mid], &a[lo]);
        if (a[hi] < a[lo]) swap_i32(&a[hi], &a[lo]);
        if (a[hi] < a[mid]) swap_i32(&a[hi], &a[mid]);
        int32_t pivot = a[mid];
        int64_t i = lo, j = hi;
        while (i <= j) {
            while (a[i] < pivot) i++;
            while (a[j] > pivot) j--;
            if (i <= j) {
                swap_i32(&a[i], &a[j]);
                i++;
                j--;
            }
        }
        if (j - lo < hi - i) {
            quicksort_i32(a, lo, j);
            lo = i;
        } else {
            quicksort_i32(a, i, hi);
            hi = j;
        }
    }
}

API int64_t porte_kernel_rank_generic(const int32_t *params, const int32_t *vals,
                                      int64_t n, int32_t *out, int64_t q) {
    if (n <= 0 || q < 0) return -1;
    int32_t *scratch = (int32_t *)malloc((size_t)n * sizeof(int32_t));
    if (!scratch) return -2;
    memcpy(scratch, vals, (size_t)n * sizeof(int32_t));
    quicksort_i32(scratch, 0, n - 1);
    int64_t r = porte_rank_result(scratch, n, params, out, q);
    free(scratch);
    return r;
}

API int64_t porte_kernel_rank(const int32_t *params, const int32_t *vals,
                              int64_t n, int32_t *out, int64_t q) {
    return porte_kernel_rank_generic(params, vals, n, out, q);
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
        host_emit(0x41554449, key, val);
        out[i] = val;
        acc += (uint64_t)(uint32_t)key;
    }
    return (int64_t)acc;
}

API int64_t porte_kernel_audit(const int32_t *params, const int32_t *recs,
                               int64_t n, int32_t *out) {
    return porte_kernel_audit_generic(params, recs, n, out);
}

API const char *porte_kernel_impl(void) { return "generic-inline-sort"; }
API uint64_t porte_kernel_caps(void) { return 0; }
API int64_t porte_kernel_counters(int which) { (void)which; return 0; }
