"""Read MSI database tables.

The format, as implemented by libmsi/Windows Installer:

- Table streams have their names encoded in a MIME-like scheme (see
  `decode_name`); the encoded name is a UTF-8 string stored in the compound
  file as UTF-16.
- Every table stream is column-major: for each column, `row_count` fixed-width
  cells, with string cells holding an index into the package string pool.
- The string pool is `_StringPool` (length/reference pairs) plus `_StringData`
  (the bytes). Two- or three-byte string references depend on a flag in the
  pool header.
- `_Tables` and `_Columns` describe the other tables; `_Columns.Type` carries
  the column width and whether the cell is a string.

Only what an installer needs is decoded: no query engine, no writes.
"""
from __future__ import annotations

import struct
from pathlib import Path

from .cfb import CompoundFile

MAGIC_PREFIX = b"\xe4\xa1\x80"  # UTF-8 for 0x4840, marks an encoded table name
STRING_POOL = "_StringPool"
STRING_DATA = "_StringData"
TABLES = "_Tables"
COLUMNS = "_Columns"


def _mime2utf(value: int) -> str:
    if value < 10:
        return chr(ord("0") + value)
    if value < 36:
        return chr(ord("A") + value - 10)
    if value < 62:
        return chr(ord("a") + value - 36)
    return "." if value == 62 else "_"


def decode_name(name: str) -> str:
    raw = name.encode("utf-8")
    if raw.startswith(MAGIC_PREFIX):
        raw = raw[3:]
    out = bytearray()
    i = 0
    while i < len(raw):
        ch = raw[i]
        nxt = raw[i + 1] if i + 1 < len(raw) else 0
        if (ch == 0xE3 and nxt >= 0xA0) or (ch == 0xE4 and nxt < 0xA0):
            out += _mime2utf(raw[i + 2] & 0x7F).encode()
            out += _mime2utf(nxt ^ 0xA0).encode()
            i += 3
            continue
        if ch == 0xE4 and nxt == 0xA0:
            out += _mime2utf(raw[i + 2] & 0x7F).encode()
            i += 3
            continue
        width = 1
        if ch >= 0xF0:
            width = 4
        elif ch >= 0xE0:
            width = 3
        elif ch >= 0xC0:
            width = 2
        out += raw[i:i + width]
        i += width
    return out.decode("utf-8", "replace")


class MSIError(RuntimeError):
    pass


