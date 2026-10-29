"""PE machine with a graphics import.

`PresentFrame(entities, count, frame)` is the PE's only graphics call. The host
serves it by handing the guest's entity buffer to the same Metal renderer the
native arms link, which is what a compatibility layer does at a draw boundary:
the GPU work is identical, the CPU path is what differs.

Timing recorded per call:
  enter/exit timestamps around the host work (stub cost, which splits into
  encode work and time blocked on the renderer's three-deep ring),
  GPU milliseconds once the command buffer completes (reported a few frames
  later by the ring).
Frame wall time is the interval between consecutive entries, so simulator work
and dispatch cost are both included, and guest time is wall minus stub time.
"""
from __future__ import annotations

import time
from pathlib import Path

from .machine import Machine


class GpuMachine(Machine):
    def __init__(self, pe_path: Path, renderer, **kwargs):
        self.renderer = renderer
        self.calls: list[dict] = []
        self.last_buffer: tuple[int, int] | None = None
        super().__init__(Path(pe_path), **kwargs)

    def api_PresentFrame(self, ents: int, count: int, frame: int) -> int:
        region = self.region_for(ents)
        if region is None or region.host_ptr is None:
            raise RuntimeError("PresentFrame received a pointer outside host memory")
        host = region.host_ptr + (ents - region.guest)
        t0 = time.perf_counter_ns()
        gpu_ms, encode_ms, wait_ms = self.renderer.frame(host, int(count),
                                                        int(frame))
        t1 = time.perf_counter_ns()
        self.last_buffer = (int(ents), int(count))
        self.calls.append({
            "frame": int(frame),
            "enter_ns": t0,
            "exit_ns": t1,
            "gpu_ms": gpu_ms,
            "encode_ms": encode_ms,
            "wait_ms": wait_ms,
        })
        return 0

    def frame_report(self, startup_ns: int) -> dict:
        """Turn the call record into per-frame measurements."""
        calls = self.calls
        if not calls:
            return {"frames": 0}
        wall = []
        stub = []
        encode = []
        waits = []
        for prev, cur in zip(calls, calls[1:]):
            wall.append((cur["enter_ns"] - prev["enter_ns"]) / 1e6)
            stub.append((cur["exit_ns"] - cur["enter_ns"]) / 1e6)
            encode.append(cur["encode_ms"])
            waits.append(cur["wait_ms"])
        gpus = [c["gpu_ms"] for c in calls if c["gpu_ms"] > 0]
        return {
            "frames": len(calls),
            "wall_ms": wall,
            "stub_ms": stub,
            "submit_ms": encode,
            "wait_ms": waits,
            "gpu_ms": gpus,
            # Same length as wall_ms, zeros included, so a frame can be mapped
            # to the GPU duration of the submission it waited on.
            "gpu_all": [c["gpu_ms"] for c in calls[1:]],
            "startup_ms": startup_ns / 1e6,
        }
