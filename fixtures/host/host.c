#include "host.h"

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;
static host_entry *g_buf;
static int64_t g_len, g_cap;
static uint64_t g_digest = 1469598103934665603ull;
static int64_t g_transitions;

static void grow_locked(int64_t want) {
    if (want <= g_cap) return;
    int64_t cap = g_cap ? g_cap : 4096;
    while (cap < want) cap *= 2;
    host_entry *n = realloc(g_buf, (size_t)cap * sizeof(host_entry));
    if (!n) abort();
    g_buf = n;
    g_cap = cap;
}

static void append_locked(int32_t tag, int32_t key, int32_t val) {
    grow_locked(g_len + 1);
    host_entry *e = &g_buf[g_len++];
    e->tag = tag;
    e->key = key;
    e->val = val;
    e->pad = 0;
    g_digest = (g_digest ^ (uint32_t)tag) * 1099511628211ull;
    g_digest = (g_digest ^ (uint32_t)key) * 1099511628211ull;
    g_digest = (g_digest ^ (uint32_t)val) * 1099511628211ull;
}

void host_reset(void) {
    pthread_mutex_lock(&g_mu);
    g_len = 0;
    g_transitions = 0;
    g_digest = 1469598103934665603ull;
    pthread_mutex_unlock(&g_mu);
}

void host_emit(int32_t tag, int32_t key, int32_t val) {
    pthread_mutex_lock(&g_mu);
    g_transitions++;
    append_locked(tag, key, val);
    pthread_mutex_unlock(&g_mu);
}

void host_emit_batch(const host_entry *entries, int64_t count) {
    pthread_mutex_lock(&g_mu);
    g_transitions++;
    grow_locked(g_len + count);
    for (int64_t i = 0; i < count; i++) {
        append_locked(entries[i].tag, entries[i].key, entries[i].val);
    }
    pthread_mutex_unlock(&g_mu);
}

int host_has_batch(void) { return 1; }
int64_t host_transitions(void) { return g_transitions; }
int64_t host_entries(void) { return g_len; }

int64_t host_ledger_write(const char *path) {
    pthread_mutex_lock(&g_mu);
    FILE *f = fopen(path, "wb");
    if (!f) { pthread_mutex_unlock(&g_mu); return -1; }
    size_t wrote = fwrite(g_buf, sizeof(host_entry), (size_t)g_len, f);
    fclose(f);
    pthread_mutex_unlock(&g_mu);
    return (int64_t)wrote;
}
