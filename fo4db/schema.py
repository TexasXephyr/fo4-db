"""fo4db.schema — decode record bodies into typed, queryable fields.

The schema layer: the `subrecords` table holds raw blobs; this module
decodes them per record type into a `fields` table that answers "what is
this record, really?" — prices, weights, effects with durations, keyword
lists, loot entries, components — with names resolved from the string
tables and the records tables.

Layouts verified against real bytes in fo4.db (2026-09-09):
- ALCH ENIT (20B): +0 value u32, +4 flags u32  (weight lives in DATA f32)
- ALCH DATA (4B): weight f32
- ALCH EFID/EFIT pairs: mag f32, arch u32, dur u32 (12B) — Jet: 0.3mag/10s
- MGEF DATA flags u32@0 (0x802 no-stack+recover, 0xA06 debuff, 0x8802
  hud-marker) — bit table from the fo4-mgef-flags skill
- LVLI: LLCT count, LVLO entries (level u16, unk u16, ref u32, count u16,
  unk u16 = 12B), LVLD chance-none, LVLF flags, LVLM
- COBJ: CNAM result ref u32, INTV, CNTO components (ref u32 + count u32)
- MISC: DATA value u16 + flags u16

The MGEF DATA flags table is authoritative from the fo4-mgef-flags skill
(verified byte-level 2026-09-03) — do NOT re-fetch the wiki.
"""
from __future__ import annotations

import sqlite3
import struct

FIELDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL REFERENCES records(id),
    field TEXT NOT NULL,
    kind TEXT NOT NULL,          -- u32 | f32 | u16 | text | ref | list
    value TEXT NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fields_record ON fields(record_id);
"""

MGEF_FLAGS = {
    0x00000001: "Hostile",
    0x00000002: "Recover",
    0x00000004: "Detrimental",
    0x00000008: "Snap to Navmesh",
    0x00000010: "No Hit Effect",
    0x00000020: "No Death Dispel",
    0x00000040: "No Duration",
    0x00000080: "No Area",
    0x00000100: "No Magnitude",
    0x00000200: "No Recast",
    0x00000400: "FX Persist",
    0x00000800: "No Stacking",
    0x00001000: "Forced Landing",
    0x00002000: "Painless",
    0x00004000: "No Hit Blip",
    0x00008000: "Hide in UI",
    0x00010000: "No Dual Cast",
    0x00020000: "Dispel on Death",
    0x00040000: "Power Armor Effect",
    0x00080000: "Charge Weapon Affect",
}

LSTRING_TYPES = ("FULL", "DESC", "SNAM", "BTXT", "ITXT", "TNAM", "RNAM", "INAM", "UNAM", "MNAM", "NNAM", "XNAM", "CNAM", "ANAM")


def _resolve_ref(conn: sqlite3.Connection, form_id: int) -> str:
    """Resolve a formID to EDID + type for display."""
    cur = conn.cursor()
    row = cur.execute(
        "SELECT r.record_type, COALESCE(r.edid, '') FROM records r "
        "WHERE r.form_id = ? GROUP BY r.record_type, r.edid LIMIT 1",
        (form_id,),
    ).fetchone()
    if row:
        return f"{form_id:08X} {row[0]}:{row[1]}"
    return f"{form_id:08X}"


def _decode_mgef_flags(value: int) -> str:
    names = [n for mask, n in sorted(MGEF_FLAGS.items(), key=lambda kv: kv[0]) if value & mask]
    return " | ".join(names) if names else "none"


def decode_record(conn: sqlite3.Connection, rid: int, rtype: str, subs: list[tuple[str, int, bytes]], resolve=None) -> list[tuple]:
    """Decode one record's subrecords into (field, kind, value, seq) rows."""
    if resolve is None:
        resolve = lambda fid: _resolve_ref(conn, fid)
    out: list[tuple] = []
    sub_map: dict[str, list[bytes]] = {}
    for st, seq, raw in subs:
        sub_map.setdefault(st, []).append(raw)

    if rtype == "ALCH":
        if "DATA" in sub_map and len(sub_map["DATA"][0]) >= 4:
            (w,) = struct.unpack("<f", sub_map["DATA"][0][:4])
            out.append(("weight", "f32", f"{w:.4f}", 0))
        if "ENIT" in sub_map and len(sub_map["ENIT"][0]) >= 8:
            raw = sub_map["ENIT"][0]
            value, flags = struct.unpack("<II", raw[:8])
            out.append(("value", "u32", str(value), 1))
            out.append(("flags", "u32", f"0x{flags:X}", 1))
        # EFID/EFIT pairs — effects
        efit_iter = iter(sub_map.get("EFIT", []))
        for i, efid_raw in enumerate(sub_map.get("EFID", [])):
            if len(efid_raw) != 4:
                continue
            (mgef_ref,) = struct.unpack("<I", efid_raw)
            try:
                efit = next(efit_iter)
                mag, arch, dur = struct.unpack("<fII", efit[:12])
            except (StopIteration, struct.error):
                continue
            out.append((f"effect[{i}].mgef", "ref", resolve(mgef_ref), i + 2))
            out.append((f"effect[{i}].magnitude", "f32", f"{mag:.4f}", i + 2))
            out.append((f"effect[{i}].archtype", "u32", str(arch), i + 2))
            out.append((f"effect[{i}].duration", "u32", str(dur), i + 2))
        for i, kw_raw in enumerate(sub_map.get("KWDA", [])):
            (kw,) = struct.unpack("<I", kw_raw[:4])
            out.append((f"keyword[{i}]", "ref", resolve(kw), 50 + i))

    elif rtype == "MGEF":
        if "DATA" in sub_map and len(sub_map["DATA"][0]) >= 4:
            (flags,) = struct.unpack("<I", sub_map["DATA"][0][:4])
            out.append(("flags", "u32", f"0x{flags:X}", 0))
            out.append(("flags.named", "text", _decode_mgef_flags(flags), 0))

    elif rtype == "LVLI":
        if "LLCT" in sub_map:
            out.append(("entry_count", "u32", str(sub_map["LLCT"][0][0]), 0))
        if "LVLD" in sub_map:
            out.append(("chance_none", "u32", str(sub_map["LVLD"][0][0]), 0))
        if "LVLF" in sub_map:
            out.append(("flags", "u32", f"0x{sub_map['LVLF'][0][0]:02X}", 0))
        for i, lvlo in enumerate(sub_map.get("LVLO", [])):
            if len(lvlo) >= 12:
                level, _u, ref, count, _u2 = struct.unpack("<HHIHH", lvlo[:12])
                out.append((f"entry[{i}].level", "u16", str(level), 10 + i))
                out.append((f"entry[{i}].ref", "ref", _resolve_ref(conn, ref), 10 + i))
                out.append((f"entry[{i}].count", "u16", str(count), 10 + i))

    elif rtype == "COBJ":
        if "CNAM" in sub_map and len(sub_map["CNAM"][0]) >= 4:
            (ref,) = struct.unpack("<I", sub_map["CNAM"][0][:4])
            out.append(("result", "ref", _resolve_ref(conn, ref), 0))
        for i, cnto in enumerate(sub_map.get("CNTO", [])):
            if len(cnto) >= 8:
                ref, count = struct.unpack("<II", cnto[:8])
                out.append((f"component[{i}].ref", "ref", _resolve_ref(conn, ref), 20 + i))
                out.append((f"component[{i}].count", "u32", str(count), 20 + i))

    elif rtype == "MISC":
        if "DATA" in sub_map and len(sub_map["DATA"][0]) >= 4:
            value, flags = struct.unpack("<HH", sub_map["DATA"][0][:4])
            out.append(("value", "u16", str(value), 0))
            out.append(("flags", "u16", f"0x{flags:04X}", 0))

    elif rtype == "KYWD":
        if "CNAM" in sub_map and sub_map["CNAM"][0]:
            raw = sub_map["CNAM"][0].rstrip(b"\x00")
            out.append(("meaning", "text", raw.decode("utf-8", "replace"), 0))

    return out


