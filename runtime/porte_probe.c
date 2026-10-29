#define _XOPEN_SOURCE 700

#include "porte_probe.h"

#include <mach/mach_time.h>
#include <mach-o/dyld.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <ucontext.h>
#include <unistd.h>

#define MAX_SAMPLES   (1u << 18)
#define MAX_BOUNDARY  4096u
#define MAX_FACTS     512u

typedef struct {
    int32_t kernel_id;
    int64_t n, q;
    uint32_t flags;
    uintptr_t p, src, dst, aux;
    uint64_t t_ns;
} boundary_rec;

typedef struct {
    int32_t kernel_id;
    int32_t mn, mx;
    int64_t n;
} fact_rec;

static int g_enabled;
static const char *g_path;
static const char *g_tag = "module";
static const char *g_module_path = "";
static uintptr_t g_slide;

static volatile uint64_t g_samples[MAX_SAMPLES];
static volatile uint32_t g_sample_count;
static volatile uint32_t g_sample_drops;

static boundary_rec g_boundary[MAX_BOUNDARY];
static uint32_t g_boundary_count;

static fact_rec g_facts[MAX_FACTS];
static uint32_t g_fact_count;

static uint64_t g_t0;

static uint64_t now_ns(void) {
    static mach_timebase_info_data_t tb;
    if (tb.denom == 0) mach_timebase_info(&tb);
    uint64_t t = mach_absolute_time();
    return (uint64_t)((long double)t * (long double)tb.numer / (long double)tb.denom);
}

static void on_sigprof(int sig, siginfo_t *si, void *uc) {
    (void)sig; (void)si;
    uint32_t i = g_sample_count;
    if (i >= MAX_SAMPLES) {
        g_sample_drops = g_sample_drops + 1;
        return;
    }
    /* Under Rosetta the signal frame carries guest architectural state, so
     * this is the x86-64 PC even though the host is arm64. */
    ucontext_t *u = (ucontext_t *)uc;
#if defined(__x86_64__)
    uint64_t pc = (uint64_t)u->uc_mcontext->__ss.__rip;
#elif defined(__aarch64__)
    uint64_t pc = (uint64_t)u->uc_mcontext->__ss.__pc;
#else
    uint64_t pc = 0;
#endif
    g_samples[i] = pc;
    g_sample_count = i + 1;
}

static void arm_timer(int on) {
    struct itimerval it;
    memset(&it, 0, sizeof it);
    if (on) {
        it.it_interval.tv_sec = 0;
        it.it_interval.tv_usec = 1000000 / PORTE_PROBE_HZ;
        it.it_value = it.it_interval;
    }
    setitimer(ITIMER_PROF, &it, NULL);
}

void porte_probe_start(const char *image_tag, const char *image_path) {
    const char *path = getenv("PORTE_PROBE");
    if (!path || !*path) return;
    if (g_enabled) return;

    g_path = strdup(path);
    g_tag = image_tag ? image_tag : "module";
    g_module_path = image_path ? image_path : "";
    g_t0 = now_ns();

    /* Static address = guest PC - slide, so downstream symbolication can use
     * the on-disk Mach-O image and the addresses objdump reports. */
    for (uint32_t i = 0; i < _dyld_image_count(); i++) {
        const char *name = _dyld_get_image_name(i);
        if (name && image_path && strstr(name, image_path)) {
            g_slide = (uintptr_t)_dyld_get_image_vmaddr_slide(i);
            break;
        }
    }

    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_sigaction = on_sigprof;
    sa.sa_flags = SA_SIGINFO | SA_RESTART;
    sigemptyset(&sa.sa_mask);
    if (sigaction(SIGPROF, &sa, NULL) != 0) return;

    g_enabled = 1;
    atexit(porte_probe_flush);
}

void porte_probe_register_module(const char *image_path) {
    if (!g_enabled || !image_path) return;
    for (uint32_t i = 0; i < _dyld_image_count(); i++) {
        const char *name = _dyld_get_image_name(i);
        if (name && strstr(name, image_path)) {
            g_module_path = strdup(name);
            g_slide = (uintptr_t)_dyld_get_image_vmaddr_slide(i);
            return;
        }
    }
}

int porte_probe_enabled(void) { return g_enabled; }

void porte_probe_sample_start(void) {
    if (!g_enabled) return;
    arm_timer(1);
}

void porte_probe_sample_stop(void) {
    if (!g_enabled) return;
    arm_timer(0);
}

void porte_probe_boundary(int kernel_id, int64_t n, int64_t q, uint32_t flags,
                          uintptr_t p, uintptr_t src, uintptr_t dst,
                          uintptr_t aux) {
    if (!g_enabled) return;
    uint32_t i = g_boundary_count;
    if (i >= MAX_BOUNDARY) return;
    boundary_rec *r = &g_boundary[i];
    r->kernel_id = kernel_id;
    r->n = n;
    r->q = q;
    r->flags = flags;
    r->p = p;
    r->src = src;
    r->dst = dst;
    r->aux = aux;
    r->t_ns = now_ns() - g_t0;
    g_boundary_count = i + 1;
}

void porte_probe_fact_minmax(int kernel_id, int32_t mn, int32_t mx, int64_t n) {
    if (!g_enabled) return;
    uint32_t i = g_fact_count;
    if (i >= MAX_FACTS) return;
    g_facts[i].kernel_id = kernel_id;
    g_facts[i].mn = mn;
    g_facts[i].mx = mx;
    g_facts[i].n = n;
    g_fact_count = i + 1;
}

void porte_probe_flush(void) {
    if (!g_enabled) return;
    g_enabled = 0;

    arm_timer(0);

    FILE *f = fopen(g_path, "w");
    if (!f) return;
    fprintf(f, "v1\n");
    fprintf(f, "module %s %llx %s\n", g_tag, (unsigned long long)g_slide,
            g_module_path);
    uint32_t ns = g_sample_count;
    for (uint32_t i = 0; i < ns; i++) {
        fprintf(f, "sample %llx\n", (unsigned long long)g_samples[i]);
    }
    for (uint32_t i = 0; i < g_boundary_count; i++) {
        boundary_rec *r = &g_boundary[i];
        fprintf(f, "boundary %d %lld %lld %u %llx %llx %llx %llx %llu\n",
                r->kernel_id, (long long)r->n, (long long)r->q, r->flags,
                (unsigned long long)r->p, (unsigned long long)r->src,
                (unsigned long long)r->dst, (unsigned long long)r->aux,
                (unsigned long long)r->t_ns);
    }
    for (uint32_t i = 0; i < g_fact_count; i++) {
        fact_rec *r = &g_facts[i];
        fprintf(f, "fact %d %d %d %lld\n", r->kernel_id, r->mn, r->mx,
                (long long)r->n);
    }
    /* Image map lets the profiler attribute sampled PCs to the right
     * image (module, libc, runtime) instead of guessing a single slide. */
    for (uint32_t i = 0; i < _dyld_image_count(); i++) {
        const char *name = _dyld_get_image_name(i);
        if (!name) continue;
        fprintf(f, "img %llx %s\n",
                (unsigned long long)_dyld_get_image_vmaddr_slide(i), name);
    }
    fprintf(f, "samples %u dropped %u\n", ns, g_sample_drops);
    fclose(f);
}
