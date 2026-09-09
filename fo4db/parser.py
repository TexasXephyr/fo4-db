"""fo4db.parser — Fallout 4 plugin record walker (harvested from espdump.py).

The proven espdump walker, promoted to a library. This is the ONLY place
plugin parsing happens — every tool reads the DB, never the esp.
"""
from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass, field
from typing import Iterator, Optional

GRUP = b"GRUP"
COMPRESSED = 0x00040000
HEADER_SIZE = 24


@dataclass
class Record:
    """One top-level record (or GRUP container) in a plugin file."""

    pos: int
    rtype: str
    flags: int
    form_id: int
    data: bytes
    is_grup: bool = False
    subrecords: list = field(default_factory=list)


@dataclass
class Subrecord:
    stype: str
    data: bytes
    pos: int = 0


def walk(path: str) -> Iterator[Record]:
    """Yield every record in a plugin file, recursing into GRUPs."""
    fsize = os.path.getsize(path)
    with open(path, "rb") as f:

        def visit(pos: int, end: int) -> Iterator[Record]:
            while pos + HEADER_SIZE <= end:
                f.seek(pos)
                hdr = f.read(HEADER_SIZE)
                rtype = hdr[:4]
                # FO4 record header: type(4) size(4) flags(4) formID(4) ...
                # (flags BEFORE formID — verified against raw bytes 2026-09-09;
                #  espdump.py's source has a yield/unpack double-swap that
                #  happens to print correctly; do NOT copy that.)
                dsize, flags, formid = struct.unpack("<III", hdr[4:16])
                if rtype == GRUP:
                    yield Record(pos, "GRUP", flags, formid, b"", is_grup=True)
                    yield from visit(pos + HEADER_SIZE, pos + dsize)
                    pos = pos + dsize
                else:
                    f.seek(pos + HEADER_SIZE)
                    data = f.read(dsize)
                    if flags & COMPRESSED and data:
                        try:
                            data = zlib.decompress(data[4:])
                        except zlib.error:
                            data = b""
                    yield Record(pos, rtype.decode("latin1"), flags, formid, data)
                    pos = pos + HEADER_SIZE + dsize

        yield from visit(0, fsize)


def subrecords(data: bytes) -> Iterator[Subrecord]:
    """Split a record body into subrecords."""
    pos = 0
    n = len(data)
    while pos + 6 <= n:
        stype = data[pos : pos + 4]
        (slen,) = struct.unpack("<H", data[pos + 4 : pos + 6])
        yield Subrecord(stype.decode("latin1"), data[pos + 6 : pos + 6 + slen], pos)
        pos += 6 + slen


def get_edid(data: bytes) -> Optional[str]:
    """Return the EDID (editor ID) of a record body, if present."""
    for sub in subrecords(data):
        if sub.stype == "EDID":
            return sub.data.decode("latin1", "replace").rstrip("\x00")
    return None


def printable(s: bytes) -> str:
    """Decode bytes as text, replacing non-printable with '.'."""
    return "".join(chr(b) if 32 <= b < 127 else "." for b in s)


def decode_text(s: bytes) -> str:
    """Best-effort text decode for string subrecords (FULL/DESC/etc)."""
    return s.decode("utf-8", "replace").rstrip("\x00")


def iter_formids(data: bytes) -> Iterator[tuple[str, int, int]]:
    """Yield (sub_type, offset, u32) for every 4-byte word in a record body.

    Used to build the reference map: any u32 that matches a known formID
    is a reference. Offsets are relative to the record body start.
    """
    for sub in subrecords(data):
        n = len(sub.data) // 4
        for i in range(n):
            (word,) = struct.unpack("<I", sub.data[i * 4 : i * 4 + 4])
            yield sub.stype, i * 4, word
