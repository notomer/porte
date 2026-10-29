"""ctypes binding for the shared Metal renderer.

The Porte-hosted PE arm submits frames through exactly the same renderer source
as the native arms, so the GPU half of the frame is identical and the measured
difference is the CPU path.
"""
from __future__ import annotations

import ctypes
from pathlib import Path

from .. import util

LIB = util.BUILD / "frame" / "libframe_render_arm64.dylib"


class FrameRenderer:
    def __init__(self, width: int = 1920, height: int = 1080,
                 quad: float = 0.004, work: int = 0,
                 lib: Path | None = None):
        path = Path(lib) if lib else LIB
        if not path.exists():
            raise FileNotFoundError(f"renderer not built: {path}")
        self.lib = ctypes.CDLL(str(path))
        self.lib.fr_create.restype = ctypes.c_void_p
        self.lib.fr_create.argtypes = [ctypes.c_int, ctypes.c_int,
                                       ctypes.c_double, ctypes.c_int]
        self.lib.fr_compile_ms.restype = ctypes.c_double
        self.lib.fr_compile_ms.argtypes = [ctypes.c_void_p]
        self.lib.fr_frame.restype = None
        self.lib.fr_frame.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_int, ctypes.c_int,
                                      ctypes.POINTER(ctypes.c_double),
                                      ctypes.POINTER(ctypes.c_double),
                                      ctypes.POINTER(ctypes.c_double)]
        self.lib.fr_destroy.argtypes = [ctypes.c_void_p]
        self.handle = self.lib.fr_create(width, height, quad, work)
        if not self.handle:
            raise RuntimeError("Metal renderer creation failed")
        self.frames = 0

    @property
    def compile_ms(self) -> float:
        return float(self.lib.fr_compile_ms(self.handle))

    def frame(self, host_ptr: int, instances: int,
              index: int) -> tuple[float, float, float]:
        gpu = ctypes.c_double(0.0)
        enc = ctypes.c_double(0.0)
        wait = ctypes.c_double(0.0)
        self.lib.fr_frame(self.handle, ctypes.c_void_p(host_ptr), instances,
                          index, ctypes.byref(gpu), ctypes.byref(enc),
                          ctypes.byref(wait))
        self.frames += 1
        return float(gpu.value), float(enc.value), float(wait.value)

    def close(self) -> None:
        if self.handle:
            self.lib.fr_destroy(self.handle)
            self.handle = None
