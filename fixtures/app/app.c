/* Deterministic workload driver for the Porte experiments.
 *
 * The same source is built for x86-64 (executed through Rosetta, the baseline
 * translation environment) and for arm64 (native baseline and nativeized
 * arms). It owns input generation, the ABI-boundary observations, timing and
 * result digests, and nothing else - so all arms run identical application
 * code and differ only in which module image answers the kernel call.
 */
#define _XOPEN_SOURCE 700

#include "porte_probe.h"
#include "host.h"
#include "kernel_contract.h"

#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

typedef int64_t (*rank_fn)(const int32_t *, const int32_t *, int64_t,
                           int32_t *, int64_t);
typedef int64_t (*audit_fn)(const int32_t *, const int32_t *, int64_t, int32_t *);

typedef struct {
    const char *name;
    int kernel;
    int64_t n;
    int64_t q;
    int32_t params[4];
    uint32_t range_mask; /* 0 means full 32-bit range */
    int misalign_ints;
} workload;

static const workload WORKLOADS[] = {
    {"rank_small_range", PORTE_KERNEL_RANK, 2000000, 200000, {12345, 0, 0, 0}, 4095u, 0},
    {"rank_wide",        PORTE_KERNEL_RANK, 2000000, 200000, {12345, 0, 0, 0}, 0u, 0},
    {"rank_unaligned",   PORTE_KERNEL_RANK, 2000000, 200000, {12345, 0, 0, 0}, 4095u, 1},
    {"rank_tiny",        PORTE_KERNEL_RANK, 16,      8,      {12345, 0, 0, 0}, 4095u, 0},
    {"audit_basic",      PORTE_KERNEL_AUDIT, 2000000, 0,     {0, 0, 0, 0},     0u, 0},
    {"audit_interleaved",PORTE_KERNEL_AUDIT, 2000000, 0,     {0, 1, 0, 0},     0u, 0},
    {NULL, 0, 0, 0, {0, 0, 0, 0}, 0u, 0},
};

