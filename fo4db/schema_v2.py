"""fo4db.schema_v2 — P3 migration: master lists, profile state, per-plugin purge.

Additive and idempotent. Nothing here drops data; the `refs` rebuild lives in
`fo4db.refs`.

New/changed objects:
  plugins.is_light, plugins.source          (ALTER, defaulted for existing rows)
  plugin_masters(plugin_id, idx, name)      the MAST list, in file order
  profile_plugins(profile, plugin, active, declared_index, source_file)
  assets.owner, .source_kind, .precedence, .source_md5, .path_norm
                                            (ALTER; existing rows become owner='game')
"""
from __future__ import annotations

import os
import sqlite3

DDL = """
CREATE TABLE IF NOT EXISTS plugin_masters (
    plugin_id INTEGER NOT NULL REFERENCES plugins(id),
    idx INTEGER NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (plugin_id, idx)
);
CREATE TABLE IF NOT EXISTS profile_plugins (
    profile TEXT NOT NULL,
    plugin TEXT NOT NULL,
    active INTEGER NOT NULL,
    declared_index INTEGER,
    source_file TEXT NOT NULL,
    PRIMARY KEY (profile, plugin)
);
"""


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure(conn: sqlite3.Connection) -> list[str]:
    """Create/alter the v2 objects. Returns the list of changes applied."""
    applied: list[str] = []
    if not _cols(conn, "assets"):
        from . import assets

        conn.executescript(assets.ASSETS_SCHEMA)
        applied.append("assets(table)")
    conn.executescript(DDL)

    pcols = _cols(conn, "plugins")
    for name, decl in (("is_light", "INTEGER"), ("source", "TEXT")):
        if name not in pcols:
            conn.execute(f"ALTER TABLE plugins ADD COLUMN {name} {decl}")
            applied.append(f"plugins.{name}")

    acols = _cols(conn, "assets")
    for name, decl in (
        ("owner", "TEXT"),
        ("source_kind", "TEXT"),
        ("precedence", "INTEGER"),
        ("source_md5", "TEXT"),
        ("path_norm", "TEXT"),
    ):
        if name not in acols:
            conn.execute(f"ALTER TABLE assets ADD COLUMN {name} {decl}")
            applied.append(f"assets.{name}")

    # Always run: idempotent backfill of rows that predate P3 (or were added
    # without an owner). Guarding this on "column was just created" silently
    # skips the repair after a failed first attempt.
    conn.execute("UPDATE assets SET owner = 'game' WHERE owner IS NULL OR owner = ''")
    conn.execute("UPDATE assets SET source_kind = 'archive' WHERE source_kind IS NULL OR source_kind = ''")
    conn.execute("UPDATE assets SET precedence = 0 WHERE precedence IS NULL")
    conn.execute(
        "UPDATE assets SET path_norm = lower(replace(path, '/', '\\')) WHERE path_norm IS NULL"
    )
    conn.commit()
    return applied


def purge_plugin(conn: sqlite3.Connection, name: str) -> dict:
    """Delete every derived row for one plugin. The unit of re-ingest."""
    cur = conn.cursor()
    row = cur.execute("SELECT id FROM plugins WHERE name = ?", (name,)).fetchone()
    if row is None:
        return {"plugin": name, "purged": False}
    pid = row[0]
    counts: dict[str, int] = {}
    sub = "SELECT id FROM records WHERE plugin_id = ?"
    for table in ("refs", "fields", "strings", "resolved_strings"):
        try:
            counts[table] = cur.execute(
                f"DELETE FROM {table} WHERE record_id IN ({sub})", (pid,)
            ).rowcount
        except sqlite3.OperationalError:
            counts[table] = 0
    # references that point AT a purged record must go too, or the map keeps
    # resolved_record_id values that no longer exist
    try:
        counts["refs_into_plugin"] = cur.execute(
            "DELETE FROM refs WHERE resolved_record_id IN (%s)" % sub, (pid,)
        ).rowcount
    except sqlite3.OperationalError:
        counts["refs_into_plugin"] = 0
    counts["subrecords"] = cur.execute(
        "DELETE FROM subrecords WHERE record_id IN (%s)" % sub, (pid,)
    ).rowcount
    counts["records"] = cur.execute("DELETE FROM records WHERE plugin_id = ?", (pid,)).rowcount
    counts["plugin_masters"] = cur.execute(
        "DELETE FROM plugin_masters WHERE plugin_id = ?", (pid,)
    ).rowcount
    counts["plugin"] = cur.execute("DELETE FROM plugins WHERE id = ?", (pid,)).rowcount
    conn.commit()
    counts["purged"] = True
    return {"plugin": name, **counts}


