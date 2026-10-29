"""Compound File Binary (OLE2) reader, enough for MSI databases.

MSI files are compound documents: every table is a stream, the embedded cabinet
is another. This reader handles the on-disk structures the format defines
(header, DIFAT/FAT, directory, mini FAT/mini stream) without external packages.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD
DIFSECT = 0xFFFFFFFC
NOSTREAM = 0xFFFFFFFF


class CompoundFileError(RuntimeError):
    pass


@dataclass
class DirEntry:
    name: str
    kind: int  # 0 empty, 1 storage, 2 stream, 5 root
    start: int
    size: int
    left: int
    right: int
    child: int


class CompoundFile:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        if len(self.data) < 512 or self.data[:8] != MAGIC:
            raise CompoundFileError(f"{self.path.name} is not a compound file")
        header = self.data[:512]
        self.sector_size = 1 << struct.unpack_from("<H", header, 30)[0]
        self.mini_size = 1 << struct.unpack_from("<H", header, 32)[0]
        if self.sector_size not in (512, 4096):
            raise CompoundFileError(f"unsupported sector size {self.sector_size}")
        self.num_fat = struct.unpack_from("<I", header, 44)[0]
        self.first_dir = struct.unpack_from("<I", header, 48)[0]
        self.mini_cutoff = struct.unpack_from("<I", header, 56)[0]
        self.first_minifat = struct.unpack_from("<I", header, 60)[0]
        self.num_minifat = struct.unpack_from("<I", header, 64)[0]
        self.first_difat = struct.unpack_from("<I", header, 68)[0]
        self.num_difat = struct.unpack_from("<I", header, 72)[0]

        self._fat = self._read_fat(header)
        self.entries = self._read_directory()
        root = next((e for e in self.entries if e.kind == 5), None)
        if root is None:
            raise CompoundFileError("compound file has no root entry")
        self.mini_stream = self._read_chain(root.start, root.size)
        self._minifat = self._read_minifat()

    # ---- low level ----------------------------------------------------
    def _sector_offset(self, sector: int) -> int:
        return 512 + sector * self.sector_size

    def _sector(self, sector: int) -> bytes:
        off = self._sector_offset(sector)
        if off + self.sector_size > len(self.data):
            raise CompoundFileError(f"sector {sector} outside file")
        return self.data[off:off + self.sector_size]

    def _read_fat(self, header: bytes) -> list[int]:
        difat: list[int] = []
        for i in range(109):
            sector = struct.unpack_from("<I", header, 76 + i * 4)[0]
            if sector in (FREESECT, ENDOFCHAIN):
                break
            difat.append(sector)
        sector = self.first_difat
        per = self.sector_size // 4 - 1
        for _ in range(self.num_difat):
            if sector in (FREESECT, ENDOFCHAIN):
                break
            raw = self._sector(sector)
            entries = struct.unpack(f"<{self.sector_size // 4}I", raw)
            difat.extend(e for e in entries[:per] if e not in (FREESECT, ENDOFCHAIN))
            sector = entries[per]
        fat: list[int] = []
        for sector in difat[:max(self.num_fat, 0)]:
            raw = self._sector(sector)
            fat.extend(struct.unpack(f"<{self.sector_size // 4}I", raw))
        return fat

    def _chain(self, start: int, table: list[int]) -> list[int]:
        chain: list[int] = []
        sector = start
        seen: set[int] = set()
        while sector not in (ENDOFCHAIN, FREESECT):
            if sector in seen or sector >= len(table):
                raise CompoundFileError("corrupt sector chain")
            seen.add(sector)
            chain.append(sector)
            sector = table[sector]
        return chain

    def _read_chain(self, start: int, size: int | None = None) -> bytes:
        if start in (ENDOFCHAIN, FREESECT):
            return b""
        chunks = [self._sector(s) for s in self._chain(start, self._fat)]
        data = b"".join(chunks)
        return data[:size] if size is not None else data

    def _read_minifat(self) -> list[int]:
        if self.first_minifat in (ENDOFCHAIN, FREESECT) or not self.num_minifat:
            return []
        raw = self._read_chain(self.first_minifat,
                               self.num_minifat * self.sector_size)
        return list(struct.unpack(f"<{len(raw) // 4}I", raw))

    def _read_mini(self, start: int, size: int) -> bytes:
        chain: list[int] = []
        sector = start
        seen: set[int] = set()
        while sector not in (ENDOFCHAIN, FREESECT):
            if sector in seen or sector >= len(self._minifat):
                raise CompoundFileError("corrupt mini sector chain")
            seen.add(sector)
            chain.append(sector)
            sector = self._minifat[sector]
        out = b"".join(
            self.mini_stream[s * self.mini_size:(s + 1) * self.mini_size]
            for s in chain)
        return out[:size]

    def _read_directory(self) -> list[DirEntry]:
        raw = self._read_chain(self.first_dir)
        entries: list[DirEntry] = []
        for off in range(0, len(raw) - 127, 128):
            block = raw[off:off + 128]
            name_len = struct.unpack_from("<H", block, 64)[0]
            kind = block[66]
            if kind not in (1, 2, 5):
                continue
            name = block[:max(0, name_len - 2)].decode("utf-16-le", "replace")
            entries.append(DirEntry(
                name=name, kind=kind,
                start=struct.unpack_from("<I", block, 116)[0],
                size=struct.unpack_from("<Q", block, 120)[0],
                left=struct.unpack_from("<I", block, 68)[0],
                right=struct.unpack_from("<I", block, 72)[0],
                child=struct.unpack_from("<I", block, 76)[0],
            ))
        return entries

    # ---- public -------------------------------------------------------
    def stream_names(self) -> list[str]:
        return [e.name for e in self.entries if e.kind == 2]

    def read_stream(self, name: str) -> bytes:
        entry = next((e for e in self.entries
                      if e.kind == 2 and e.name == name), None)
        if entry is None:
            raise KeyError(name)
        if entry.size < self.mini_cutoff:
            return self._read_mini(entry.start, entry.size)
        return self._read_chain(entry.start, entry.size)

    def __contains__(self, name: str) -> bool:
        return any(e.kind == 2 and e.name == name for e in self.entries)
