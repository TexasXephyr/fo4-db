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

P3 (2026-11-01): reference resolution is per plugin. A 4-byte word's top byte
indexes the WRITING plugin's own MAST list, so `0x0002F3` in CannabisWorld.esp
is "Fallout4.esm record 0x0002F3" while `0x0102F3` is one of its own. Resolving
against a flat formID table picked whichever row happened to sort first, and the
mods' records had never been decoded at all. Both are fixed here, and the
resolution rules are identical to `fo4db.refs.resolve_word`.
"""
from __future__ import annotations

import sqlite3
import struct
from functools import partial

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

LOCAL_MASK = 0x00FFFFFF


class RefResolver:
    """Resolve a formID word written by a given plugin to a readable name.

    Same identity rule as `fo4db.refs.resolve_word`, with `(plugin, local id)`
    as the key and the plugin's own definition preferred over a master override.
    Meta (record type + EDID) is fetched lazily and cached, because the number of
    distinct targets referenced by decoded records is small.
    """

    def __init__(self, cur: sqlite3.Cursor):
        self._cur = cur
        self.name_by_id = {pid: n for pid, n in cur.execute("SELECT id, name FROM plugins")}
        self.id_by_name = {n: p for p, n in self.name_by_id.items()}
        self.masters: dict[int, list[str]] = {}
        for pid, _idx, mname in cur.execute(
            "SELECT plugin_id, idx, name FROM plugin_masters ORDER BY plugin_id, idx"
        ):
            self.masters.setdefault(pid, []).append(mname)
        self.rid_own: dict[tuple[int, int], int] = {}
        self.rid_any: dict[tuple[int, int], int] = {}
        for pid, form_id, rtype, rid in cur.execute(
            "SELECT plugin_id, form_id, record_type, id FROM records"
        ):
            if rtype == "TES4":
                continue
            key = (pid, form_id & LOCAL_MASK)
            if (form_id >> 24) == len(self.masters.get(pid) or []):
                self.rid_own.setdefault(key, rid)
            else:
                self.rid_any.setdefault(key, rid)
        self._meta: dict[int, str] = {}

    def _meta_of(self, rid: int) -> str:
        if rid not in self._meta:
            row = self._cur.execute(
                "SELECT record_type, COALESCE(edid, '') FROM records WHERE id = ?", (rid,)
            ).fetchone()
            self._meta[rid] = f"{row[0]}:{row[1]}" if row else "?"
        return self._meta[rid]

    def resolve(self, pid: int, word: int) -> str:
        mast = self.masters.get(pid) or []
        idx = (word >> 24) & 0xFF
        local = word & LOCAL_MASK
        if idx > len(mast):
            return f"{word:08X} (beyond own slot)"
        if idx < len(mast):
            tname = mast[idx]
            tpid = self.id_by_name.get(tname)
            if tpid is None:
                return f"{tname}:{local:06X} (open)"
        else:
            tpid = pid
            tname = self.name_by_id.get(pid, "?")
        rid = self.rid_own.get((tpid, local))
        if rid is None:
            rid = self.rid_any.get((tpid, local))
        if rid is None:
            return f"{tname}:{local:06X} (absent)"
        return f"{tname}:{local:06X} {self._meta_of(rid)}"


def _resolve_ref(conn: sqlite3.Connection, form_id: int) -> str:
    """Legacy flat lookup, kept only as `decode_record`'s fallback.

    Wrong for mod content by construction — two plugins can own the same local
    id. Use `RefResolver.resolve` for anything real.
    """
    row = conn.execute(
        "SELECT r.record_type, COALESCE(r.edid, '') FROM records r "
        "WHERE r.form_id = ? GROUP BY r.record_type, r.edid LIMIT 1",
        (form_id,),
    ).fetchone()
    return f"{form_id:08X} {row[0]}:{row[1]}" if row else f"{form_id:08X}"


def _decode_mgef_flags(value: int) -> str:
    names = [n for mask, n in sorted(MGEF_FLAGS.items(), key=lambda kv: kv[0]) if value & mask]
    return " | ".join(names) if names else "none"


def decode_record(conn, rid: int, rtype: str, subs: list[tuple[str, int, bytes]], resolve=None) -> list[tuple]:
    """Decode one record's subrecords into (field, kind, value, seq) rows."""
    if resolve is None:
        if conn is None:
            raise ValueError("decode_record needs either a resolve callable or a connection")
        resolve = partial(_resolve_ref, conn)
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
                out.append((f"entry[{i}].ref", "ref", resolve(ref), 10 + i))
                out.append((f"entry[{i}].count", "u16", str(count), 10 + i))

    elif rtype == "COBJ":
        if "CNAM" in sub_map and len(sub_map["CNAM"][0]) >= 4:
            (ref,) = struct.unpack("<I", sub_map["CNAM"][0][:4])
            out.append(("result", "ref", resolve(ref), 0))
        # Fallout 4 stores recipe components as FVPA pairs (ref u32, count u32).
        # One FVPA subrecord can hold 1..5 pairs (8..40 bytes), so the pairs are
        # flattened across every FVPA in the record. The Skyrim-era CNTO never
        # appears: zero COBJ records in this database have one, which is why the
        # first pass decoded recipes with no components at all.
        pairs: list[tuple[int, int]] = []
        for fvpa in sub_map.get("FVPA", []):
            for off in range(0, len(fvpa) - 7, 8):
                pairs.append(struct.unpack("<II", fvpa[off : off + 8]))
        for i, (ref, count) in enumerate(pairs):
            out.append((f"component[{i}].ref", "ref", resolve(ref), 20 + i))
            out.append((f"component[{i}].count", "u32", str(count), 20 + i))
        if "BNAM" in sub_map and len(sub_map["BNAM"][0]) >= 4:
            (ref,) = struct.unpack("<I", sub_map["BNAM"][0][:4])
            out.append(("workbench", "ref", resolve(ref), 90))
        if "FNAM" in sub_map and len(sub_map["FNAM"][0]) >= 4:
            (ref,) = struct.unpack("<I", sub_map["FNAM"][0][:4])
            out.append(("category", "ref", resolve(ref), 91))
        if "INTV" in sub_map and len(sub_map["INTV"][0]) >= 4:
            (n,) = struct.unpack("<I", sub_map["INTV"][0][:4])
            out.append(("produced", "u32", str(n), 92))

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


