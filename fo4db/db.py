"""fo4db.db — SQLite schema + ingestion for the canonical FO4 database."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import struct
from datetime import datetime, timezone
from typing import Iterable, Optional

from . import parser

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    game_version TEXT,
    plugins_md5 TEXT,
    plugin_count INTEGER,
    record_count INTEGER,
    subrecord_count INTEGER,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plugins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    md5 TEXT NOT NULL,
    size INTEGER NOT NULL,
    load_order INTEGER,
    ingest_run INTEGER REFERENCES ingest_runs(id)
);

CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plugin_id INTEGER NOT NULL REFERENCES plugins(id),
    form_id INTEGER NOT NULL,
    record_type TEXT NOT NULL,
    edid TEXT,
    flags INTEGER,
    data_size INTEGER,
    UNIQUE (plugin_id, form_id)
);

CREATE TABLE IF NOT EXISTS subrecords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL REFERENCES records(id),
    sub_type TEXT NOT NULL,
    seq INTEGER NOT NULL,
    raw BLOB
);

CREATE TABLE IF NOT EXISTS strings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL REFERENCES records(id),
    sub_type TEXT NOT NULL,
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL REFERENCES records(id),
    sub_type TEXT NOT NULL,
    offset INTEGER NOT NULL,
    target_form_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS string_tables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plugin_id INTEGER NOT NULL REFERENCES plugins(id),
    table_type TEXT NOT NULL,
    source TEXT NOT NULL,
    UNIQUE (plugin_id, table_type)
);

CREATE TABLE IF NOT EXISTS resolved_strings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL REFERENCES records(id),
    sub_type TEXT NOT NULL,
    text TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_records_type ON records(record_type);
CREATE INDEX IF NOT EXISTS idx_records_edid ON records(edid);
CREATE INDEX IF NOT EXISTS idx_strings_text ON strings(text);
CREATE INDEX IF NOT EXISTS idx_refs_target ON refs(target_form_id);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def md5_file(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ingest_plugin(
    conn: sqlite3.Connection,
    path: str,
    load_order: Optional[int] = None,
    ingest_run: Optional[int] = None,
) -> dict:
    """Ingest one plugin file into the DB. Returns record/subrecord counts."""
    name = os.path.basename(path)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO plugins (name, path, md5, size, load_order, ingest_run) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, path, md5_file(path), os.path.getsize(path), load_order, ingest_run),
    )
    plugin_id = cur.execute("SELECT id FROM plugins WHERE name = ?", (name,)).fetchone()[0]

    rec_count = 0
    sub_count = 0
    for rec in parser.walk(path):
        if rec.is_grup:
            continue
        edid = parser.get_edid(rec.data)
        cur.execute(
            "INSERT OR REPLACE INTO records "
            "(plugin_id, form_id, record_type, edid, flags, data_size) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (plugin_id, rec.form_id, rec.rtype, edid, rec.flags, len(rec.data)),
        )
        record_id = cur.execute(
            "SELECT id FROM records WHERE plugin_id = ? AND form_id = ?",
            (plugin_id, rec.form_id),
        ).fetchone()[0]
        for seq, sub in enumerate(parser.subrecords(rec.data)):
            cur.execute(
                "INSERT INTO subrecords (record_id, sub_type, seq, raw) VALUES (?, ?, ?, ?)",
                (record_id, sub.stype, seq, sub.data),
            )
            sub_count += 1
            if sub.stype in ("FULL", "DESC", "SNAM", "BTXT", "ITXT", "TNAM", "RNAM", "INAM", "UNAM", "MNAM", "NNAM", "XNAM", "CNAM", "ANAM"):
                text = parser.decode_text(sub.data)
                if text.strip():
                    cur.execute(
                        "INSERT INTO strings (record_id, sub_type, text) VALUES (?, ?, ?)",
                        (record_id, sub.stype, text),
                    )
        rec_count += 1
    conn.commit()
    return {"records": rec_count, "subrecords": sub_count}


def build_references(conn: sqlite3.Connection) -> int:
    """Scan every record body for u32 words that match known formIDs.

    A word is a reference if it matches any known formID in the DB
    (cross-plugin refs included). This is the raw reference map — the
    wiki's "what points at what" backbone.

    Performance: walks the plugin FILES in memory (like ingestion) and
    batch-inserts. The per-record SQLite round-trip version took >10
    minutes on 2.2M records and was killed (2026-09-09).
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM refs")
    known: set[int] = set()
    for (fid,) in cur.execute("SELECT form_id FROM records"):
        known.add(fid)

    # plugin_id -> file path
    paths = dict(cur.execute("SELECT id, path FROM plugins"))

    rows: list[tuple[int, str, int, int]] = []
    for pid, path in paths.items():
        # record form_id -> record id for this plugin
        fid_to_rid = dict(
            cur.execute("SELECT form_id, id FROM records WHERE plugin_id = ?", (pid,))
        )
        for rec in parser.walk(path):
            if rec.is_grup:
                continue
            rid = fid_to_rid.get(rec.form_id)
            if rid is None:
                continue
            for sub in parser.subrecords(rec.data):
                n = len(sub.data) // 4
                for i in range(n):
                    (word,) = struct.unpack("<I", sub.data[i * 4 : i * 4 + 4])
                    if word in known:
                        rows.append((rid, sub.stype, i * 4, word))
    cur.executemany(
        "INSERT INTO refs (record_id, sub_type, offset, target_form_id) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def resolve_localized_strings(conn: sqlite3.Connection, strings_dir: str) -> int:
    """Resolve lstring indices for localized plugins using .STRINGS tables.

    Vanilla esms are localized (TES4 flag 0x80): FULL/DESC/etc. are u32
    indices into the string tables (in the game's BA2 or loose
    Data/strings/). Mod plugins use inline strings and are skipped.
    Returns the number of resolved strings.
    """
    from . import stringtable

    tables = stringtable.load_all(strings_dir)
    if not tables:
        return 0

    cur = conn.cursor()
    cur.execute("DELETE FROM resolved_strings")
    cur.execute("DELETE FROM string_tables")

    # which plugins are localized? check TES4 flags (0x80)
    # (separate cursors: the inner tes4 query would rebind the outer
    #  iteration cursor and kill it — same bug class as build_references)
    plug_cur = conn.cursor()
    tes4_cur = conn.cursor()
    localized: dict[int, str] = {}
    for pid, name in plug_cur.execute("SELECT id, name FROM plugins"):
        tes4 = tes4_cur.execute(
            "SELECT flags FROM records WHERE plugin_id = ? AND record_type = 'TES4'",
            (pid,),
        ).fetchone()
        if tes4 and tes4[0] & 0x80:
            localized[pid] = name

    count = 0
    # separate cursors: the INSERT inside the loop would rebind the
    # iteration cursor and kill it after the first row (same bug class
    # as build_references, 2026-09-09).
    sel = conn.cursor()
    ins = conn.cursor()
    for pid, pname in localized.items():
        for ttype, table in tables.items():
            ins.execute(
                "INSERT OR REPLACE INTO string_tables (plugin_id, table_type, source) "
                "VALUES (?, ?, ?)",
                (pid, ttype, strings_dir),
            )
        # resolve lstring subrecords: FULL/DESC/SNAM/BTXT/ITXT/TNAM/RNAM/INAM/UNAM/MNAM/NNAM/XNAM/CNAM/ANAM
        lstring_types = ("FULL", "DESC", "SNAM", "BTXT", "ITXT", "TNAM", "RNAM", "INAM", "UNAM", "MNAM", "NNAM", "XNAM", "CNAM", "ANAM")
        for rid, stype, raw in sel.execute(
            "SELECT s.record_id, s.sub_type, s.raw FROM subrecords s "
            "JOIN records r ON s.record_id = r.id "
            "WHERE r.plugin_id = ? AND s.sub_type IN (%s)"
            % ",".join("?" * len(lstring_types)),
            (pid, *lstring_types),
        ):
            if len(raw) != 4:
                continue
            (idx,) = struct.unpack("<I", raw)
            # try each table type in order
            for ttype in ("STRINGS", "DLSTRINGS", "ILSTRINGS"):
                text = tables[ttype].get(idx)
                if text is not None:
                    ins.execute(
                        "INSERT INTO resolved_strings (record_id, sub_type, text) VALUES (?, ?, ?)",
                        (rid, stype, text),
                    )
                    count += 1
                    break
    conn.commit()
    return count


def start_run(conn: sqlite3.Connection, game_version: str, plugins_md5: str) -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO ingest_runs (started_at, game_version, plugins_md5, status) "
        "VALUES (?, ?, ?, 'running')",
        (datetime.now(timezone.utc).isoformat(), game_version, plugins_md5),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, rec_count: int, sub_count: int) -> None:
    cur = conn.cursor()
    cur.execute(
        "UPDATE ingest_runs SET status='done', record_count=?, subrecord_count=? WHERE id=?",
        (rec_count, sub_count, run_id),
    )
    conn.commit()
