/* Fixture generation source. The optimizer never sees this file.
 *
 * A freestanding Windows x86-64 PE with one exported entry point that runs a
 * counted, branch-free transform over a caller-provided uint32 buffer. The
 * loop is deliberately ordinary: it is the kind of integer kernel a compiler
 * emits for hashing, checksums or in-place transforms.
 */
typedef unsigned int uint32_t;
typedef unsigned long long uint64_t;

__declspec(dllexport)
uint64_t porte_work(uint32_t *buf, uint64_t n, uint32_t *out, uint64_t seed) {
    uint64_t sum = seed;
    for (uint64_t i = 0; i < n; i++) {
        uint32_t x = buf[i];
        x = x * 1664525u + 1013904223u;
        x ^= x >> 13;
        buf[i] = x;
        sum += x;
    }
    out[0] = (uint32_t)sum;
    out[1] = (uint32_t)(sum >> 32);
    return sum;
}

/* Minimal PE entry point: never called by the harness, but the image needs a
 * valid AddressOfEntryPoint. */
void porte_entry(void) {
    for (;;) {
    }
}
