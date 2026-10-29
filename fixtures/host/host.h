/* Host-side effect sink for the audit kernel.
 *
 * This stands in for the "outside the translated process" side of a Win32 API
 * call: an indirect call through a separate image, a lock, a ledger append and
 * a digest update. Batching exists so a specialization can fuse a run of
 * calls into one transition, the same shape as hoisting/fusing Win32 calls.
 */
#ifndef PORTE_HOST_H
#define PORTE_HOST_H

#include <stdint.h>

typedef struct {
    int32_t tag;
    int32_t key;
    int32_t val;
    int32_t pad;
} host_entry;

void host_reset(void);
void host_emit(int32_t tag, int32_t key, int32_t val);
void host_emit_batch(const host_entry *entries, int64_t count);
int host_has_batch(void);
int64_t host_transitions(void);
int64_t host_entries(void);
int64_t host_ledger_write(const char *path);

#endif
