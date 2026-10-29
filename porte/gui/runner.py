"""Run a portable Windows console image in an isolated child process.

The GUI bridge spawns this module so that a guest program cannot take the
protocol process down with it, and so the UI can cancel a run by terminating
one child. The engine's console path is synchronous: it collects the program's
writes and returns. This runner therefore prints exactly one JSON document on
stdout when the guest finishes.

    python -m porte.gui.runner <image.exe> [guest arguments…]

Exit status: 0 if the guest exited 0, 70 on a guest fault, 65 if the image could
not be loaded, 2 on a usage error. The result document is printed even when the
guest faults.
"""
from __future__ import annotations

import collections
import json
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m porte.gui.runner <image.exe> [args…]",
              file=sys.stderr)
        return 2
    image = Path(argv[0]).expanduser()
    guest_args = [str(a) for a in argv[1:]]
    if not image.exists():
        print(json.dumps({"error": f"image not found: {image}"}))
        return 65
    try:
        from ..pe.machine import Machine
    except Exception as exc:  # pragma: no cover - import failures are fatal
        print(json.dumps({"error": f"engine unavailable: {exc}"}))
        return 65

    started = time.perf_counter()
    try:
        machine = Machine(image)
        result = machine.run_entry(guest_args)
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 65
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    calls = collections.Counter(entry["call"] for entry in machine.api_log)
    output = bytes(machine.writes)
    doc = {
        "exit_code": result.exit_code if result.exit_code is not None else 0,
        "fault": result.fault,
        "run_ms": elapsed_ms,
        "output": output.decode("latin-1", "replace"),
        "output_bytes": len(output),
        "api_calls": dict(calls),
        "dispatches": machine.dispatches,
    }
    print(json.dumps(doc))
    if result.fault:
        return 70
    return int(doc["exit_code"] & 0xFF)


if __name__ == "__main__":
    raise SystemExit(main())
