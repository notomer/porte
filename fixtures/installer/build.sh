#!/bin/bash
# Build the installer fixtures.
#
#   build/installer/PorteFixture.msi              files + registry + shortcut
#   build/installer/PorteFixture-1.1.0.msi        same UpgradeCode, newer version
#   build/installer/PorteFixture-customaction.msi declares a code custom action
#   build/installer/PorteFixture.msix             block-mapped MSIX container
#   build/installer/PorteFixture-tampered.msix    same, with one payload byte flipped
#
# wixl (msitools) is the reference MSI authoring tool, so the packages are
# produced by an implementation independent of Porte's reader.
set -euo pipefail
cd "$(dirname "$0")"
OUT=../../build/installer
mkdir -p "$OUT" tree

CC=x86_64-w64-mingw32-gcc
STRIP=x86_64-w64-mingw32-strip
$CC -O2 -std=c99 -nostartfiles -fno-stack-protector -mno-stack-arg-probe \
    -Wl,-e,porte_entry -Wl,--subsystem,console \
    -o tree/PorteFixture.exe payload.c -lkernel32
$STRIP --strip-all tree/PorteFixture.exe
$CC -O2 -std=c99 -nostartfiles -fno-stack-protector -mno-stack-arg-probe \
    -Wl,-e,porte_entry -Wl,--subsystem,windows \
    -o tree/PorteFixtureGui.exe payload_gui.c -luser32
$STRIP --strip-all tree/PorteFixtureGui.exe

wixl -o "$OUT/PorteFixture.msi" fixture.wxs
wixl -o "$OUT/PorteFixture-customaction.msi" fixture-customaction.wxs
sed 's/Version="1.0.0"/Version="1.1.0"/' fixture.wxs > "$OUT/fixture-1.1.0.wxs"
wixl -o "$OUT/PorteFixture-1.1.0.msi" "$OUT/fixture-1.1.0.wxs"
python3 make_msix.py "$OUT/PorteFixture.msix" "$OUT/PorteFixture-tampered.msix"

ls -l "$OUT"/*.msi "$OUT"/*.msix
