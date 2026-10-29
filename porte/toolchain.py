"""Thin wrapper around the host clang toolchain."""
from __future__ import annotations

import shutil
from pathlib import Path

from . import util


class Toolchain:
    def __init__(self) -> None:
        self.clang = shutil.which("clang") or "/usr/bin/clang"
        self.ident = util.toolchain_info()

    def _base(self, arch: str) -> list[str]:
        return [self.clang, "-arch", arch, "-O3", "-fPIC", "-Wall", "-Wextra",
                "-Wno-unused-parameter"]

    def dylib(self, out: Path, sources: list[Path], arch: str,
              include: list[Path], links: list[Path] | None = None,
              defines: list[str] | None = None,
              install_name: str | None = None, verbose: bool = False):
        util.ensure_dir(out.parent)
        cmd = self._base(arch) + ["-dynamiclib"]
        for d in defines or []:
            cmd.append(f"-D{d}")
        for inc in include:
            cmd += ["-I", str(inc)]
        cmd += [str(s) for s in sources]
        cmd += ["-o", str(out)]
        if install_name:
            cmd += ["-Wl,-install_name," + install_name]
        for link in links or []:
            cmd.append(str(link))
        proc = util.run(cmd, check=True)
        if verbose:
            print(proc.stdout, proc.stderr)
        return out

    def executable(self, out: Path, sources: list[Path], arch: str,
                   include: list[Path], links: list[Path] | None = None,
                   defines: list[str] | None = None, verbose: bool = False):
        util.ensure_dir(out.parent)
        cmd = self._base(arch)
        for d in defines or []:
            cmd.append(f"-D{d}")
        for inc in include:
            cmd += ["-I", str(inc)]
        cmd += [str(s) for s in sources]
        cmd += ["-o", str(out)]
        for link in links or []:
            cmd.append(str(link))
        proc = util.run(cmd, check=True)
        if verbose:
            print(proc.stdout, proc.stderr)
        return out
