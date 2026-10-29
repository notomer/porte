#!/bin/bash
# Build the Win32 compatibility fixtures.
#
#   build/win32/porte_gui.exe    genuine Win32 GUI program (window, controls,
#                               menu, comdlg32 file dialog, clipboard)
#   build/win32/porte_probe.exe  Win32 driver/verifier for the program above
#                               and for third-party Windows GUI programs
#   build/win32/porte_bench.exe  Win32 CPU benchmark, same source as the native
#                               arms (bench_arm64, bench_x86_64)
#   build/win32/winlist          macOS helper: on-screen windows of a pid
#
# Everything here is a real Windows PE built by mingw-w64, or a real macOS
# binary built by clang. No part of it is a macOS re-creation of a Windows UI.
set -euo pipefail
cd "$(dirname "$0")/../.."
OUT=build/win32
mkdir -p "$OUT"

CC=${CC_WIN:-x86_64-w64-mingw32-gcc}
STRIP=${STRIP_WIN:-x86_64-w64-mingw32-strip}

"$CC" -O2 -std=c99 -Wall -Wextra -Wno-unused-parameter -mwindows \
    -o "$OUT/porte_gui.exe" fixtures/win32/gui.c -lcomdlg32 -lgdi32 -luser32
"$STRIP" --strip-all "$OUT/porte_gui.exe"

"$CC" -O2 -std=c99 -Wall -Wextra -Wno-unused-parameter \
    -o "$OUT/porte_probe.exe" fixtures/win32/probe.c -luser32
"$STRIP" --strip-all "$OUT/porte_probe.exe"

"$CC" -O2 -std=c99 -Wall -Wextra -mconsole \
    -o "$OUT/porte_bench.exe" fixtures/win32/bench.c
"$STRIP" --strip-all "$OUT/porte_bench.exe"

clang -O2 -Wall -o "$OUT/bench_arm64" fixtures/win32/bench.c
clang -O2 -Wall -arch x86_64 -o "$OUT/bench_x86_64" fixtures/win32/bench.c

clang -O2 -Wall -o "$OUT/winlist" fixtures/win32/winlist.c \
    -framework CoreGraphics -framework CoreFoundation

file "$OUT"/porte_gui.exe "$OUT"/porte_probe.exe "$OUT"/porte_bench.exe \
    "$OUT"/winlist | sed 's/, built by.*//'
