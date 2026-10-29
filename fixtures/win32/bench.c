/* Deterministic compute benchmark, built three ways from one source.
 *
 *   Windows x86-64 PE      runs inside the compatibility runtime
 *   macOS x86-64           the same code with Rosetta translation only
 *   macOS arm64            the same code natively
 *
 * The three arms separate translation cost (x86-64 vs arm64) from Windows API
 * and runtime cost (PE under the runtime vs the same x86-64 code as a macOS
 * process). The arithmetic is identical in every arm: a mixed integer / double
 * loop whose result is printed, so a difference in output would be a bug, not
 * a speed difference.
 *
 *   bench.exe [iterations]
 *
 * prints: porte-bench iterations=<n> ms=<float> hash=<hex> sum=<float>
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>

#ifdef _WIN32
#include <windows.h>
static double now_ms(void)
{
    LARGE_INTEGER frequency, counter;
    QueryPerformanceFrequency(&frequency);
    QueryPerformanceCounter(&counter);
    return (double)counter.QuadPart * 1000.0 / (double)frequency.QuadPart;
}
#else
#include <time.h>
static double now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec * 1000.0 + (double)ts.tv_nsec / 1e6;
}
#endif

int main(int argc, char **argv)
{
    long iterations = 40000000L;
    uint32_t hash = 2166136261u;
    double accumulator = 1.0;
    double started, elapsed;
    long index;

    if (argc > 1) {
        long parsed = strtol(argv[1], NULL, 10);
        if (parsed > 0) iterations = parsed;
    }

    started = now_ms();
    for (index = 0; index < iterations; index++) {
        uint32_t value = hash ^ (uint32_t)index;
        hash = (value ^ (value >> 16)) * 2246822519u;
        hash ^= hash >> 13;
        accumulator = accumulator * 1.0000001 + (double)(hash & 0xffff) * 1e-9;
        if (accumulator > 1e12) accumulator -= 1e6;
    }
    elapsed = now_ms() - started;

    printf("porte-bench iterations=%ld ms=%.3f hash=%08x sum=%.9f\n",
           iterations, elapsed, hash, accumulator);
    return 0;
}
