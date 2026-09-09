"""fo4db.stringtable — Bethesda .STRINGS/.DLSTRINGS/.ILSTRINGS parser.

Format (verified 2026-09-09 against Fallout4_en.* extracted from
Fallout4 - Interface.ba2, cross-checked with KNY00/lib-bethesda-strings):

    u32 count
    u32 textLength          (bytes of string data at the END of the file)
    (u32 id, u32 relativeOffset) * count   -- directory entries from byte 8
    ... string data (last textLength bytes of the file)

Absolute offset of a string = file_size - textLength + relativeOffset.

.STRINGS strings are null-terminated (C-style).
.DLSTRINGS/.ILSTRINGS strings are u32 length-prefixed + chars + null.

The esm's lstring fields (FULL/DESC/etc.) are u32 indices into these
tables when the plugin has the localization flag (TES4 0x80). The tables
live in the game's BA2 archives (Fallout4 - Interface.ba2) or loose in
Data/strings/.
"""
from __future__ import annotations

import struct
from typing import Iterator, Optional


def parse(path: str) -> dict[int, str]:
    """Parse a string table file into {id: text}."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 8:
        return {}
    (count,) = struct.unpack("<I", data[:4])
    (text_len,) = struct.unpack("<I", data[4:8])
    pairs_end = 8 + 8 * count
    if pairs_end > len(data):
        return {}
    pairs = struct.unpack("<%dI" % (2 * count), data[8:pairs_end])
    initial_point = len(data) - text_len
    is_length_prefixed = path.lower().endswith((".dlstrings", ".ilstrings"))

    out: dict[int, str] = {}
    for i in range(count):
        sid, rel = pairs[2 * i], pairs[2 * i + 1]
        abs_off = initial_point + rel
        if abs_off >= len(data):
            continue
        if is_length_prefixed:
            if abs_off + 4 > len(data):
                continue
            (slen,) = struct.unpack("<I", data[abs_off : abs_off + 4])
            s = data[abs_off + 4 : abs_off + 4 + slen]
        else:
            end = data.find(b"\x00", abs_off)
            if end < 0:
                end = len(data)
            s = data[abs_off:end]
        out[sid] = s.decode("utf-8", "replace")
    return out


def load_all(strings_dir: str) -> dict[str, dict[int, str]]:
    """Load all three table types from a directory (loose or extracted).

    Returns {"STRINGS": {id: text}, "DLSTRINGS": {...}, "ILSTRINGS": {...}}.
    """
    import glob
    import os

    result: dict[str, dict[int, str]] = {}
    for ext in ("STRINGS", "DLSTRINGS", "ILSTRINGS"):
        matches = sorted(glob.glob(os.path.join(strings_dir, f"*_en.{ext}")))
        if not matches:
            matches = sorted(glob.glob(os.path.join(strings_dir, f"*.{ext}")))
        if matches:
            result[ext] = parse(matches[0])
    return result