static double now_sec(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

static uint64_t splitmix64(uint64_t *s) {
    uint64_t z = (*s += 0x9e3779b97f4a7c15ull);
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ull;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebull;
    return z ^ (z >> 31);
}

static uint64_t digest_bytes(const void *p, size_t bytes) {
    const uint8_t *b = (const uint8_t *)p;
    uint64_t h = 1469598103934665603ull;
    for (size_t i = 0; i < bytes; i++) h = (h ^ b[i]) * 1099511628211ull;
    return h;
}

static void json_escape(const char *s, char *out, size_t cap) {
    size_t j = 0;
    for (size_t i = 0; s[i] && j + 2 < cap; i++) {
        char c = s[i];
        if (c == '"' || c == '\\') { out[j++] = '\\'; out[j++] = c; }
        else if ((unsigned char)c < 0x20) { out[j++] = '?'; }
        else out[j++] = c;
    }
    out[j] = 0;
}

int main(int argc, char **argv) {
    const char *kernel_path = NULL, *workload_name = NULL, *ledger_path = NULL;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--kernel") && i + 1 < argc) kernel_path = argv[++i];
        else if (!strcmp(argv[i], "--workload") && i + 1 < argc) workload_name = argv[++i];
        else if (!strcmp(argv[i], "--ledger") && i + 1 < argc) ledger_path = argv[++i];
    }
    if (!kernel_path || !workload_name) {
        fprintf(stderr, "usage: %s --kernel <dylib> --workload <name> [--ledger <path>]\n", argv[0]);
        return 2;
    }

    const workload *w = NULL;
    for (int i = 0; WORKLOADS[i].name; i++)
        if (!strcmp(WORKLOADS[i].name, workload_name)) w = &WORKLOADS[i];
    if (!w) { fprintf(stderr, "unknown workload %s\n", workload_name); return 2; }

    porte_probe_start("app", argv[0]);

    void *h = dlopen(kernel_path, RTLD_NOW | RTLD_LOCAL);
    if (!h) { fprintf(stderr, "dlopen: %s\n", dlerror()); return 3; }
    porte_probe_register_module(kernel_path);
    rank_fn rank = (rank_fn)dlsym(h, "porte_kernel_rank");
    audit_fn audit = (audit_fn)dlsym(h, "porte_kernel_audit");
    const char *(*impl)(void) = (const char *(*)(void))dlsym(h, "porte_kernel_impl");
    int64_t (*counters)(int) = (int64_t (*)(int))dlsym(h, "porte_kernel_counters");
    if (!rank || !audit) { fprintf(stderr, "dlsym: missing kernel entry\n"); return 3; }

    int64_t n = w->n, q = w->q;
    int32_t *params = (int32_t *)calloc(8, sizeof(int32_t));
    memcpy(params, w->params, sizeof(w->params));

    /* Buffers are 64-byte aligned unless the case asks for a misaligned src,
     * which is how a guard-violation case reaches the general path. */
    int32_t *vals = NULL, *out = NULL;
    if (posix_memalign((void **)&vals, 64, (size_t)(n + 8) * sizeof(int32_t)) != 0) return 4;
    if (posix_memalign((void **)&out, 64, (size_t)(n + 8) * sizeof(int32_t)) != 0) return 4;
    int32_t *src = vals + w->misalign_ints;

    uint64_t seed = 0x243f6a8885a308d3ull ^ (uint64_t)n ^ ((uint64_t)w->range_mask << 17);
    for (int64_t i = 0; i < n; i++) {
        uint64_t r = splitmix64(&seed);
        src[i] = w->range_mask ? (int32_t)(r & w->range_mask) : (int32_t)(r >> 32);
    }
    memset(out, 0, (size_t)(n + 8) * sizeof(int32_t));
    host_reset();

    int64_t ret = 0;
    if (w->kernel == PORTE_KERNEL_RANK) {
        porte_probe_boundary(PORTE_KERNEL_RANK, n, q, 0, (uintptr_t)params,
                             (uintptr_t)src, (uintptr_t)out, 0);
    } else {
        porte_probe_boundary(PORTE_KERNEL_AUDIT, n, 0, (uint32_t)params[1],
                             (uintptr_t)params, (uintptr_t)src, (uintptr_t)out, 0);
    }
    porte_probe_sample_start();
    double t0 = now_sec();
    ret = (w->kernel == PORTE_KERNEL_RANK) ? rank(params, src, n, out, q)
                                          : audit(params, src, n, out);
    double t1 = now_sec();
    porte_probe_sample_stop();

    size_t out_bytes = (size_t)(w->kernel == PORTE_KERNEL_RANK ? q : n) * sizeof(int32_t);
    if (ledger_path) host_ledger_write(ledger_path);

    char impl_buf[128], wl_buf[64];
    json_escape(impl && impl() ? impl() : "unknown", impl_buf, sizeof impl_buf);
    json_escape(w->name, wl_buf, sizeof wl_buf);

    printf("{\"workload\":\"%s\",\"kernel\":\"%s\",\"impl\":\"%s\","
           "\"ret\":%lld,\"out_digest\":\"%016llx\",\"kernel_ns\":%lld,"
           "\"transitions\":%lld,\"entries\":%lld,\"fast\":%lld,\"fallback\":%lld}\n",
           wl_buf,
           w->kernel == PORTE_KERNEL_RANK ? "rank" : "audit",
           impl_buf,
           (long long)ret,
           (unsigned long long)digest_bytes(out, out_bytes),
           (long long)((t1 - t0) * 1e9),
           (long long)host_transitions(), (long long)host_entries(),
           (long long)(counters ? counters(0) : 0),
           (long long)(counters ? counters(1) : 0));

    porte_probe_flush();
    return 0;
}
