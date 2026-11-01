"""fo4db.assets — index BA2 archive contents (metadata only, never copy).

Seek-based BA2 reader: reads only the 24-byte header, the directory
records, and the name table — never file data (the game's ~36GB stay on
disk; the DB knows everything about them). Record layouts verified
against bethesda-structs 0.1.4 source (stephen-bunn; itself reading the
BAE reference) and against BSArch -list on Fallout4 - Interface.ba2
(1101 files, names match) 2026-09-09.

Archives come in two types (magic 'BTDX'):
  GNRL (version 8): file_count fixed-size records:
      hash u32, ext[4], directory_hash u32, _unk u32,
      offset u64, packed u32, unpacked u32, _unk u32            (32B)
  DX10 (version 7): variable-size texture records:
      hash u32, ext[4], directory_hash u32, _unk u8,
      chunks_count u8, chunk_header_size u16, height u16,
      width u16, mips u8, format u8, _unk u16                   (20B)
      then chunks_count × {offset u64, packed u32, unpacked u32,
                           start_mip u16, end_mip u16, _unk u32} (24B)

Name table at names_offset: per file, [VarInt length][1 NUL][path].
  (bethesda-structs reads path[1:] because its PascalString consumes the
   NUL; the true path includes the extension — the table string is the
   authoritative full path.)
"""
from __future__ import annotations

import glob
import os
import sqlite3
import struct

