"""PE32+ parsing built on pefile, with no dependency on symbols or sources."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pefile

PAGE = 0x1000


def align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


@dataclass
class Section:
    name: str
    va: int
    vsize: int
    raw_ptr: int
    raw_size: int
    characteristics: int

    @property
    def readable(self) -> bool:
        return bool(self.characteristics & 0x40000000)

    @property
    def writable(self) -> bool:
        return bool(self.characteristics & 0x80000000)

    @property
    def executable(self) -> bool:
        return bool(self.characteristics & 0x20000000)


@dataclass
class ImportRef:
    dll: str
    name: str | None
    ordinal: int | None
    iat_rva: int


@dataclass
class PEImage:
    path: Path
    data: bytes
    image_base: int
    entry_rva: int
    size_of_image: int
    size_of_headers: int
    sections: list[Section] = field(default_factory=list)
    exports: dict[str, int] = field(default_factory=dict)
    export_rvas: list[int] = field(default_factory=list)
    imports: list[ImportRef] = field(default_factory=list)
    relocations: list[tuple[int, int]] = field(default_factory=list)
    is_dll: bool = False

    @classmethod
    def load(cls, path: Path) -> "PEImage":
        data = Path(path).read_bytes()
        pe = pefile.PE(data=data, fast_load=False)
        opt = pe.OPTIONAL_HEADER
        if opt.Magic != 0x20B:
            raise ValueError("only PE32+ (x86-64) images are supported")
        if pe.FILE_HEADER.Machine != 0x8664:
            raise ValueError(
                f"unsupported machine type 0x{pe.FILE_HEADER.Machine:x}")

        sections = []
        for s in pe.sections:
            sections.append(Section(
                name=s.Name.rstrip(b"\x00").decode("ascii", "replace"),
                va=s.VirtualAddress, vsize=s.Misc_VirtualSize,
                raw_ptr=s.PointerToRawData, raw_size=s.SizeOfRawData,
                characteristics=s.Characteristics))

        exports: dict[str, int] = {}
        export_rvas: list[int] = []
        if hasattr(pe, "DIRECTORY_ENTRY_EXPORT"):
            for sym in pe.DIRECTORY_ENTRY_EXPORT.symbols:
                export_rvas.append(sym.address)
                if sym.name:
                    exports[sym.name.decode("ascii", "replace")] = sym.address

        imports: list[ImportRef] = []
        if hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
            for entry in pe.DIRECTORY_ENTRY_IMPORT:
                dll = entry.dll.decode("ascii", "replace")
                for imp in entry.imports:
                    imports.append(ImportRef(
                        dll=dll,
                        name=imp.name.decode("ascii", "replace") if imp.name else None,
                        ordinal=imp.ordinal if not imp.name else None,
                        iat_rva=imp.address))

        relocs: list[tuple[int, int]] = []
        if hasattr(pe, "DIRECTORY_ENTRY_BASERELOC"):
            for block in pe.DIRECTORY_ENTRY_BASERELOC:
                for r in block.entries:
                    relocs.append((r.rva, r.type))

        return cls(
            path=Path(path), data=data,
            image_base=opt.ImageBase,
            entry_rva=opt.AddressOfEntryPoint,
            size_of_image=opt.SizeOfImage,
            size_of_headers=opt.SizeOfHeaders,
            sections=sections, exports=exports, export_rvas=export_rvas,
            imports=imports, relocations=relocs,
            is_dll=bool(pe.FILE_HEADER.Characteristics & 0x2000))

    def file_offset(self, rva: int) -> int | None:
        for s in self.sections:
            if s.va <= rva < s.va + max(s.vsize, s.raw_size):
                off = s.raw_ptr + (rva - s.va)
                if off < len(self.data):
                    return off
        if rva < self.size_of_headers and rva < len(self.data):
            return rva
        return None

    def read_rva(self, rva: int, size: int) -> bytes:
        off = self.file_offset(rva)
        if off is None:
            raise ValueError(f"RVA 0x{rva:x} is not backed by the file")
        return self.data[off:off + size]

    def section_for_rva(self, rva: int) -> Section | None:
        for s in self.sections:
            if s.va <= rva < s.va + max(s.vsize, s.raw_size):
                return s
        return None

    def export_code(self, name: str | None = None) -> tuple[str, int]:
        """Pick the export to use as the entry point.

        The name is invocation metadata, not kernel identification: Porte still
        discovers the hot code from execution.
        """
        if name:
            if name not in self.exports:
                raise KeyError(f"export {name!r} not found in {self.path.name}")
            return name, self.exports[name]
        if not self.exports:
            raise ValueError("image has no named exports; pass an entry RVA")
        name, rva = sorted(self.exports.items())[0]
        return name, rva
