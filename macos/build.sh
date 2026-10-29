#!/usr/bin/env bash
# Build Porte.app: the SwiftUI application, its Info.plist, icon, and the
# record of the runtime it should talk to.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIGURATION="${CONFIGURATION:-release}"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
APP="$ROOT/build/Porte.app"
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"

cd "$ROOT"

if [[ ! -x "$PYTHON" ]]; then
    echo "error: no Python interpreter at $PYTHON" >&2
    echo "       python3 -m venv .venv && .venv/bin/pip install -e ." >&2
    exit 1
fi

echo "Building Porte (Swift, $CONFIGURATION)…"
swift build --package-path "$ROOT/macos" -c "$CONFIGURATION" --product Porte
swift build --package-path "$ROOT/macos" -c "$CONFIGURATION" --product porte-ls
BIN_DIR="$(swift build --package-path "$ROOT/macos" -c "$CONFIGURATION" --show-bin-path)"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN_DIR/Porte" "$APP/Contents/MacOS/Porte"
cp "$ROOT/macos/Resources/Info.plist" "$APP/Contents/Info.plist"
cp "$BIN_DIR/porte-ls" "$ROOT/build/porte-ls"
plutil -lint "$APP/Contents/Info.plist" > /dev/null

"$PYTHON" - "$APP/Contents/Resources/AppIcon.icns" <<'PY'
import shutil
import sys
import tempfile
from pathlib import Path

from porte.app.icon import default_icon_png, make_icns

destination = Path(sys.argv[1])
tmp = Path(tempfile.mkdtemp(prefix="porte-app-icon-", dir=str(destination.parent)))
try:
    make_icns(default_icon_png(), destination, tmp)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
PY

"$PYTHON" - "$APP/Contents/Resources/porte-runtime.json" <<'PY'
import json
import sys
from pathlib import Path

from porte import util
from porte.app.bundle import runtime_interpreter

try:
    from importlib.metadata import version

    porte_version = version("porte")
except Exception:
    porte_version = "0.1.0"

python, pythonpath = runtime_interpreter()
document = {
    "schema": 1,
    "python": str(python),
    "root": str(util.ROOT),
    "pythonpath": pythonpath,
    "version": porte_version,
}
Path(sys.argv[1]).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
PY

# Ad-hoc signature only. The app is not notarized and the GUI says so by never
# claiming otherwise; Gatekeeper's first-launch warning is not bypassed.
if ! codesign --force --sign - "$APP" > /dev/null 2>&1; then
    echo "warning: ad-hoc signing failed; macOS may refuse to launch the app" >&2
fi

# Make the bundle visible to Launch Services immediately (Finder would do this
# on first launch; the script does it so `open -a` and Open With work at once).
if [[ -x "$LSREGISTER" ]]; then
    "$LSREGISTER" -f "$APP" > /dev/null 2>&1 || true
fi

echo "Built $APP"
