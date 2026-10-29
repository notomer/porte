#!/usr/bin/env python3
"""Fixture generation for the opaque-PE experiment.

This file lives on the *fixture* side of the wall. It owns three things:

1. the C sources for three self-contained Windows x86-64 programs,
2. the build (mingw, freestanding, stripped), and
3. an independent Python implementation of each program's arithmetic, used
   only to check that Porte's *baseline* PE execution is right.

Porte itself is handed nothing but the resulting `.exe` paths. It never reads
this directory, the sources, the reference values or the symbol tables.

Run:  python3 fixtures/opaque/gen.py [--clean]
"""
from __future__ import annotations

import argparse
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "build" / "opaque" / "src"
OUT = ROOT / "fixtures" / "opaque" / "out"
REF = ROOT / "fixtures" / "opaque" / "reference.json"

CC = "x86_64-w64-mingw32-gcc"
STRIP = "x86_64-w64-mingw32-strip"
CFLAGS = [
    "-O2", "-std=c99", "-nostartfiles", "-fno-stack-protector",
    "-fno-asynchronous-unwind-tables", "-mno-stack-arg-probe",
    "-Wl,-e,porte_entry", "-Wl,--subsystem,console", "-lkernel32",
]

PROLOGUE = r"""
/* Generated fixture. Not visible to the optimizer. */
typedef unsigned int u32;
typedef unsigned long long u64;
typedef unsigned char u8;

__declspec(dllimport) char *GetCommandLineA(void);
__declspec(dllimport) void *GetStdHandle(u32 which);
__declspec(dllimport) int WriteFile(void *h, const void *p, u32 n, u32 *w, void *o);
__declspec(dllimport) void ExitProcess(u32 code);

static char MSG[128];

static u64 parse_n(char *s) {
    u64 v = 0;
    while (*s && *s != ' ') s++;
    while (*s == ' ') s++;
    if (*s < '0' || *s > '9') return 256;
    while (*s >= '0' && *s <= '9') { v = v * 10 + (u32)(*s - '0'); s++; }
    return v;
}

static int put_hex(u64 v, int p) {
    static const char D[] = "0123456789abcdef";
    for (int i = 60; i >= 0; i -= 4) MSG[p++] = D[(v >> i) & 15];
    return p;
}

static int put_dec(u64 v, int p) {
    char t[24];
    int n = 0;
    if (!v) t[n++] = '0';
    while (v) { t[n++] = (char)('0' + (v % 10)); v /= 10; }
    while (n) MSG[p++] = t[--n];
    return p;
}

static void finish(int p) {
    u32 w = 0;
    MSG[p++] = '\n';
    WriteFile(GetStdHandle((u32)-11), MSG, (u32)p, &w, 0);
    ExitProcess(0);
}
"""


