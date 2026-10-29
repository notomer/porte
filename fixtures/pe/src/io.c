/* Fixture generation source. The optimizer never sees this file.
 *
 * A Windows PE that imports a small set of kernel32 entry points and writes
 * through them. It exists to exercise the loader's import table, the host
 * stubs, and an API effect ledger; it is not the specialization target.
 */
typedef unsigned int uint32_t;
typedef unsigned long long uint64_t;
typedef void *HANDLE;

__declspec(dllimport) HANDLE GetStdHandle(uint32_t which);
__declspec(dllimport) int WriteFile(HANDLE h, const void *buf, uint32_t len,
                                    uint32_t *written, void *overlapped);
__declspec(dllimport) void ExitProcess(uint32_t code);

static const char MESSAGE[] = "porte-io:";

__declspec(dllexport)
uint32_t porte_io(uint32_t repeats, uint32_t *out) {
    HANDLE out_handle = GetStdHandle((uint32_t)-11);
    uint32_t total = 0;
    for (uint32_t i = 0; i < repeats; i++) {
        uint32_t written = 0;
        WriteFile(out_handle, MESSAGE, sizeof(MESSAGE) - 1, &written, 0);
        total += written;
    }
    out[0] = total;
    out[1] = repeats;
    return total;
}

void porte_entry(void) {
    uint32_t out[2];
    porte_io(3, out);
    ExitProcess(out[0]);
}