def set_profile_plugins(conn: sqlite3.Connection, profile: str, plugins_txt: str) -> dict:
    """Record a profile's declared plugin set from its own plugins.txt.

    `plugins.txt` is the file the engine reads: `*` marks active, line order is
    load order. `loadorder.txt` beside it is stale and is deliberately not read.
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM profile_plugins WHERE profile = ?", (profile,))
    rows: list[tuple] = []
    idx = 0
    with open(plugins_txt, encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            active = 1 if s.startswith("*") else 0
            name = s.lstrip("*").strip()
            if not name:
                continue
            rows.append((profile, name, active, idx, plugins_txt))
            idx += 1
    cur.executemany(
        "INSERT OR REPLACE INTO profile_plugins "
        "(profile, plugin, active, declared_index, source_file) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return {"profile": profile, "plugins": len(rows), "active": sum(r[2] for r in rows)}


def link_in_scope(conn: sqlite3.Connection) -> int:
    """Mark profile_plugins rows whose plugin is in the library."""
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE profile_plugins ADD COLUMN in_library INTEGER")
    except sqlite3.OperationalError:
        pass
    n = cur.execute(
        "UPDATE profile_plugins SET in_library = "
        "(SELECT 1 FROM plugins p WHERE p.name = profile_plugins.plugin) IS NOT NULL"
    ).rowcount
    conn.commit()
    return n


def backfill_masters(conn: sqlite3.Connection, log=print) -> int:
    """Read MAST lists for plugins already in the DB that have none recorded."""
    from . import masters as masters_mod

    cur = conn.cursor()
    todo = cur.execute(
        "SELECT p.id, p.name, p.path FROM plugins p WHERE p.is_light IS NULL"
    ).fetchall()
    done = 0
    for pid, name, path in todo:
        if not path or not os.path.exists(path):
            log(f"  NO FILE for {name} ({path}) — masters unknown")
            continue
        flags, mast = masters_mod.read_tes4(path)
        cur.executemany(
            "INSERT OR REPLACE INTO plugin_masters (plugin_id, idx, name) VALUES (?, ?, ?)",
            [(pid, i, m) for i, m in enumerate(mast)],
        )
        cur.execute(
            "UPDATE plugins SET is_light = ? WHERE id = ?",
            (1 if flags & masters_mod.LIGHT_FLAG else 0, pid),
        )
        done += 1
        log(f"  {name}: {len(mast)} master(s){' [light]' if flags & masters_mod.LIGHT_FLAG else ''}")
    conn.commit()
    return done


def find_plugins(name: str, root: str, max_depth: int = 4) -> list[str]:
    """Every file called `name` under a mod folder or the shared mod store."""
    direct = os.path.join(root, name)
    if os.path.isfile(direct):
        return [direct]
    root = root.rstrip(os.sep)
    base_depth = root.count(os.sep)
    lowered = name.lower()
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.count(os.sep) - base_depth >= max_depth:
            dirnames[:] = []
            continue
        for f in filenames:
            if f.lower() == lowered:
                hits.append(os.path.join(dirpath, f))
    return sorted(hits)


def find_plugin(name: str, root: str, max_depth: int = 4) -> str | None:
    """Locate a plugin file. Returns None when absent OR ambiguous.

    Ambiguity is real here: two Cannabis Commonwealth builds are installed and
    both supply `Cannabis Commonwealth.esp`. Silently taking the first match is
    how the wrong build gets ingested — use `find_plugins` and a manifest.
    """
    hits = find_plugins(name, root, max_depth)
    return hits[0] if len(hits) == 1 else None