class MSIDatabase:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.cfb = CompoundFile(self.path)
        self.streams: dict[str, bytes] = {}
        for entry in self.cfb.entries:
            if entry.kind != 2:
                continue
            try:
                self.streams[decode_name(entry.name)] = self.cfb.read_stream(entry.name)
            except Exception:
                continue
        if STRING_POOL not in self.streams:
            raise MSIError(f"{self.path.name} has no string pool")
        self.codepage, self.strref = self._read_pool()
        self._tables: list[str] = []
        self._columns: dict[str, list[dict]] = {}
        self._cache: dict[str, list[dict]] = {}
        self._read_schema()

    # ---- string pool --------------------------------------------------
    def _read_pool(self) -> tuple[int, int]:
        pool = self.streams[STRING_POOL]
        data = self.streams.get(STRING_DATA, b"")
        count = len(pool) // 4
        entries = list(struct.unpack(f"<{count * 2}H", pool[:count * 4])) if count else []
        flag = entries[1] if len(entries) > 1 else 0
        strref = 3 if len(pool) > 4 and flag & 0x8000 else 2
        codepage = 1252
        if len(pool) > 4:
            raw = entries[0] | ((flag & ~0x8000) << 16)
            codepage = raw or 1252
        self.strings: dict[int, str] = {}
        offset = 0
        index = 1
        i = 1
        while i < count:
            length = entries[i * 2]
            refs = entries[i * 2 + 1]
            if length == 0 and refs == 0:
                i += 1
                index += 1
                continue
            if length == 0:
                length = (entries[i * 2 + 3] << 16) + entries[i * 2 + 2]
                i += 2
            else:
                i += 1
            chunk = data[offset:offset + length]
            try:
                text = chunk.decode(f"cp{codepage}")
            except (LookupError, UnicodeDecodeError):
                text = chunk.decode("latin-1")
            self.strings[index] = text.rstrip("\x00")
            index += 1
            offset += length
        return codepage, strref

    def string(self, ident: int) -> str:
        return self.strings.get(ident, "")

    # ---- schema -------------------------------------------------------
    def _read_schema(self) -> None:
        # _Tables: one string column; _Columns: string, int16, string, int16.
        raw = self.streams.get(TABLES, b"")
        width = self.strref
        if raw and len(raw) % width == 0:
            self._tables = [self.string(int.from_bytes(raw[i:i + width], "little"))
                            for i in range(0, len(raw), width)]
        cols = self._read_columns_table()
        for table in self._tables:
            self._columns[table] = sorted(
                [c for c in cols if c["Table"] == table],
                key=lambda c: c["Number"])
        self._columns[TABLES] = [
            {"Table": TABLES, "Number": 1, "Name": "Name", "Type": 0x0900}]
        self._columns[COLUMNS] = [
            {"Table": COLUMNS, "Number": 1, "Name": "Table", "Type": 0x0900},
            {"Table": COLUMNS, "Number": 2, "Name": "Number", "Type": 0x0102},
            {"Table": COLUMNS, "Number": 3, "Name": "Name", "Type": 0x0900},
            {"Table": COLUMNS, "Number": 4, "Name": "Type", "Type": 0x0102},
        ]

    def _read_columns_table(self) -> list[dict]:
        raw = self.streams.get(COLUMNS, b"")
        if not raw:
            return []
        widths = [self.strref, 2, self.strref, 2]
        row = sum(widths)
        if row == 0 or len(raw) % row:
            raise MSIError("corrupt _Columns stream")
        rows = len(raw) // row
        columns: list[dict] = []
        offsets = [0]
        for width in widths[:-1]:
            offsets.append(offsets[-1] + width * rows)
        for r in range(rows):
            cells = []
            for ci, width in enumerate(widths):
                start = offsets[ci] + r * width
                value = int.from_bytes(raw[start:start + width], "little")
                if ci in (0, 2):
                    cells.append(self.string(value))
                else:
                    # Integers are stored with the sign bit biased so that a
                    # byte comparison sorts them.
                    cells.append(value - 0x8000)
            if not cells[0]:
                continue
            columns.append({"Table": cells[0], "Number": cells[1],
                            "Name": cells[2] or f"Col{cells[1]}",
                            "Type": cells[3]})
        return columns

    # ---- tables -------------------------------------------------------
    @property
    def table_names(self) -> list[str]:
        return sorted(self._columns)

    def columns(self, table: str) -> list[dict]:
        return self._columns.get(table, [])

    def _width(self, coltype: int) -> int:
        if (coltype & ~0x1000) == 0x0900:  # binary stream column
            return 2
        if coltype & 0x0800:
            return self.strref
        size = coltype & 0xFF
        if size <= 2:
            return 2
        if size == 4:
            return 4
        raise MSIError(f"unsupported column type {coltype:#x}")

    def rows(self, table: str) -> list[dict]:
        if table in self._cache:
            return self._cache[table]
        cols = self._columns.get(table)
        if not cols:
            return []
        raw = self.streams.get(table, b"")
        if not raw:
            self._cache[table] = []
            return []
        widths = [self._width(c["Type"]) for c in cols]
        row_size = sum(widths)
        if row_size == 0 or len(raw) % row_size:
            raise MSIError(f"{table}: stream size {len(raw)} not a multiple of {row_size}")
        count = len(raw) // row_size
        offsets = []
        pos = 0
        for width in widths:
            offsets.append(pos)
            pos += width * count
        out: list[dict] = []
        for r in range(count):
            row: dict[str, object] = {}
            for col, width, off in zip(cols, widths, offsets):
                start = off + r * width
                value = int.from_bytes(raw[start:start + width], "little")
                if col["Type"] & 0x0800 and (col["Type"] & ~0x1000) != 0x0900:
                    row[col["Name"]] = self.string(value) if value else ""
                elif width == 2:
                    row[col["Name"]] = value - 0x8000
                else:
                    row[col["Name"]] = value ^ 0x80000000
            out.append(row)
        self._cache[table] = out
        return out

    # ---- convenience --------------------------------------------------
    def properties(self) -> dict[str, str]:
        return {row["Property"]: row["Value"] for row in self.rows("Property")}

    def summary(self) -> dict:
        info = {"codepage": self.codepage, "string_ref_bytes": self.strref,
                "tables": self.table_names, "streams": sorted(self.streams),
                "properties": self.properties()}
        return info
