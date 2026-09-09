"""fo4db.cli — query verbs for the FO4 data wiki.

Verbs:
  ingest <data_dir> [--plugins plugins.txt]   build/refresh the DB
  records [--type T] [--edid X] [--limit N]   list records
  text [--search S] [--type T] [--limit N]    search strings
  refs <formID>                               what points at a formID
  freshness                                   does the DB match the live game?
  stats                                       DB summary
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys

from . import db

DEFAULT_DB = os.path.expanduser("~/dev/fo4-db/fo4.db")


def _connect() -> sqlite3.Connection:
    if not os.path.exists(DEFAULT_DB):
        print(f"DB not found: {DEFAULT_DB}\nRun: fo4-db ingest <game Data dir>")
        sys.exit(1)
    return db.connect(DEFAULT_DB)


def _game_version(game_root: str) -> str:
    exe = os.path.join(game_root, "Fallout4.exe")
    if os.path.exists(exe):
        with open(exe, "rb") as f:
            data = f.read(1 << 20)
        for s in data.split(b"\x00"):
            if s.startswith(b"1.") and b"." in s[2:]:
                return s.decode("latin1")
    return "unknown"


def _plugins_md5(plugins_txt: str) -> str:
    if not os.path.exists(plugins_txt):
        return "none"
    h = hashlib.md5()
    with open(plugins_txt, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def cmd_ingest(args: list[str]) -> None:
    if not args:
        print("usage: fo4-db ingest <game Data dir> [--plugins <plugins.txt>]")
        sys.exit(1)
    data_dir = args[0]
    plugins_txt = None
    if "--plugins" in args:
        plugins_txt = args[args.index("--plugins") + 1]

    game_root = os.path.dirname(data_dir)
    version = _game_version(game_root)
    pmd5 = _plugins_md5(plugins_txt or "")

    conn = db.connect(DEFAULT_DB)
    run_id = db.start_run(conn, version, pmd5)

    # load order from plugins.txt (starred = active), then remaining files
    order: list[str] = []
    if plugins_txt and os.path.exists(plugins_txt):
        with open(plugins_txt) as f:
            for line in f:
                line = line.strip().lstrip("*")
                if line and not line.startswith("#"):
                    order.append(line)

    files = sorted(
        f
        for f in os.listdir(data_dir)
        if f.lower().endswith((".esm", ".esp", ".esl"))
    )
    # masters first, then load order, then the rest
    files.sort(key=lambda f: (f.lower() not in ("fallout4.esm",), order.index(f) if f in order else 999))

    total_rec = 0
    total_sub = 0
    for i, fname in enumerate(files):
        path = os.path.join(data_dir, fname)
        if not os.path.isfile(path):
            continue
        lo = order.index(fname) if fname in order else None
        print(f"  ingesting {fname} ...", flush=True)
        res = db.ingest_plugin(conn, path, load_order=lo, ingest_run=run_id)
        total_rec += res["records"]
        total_sub += res["subrecords"]

    print("  building reference map ...", flush=True)
    refs = db.build_references(conn)
    db.finish_run(conn, run_id, total_rec, total_sub)
    print(f"done: {len(files)} plugins, {total_rec} records, {total_sub} subrecords, {refs} references")


def cmd_records(args: list[str]) -> None:
    conn = _connect()
    cur = conn.cursor()
    q = "SELECT r.form_id, r.record_type, r.edid, p.name FROM records r JOIN plugins p ON r.plugin_id = p.id"
    conds, params = [], []
    if "--type" in args:
        conds.append("r.record_type = ?")
        params.append(args[args.index("--type") + 1])
    if "--edid" in args:
        conds.append("r.edid LIKE ?")
        params.append(f"%{args[args.index('--edid') + 1]}%")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY r.form_id LIMIT ?"
    params.append(int(args[args.index("--limit") + 1]) if "--limit" in args else 50)
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
        params.append(f"%{args[args.index('--search') + 1]}%")
    if "--type" in args:
        conds.append("record_type = ?")
        params.append(args[args.index("--type") + 1])
    if conds:
        q = f"SELECT * FROM ({q}) AS t WHERE " + " AND ".join(conds)
    q += " LIMIT ?"
    params.append(int(args[args.index("--limit") + 1]) if "--limit" in args else 30)
    for fid, rtype, edid, stype, text, src in cur.execute(q, params):
        print(f"{fid:08X} {rtype:6s} {edid or '':40s} {stype:5s} [{src:8s}] {text[:120]}")


def cmd_refs(args: list[str]) -> None:
    conn = _connect()
    cur = conn.cursor()
    if not args:
        print("usage: fo4-db refs <formID hex>")
        sys.exit(1)
    target = int(args[0], 16)
    rows = cur.execute(
        """SELECT r.form_id, r.record_type, r.edid, p.name, x.sub_type, x.offset
           FROM refs x
           JOIN records r ON x.record_id = r.id
           JOIN plugins p ON r.plugin_id = p.id
           WHERE x.target_form_id = ? ORDER BY r.form_id LIMIT 100""",
        (target,),
    ).fetchall()
    print(f"references to {target:08X}: {len(rows)}")
    for fid, rtype, edid, pname, stype, off in rows:
        print(f"  {fid:08X} {rtype:6s} {edid or '':40s} {pname}  [{stype} @{off}]")


def cmd_assets(args: list[str]) -> None:
    """Index BA2 archives (metadata only — never copies file data)."""
    conn = _connect()
    data_dir = args[0] if args else os.path.expanduser(
        "~/snap/steam/common/.local/share/Steam/steamapps/common/Fallout 4/Data_Core"
    )
    result = db.index_assets(conn, data_dir)
    print(f"\nindexed {result.get('_total', 0)} assets across {len(result) - 1} archives")


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
        "SELECT id, started_at, game_version, plugins_md5, plugin_count, record_count FROM ingest_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not run:
        print("no ingest run recorded")
        return
    run_id, started, version, pmd5, pcount, rcount = run
    print(f"last ingest: run {run_id} @ {started}")
    print(f"  game version recorded: {version}")
    print(f"  plugins: {pcount}, records: {rcount}")
    # compare against live
    data_dir = os.path.expanduser("~/snap/steam/common/.local/share/Steam/steamapps/common/Fallout 4/Data_Core")
    if os.path.isdir(data_dir):
        live = sorted(f for f in os.listdir(data_dir) if f.lower().endswith((".esm", ".esp", ".esl")))
        recorded = [r[0] for r in cur.execute("SELECT name FROM plugins")]
        missing = [f for f in live if f not in recorded]
        extra = [f for f in recorded if f not in live]
        print(f"  live plugins: {len(live)}, recorded: {len(recorded)}")
        if missing:
            print(f"  MISSING from DB (game updated?): {missing}")
        if extra:
            print(f"  in DB but not live: {extra}")
        if not missing and not extra:
            print("  plugin set matches live install")
    else:
        print(f"  (live Data dir not found at {data_dir})")


def cmd_stats(args: list[str]) -> None:
    conn = _connect()
    cur = conn.cursor()
    for label, q in [
        ("plugins", "SELECT COUNT(*) FROM plugins"),
        ("records", "SELECT COUNT(*) FROM records"),
        ("subrecords", "SELECT COUNT(*) FROM subrecords"),
        ("strings", "SELECT COUNT(*) FROM strings"),
        ("refs", "SELECT COUNT(*) FROM refs"),
    ]:
        print(f"{label:12s} {cur.execute(q).fetchone()[0]}")
    print("\nrecord types:")
    for rtype, n in cur.execute(
        "SELECT record_type, COUNT(*) FROM records GROUP BY record_type ORDER BY COUNT(*) DESC LIMIT 20"
    ):
        print(f"  {rtype:6s} {n}")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    args = sys.argv[2:]
    {"ingest": cmd_ingest, "records": cmd_records, "text": cmd_text,
     "refs": cmd_refs, "resolve": cmd_resolve, "assets": cmd_assets,
     "freshness": cmd_freshness, "stats": cmd_stats}.get(cmd, lambda a: print(__doc__))(args)


if __name__ == "__main__":
    main()
