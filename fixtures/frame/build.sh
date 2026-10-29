#!/bin/bash
# Build every arm of the frame-path experiment.
#   frame_arm64        native macOS ARM64 + Metal
#   frame_x86_64       same source for x86-64, runs under Rosetta
#   libframe_render    ARM64 renderer dylib for the Porte host (ctypes)
#   frame_win.exe      Windows x86-64 PE (mingw-gcc, scalar) calling the host
#                      PresentFrame stub
#   frame_win_clang.exe the same PE built by clang (auto-vectorized x86)
set -euo pipefail
cd "$(dirname "$0")/../.."
OUT=build/frame
mkdir -p "$OUT"

CFLAGS="-O2 -Wall -Wno-unused-parameter"
clang $CFLAGS -o "$OUT/frame_arm64" fixtures/frame/app.c fixtures/frame/sim.c \
    fixtures/frame/renderer.m -framework Metal -framework Foundation
clang $CFLAGS -arch x86_64 -o "$OUT/frame_x86_64" fixtures/frame/app.c \
    fixtures/frame/sim.c fixtures/frame/renderer.m -framework Metal -framework Foundation
clang $CFLAGS -arch arm64 -dynamiclib -fPIC -o "$OUT/libframe_render_arm64.dylib" \
    fixtures/frame/renderer.m -framework Metal -framework Foundation

CC=x86_64-w64-mingw32-gcc
DLLTOOL=x86_64-w64-mingw32-dlltool
STRIP=x86_64-w64-mingw32-strip
cat > "$OUT/portegpu.def" <<'DEF'
LIBRARY portegpu.dll
EXPORTS
    PresentFrame
DEF
$DLLTOOL -d "$OUT/portegpu.def" -l "$OUT/libportegpu.a"
$CC -O2 -std=c99 -nostartfiles -fno-stack-protector -mno-stack-arg-probe \
    -Wl,-e,porte_entry -Wl,--subsystem,console -o "$OUT/frame_win.exe" \
    fixtures/frame/win/frame_win.c fixtures/frame/sim.c \
    "$OUT/libportegpu.a" -lkernel32
$STRIP --strip-all "$OUT/frame_win.exe"

# The same Windows source compiled by clang instead of mingw-gcc, as a second
# opaque PE. clang's x86-64 build is auto-vectorized like the native arm, so
# running both images through Porte separates "the guest code is scalar" from
# "the execution engine costs this much per instruction".
clang --target=x86_64-w64-windows-gnu -O2 -std=c99 -c "fixtures/frame/win/frame_win.c" -o "$OUT/frame_win_clang.o"
clang --target=x86_64-w64-windows-gnu -O2 -std=c99 -c "fixtures/frame/sim.c" -o "$OUT/sim_clang.o"
$CC -nostartfiles -Wl,-e,porte_entry -Wl,--subsystem,console \
    -o "$OUT/frame_win_clang.exe" "$OUT/frame_win_clang.o" "$OUT/sim_clang.o" \
    "$OUT/libportegpu.a" -lkernel32
$STRIP --strip-all "$OUT/frame_win_clang.exe"

ls -l "$OUT"/frame_arm64 "$OUT"/frame_x86_64 "$OUT"/libframe_render_arm64.dylib \
    "$OUT"/frame_win.exe "$OUT"/frame_win_clang.exe
