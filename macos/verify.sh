#!/usr/bin/env bash
# End-to-end verification for the native macOS application.
#
#   ./macos/verify.sh            builds, checks, and drives the real app
#   ./macos/verify.sh --no-gui   skips steps that need a window server
#
# It backs up and restores ~/Library/Application Support/Porte/GUI, the app's
# defaults, and the saved runtime, so the machine ends where it started. It
# never changes the default .exe/.msi handler except when
# PORTE_VERIFY_SET_DEFAULT=1 is explicitly set.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
APP="$ROOT/build/Porte.app"
TOOL="$ROOT/build/porte-ls"
SUPPORT="$HOME/Library/Application Support/Porte"
GUI_DIR="$SUPPORT/GUI"
SETUP="$GUI_DIR/setup.json"
SAVED_RUNTIME="$GUI_DIR/runtime.json"
EVENTS="$GUI_DIR/events.jsonl"
FRAME_EXE="$ROOT/build/frame/frame_win.exe"
CRC_EXE="$ROOT/fixtures/opaque/out/crc.exe"
MSI="$ROOT/build/installer/PorteFixture.msi"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/porte-verify.XXXXXX")"
TMP_APPS="$TMP/Applications"
GUI=1
[[ "${1:-}" == "--no-gui" ]] && GUI=0

cd "$ROOT"

step() { printf '\n== %s ==\n' "$*"; }
ok() { printf 'ok    %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*" >&2; exit 1; }

pkill_app() {
    pkill -TERM -f "$APP/Contents/MacOS/Porte" >/dev/null 2>&1 || true
}

wait_app_gone() {
    local i
    for i in $(seq 1 40); do
        pgrep -f "$APP/Contents/MacOS/Porte" >/dev/null || return 0
        sleep 0.25
    done
    pkill -KILL -f "$APP/Contents/MacOS/Porte" >/dev/null 2>&1 || true
}

wait_app_running() {
    local i
    for i in $(seq 1 80); do
        pgrep -f "$APP/Contents/MacOS/Porte" >/dev/null && return 0
        sleep 0.25
    done
    return 1
}

wait_for_event() {
    "$PYTHON" - "$EVENTS" "$1" "${2:-45}" <<'PY'
import sys
import time

path, needle, timeout = sys.argv[1], sys.argv[2], float(sys.argv[3])
deadline = time.time() + timeout
while time.time() < deadline:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            if needle in handle.read():
                sys.exit(0)
    except FileNotFoundError:
        pass
    time.sleep(0.25)
print(f"timed out waiting for {needle!r} in {path}", file=sys.stderr)
sys.exit(1)
PY
}

seed_setup() {
    "$PYTHON" - "$SETUP" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from porte import util
from porte.app.bundle import runtime_interpreter

python, pythonpath = runtime_interpreter()
try:
    from importlib.metadata import version
    porte_version = version("porte")
except Exception:
    porte_version = "0.1.0"
document = {
    "schema": 1,
    "completedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "runtime": {
        "schema": 1,
        "python": str(python),
        "root": str(util.ROOT),
        "pythonpath": pythonpath,
        "version": porte_version,
    },
    "fileAssociations": "unset",
}
destination = Path(sys.argv[1])
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
PY
}

backup_state() {
    mkdir -p "$TMP/backup"
    [[ -f "$SETUP" ]] && cp "$SETUP" "$TMP/backup/setup.json"
    [[ -f "$SAVED_RUNTIME" ]] && cp "$SAVED_RUNTIME" "$TMP/backup/runtime.json"
    [[ -f "$EVENTS" ]] && cp "$EVENTS" "$TMP/backup/events.jsonl"
    defaults export com.notomer.porte "$TMP/backup/defaults.plist" >/dev/null 2>&1 || true
    return 0
}

cleanup() {
    local status=$?
    set +e
    pkill_app
    wait_app_gone
    if [[ -f "$TMP/backup/setup.json" ]]; then cp "$TMP/backup/setup.json" "$SETUP"; else rm -f "$SETUP"; fi
    if [[ -f "$TMP/backup/runtime.json" ]]; then cp "$TMP/backup/runtime.json" "$SAVED_RUNTIME"; else rm -f "$SAVED_RUNTIME"; fi
    if [[ -f "$TMP/backup/events.jsonl" ]]; then cp "$TMP/backup/events.jsonl" "$EVENTS"; else rm -f "$EVENTS"; fi
    if [[ -f "$TMP/backup/defaults.plist" ]]; then
        defaults import com.notomer.porte "$TMP/backup/defaults.plist" >/dev/null 2>&1
    else
        defaults delete com.notomer.porte >/dev/null 2>&1
    fi
    rm -rf "$TMP"
    exit "$status"
}
trap cleanup EXIT

mkdir -p "$GUI_DIR"
backup_state
pkill_app
wait_app_gone

