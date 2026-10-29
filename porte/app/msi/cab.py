"""Cabinet (CAB) reader for MSI payloads.

Supports uncompressed folders and MSZIP (deflate blocks with the `CK` signature
and a 32 KiB sliding dictionary). LZX and Quantum need dedicated decoders and
are refused explicitly rather than producing corrupt files.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

COMPRESS_NONE = 0
COMPRESS_MSZIP = 1
COMPRESS_QUANTUM = 2
COMPRESS_LZX = 3


class CabError(RuntimeError):
    pass


@dataclass
class CabFile:
    name: str
    size: int
    folder: int
    offset: int
    data: bytes = b""


class Cabinet:
    def __init__(self, data: bytes, name: str = "cabinet"):
        self.name = name
        self.data = data
        if len(data) < 36 or data[:4] != b"MSCF":
            raise CabError(f"{name}: not a cabinet")
        self.cb_cabinet = struct.unpack_from("<I", data, 8)[0]
        self.coff_files = struct.unpack_from("<I", data, 16)[0]
        self.cfolders, self.cfiles, self.flags, self.set_id, self.icabinet = \
            struct.unpack_from("<HHHHH", data, 26)
        header_extra = 0
        folder_extra = 0
        data_extra = 0
        if self.flags & 0x0004:  # reserved fields present
            header_extra, folder_extra, data_extra = struct.unpack_from(
                "<HBB", data, 36)
        self.reserve_header = header_extra
        self.reserve_folder = folder_extra
        self.reserve_data = data_extra
        self.folders = self._read_folders()
        self.files = self._read_files()

    def _read_folders(self) -> list[dict]:
        base = 36 + self.reserve_header
        folders = []
        for i in range(self.cfolders):
            off = base + i * (8 + self.reserve_folder)
            start, blocks, method = struct.unpack_from("<IHH", self.data, off)
            folders.append({"start": start, "blocks": blocks,
                            "method": method & 0x000F})
        return folders

    def _read_files(self) -> list[CabFile]:
        files = []
        off = self.coff_files
        for _ in range(self.cfiles):
            if off + 16 > len(self.data):
                raise CabError(f"{self.name}: truncated file table")
            size, offset, folder, _date, _time, attribs = struct.unpack_from(
                "<IIHHHH", self.data, off)
            off += 16
            end = self.data.index(b"\x00", off)
            raw = self.data[off:end]
            name = raw.decode("cp1252", "replace")
            off = end + 1
            files.append(CabFile(name=name, size=size, folder=folder,
                                 offset=offset))
        return files

    def unsupported_folders(self) -> list[str]:
        """Describe folders this reader cannot decode, without decompressing.

        Used during inspection: the caller must be able to tell whether the
        package can be applied *before* anything is written, and to route the
        package to a runtime that does implement the codec.
        """
        problems: list[str] = []
        for index, folder in enumerate(self.folders):
            method = folder["method"]
            if method in (COMPRESS_NONE, COMPRESS_MSZIP):
                continue
            label = {COMPRESS_QUANTUM: "Quantum", COMPRESS_LZX: "LZX"}.get(
                method, f"method {method}")
            problems.append(
                f"{self.name}: folder {index} uses {label} compression, which "
                f"Porte does not decode; MSZIP or uncompressed cabinets are "
                f"supported")
        return problems

    def _folder_stream(self, index: int) -> bytes:
        folder = self.folders[index]
        method = folder["method"]
        if method not in (COMPRESS_NONE, COMPRESS_MSZIP):
            label = {COMPRESS_QUANTUM: "Quantum", COMPRESS_LZX: "LZX"}.get(
                method, f"method {method}")
            raise CabError(
                f"{self.name}: folder {index} uses {label} compression, which "
                f"Porte does not decode; MSZIP or uncompressed cabinets are "
                f"supported")
        pos = folder["start"]
        out = bytearray()
        history = b""
        for _ in range(folder["blocks"]):
            if pos + 8 > len(self.data):
                raise CabError(f"{self.name}: truncated CFDATA")
            _checksum, cb_data, cb_uncomp = struct.unpack_from(
                "<IHH", self.data, pos)
            pos += 8 + self.reserve_data
            payload = self.data[pos:pos + cb_data]
            pos += cb_data
            if method == COMPRESS_NONE:
                out += payload[:cb_uncomp]
                continue
            if payload[:2] != b"CK":
                raise CabError(f"{self.name}: MSZIP block without CK signature")
            decomp = zlib.decompressobj(-15, zdict=history) if history else \
                zlib.decompressobj(-15)
            chunk = decomp.decompress(payload[2:], cb_uncomp) + decomp.flush()
            if len(chunk) != cb_uncomp:
                raise CabError(f"{self.name}: MSZIP block size mismatch")
            out += chunk
            history = bytes(out[-32768:])
        return bytes(out)

    def extract(self) -> dict[str, bytes]:
        streams: dict[int, bytes] = {}
        result: dict[str, bytes] = {}
        for index in range(self.cfolders):
            streams[index] = self._folder_stream(index)
        for f in self.files:
            stream = streams[f.folder]
            f.data = stream[f.offset:f.offset + f.size]
            if len(f.data) != f.size:
                raise CabError(f"{self.name}: {f.name} truncated")
            result[f.name] = f.data
        return result
