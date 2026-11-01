"""fo4db.refs — reference resolution through each plugin's own master list.

The v1 map matched every 4-byte word against one flat set of formIDs drawn from
every plugin, which cannot distinguish `0x00000F99` as a Fallout4.esm record from
the same number used by a mod. With mods ingested that is not a rounding error:
5,706 of CannabisWorld.esp's 5,775 records share an integer with an existing
record.

v2 resolves every word correctly:

    word -> (top byte = index into the REFERRING plugin's MAST list, local id)
         -> target plugin name
         -> that plugin's record with that local id, if the plugin is in the library

Outcomes, all recorded rather than guessed:

  target_plugin_id set, resolved_record_id set   a real, resolved reference
  target_plugin_id set, resolved_record_id NULL  target plugin present, id absent
                                                 -> not a reference; row dropped
  target_plugin_id NULL, open_plugin set         target plugin not in the library
                                                 -> OPEN reference, kept

**The scan reads the plugin files.** An earlier version re-mapped the existing
`refs` rows instead, which made purge+ingest lose a plugin's references entirely:
the rows had been deleted and nothing re-derived them. A rebuild must be able to
produce rows that were never there, so it walks the bodies. Record ids come from
the DB, never from the file, so the map stays tied to what was ingested.
"""
from __future__ import annotations

import os
import sqlite3
import struct
import time

from . import parser

DDL = """
DROP TABLE IF EXISTS refs_v2;
CREATE TABLE refs_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL,
    sub_type TEXT NOT NULL,
    offset INTEGER NOT NULL,
    raw_word INTEGER NOT NULL,
    target_plugin_id INTEGER,
    target_local INTEGER NOT NULL,
    resolved_record_id INTEGER,
    open_plugin TEXT,
    confidence TEXT NOT NULL DEFAULT 'high'
);
"""

INDEXES = (
    "CREATE INDEX idx_refs_record ON refs(record_id)",
    "CREATE INDEX idx_refs_target ON refs(resolved_record_id)",
    "CREATE INDEX idx_refs_plocal ON refs(target_plugin_id, target_local)",
    "CREATE INDEX idx_refs_open ON refs(open_plugin) WHERE open_plugin IS NOT NULL",
)

LOCAL_MASK = 0x00FFFFFF
# The engine never assigns record ids below 0x800, but a handful of engine
# markers and special forms live there (Player 0x000007, DoorMarker 0x000001…).
# A word in that range is far more often a count or an operator value than a
# reference, so those rows are kept but marked low-confidence.
ENGINE_ID_FLOOR = 0x000800
BATCH = 200_000


def resolve_word(
    word: int,
    pid: int,
    mast: list[str],
    id_by_name: dict[str, int],
    rid_own: dict[tuple[int, int], int],
    rid_any: dict[tuple[int, int], int],
) -> tuple[str, int | None, int, int | None, str | None]:
    """Classify one 4-byte word: (outcome, target_plugin_id, local, target_rid, open_name).

    outcome is 'resolved' | 'open' | 'absent' | 'unmappable'. This is the whole
    of the P3 correctness argument; `rebuild` calls it per word.

    Target lookup prefers the plugin's OWN definition of that local id over a row
    that merely overrides one of its masters: inside one plugin both can exist
    (a mod may both add id 0xF0C3 and override vanilla 0x0000F0C3), and keying on
    the masked id alone silently merged them.
    """
    own = len(mast)
    idx = word >> 24
    local = word & LOCAL_MASK
    if idx > own:
        return ("unmappable", None, local, None, None)
    if idx < own:
        tname = mast[idx]
        tpid = id_by_name.get(tname)
        if tpid is None:
            return ("open", None, local, None, tname)
    else:
        tpid = pid
    target = rid_own.get((tpid, local))
    if target is None:
        target = rid_any.get((tpid, local))
    if target is None:
        return ("absent", tpid, local, None, None)
    return ("resolved", tpid, local, target, None)


