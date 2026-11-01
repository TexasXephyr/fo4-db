"""fo4db.cli — query verbs for the FO4 data wiki.

Verbs:
  ingest [<data_dir>] [--plugins txt] [--plugin NAME ...] [--mod-dir DIR]
         [--purge] [--dry-run]                  build/refresh the DB
  migrate                                      apply the P3 schema (idempotent)
  refs <EDID|0xLOCAL> [--plugin NAME]          what points at a record
  out <EDID|0xLOCAL> [--plugin NAME]           what a record points at
  open-refs                                    references to plugins not in the library
  records [--type T] [--edid X] [--plugin P]   list records
  text [--search S] [--type T] [--limit N]     search strings
  freshness                                    does the DB match the files on disk?
  stats                                        DB summary
  field <EDID|0xLOCAL> [--plugin NAME]          decoded fields for one record
  fields                                       decode record types (schema layer)
  resolve <strings_dir>                        resolve localized lstrings
  assets [<data_dir>] [--owner game]           index a game Data dir's BA2s
  assets-mod <owner> <mod_dir>                 index one mod: BA2s + loose files
  profile <name> <plugins.txt>                 record a profile's declared plugin set
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys

from . import db

DEFAULT_DB = os.environ.get("FO4_DB") or os.path.expanduser("~/dev/fo4-db/fo4.db")


def _connect() -> sqlite3.Connection:
    if not os.path.exists(DEFAULT_DB):
        print(f"DB not found: {DEFAULT_DB}\nRun: fo4-db ingest <game Data dir>")
        sys.exit(1)
    return db.connect(DEFAULT_DB)


def _opt(args: list[str], name: str) -> str | None:
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def _multi(args: list[str], name: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == name and i + 1 < len(args)]


def _game_version(game_root: str) -> str:
    exe = os.path.join(game_root, "Fallout4.exe")
    if os.path.exists(exe):
        with open(exe, "rb") as f:
            data = f.read(1 << 20)
        for s in data.split(b"\x00"):
            if s.startswith(b"1.") and b"." in s[2:]:
                return s.decode("latin1")
    return "unknown"


def _plugins_md5(plugins_txt: str | None) -> str:
    if not plugins_txt or not os.path.exists(plugins_txt):
        return "none"
    h = hashlib.md5()
    with open(plugins_txt, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _preparse(path: str) -> tuple[int, int]:
    from . import parser

    recs = subs = 0
    for rec in parser.walk(path):
        if rec.is_grup:
            continue
        recs += 1
        subs += sum(1 for _ in parser.subrecords(rec.data))
    return recs, subs


def cmd_migrate(args: list[str]) -> None:
    from . import schema_v2

    conn = _connect()
    applied = schema_v2.ensure(conn)
    print("schema v2 applied:", ", ".join(applied) if applied else "already current")
    n = schema_v2.backfill_masters(conn)
    print(f"master lists read for {n} plugin(s)")


def _load_manifest(path: str, mods_root: str | None) -> list[tuple[str, str]]:
    """Read `<plugin>\t<folder-or-path>` lines. No guessing: every path must exist."""
    out: list[tuple[str, str]] = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = [p for p in s.split("\t") if p.strip()]
            if len(parts) < 2:
                raise SystemExit(f"{path}:{lineno}: expected '<plugin>\\t<folder-or-path>'")
            name, sub = parts[0].strip(), parts[1].strip().rstrip("/")
            p = sub if sub.lower().endswith(name.lower()) else os.path.join(sub, name)
            if mods_root and not os.path.isabs(p):
                p = os.path.join(mods_root, p)
            out.append((name, p))
    return out


def _resolve_one(name: str, roots: list[str]) -> tuple[str | None, str | None]:
    """(path, error). Refuses ambiguity rather than choosing a build."""
    from . import schema_v2

    if os.path.isfile(name):
        return os.path.abspath(name), None
    hits: list[str] = []
    for root in roots:
        hits += schema_v2.find_plugins(name, root)
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, f"not found under {roots}"
    return None, "ambiguous, pass an explicit path:\n    " + "\n    ".join(hits)


def cmd_ingest(args: list[str]) -> None:
    from . import schema_v2

    data_dir = args[0] if args and not args[0].startswith("--") else None
    plugins_txt = _opt(args, "--plugins")
    mod_dir = _opt(args, "--mod-dir")
    mods_root = _opt(args, "--mods-root")
    manifest = _opt(args, "--manifest")
    want_purge = "--purge" in args
    dry = "--dry-run" in args
    names = _multi(args, "--plugin")

    if not data_dir and not names and not manifest:
        print(
            "usage: fo4-db ingest [<data_dir>] [--plugin NAME ...] [--mod-dir DIR]\n"
            "       fo4-db ingest --manifest scope.tsv [--mods-root DIR] [--purge] [--dry-run]"
        )
        sys.exit(1)

    conn = db.connect(DEFAULT_DB)
    if not dry:
        schema_v2.ensure(conn)
    run_id = None if dry else db.start_run(conn, _game_version(data_dir or ""), _plugins_md5(plugins_txt))

    order: list[str] = []
    if plugins_txt and os.path.exists(plugins_txt):
        with open(plugins_txt) as f:
            order = [
                line.strip().lstrip("*")
                for line in f
                if line.strip() and not line.strip().startswith("#")
            ]

    planned: list[tuple[str, str]] = []
    if manifest:
        entries = _load_manifest(manifest, mods_root)
        for n, p in entries:
            if not os.path.isfile(p):
                print(f"  MISSING IN MANIFEST: {n} -> {p}")
                continue
            planned.append((n, p))
    elif names:
        roots = [r for r in (mod_dir, data_dir) if r]
        for n in names:
            path, err = _resolve_one(n, roots)
            if path is None:
                print(f"  SKIP {n}: {err}")
                continue
            planned.append((n, path))
    else:
        assert data_dir is not None
        files = sorted(
            f for f in os.listdir(data_dir) if f.lower().endswith((".esm", ".esp", ".esl"))
        )
        files.sort(key=lambda f: (f.lower() not in ("fallout4.esm",),
                                  order.index(f) if f in order else 999))
        planned = [(f, os.path.join(data_dir, f)) for f in files]

    total_rec = total_sub = 0

    for fname, path in planned:
        if dry:
            recs, subs = _preparse(path)
            row = conn.execute("SELECT id, md5 FROM plugins WHERE name = ?", (fname,)).fetchone()
            cur_rec = cur_sub = 0
            if row:
                cur_rec = conn.execute(
                    "SELECT COUNT(*) FROM records WHERE plugin_id = ?", (row[0],)
                ).fetchone()[0]
                cur_sub = conn.execute(
                    "SELECT COUNT(*) FROM subrecords s JOIN records r ON s.record_id = r.id "
                    "WHERE r.plugin_id = ?", (row[0],)
                ).fetchone()[0]
            new_md5 = db.md5_file(path)
            state = "new" if not row else ("unchanged" if row[1] == new_md5 else "changed")
            print(
                f"  DRY {fname}: file={recs} records/{subs} subrecords  "
                f"in_db={cur_rec}/{cur_sub}  md5={state}"
            )
            continue

        if want_purge:
            res = schema_v2.purge_plugin(conn, fname)
            if res.get("purged"):
                print(
                    f"  purge {fname}: records={res['records']} subrecords={res['subrecords']} "
                    f"refs_out={res['refs']} refs_in={res.get('refs_into_plugin', 0)} "
                    f"strings={res['strings']} fields={res['fields']}"
                )

        lo = order.index(fname) if fname in order else None
        print(f"  ingesting {fname} ...", flush=True)
        res = db.ingest_plugin(
            conn, path, load_order=lo, ingest_run=run_id, source=os.path.dirname(path)
        )
        total_rec += res["records"]
        total_sub += res["subrecords"]
        print(f"    {res['records']} records, {res['subrecords']} subrecords", flush=True)

    if dry:
        print("\ndry run: nothing written")
        return

    print("  rebuilding reference map (master-mapped) ...", flush=True)
    assert run_id is not None
    nrefs = db.build_references(conn)
    db.finish_run(conn, run_id, total_rec, total_sub)
    print(
        f"done: {len(planned)} plugins, {total_rec} records, "
        f"{total_sub} subrecords, {nrefs} references"
    )


def cmd_records(args: list[str]) -> None:
    conn = _connect()
    cur = conn.cursor()
    q = "SELECT r.form_id, r.record_type, r.edid, p.name FROM records r JOIN plugins p ON r.plugin_id = p.id"
    conds, params = [], []
    if "--type" in args:
        conds.append("r.record_type = ?")
        params.append(_opt(args, "--type"))
    if "--edid" in args:
        conds.append("r.edid LIKE ?")
        params.append(f"%{_opt(args, '--edid')}%")
    if "--plugin" in args:
        conds.append("p.name = ?")
        params.append(_opt(args, "--plugin"))
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY r.form_id LIMIT ?"
    params.append(int(_opt(args, "--limit") or 50))
    for fid, rtype, edid, pname in cur.execute(q, params):
        print(f"{fid:08X} {rtype:6s} {edid or '':40s} {pname}")


def cmd_text(args: list[str]) -> None:
    conn = _connect()
    cur = conn.cursor()
    # search BOTH inline strings (mod plugins) and resolved lstrings (vanilla)
    q = """SELECT r.form_id, r.record_type, r.edid, s.sub_type, s.text, 'inline' AS src
           FROM strings s JOIN records r ON s.record_id = r.id
           UNION ALL
           SELECT r.form_id, r.record_type, r.edid, s.sub_type, s.text, 'resolved' AS src
           FROM resolved_strings s JOIN records r ON s.record_id = r.id"""
    conds, params = [], []
    if "--search" in args:
        conds.append("text LIKE ?")
        params.append(f"%{_opt(args, '--search')}%")
    if "--type" in args:
        conds.append("record_type = ?")
        params.append(_opt(args, "--type"))
    if conds:
        q = f"SELECT * FROM ({q}) AS t WHERE " + " AND ".join(conds)
    q += " LIMIT ?"
    params.append(int(_opt(args, "--limit") or 30))
    for fid, rtype, edid, stype, text, src in cur.execute(q, params):
        print(f"{fid:08X} {rtype:6s} {edid or '':40s} {stype:5s} [{src:8s}] {text[:120]}")


def _find_record(conn: sqlite3.Connection, target: str, plugin: str | None) -> int | None:
    cur = conn.cursor()
    if target.lower().startswith("0x"):
        fid = int(target, 16)
        if plugin:
            row = cur.execute(
                "SELECT r.id FROM records r JOIN plugins p ON r.plugin_id = p.id "
                "WHERE p.name = ? AND r.form_id = ?", (plugin, fid)
            ).fetchone()
            return row[0] if row else None
        rows = cur.execute("SELECT id FROM records WHERE form_id = ?", (fid,)).fetchall()
        if len(rows) > 1:
            print(f"  {len(rows)} records share formID {fid:08X}; pass --plugin")
        return rows[0][0] if rows else None
    if plugin:
        row = cur.execute(
            "SELECT r.id FROM records r JOIN plugins p ON r.plugin_id = p.id "
            "WHERE p.name = ? AND r.edid = ?", (plugin, target)
        ).fetchone()
        return row[0] if row else None
    rows = cur.execute("SELECT id FROM records WHERE edid = ?", (target,)).fetchall()
    if len(rows) > 1:
        names = [
            n[0] for n in cur.execute(
                "SELECT p.name FROM records r JOIN plugins p ON r.plugin_id = p.id "
                "WHERE r.edid = ?", (target,)
            )
        ]
        print(f"  {len(rows)} records named {target}: {names} — pass --plugin")
    return rows[0][0] if rows else None


def cmd_refs(args: list[str]) -> None:
    conn = _connect()
    cur = conn.cursor()
    if not args:
        print("usage: fo4-db refs <EDID|0xLOCAL> [--plugin NAME]")
        sys.exit(1)
    target = args[0]
    rid = _find_record(conn, target, _opt(args, "--plugin"))
    if rid is None:
        print(f"record {target} not found")
        return
    conf = "" if "--all" in args else " AND x.confidence = 'high'"
    rows = cur.execute(
        """SELECT rr.form_id, rr.record_type, rr.edid, pp.name, x.sub_type, x.offset
           FROM refs x
           JOIN records rr ON x.record_id = rr.id
           JOIN plugins pp ON rr.plugin_id = pp.id
           WHERE x.resolved_record_id = ?""" + conf + """
           ORDER BY rr.form_id LIMIT 100""",
        (rid,),
    ).fetchall()
    print(f"references to {target} (record id {rid}): {len(rows)}"
          + ("" if "--all" in args else "  [high-confidence; --all includes numeric-field noise]"))
    for fid, rtype, edid, pname, stype, off in rows:
        print(f"  {fid:08X} {rtype:6s} {edid or '':40s} {pname}  [{stype} @{off}]")


def cmd_out(args: list[str]) -> None:
    conn = _connect()
    cur = conn.cursor()
    if not args:
        print("usage: fo4-db out <EDID|0xLOCAL> [--plugin NAME]")
        sys.exit(1)
    target = args[0]
    rid = _find_record(conn, target, _opt(args, "--plugin"))
    if rid is None:
        print(f"record {target} not found")
        return
    conf = "" if "--all" in args else " AND x.confidence = 'high'"
    rows = cur.execute(
        """SELECT x.sub_type, x.offset, x.target_local, tp.name, tr.record_type, tr.edid, x.open_plugin
           FROM refs x
           LEFT JOIN plugins tp ON x.target_plugin_id = tp.id
           LEFT JOIN records tr ON x.resolved_record_id = tr.id
           WHERE x.record_id = ?""" + conf + """
           ORDER BY x.offset LIMIT 200""",
        (rid,),
    ).fetchall()
    print(f"{target} points at {len(rows)}"
          + ("" if "--all" in args else "  [high-confidence; --all includes numeric-field noise]"))
    for stype, off, local, tname, rtype, edid, openp in rows:
        who = tname or openp or "?"
        what = f"{rtype}:{edid}" if rtype else ("OPEN" if openp else "absent")
        print(f"  [{stype} @{off:>3}] {who}:{local:06X}  {what}")


def cmd_open_refs(args: list[str]) -> None:
    conn = _connect()
    rows = conn.execute(
        """SELECT open_plugin, COUNT(*), COUNT(DISTINCT record_id)
           FROM refs WHERE open_plugin IS NOT NULL
           GROUP BY open_plugin ORDER BY 2 DESC"""
    ).fetchall()
    if not rows:
        print("open references: none — every reference resolves inside the library")
        return
    print(f"open references to {len(rows)} plugin(s) not in the library:")
    for name, n, recs in rows:
        print(f"  {name:40s} {n:6d} refs from {recs} records")


def cmd_assets(args: list[str]) -> None:
    """Index BA2 archives (metadata only — never copies file data)."""
    conn = _connect()
    data_dir = args[0] if args and not args[0].startswith("--") else os.path.expanduser(
        "~/snap/steam/common/.local/share/Steam/steamapps/common/Fallout 4/Data_Core"
    )
    owner = _opt(args, "--owner") or "game"
    result = db.index_assets(conn, data_dir, owner=owner)
    print(f"\nindexed {result.get('_total', 0)} assets from {len(result) - 1} archives (owner={owner})")


def cmd_assets_mod(args: list[str]) -> None:
    from . import assets

    if len(args) < 2:
        print("usage: fo4-db assets-mod <owner> <mod_dir>")
        sys.exit(1)
    conn = _connect()
    conn.execute("DELETE FROM assets WHERE owner = ?", (args[0],))
    conn.commit()
    result = assets.index_source(conn, args[0], args[1])
    total = sum(v for k, v in result.items() if not k.startswith("_"))
    print(
        f"\n{args[0]}: {total} archive entries, {result['_loose']} loose files, "
        f"{result['_shadowed_by_loose']} shadowing own archives"
    )


def cmd_profile(args: list[str]) -> None:
    from . import schema_v2

    if len(args) < 2:
        print("usage: fo4-db profile <name> <plugins.txt>")
        sys.exit(1)
    conn = _connect()
    schema_v2.ensure(conn)
    res = schema_v2.set_profile_plugins(conn, args[0], args[1])
    schema_v2.link_in_scope(conn)
    inlib = conn.execute(
        "SELECT COUNT(*) FROM profile_plugins WHERE profile = ? AND in_library = 1", (args[0],)
    ).fetchone()[0]
    print(
        f"{res['profile']}: {res['plugins']} plugins declared, {res['active']} active, "
        f"{inlib} in library"
    )


def cmd_fields(args: list[str]) -> None:
    """Decode record types into the fields table (schema layer)."""
    conn = _connect()
    from . import schema

    n = schema.build_fields(conn)
    print(f"decoded {n} fields")


def cmd_field(args: list[str]) -> None:
    """Show decoded fields for one record by EDID or formID."""
    conn = _connect()
    if not args:
        print("usage: fo4-db field <EDID|0xLOCAL> [--plugin NAME]")
        return
    rid = _find_record(conn, args[0], _opt(args, "--plugin"))
    if rid is None:
        print(f"record {args[0]} not found")
        return
    meta = conn.execute(
        "SELECT r.form_id, r.record_type, r.edid, COALESCE(p.name,'') FROM records r "
        "JOIN plugins p ON r.plugin_id = p.id WHERE r.id = ?", (rid,)
    ).fetchone()
    print(f"{meta[0]:08X} {meta[1]} {meta[2] or ''} [{meta[3]}]")
    for field, kind, value in conn.execute(
        "SELECT field, kind, value FROM fields WHERE record_id = ? ORDER BY seq", (rid,)
    ):
        print(f"  {field:26s} {kind:6s} {value}")


def cmd_resolve(args: list[str]) -> None:
    """Resolve localized lstring indices using .STRINGS tables."""
    conn = _connect()
    strings_dir = args[0] if args else "/tmp/fo4-strings/STRINGS"
    n = db.resolve_localized_strings(conn, strings_dir)
    print(f"resolved {n} strings")


def cmd_freshness(args: list[str]) -> None:
    conn = _connect()
    cur = conn.cursor()
    run = cur.execute(
        "SELECT id, started_at, game_version, plugin_count, record_count "
        "FROM ingest_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if run:
        print(f"last ingest: run {run[0]} @ {run[1]}  version={run[2]} "
              f"plugins={run[3]} records={run[4]}")
    same = changed = missing = 0
    for name, path, md5, size in cur.execute("SELECT name, path, md5, size FROM plugins ORDER BY name"):
        if not os.path.exists(path):
            missing += 1
            print(f"  MISSING  {name}  ({path})")
        elif os.path.getsize(path) != size or db.md5_file(path) != md5:
            changed += 1
            print(f"  CHANGED  {name}  ({path})")
        else:
            same += 1
    print(f"fresh: {same} unchanged, {changed} changed, {missing} missing")


def cmd_stats(args: list[str]) -> None:
    conn = _connect()
    cur = conn.cursor()
    for label, q in [
        ("plugins", "SELECT COUNT(*) FROM plugins"),
        ("records", "SELECT COUNT(*) FROM records"),
        ("subrecords", "SELECT COUNT(*) FROM subrecords"),
        ("strings", "SELECT COUNT(*) FROM strings"),
        ("refs", "SELECT COUNT(*) FROM refs"),
        ("refs_open", "SELECT COUNT(*) FROM refs WHERE open_plugin IS NOT NULL"),
        ("assets", "SELECT COUNT(*) FROM assets"),
        ("fields", "SELECT COUNT(*) FROM fields"),
    ]:
        try:
            print(f"{label:12s} {cur.execute(q).fetchone()[0]}")
        except sqlite3.OperationalError as e:
            print(f"{label:12s} ({e})")
    print("\nassets by owner:")
    try:
        for owner, kind, n in cur.execute(
            "SELECT owner, source_kind, COUNT(*) FROM assets "
            "GROUP BY owner, source_kind ORDER BY 3 DESC"
        ):
            print(f"  {owner:28s} {kind:8s} {n}")
    except sqlite3.OperationalError:
        print("  (assets table has no owner column yet)")
    print("\nrecord types:")
    for rtype, n in cur.execute(
        "SELECT record_type, COUNT(*) FROM records GROUP BY record_type "
        "ORDER BY COUNT(*) DESC LIMIT 12"
    ):
        print(f"  {rtype:6s} {n}")


VERBS = {
    "ingest": cmd_ingest,
    "migrate": cmd_migrate,
    "records": cmd_records,
    "text": cmd_text,
    "refs": cmd_refs,
    "out": cmd_out,
    "open-refs": cmd_open_refs,
    "resolve": cmd_resolve,
    "assets": cmd_assets,
    "assets-mod": cmd_assets_mod,
    "profile": cmd_profile,
    "fields": cmd_fields,
    "field": cmd_field,
    "freshness": cmd_freshness,
    "stats": cmd_stats,
}


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    VERBS.get(sys.argv[1], lambda a: print(__doc__))(sys.argv[2:])


if __name__ == "__main__":
    main()
