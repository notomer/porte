/* Native frame loop: integer simulation + one Metal draw per frame.
 *
 * Built twice from this source: arm64 (native baseline) and x86-64 (run under
 * Rosetta). The JSON it writes is the measurement record for the arm.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/sysctl.h>
#include <sys/resource.h>

#include "sim.h"
#include "renderer.h"

static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

/* FNV-1a over the entity array. The Porte-hosted Windows arm computes the same
 * hash over its guest memory, so the performance comparison is only published if
 * the two simulations ended in bit-identical state. */
static uint64_t fnv1a(const void *data, size_t size) {
    const unsigned char *p = (const unsigned char *)data;
    uint64_t h = 0xcbf29ce484222325ull;
    for (size_t i = 0; i < size; i++) {
        h ^= p[i];
        h *= 0x100000001b3ull;
    }
    return h;
}

static int translated(void) {
    int value = 0;
    size_t size = sizeof(value);
    if (sysctlbyname("sysctl.proc_translated", &value, &size, NULL, 0) != 0) {
        return 0;
    }
    return value;
}

int main(int argc, char **argv) {
    double t_start = now_ms();
    int frames = 300, entities = FRAME_ENTITIES_DEFAULT, substeps = 1;
    double quad = 0.004;
    int work = 0, width = 1920, height = 1080;
    const char *out = NULL;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--frames") && i + 1 < argc) frames = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--entities") && i + 1 < argc) entities = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--quad") && i + 1 < argc) quad = atof(argv[++i]);
        else if (!strcmp(argv[i], "--work") && i + 1 < argc) work = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--substeps") && i + 1 < argc) substeps = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--width") && i + 1 < argc) width = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--height") && i + 1 < argc) height = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--out") && i + 1 < argc) out = argv[++i];
    }
    if (!out) {
        fprintf(stderr, "usage: %s --frames N --entities N --quad F --work N --out FILE\n", argv[0]);
        return 2;
    }
    frame_entity *ents = (frame_entity *)aligned_alloc(64, (size_t)entities * sizeof(frame_entity));
    if (!ents) return 3;
    frame_sim_init(ents, entities, 0x5EED1234u);

    frame_renderer *r = fr_create(width, height, quad, work);
    if (!r) return 4;

    double *sim = calloc((size_t)frames, sizeof(double));
    double *enc = calloc((size_t)frames, sizeof(double));
    double *wait = calloc((size_t)frames, sizeof(double));
    double *gpu = calloc((size_t)frames, sizeof(double));
    double startup_ms = 0.0;
    struct rusage ru0, ru1;
    getrusage(RUSAGE_SELF, &ru0);

    for (int f = 0; f < frames; f++) {
        int32_t lx, ly;
        frame_light_for((uint64_t)f, &lx, &ly);
        double t0 = now_ms();
        for (int s = 0; s < substeps; s++) {
            frame_sim_step(ents, entities, lx + s, ly - s);
        }
        double t1 = now_ms();
        double g = 0.0, e = 0.0, w = 0.0;
        fr_frame(r, ents, entities, f, &g, &e, &w);
        double t2 = now_ms();
        sim[f] = t1 - t0;
        enc[f] = e;
        wait[f] = w + (t2 - t1);
        gpu[f] = g;
        if (f == 0) startup_ms = now_ms() - t_start;
    }
    getrusage(RUSAGE_SELF, &ru1);
    double cpu_seconds = (ru1.ru_utime.tv_sec - ru0.ru_utime.tv_sec)
        + (ru1.ru_utime.tv_usec - ru0.ru_utime.tv_usec) / 1e6
        + (ru1.ru_stime.tv_sec - ru0.ru_stime.tv_sec)
        + (ru1.ru_stime.tv_usec - ru0.ru_stime.tv_usec) / 1e6;

    FILE *fp = fopen(out, "w");
    if (!fp) return 5;
    fprintf(fp, "{\"meta\":{\"frames\":%d,\"entities\":%d,\"substeps\":%d,"
                "\"quad\":%.6f,\"work\":%d,"
                "\"width\":%d,\"height\":%d,\"shader_ms\":%.3f,\"startup_ms\":%.3f,"
                "\"final_hash\":\"%016llx\",\"translated\":%d,"
                "\"cpu_seconds\":%.4f,\"max_rss_kb\":%ld,"
                "\"arm\":\"%s\"},\n",
            frames, entities, substeps, quad, work, width, height, fr_compile_ms(r),
            startup_ms,
            (unsigned long long)fnv1a(ents, (size_t)entities * sizeof(frame_entity)),
            translated(), cpu_seconds, (long)(ru1.ru_maxrss / 1024),
#ifdef __x86_64__
            "x86_64"
#else
            "arm64"
#endif
    );
    const char *names[4] = {"sim_ms", "encode_ms", "wait_ms", "gpu_ms"};
    double *arrays[4] = {sim, enc, wait, gpu};
    for (int a = 0; a < 4; a++) {
        fprintf(fp, "\"%s\":[", names[a]);
        for (int f = 0; f < frames; f++) {
            fprintf(fp, "%s%.4f", f ? "," : "", arrays[a][f]);
        }
        fprintf(fp, "]%s\n", a == 3 ? "" : ",");
    }
    fprintf(fp, "}\n");
    fclose(fp);
    fr_destroy(r);
    free(ents);
    free(sim); free(enc); free(wait); free(gpu);
    return 0;
}
