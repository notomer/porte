/* Porte execution probe.
 *
 * Two observation channels, both disabled unless PORTE_PROBE names a path:
 *   1. PC sampling via ITIMER_PROF (low overhead; samples are attributed to
 *      guest static addresses after the run).
 *   2. Boundary records written by the caller of the module under study
 *      (arguments at the ABI boundary, return values), plus module-side facts
 *      that need an instrumented build of the generic module.
 *
 * The probe deliberately does not write to the output file from signal
 * context. Samples go into a fixed buffer and are flushed from atexit(3).
 */
#ifndef PORTE_PROBE_H
#define PORTE_PROBE_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Returns non-zero when PORTE_PROBE is set. Cheap enough for a hot path. */
int porte_probe_enabled(void);

/* Start sampling and register the output path. Safe to call twice. */
void porte_probe_start(const char *image_tag, const char *image_path);

/* Register the module under study (call after dlopen) so sampled guest PCs
 * can be converted to on-disk addresses for symbolication. */
void porte_probe_register_module(const char *image_path);

/* Restrict sampling to the region of interest (the module call). */
void porte_probe_sample_start(void);
void porte_probe_sample_stop(void);

/* Explicit stop/flush; also called from atexit. */
void porte_probe_flush(void);

/* Boundary observation: called by the *caller* immediately before entering
 * the module under study, so the measured module itself stays undisturbed. */
void porte_probe_boundary(int kernel_id, int64_t n, int64_t q, uint32_t flags,
                          uintptr_t p, uintptr_t src, uintptr_t dst,
                          uintptr_t aux);

/* Module-side facts (only compiled into the instrumented generic module). */
void porte_probe_fact_minmax(int kernel_id, int32_t mn, int32_t mx, int64_t n);

/* Effective sampling rate in Hz requested from ITIMER_PROF. */
#ifndef PORTE_PROBE_HZ
#define PORTE_PROBE_HZ 4000
#endif

#ifdef __cplusplus
}
#endif

#endif /* PORTE_PROBE_H */