def build_fields(conn: sqlite3.Connection, limit_types: tuple[str, ...] | None = None) -> int:
    """Decode all records of the supported types into the fields table.

    Performance: one bulk subrecords query grouped in Python, one
    memoized resolve per unique referenced formID. The first version
    (per-record query + per-ref query) took > 300s and timed out
    (2026-09-09).
    """
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS fields")
    conn.executescript(FIELDS_SCHEMA)
    types = limit_types or ("ALCH", "MGEF", "LVLI", "COBJ", "MISC", "KYWD")

    # bulk: all subrecords for the target types, grouped by record_id
    subs_by_record: dict[int, list[tuple[str, int, bytes]]] = {}
    for rid, st, seq, raw in cur.execute(
        "SELECT s.record_id, s.sub_type, s.seq, s.raw FROM subrecords s "
        "JOIN records r ON s.record_id = r.id "
        "WHERE r.record_type IN (%s) ORDER BY s.record_id, s.seq"
        % ",".join("?" * len(types)),
        types,
    ):
        subs_by_record.setdefault(rid, []).append((st, seq, raw))

    # all record types in one bulk dict
    rec_types = dict(cur.execute(
        "SELECT id, record_type FROM records WHERE record_type IN (%s)" % ",".join("?" * len(types)),
        types,
    ))

    # memo: formID -> "TYPE:EDID" (one query per unique ref, not per ref)
    ref_cache: dict[int, str] = {}

    def resolve_ref(form_id: int) -> str:
        if form_id not in ref_cache:
            row = cur.execute(
                "SELECT r.record_type, COALESCE(r.edid, '') FROM records r "
                "WHERE r.form_id = ? GROUP BY r.record_type, r.edid LIMIT 1",
                (form_id,),
            ).fetchone()
            ref_cache[form_id] = (
                f"{form_id:08X} {row[0]}:{row[1]}" if row else f"{form_id:08X}"
            )
        return ref_cache[form_id]

    rows: list[tuple] = []
    for rid, subs in subs_by_record.items():
        rtype = rec_types.get(rid)
        if rtype is None:
            continue
        decoded = decode_record(conn, rid, rtype, subs, resolve=resolve_ref)
        for field, kind, value, seq in decoded:
            rows.append((rid, field, kind, value, seq))

    cur.executemany(
        "INSERT INTO fields (record_id, field, kind, value, seq) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)