ASSETS_SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archive TEXT NOT NULL,
    archive_path TEXT NOT NULL,
    path TEXT NOT NULL,
    ext TEXT,
    size INTEGER NOT NULL,
    packed_size INTEGER NOT NULL,
    offset INTEGER NOT NULL,
    hash INTEGER NOT NULL,
    UNIQUE (archive_path, path)
);
CREATE INDEX IF NOT EXISTS idx_assets_path ON assets(path);
CREATE INDEX IF NOT EXISTS idx_assets_ext ON assets(ext);
CREATE INDEX IF NOT EXISTS idx_assets_archive ON assets(archive);
"""

HEADER_FMT = "<4sI4sIQ"  # magic, version, type, file_count, names_offset
GNRL_REC_FMT = "<I4sIIQIII"  # 36 bytes
# DX10 24-byte texture header (bethesda-structs layout):
#   hash u32, ext[4], directory_hash u32, _unk u8, chunks_count u8,
#   chunk_header_size u16, height u16, width u16, mips u8, format u8, _unk u16
DX10_HDR_FMT = "<I4sIBBH HHBBH"

# DXGI texture format codes (from dxgi.h) — decode on index so queries
# can filter by actual texture format.
DXGI_NAMES = {
    1: "R32G32B32A32_FLOAT", 2: "R32G32B32A32_UINT", 3: "R32G32B32A32_SINT",
    4: "R32G32B32_FLOAT", 5: "R32G32B32_UINT", 6: "R32G32B32_SINT",
    7: "R16G16B16A16_FLOAT", 8: "R16G16B16A16_UNORM", 9: "R16G16B16A16_UINT",
    10: "R16G16B16A16_SNORM", 11: "R16G16B16A16_SINT",
    24: "R8G8B8A8_UNORM", 25: "R8G8B8A8_UNORM_SRGB", 26: "R8G8B8A8_UINT",
    27: "R8G8B8A8_SNORM", 28: "R8G8B8A8_SINT",
    34: "R8_UNORM", 38: "R8_UINT", 39: "R8_SINT",
    71: "BC1_UNORM", 72: "BC1_UNORM_SRGB",
    74: "BC2_UNORM", 75: "BC2_UNORM_SRGB",
    77: "BC3_UNORM", 78: "BC3_UNORM_SRGB",
    79: "BC4_UNORM", 80: "BC4_SNORM",
    81: "BC5_UNORM", 82: "BC5_SNORM",
    83: "BC6H_UF16", 84: "BC6H_SF16",
    95: "BC7_UNORM", 96: "BC7_UNORM_SRGB",
    100: "P8", 101: "A8_UNORM",
}


def find_archives(data_dir: str) -> list[str]:
    """All .ba2 files in a game Data directory, sorted."""
    return sorted(glob.glob(os.path.join(data_dir, "*.ba2")))


class ArchiveRecord:
    __slots__ = ("hash", "ext", "offset", "packed", "unpacked", "chunks")

    def __init__(self, hash_: int, ext: str, offset: int, packed: int, unpacked: int, chunks: int = 0):
        self.hash = hash_
        self.ext = ext
        self.offset = offset
        self.packed = packed
        self.unpacked = unpacked
        self.chunks = chunks


def read_records(f, file_count: int, atype: str) -> list[ArchiveRecord]:
    """Read directory records (metadata only)."""
    recs: list[ArchiveRecord] = []
    if atype == "GNRL":
        for _ in range(file_count):
            h, ext, _dh, _u0, off, packed, unpacked, _u1 = struct.unpack(
                GNRL_REC_FMT, f.read(36)
            )
            recs.append(ArchiveRecord(h, ext.rstrip(b"\x00").decode("latin1"), off, packed, unpacked))
    else:  # DX10: 24-byte texture header, then chunks_count × 24-byte chunk entries
        for _ in range(file_count):
            h, ext, _dh, _u0, chunks, _chs, height, width, _mips, _fmt, _u1 = struct.unpack(
                DX10_HDR_FMT, f.read(24)
            )
            # chunk entries: offset u64, packed u32, unpacked u32,
            # start_mip u16, end_mip u16, _unk u32  (24B each)
            offset = 0
            packed = 0
            unpacked = 0
            for _ in range(chunks):
                coff, cpk, cun, _, _, _ = struct.unpack("<QIIHH I", f.read(24))
                if offset == 0:
                    offset = coff
                packed += cpk
                unpacked += cun
            recs.append(ArchiveRecord(h, ext.rstrip(b"\x00").decode("latin1"), offset, packed, unpacked, chunks))
    return recs


def read_names(f, names_offset: int, count: int) -> list[str]:
    """Read the name table (per file: VarInt length, NUL, path)."""
    f.seek(names_offset)
    names: list[str] = []
    for _ in range(count):
        # VarInt length (LEB128, 1-5 bytes for realistic sizes)
        shift = 0
        length = 0
        while True:
            b = f.read(1)[0]
            length |= (b & 0x7F) << shift
            if not b & 0x80:
                break
            shift += 7
        f.read(1)  # NUL separator
        names.append(f.read(length).decode("utf-8", "replace"))
    return names


def index_archive(
    conn: sqlite3.Connection,
    archive_path: str,
    archive: str,
    cur: sqlite3.Cursor,
    owner: str = "game",
    source_md5: str | None = None,
) -> int:
    """Index one archive (seek-only, no decompression). Returns file count."""
    with open(archive_path, "rb") as f:
        magic, version, atype, file_count, names_offset = struct.unpack(
            HEADER_FMT, f.read(24)
        )
        if magic != b"BTDX":
            return 0
        atype = atype.decode("latin1")
        recs = read_records(f, file_count, atype)
        names = read_names(f, names_offset, file_count)

    rows = []
    for rec, name in zip(recs, names):
        rows.append(
            (
                archive, archive_path, name, rec.ext,
                rec.unpacked, rec.packed, rec.offset, rec.hash,
                owner, "archive", 0, source_md5, normalise(name),
            )
        )
    cur.executemany(
        "INSERT OR REPLACE INTO assets "
        "(archive, archive_path, path, ext, size, packed_size, offset, hash, "
        " owner, source_kind, precedence, source_md5, path_norm) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def normalise(path: str) -> str:
    """Case- and separator-insensitive key the game also uses."""
    return path.replace("/", "\\").lower()


def index_source(conn: sqlite3.Connection, owner: str, mod_dir: str, cur: sqlite3.Cursor | None = None) -> dict:
    """Index one mod: its BA2 archives plus its loose `Data/` tree.

    Loose files carry precedence 1 so a query can tell that the same mod ships
    the file twice and which copy the engine reads (`bInvalidateOlderFiles=1`).
    Loose coverage is not optional: Cannabis World ships 204 loose files, 33 of
    which shadow its own `- Main.ba2`, and Smoke-able Cigars ships no BA2 at all.
    """
    import hashlib

    cur = cur or conn.cursor()
    result: dict[str, int] = {}

    archives = sorted(
        p for p in glob.glob(os.path.join(mod_dir, "*"))
        if p.lower().endswith(".ba2")
    )
    for ap in archives:
        archive = os.path.basename(ap)
        h = hashlib.md5()
        with open(ap, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        try:
            n = index_archive(conn, ap, archive, cur, owner=owner, source_md5=h.hexdigest())
            result[archive] = n
            print(f"  [{owner}] {archive}: {n} entries  md5={h.hexdigest()[:12]}", flush=True)
        except Exception as e:  # noqa: BLE001 — one bad archive must not kill the pass
            print(f"  [{owner}] {archive}: ERROR {e}", flush=True)

    # loose pass: the mod's overlay root is Data/ when present, else the mod root
    loose_root = os.path.join(mod_dir, "Data")
    if not os.path.isdir(loose_root):
        loose_root = mod_dir
    loose = 0
    shadowed = 0
    archive_keys = {
        row[0]
        for row in cur.execute(
            "SELECT path_norm FROM assets WHERE owner = ? AND source_kind = 'archive'", (owner,)
        )
    }
    rows = []
    for dirpath, _dirnames, filenames in os.walk(loose_root):
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, loose_root)
            if rel.lower().endswith((".ba2", ".esp", ".esm", ".esl", ".meta.ini")):
                continue
            if fn.lower() in ("meta.ini", "readme.txt", "readme.md"):
                continue
            key = normalise(rel)
            shadow = 1 if key in archive_keys else 0
            shadowed += shadow
            h = hashlib.md5()
            try:
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
            except OSError:
                continue
            rows.append(
                (
                    owner, p, rel, os.path.splitext(fn)[1].lstrip(".").lower(),
                    os.path.getsize(p), 0, 0, 0,
                    owner, "loose", 1, h.hexdigest(), key,
                )
            )
            loose += 1
    cur.executemany(
        "INSERT OR REPLACE INTO assets "
        "(archive, archive_path, path, ext, size, packed_size, offset, hash, "
        " owner, source_kind, precedence, source_md5, path_norm) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    result["_loose"] = loose
    result["_shadowed_by_loose"] = shadowed
    print(f"  [{owner}] loose files: {loose}  shadowing own archives: {shadowed}", flush=True)
    return result


def index_all(conn: sqlite3.Connection, data_dir: str, owner: str = "game") -> dict:
    """Index every BA2 in a game data dir, replacing that owner's rows only."""
    cur = conn.cursor()
    cur.execute("DELETE FROM assets WHERE owner = ?", (owner,))
    conn.commit()
    archives = find_archives(data_dir)
    result: dict[str, int] = {}
    total = 0
    for ap in archives:
        archive = os.path.basename(ap)
        try:
            n = index_archive(conn, ap, archive, cur, owner=owner)
            result[archive] = n
            total += n
            print(f"  {archive}: {n} files", flush=True)
        except Exception as e:  # noqa: BLE001 — one bad archive shouldn't kill the pass
            print(f"  {archive}: ERROR {e}", flush=True)
    conn.commit()
    result["_total"] = total
    return result