step "Build Porte.app"
./macos/build.sh
[[ -x "$APP/Contents/MacOS/Porte" ]] || fail "Porte executable missing from the bundle"
[[ -x "$TOOL" ]] || fail "porte-ls was not built"
plutil -lint "$APP/Contents/Info.plist" > /dev/null
codesign --verify --strict "$APP"
[[ "$(lipo -archs "$APP/Contents/MacOS/Porte")" == "arm64" ]] || fail "app is not arm64"
"$TOOL" find com.notomer.porte > /dev/null || fail "Launch Services does not know the app"
plutil -extract CFBundleDocumentTypes json -o - "$APP/Contents/Info.plist" \
    | grep -q "com.microsoft.windows-executable" || fail "EXE document type missing"
plutil -extract CFBundleDocumentTypes json -o - "$APP/Contents/Info.plist" \
    | grep -q "com.notomer.porte.windows-msi" || fail "MSI document type missing"
ok "valid arm64 bundle, registered with Launch Services, both document types declared"

step "Swift checks (app logic, bridge client, Info.plist)"
swift run --package-path macos -c debug porte-check

step "Python regression tests (CLI, importer, GUI bridge)"
"$PYTHON" -m unittest discover -s tests -v

if [[ $GUI -eq 0 ]]; then
    step "GUI steps skipped (--no-gui)"
    printf '\nVerification passed (build, checks, protocol, tests).\n'
    exit 0
fi

# A supported portable EXE to open. Prefer the frame workload; fall back to the
# opaque-phase CRC image so CI does not need the Metal fixture.
OPEN_EXE=""
for candidate in "$FRAME_EXE" "$CRC_EXE"; do
    if [[ -f "$candidate" ]]; then OPEN_EXE="$candidate"; break; fi
done
if [[ -z "$OPEN_EXE" ]]; then
    "$PYTHON" fixtures/opaque/gen.py --clean
    OPEN_EXE="$CRC_EXE"
fi
if [[ ! -f "$MSI" ]]; then
    ./fixtures/installer/build.sh
fi

step "First launch with no setup state"
rm -f "$SETUP" "$EVENTS"
open -a "$APP"
wait_app_running || fail "the application did not start"
wait_for_event runtime_ready 45
if grep -q "analysis_started" "$EVENTS"; then
    fail "the app analyzed a file before setup completed"
fi
ok "app launched, validated the runtime, and stopped at setup"
pkill_app
wait_app_gone

step "Warm open: Finder hands an EXE to the running app"
seed_setup
defaults write com.notomer.porte PorteInstallationDirectory -string "$TMP_APPS"
rm -f "$EVENTS"
open -a "$APP"
wait_app_running || fail "the application did not start"
wait_for_event runtime_ready 45
sleep 1
open -a "$APP" "$OPEN_EXE"
wait_for_event analysis_result 60
grep -q '"kind":"exe-portable"' "$EVENTS" || fail "the EXE was not analyzed as a portable app"
ok "the running app received the EXE through Launch Services and analyzed it"
[[ "$(pgrep -f "$APP/Contents/MacOS/Porte" | wc -l | tr -d ' ')" == "1" ]] \
    || fail "opening a file created a second application instance"
pkill_app
wait_app_gone

step "Cold launch: Finder hands an MSI to a stopped app"
rm -f "$EVENTS"
open -a "$APP" "$MSI"
wait_app_running || fail "the application did not launch from the document request"
wait_for_event analysis_result 60
grep -q '"kind":"msi"' "$EVENTS" || fail "the MSI was not analyzed as a Windows Installer package"
ok "cold launch received the MSI and reached the installation analysis"

step "Default handler open (no -a flag)"
if "$TOOL" status exe "$APP" | grep -q '"is_porte":true'; then
    open -a "$APP"
    wait_app_running || fail "the application is not running"
    open "$OPEN_EXE"
    wait_for_event open_received 30
    ok "double-click path: macOS routed the EXE to Porte through the default handler"
else
    printf 'skip  Porte is not the default .exe handler on this Mac; the setup screen offers to set it.\n'
fi
pkill_app
wait_app_gone

step "Missing runtime: setup recovery instead of a broken window"
rm -rf "$TMP/Porte-NoRuntime.app"
cp -R "$APP" "$TMP/Porte-NoRuntime.app"
rm -f "$TMP/Porte-NoRuntime.app/Contents/Resources/porte-runtime.json"
mv "$SAVED_RUNTIME" "$TMP/runtime.saved" 2>/dev/null || true
rm -f "$EVENTS"
"$TMP/Porte-NoRuntime.app/Contents/MacOS/Porte" >/dev/null 2>&1 &
NO_RUNTIME_PID=$!
sleep 4
grep -q "runtime_error" "$EVENTS" || fail "a missing runtime was not reported"
if grep -q "runtime_ready" "$EVENTS"; then
    fail "the app claimed a runtime was ready when none was configured"
fi
kill "$NO_RUNTIME_PID" >/dev/null 2>&1 || true
ok "missing runtime is reported and setup is required again"

step "Install a package, launch its application from Applications"
"$PYTHON" "$ROOT/macos/verify_generated_app.py" "$MSI" --dest "$TMP_APPS"

step "Bundle uninstall cleanup"
[[ ! -e "$TMP_APPS/Porte Installer Fixture.app" ]] \
    || fail "uninstall left the generated application behind"
ok "no generated application remains"

printf '\nVerification passed.\n'