def table(seed: int, count: int, mask: int = 0xFFFFFFFF) -> list[int]:
    x = seed

    def nxt() -> int:
        nonlocal x
        x = (x * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
        return (x >> 17) & mask

    return [nxt() for _ in range(count)]


def c_array(name: str, values: list[int], ctype: str, per_line: int = 8) -> str:
    rows = [", ".join(str(v) for v in values[i:i + per_line])
            for i in range(0, len(values), per_line)]
    body = ",\n    ".join(rows)
    return f"static const {ctype} {name}[{len(values)}] = {{\n    {body}\n}};\n"


# --------------------------------------------------------------------------
# Program 1: integer transform over an embedded table.
# --------------------------------------------------------------------------
BLOB_TABLE = table(0x5EEDB10B, 131072)
BLOB_SRC = PROLOGUE + c_array("TABLE", BLOB_TABLE, "u32") + r"""
static u32 OUT[131072];

void porte_entry(void) {
    u64 n = parse_n(GetCommandLineA());
    if (n > 131072) n = 131072;
    u64 sum = 0x9E3779B97F4A7C15ull;
    for (u64 i = 0; i < n; i++) {
        u32 v = TABLE[i];
        v = v * 1664525u + 1013904223u;
        v ^= v >> 13;
        v = v * 2654435761u;
        v += (u32)i;
        OUT[i] = v;
        sum += v;
    }
    int p = 0;
    p = put_dec(n, p);
    MSG[p++] = ' ';
    p = put_hex(sum, p);
    MSG[p++] = ' ';
    p = put_hex(n ? OUT[0] : 0xFFFFFFFFull, p);
    MSG[p++] = ' ';
    p = put_hex(n ? OUT[n - 1] : 0xFFFFFFFFull, p);
    finish(p);
}
"""


def ref_blob(n: int) -> str:
    n = min(n, 131072)
    total = 0x9E3779B97F4A7C15
    out = []
    for i in range(n):
        v = BLOB_TABLE[i]
        v = (v * 1664525 + 1013904223) & 0xFFFFFFFF
        v ^= v >> 13
        v = (v * 2654435761) & 0xFFFFFFFF
        v = (v + i) & 0xFFFFFFFF
        out.append(v)
        total = (total + v) & 0xFFFFFFFFFFFFFFFF
    first = out[0] if out else 0xFFFFFFFF
    last = out[-1] if out else 0xFFFFFFFF
    return f"{n} {total:016x} {first:016x} {last:016x}"


# --------------------------------------------------------------------------
# Program 2: sieve of Eratosthenes, strided inner loop.
# --------------------------------------------------------------------------
SIEVE_SRC = PROLOGUE + r"""
static u8 FLAGS[1 << 22];

void porte_entry(void) {
    u64 n = parse_n(GetCommandLineA());
    if (n > (1 << 22)) n = 1 << 22;
    if (n < 2) n = 2;
    for (u64 i = 0; i < n; i++) FLAGS[i] = 0;
    for (u64 p = 2; p * p < n; p++) {
        if (FLAGS[p]) continue;
        for (u64 m = p * p; m < n; m += p) FLAGS[m] = 1;
    }
    u64 count = 0;
    u64 sum = 0;
    for (u64 i = 2; i < n; i++) {
        if (!FLAGS[i]) { count++; sum += i; }
    }
    int q = 0;
    q = put_dec(count, q);
    MSG[q++] = ' ';
    q = put_hex(sum, q);
    finish(q);
}
"""


def ref_sieve(n: int) -> str:
    n = max(2, min(n, 1 << 22))
    flags = bytearray(n)
    p = 2
    while p * p < n:
        if not flags[p]:
            for m in range(p * p, n, p):
                flags[m] = 1
        p += 1
    count = sum(1 for i in range(2, n) if not flags[i])
    total = sum(i for i in range(2, n) if not flags[i])
    return f"{count} {total:016x}"


# --------------------------------------------------------------------------
# Program 5: CRC-32 over a byte table (added after the pipeline was frozen).
# --------------------------------------------------------------------------
CRC_TABLE: list[int] = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (0xEDB88320 ^ (_c >> 1)) if (_c & 1) else (_c >> 1)
    CRC_TABLE.append(_c & 0xFFFFFFFF)
CRC_DATA = table(0x1234ABCD, 262144, mask=0xFF)
CRC_SRC = PROLOGUE + c_array("T", CRC_TABLE, "u32") + c_array("DATA", CRC_DATA, "u8", 16) + r"""
void porte_entry(void) {
    u64 n = parse_n(GetCommandLineA());
    if (n > 262144) n = 262144;
    u32 crc = 0xFFFFFFFFu;
    for (u64 i = 0; i < n; i++) {
        crc = T[(crc ^ DATA[i]) & 255u] ^ (crc >> 8);
    }
    crc ^= 0xFFFFFFFFu;
    int p = 0;
    p = put_dec(n, p);
    MSG[p++] = ' ';
    p = put_hex(crc, p);
    finish(p);
}
"""


def ref_crc(n: int) -> str:
    n = min(n, 262144)
    crc = 0xFFFFFFFF
    for i in range(n):
        crc = CRC_TABLE[(crc ^ CRC_DATA[i]) & 255] ^ (crc >> 8)
    crc ^= 0xFFFFFFFF
    return f"{n} {crc:016x}"


# --------------------------------------------------------------------------
# Program 3: table-driven mixing with data-dependent addressed loads.
# --------------------------------------------------------------------------
LOOKUP_T1 = table(0xA5A5F00D, 256)
LOOKUP_T2 = table(0x0DDF00D5, 256)
LOOKUP_IN = table(0xC0FFEE11, 65536)
LOOKUP_SRC = PROLOGUE + c_array("T1", LOOKUP_T1, "u32") \
    + c_array("T2", LOOKUP_T2, "u32") \
    + c_array("IN", LOOKUP_IN, "u32") + r"""
static u32 OUT[65536];

void porte_entry(void) {
    u64 n = parse_n(GetCommandLineA());
    if (n > 65536) n = 65536;
    u64 h = 0xCBF29CE484222325ull;
    for (u64 i = 0; i < n; i++) {
        u32 v = IN[i];
        h ^= T1[v & 255];
        h = (h << 7) | (h >> 57);
        h += T2[(h >> 24) & 255];
        h ^= v;
        OUT[i] = (u32)h;
    }
    int p = 0;
    p = put_dec(n, p);
    MSG[p++] = ' ';
    p = put_hex(h, p);
    MSG[p++] = ' ';
    p = put_hex(n ? OUT[n - 1] : 0, p);
    finish(p);
}
"""


def ref_lookup(n: int) -> str:
    n = min(n, 65536)
    h = 0xCBF29CE484222325
    last = 0
    for i in range(n):
        v = LOOKUP_IN[i]
        h ^= LOOKUP_T1[v & 255]
        h = ((h << 7) | (h >> 57)) & 0xFFFFFFFFFFFFFFFF
        h = (h + LOOKUP_T2[(h >> 24) & 255]) & 0xFFFFFFFFFFFFFFFF
        h ^= v
        last = h & 0xFFFFFFFF
    return f"{n} {h:016x} {last:016x}"


# --------------------------------------------------------------------------
# Program 4: a deliberate fault, for the deoptimization path.
# --------------------------------------------------------------------------
FAULT_SRC = PROLOGUE + r"""
static u32 ACC[64];

void porte_entry(void) {
    u64 n = parse_n(GetCommandLineA());
    if (n > 64) n = 64;
    u64 sum = 0;
    volatile u32 *bad = (volatile u32 *)0x5A5A0000ull;
    for (u64 i = 0; i < n; i++) {
        u32 v = ACC[i];
        v = v * 1103515245u + 12345u;
        ACC[i] = v;
        sum += v;
        if (i == 8) {
            volatile u32 x = bad[0];
            sum += x;
        }
    }
    int p = 0;
    p = put_dec(n, p);
    MSG[p++] = ' ';
    p = put_hex(sum, p);
    finish(p);
}
"""


def ref_fault(n: int) -> str:
    # Execution faults at i == 8 before any output is written.
    return ""


def native_source() -> dict[str, str]:
    """ARM64 reference implementations of the same work, for context only.

    These are the "what would this cost if it were native to begin with"
    numbers. They share the data tables above and are built by the fixture
    process, not by Porte.
    """
    blob = """
#include <stdint.h>
static uint32_t OUT[131072];
""" + c_array("TABLE", BLOB_TABLE, "uint32_t") + """
uint64_t ref_blob(uint64_t n) {
    if (n > 131072) n = 131072;
    uint64_t sum = 0x9E3779B97F4A7C15ull;
    for (uint64_t i = 0; i < n; i++) {
        uint32_t v = TABLE[i];
        v = v * 1664525u + 1013904223u;
        v ^= v >> 13;
        v = v * 2654435761u;
        v += (uint32_t)i;
        OUT[i] = v;
        sum += v;
    }
    return sum;
}
"""
    sieve = """
#include <stdint.h>
static uint8_t FLAGS[1 << 22];
static void zero(uint8_t *p, uint64_t n) { for (uint64_t i = 0; i < n; i++) p[i] = 0; }
uint64_t ref_sieve(uint64_t n) {
    if (n > (1u << 22)) n = 1u << 22;
    if (n < 2) n = 2;
    zero(FLAGS, n);
    for (uint64_t p = 2; p * p < n; p++) {
        if (FLAGS[p]) continue;
        for (uint64_t m = p * p; m < n; m += p) FLAGS[m] = 1;
    }
    uint64_t count = 0;
    for (uint64_t i = 2; i < n; i++) if (!FLAGS[i]) count++;
    return count;
}
"""
    lookup = """
#include <stdint.h>
static uint32_t OUT[65536];
""" + c_array("T1", LOOKUP_T1, "uint32_t") + c_array("T2", LOOKUP_T2, "uint32_t") \
        + c_array("IN", LOOKUP_IN, "uint32_t") + """
uint64_t ref_lookup(uint64_t n) {
    if (n > 65536) n = 65536;
    uint64_t h = 0xCBF29CE484222325ull;
    for (uint64_t i = 0; i < n; i++) {
        uint32_t v = IN[i];
        h ^= T1[v & 255];
        h = (h << 7) | (h >> 57);
        h += T2[(h >> 24) & 255];
        h ^= v;
        OUT[i] = (uint32_t)h;
    }
    return h;
}
"""
    crc = """
#include <stdint.h>
""" + c_array("DATA", CRC_DATA, "uint8_t", 16) + c_array("T", CRC_TABLE, "uint32_t") + """
uint64_t ref_crc(uint64_t n) {
    if (n > 262144) n = 262144;
    uint32_t crc = 0xFFFFFFFFu;
    for (uint64_t i = 0; i < n; i++) {
        crc = T[(crc ^ DATA[i]) & 255u] ^ (crc >> 8);
    }
    return (uint64_t)(crc ^ 0xFFFFFFFFu);
}
"""
    return {"blob": blob, "sieve": sieve, "lookup": lookup, "crc": crc}


PROGRAMS = [
    ("blob", BLOB_SRC, ref_blob, [0, 1, 2, 17, 256, 2047, 2048, 65536]),
    ("sieve", SIEVE_SRC, ref_sieve, [0, 1, 2, 3, 64, 1000, 65536, 1000000]),
    ("lookup", LOOKUP_SRC, ref_lookup, [0, 1, 2, 33, 256, 1023, 65536]),
    ("fault", FAULT_SRC, ref_fault, [32, 64]),
    ("crc", CRC_SRC, ref_crc, [0, 1, 2, 255, 256, 65536, 262144]),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args(argv)
    if args.clean:
        for d in (SRC, OUT):
            if d.exists():
                shutil.rmtree(d)
    SRC.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)

    reference: dict[str, dict] = {}
    for name, source, ref, sizes in PROGRAMS:
        c_path = SRC / f"{name}.c"
        c_path.write_text(source)
        exe = OUT / f"{name}.exe"
        cmd = [CC, *CFLAGS, "-o", str(exe), str(c_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            print(proc.stdout)
            print(proc.stderr)
            return 1
        subprocess.run([STRIP, "--strip-all", str(exe)], check=True)
        reference[name] = {
            "exe": str(exe.relative_to(ROOT)),
            "sha256": __import__("hashlib").sha256(exe.read_bytes()).hexdigest(),
            "sizes": {str(n): ref(n) for n in sizes},
        }
        print(f"{name}.exe  {exe.stat().st_size} bytes  "
              f"sha256 {reference[name]['sha256'][:16]}")
    for name, source in native_source().items():
        c_path = SRC / f"native_{name}.c"
        c_path.write_text(source)
        lib = OUT / f"native_{name}.dylib"
        proc = subprocess.run(
            ["clang", "-O3", "-arch", "arm64", "-dynamiclib", "-fPIC",
             "-o", str(lib), str(c_path)], capture_output=True, text=True)
        if proc.returncode != 0:
            print(proc.stderr)
            return 1
        reference[name]["native"] = str(lib.relative_to(ROOT))
        print(f"native_{name}.dylib  {lib.stat().st_size} bytes")

    REF.write_text(json.dumps(reference, indent=2, sort_keys=True) + "\n")
    print(f"wrote {REF.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
