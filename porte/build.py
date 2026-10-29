"""Build the experiment fixtures.

Three images per architecture:
  libporte_probe.dylib          shared PC sampler / boundary recorder
  libporte_host.dylib           host effect sink (the "outside" of the ABI)
  libporte_kernel_generic*.dylib the module under study (baseline)
  app                           deterministic workload driver

The x86-64 images are executed through Rosetta; the arm64 images run natively.
"""
from __future__ import annotations

from pathlib import Path

from . import util
from .toolchain import Toolchain

ARCHS = ("x86_64", "arm64")

INCLUDE_DIRS = [util.RUNTIME, util.FIXTURES / "host"]


def arch_dir(arch: str) -> Path:
    return util.BUILD / arch


def paths_for(arch: str) -> dict[str, Path]:
    d = arch_dir(arch)
    return {
        "dir": d,
        "probe": d / "libporte_probe.dylib",
        "host": d / "libporte_host.dylib",
        "generic": d / "libporte_kernel_generic.dylib",
        "generic_profile": d / "libporte_kernel_generic_profile.dylib",
        "inline": d / "libporte_kernel_inline.dylib",
        "app": d / "app",
    }


def build_arch(arch: str, force: bool = False) -> dict[str, Path]:
    p = paths_for(arch)
    util.ensure_dir(p["dir"])
    tc = Toolchain()

    if force or not p["probe"].exists():
        tc.dylib(p["probe"], [util.RUNTIME / "porte_probe.c"], arch,
                 INCLUDE_DIRS, install_name=str(p["probe"]))
    if force or not p["host"].exists():
        tc.dylib(p["host"], [util.FIXTURES / "host" / "host.c"], arch,
                 INCLUDE_DIRS, install_name=str(p["host"]))

    kernel_src = util.FIXTURES / "kernel" / "kernel_generic.c"
    if force or not p["generic"].exists():
        tc.dylib(p["generic"], [kernel_src], arch, INCLUDE_DIRS,
                 links=[p["probe"], p["host"]],
                 install_name=str(p["generic"]))
    if force or not p["generic_profile"].exists():
        tc.dylib(p["generic_profile"], [kernel_src], arch, INCLUDE_DIRS,
                 links=[p["probe"], p["host"]], defines=["PORTE_PROFILE"],
                 install_name=str(p["generic_profile"]))

    inline_src = util.FIXTURES / "kernel" / "kernel_inline_sort.c"
    if force or not p["inline"].exists():
        tc.dylib(p["inline"], [inline_src], arch, INCLUDE_DIRS,
                 links=[p["probe"], p["host"]],
                 install_name=str(p["inline"]))

    if force or not p["app"].exists():
        tc.executable(p["app"], [util.FIXTURES / "app" / "app.c"], arch,
                      INCLUDE_DIRS, links=[p["probe"], p["host"]])
    return p


def build_all(force: bool = False) -> dict[str, dict[str, Path]]:
    return {arch: build_arch(arch, force=force) for arch in ARCHS}


def build_specialization(spec_c: Path, out_dylib: Path, arch: str,
                         generic_dylib: Path) -> Path:
    """Compile generated specialization C into a module image.

    The generated module keeps the generic module as a link-time fallback, so
    a guard failure inside the specialization is a real call into baseline
    behaviour rather than a second implementation.
    """
    tc = Toolchain()
    p = paths_for(arch)
    return tc.dylib(out_dylib, [spec_c], arch,
                    [util.RUNTIME, util.FIXTURES / "host"],
                    links=[generic_dylib, p["host"], p["probe"]],
                    install_name=str(out_dylib))


def artifact_hashes(p: dict[str, Path]) -> dict[str, str]:
    keys = ("app", "generic", "generic_profile", "inline", "probe", "host")
    return {k: util.sha256_file(p[k]) for k in keys}