def build_fields(conn: sqlite3.Connection, limit_types: tuple[str, ...] | None = None) -> dict:
    """Decode all records of the supported types into the fields table.

    Every plugin is decoded, mods included, and each record's references are
    resolved in its own plugin's master-list context.

    Performance: one bulk subrecords query grouped in Python, then one decode per
    record. The first version (per-record query + per-ref query) took > 300s and
    timed out (2026-09-09).
    """
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS fields")
    conn.executescript(FIELDS_SCHEMA)
    types = limit_types or ("ALCH", "MGEF", "LVLI", "COBJ", "MISC", "KYWD")

    subs_by_record: dict[int, list[tuple[str, int, bytes]]] = {}
    for rid, st, seq, raw in cur.execute(
        "SELECT s.record_id, s.sub_type, s.seq, s.raw FROM subrecords s "
        "JOIN records r ON s.record_id = r.id "
        "WHERE r.record_type IN (%s) ORDER BY s.record_id, s.seq"
        % ",".join("?" * len(types)),
        types,
    ):
        subs_by_record.setdefault(rid, []).append((st, seq, raw))

    rec_meta = {
        rid: (rtype, pid)
        for rid, rtype, pid in cur.execute(
            "SELECT id, record_type, plugin_id FROM records WHERE record_type IN (%s)"
            % ",".join("?" * len(types)),
            types,
        )
    }

    resolver = RefResolver(cur)
    rows: list[tuple] = []
    per_plugin: dict[int, int] = {}
    for rid, subs in subs_by_record.items():
        meta = rec_meta.get(rid)
        if meta is None:
            continue
        rtype, pid = meta
        decoded = decode_record(None, rid, rtype, subs, resolve=partial(resolver.resolve, pid))
        for field, kind, value, seq in decoded:
            rows.append((rid, field, kind, value, seq))
        per_plugin[pid] = per_plugin.get(pid, 0) + 1

    cur.executemany(
        "INSERT INTO fields (record_id, field, kind, value, seq) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return {
        "rows": len(rows),
        "records": sum(per_plugin.values()),
        "by_plugin": {resolver.name_by_id.get(p, str(p)): n for p, n in per_plugin.items()},
    }