def rebuild(conn: sqlite3.Connection, log=print) -> dict:
    """Rebuild `refs` by scanning every ingested plugin. Returns measured counts."""
    t0 = time.time()
    cur = conn.cursor()

    plugins = cur.execute("SELECT id, name, path FROM plugins ORDER BY id").fetchall()
    name_by_id = {pid: name for pid, name, _ in plugins}
    id_by_name = {name: pid for pid, name, _ in plugins}
    masters: dict[int, list[str]] = {}
    for pid, _idx, mname in cur.execute(
        "SELECT plugin_id, idx, name FROM plugin_masters ORDER BY plugin_id, idx"
    ):
        masters.setdefault(pid, []).append(mname)
    log(f"  plugins={len(plugins)} with_masters={len(masters)}")

    rid_own: dict[tuple[int, int], int] = {}
    rid_any: dict[tuple[int, int], int] = {}
    rid_row: dict[tuple[int, int], int] = {}
    for pid, form_id, rtype, rid in cur.execute(
        "SELECT plugin_id, form_id, record_type, id FROM records"
    ):
        if rtype == "TES4":
            # a plugin header is never a reference target; its formID is 0, which
            # is why every zero-valued field used to resolve to one
            continue
        own = len(masters.get(pid) or [])
        local = form_id & LOCAL_MASK
        rid_row[(pid, form_id)] = rid
        key = (pid, local)
        if (form_id >> 24) == own:
            rid_own.setdefault(key, rid)
        else:
            rid_any.setdefault(key, rid)
    log(f"  records={len(rid_row)} (TES4 headers excluded, {len(rid_own)} own + {len(rid_any)} override identities)")

    before = cur.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
    log(f"  refs before={before}")

    conn.executescript(DDL)
    ins = conn.cursor()
    rows: list[tuple] = []
    stats = {
        "refs_before": before,
        "words_scanned": 0,
        "skipped_zero": 0,
        "resolved": 0,
        "resolved_low": 0,
        "open": 0,
        "dropped_absent_id": 0,
        "dropped_unmappable": 0,
        "records_scanned": 0,
    }

    def flush() -> None:
        if not rows:
            return
        ins.executemany(
            "INSERT INTO refs_v2 (record_id, sub_type, offset, raw_word, "
            "target_plugin_id, target_local, resolved_record_id, open_plugin, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        rows.clear()

    for pid, name, path in plugins:
        if not path or not os.path.exists(path):
            log(f"  SKIP {name}: file not found at {path}")
            continue
        mast = masters.get(pid) or []
        for rec in parser.walk(path):
            if rec.is_grup or rec.rtype == "TES4":
                continue
            rid = rid_row.get((pid, rec.form_id))
            if rid is None:
                continue
            stats["records_scanned"] += 1
            for sub in parser.subrecords(rec.data):
                d = sub.data
                n4 = len(d) // 4
                if not n4:
                    continue
                for k, (word,) in enumerate(struct.iter_unpack("<I", d[: n4 * 4])):
                    if word == 0:
                        # formID 0 is the plugin header (TES4); a zero word is
                        # padding or a zero-valued field, never a reference
                        stats["skipped_zero"] += 1
                        continue
                    stats["words_scanned"] += 1
                    outcome, tpid, local, target, open_name = resolve_word(
                        word, pid, mast, id_by_name, rid_own, rid_any
                    )
                    if outcome == "resolved":
                        conf = "high" if local >= ENGINE_ID_FLOOR else "low"
                        rows.append((rid, sub.stype, k * 4, word, tpid, local, target, None, conf))
                        stats["resolved"] += 1
                        if conf == "low":
                            stats["resolved_low"] += 1
                        if len(rows) >= BATCH:
                            flush()
                    elif outcome == "open":
                        rows.append((rid, sub.stype, k * 4, word, None, local, None, open_name,
                                     "high" if local >= ENGINE_ID_FLOOR else "low"))
                        stats["open"] += 1
                        if len(rows) >= BATCH:
                            flush()
                    elif outcome == "absent":
                        stats["dropped_absent_id"] += 1
                    else:
                        stats["dropped_unmappable"] += 1
        flush()
        log(
            f"  {name}: words={stats['words_scanned']} resolved={stats['resolved']} "
            f"open={stats['open']} dropped={stats['dropped_absent_id']} ({time.time() - t0:.0f}s)"
        )
        conn.commit()

    flush()
    conn.execute("DROP TABLE refs")
    conn.execute("ALTER TABLE refs_v2 RENAME TO refs")
    for stmt in INDEXES:
        conn.execute(stmt)
    conn.commit()

    stats["refs_after"] = conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
    stats["seconds"] = round(time.time() - t0, 1)
    log(
        f"  refs after={stats['refs_after']} resolved={stats['resolved']} "
        f"(low-confidence {stats['resolved_low']}) open={stats['open']} "
        f"dropped={stats['dropped_absent_id']} skipped_zero={stats['skipped_zero']} "
        f"of {stats['words_scanned']} words in {stats['seconds']}s"
    )
    return stats
