#!/usr/bin/env python3
"""Read-only probe: can fo4-db rows be resolved to real files on disk?

Answers three questions with numbers, not opinion:
  1. Do record-level mesh references (MODL) land on a BA2 entry in `assets`?
  2. Do record-level material references (SNAM/MNAM *.bgsm) land on one?
  3. Does the resolved archive path name a file that exists under the
     unpacked library root — after case folding, which is how FO4 resolves?

No writes to fo4.db. Prints a summary and a few sample chains.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

DB = "/home/xephyr/dev/fo4-db/fo4.db"
ROOT = sys.argv[1] if len(sys.argv) > 1 else \
    "/media/xephyr/Local Data/fallout4lib/gamedata/Data"


def bs(s: bytes) -> str:
    """Subrecord payload -> path string (strip NUL terminator)."""
    return s.rstrip(b"\x00").decode("latin1").replace("/", "\\")


def main() -> int:
    t0 = time.time()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    assets = {}
    for path, archive, size, ext in con.execute(
            "SELECT path, archive, size, ext FROM assets"):
        assets[path.lower()] = (path, archive, size, ext)
    print(f"assets loaded              : {len(assets):,} "
          f"({time.time() - t0:.1f}s)")

    # 1. meshes ------------------------------------------------------------
    rows = [bs(r) for (r,) in con.execute(
        "SELECT raw FROM subrecords WHERE sub_type='MODL' AND length(raw)>8")]
    mesh_hit = mesh_miss = 0
    miss_examples = []
    for p in rows:
        if ("meshes\\" + p).lower() in assets:
            mesh_hit += 1
        else:
            mesh_miss += 1
            if len(miss_examples) < 5:
                miss_examples.append(p)
    print(f"\nMODL mesh path strings     : {len(rows):,}")
    print(f"  resolved to a BA2 entry  : {mesh_hit:,} "
          f"({100*mesh_hit/len(rows):.1f}%)")
    print(f"  unresolved               : {mesh_miss:,}")
    for p in miss_examples:
        print(f"     ! {p}")

    # 2. materials ---------------------------------------------------------
    mats = [t for (t,) in con.execute(
        "SELECT text FROM strings WHERE lower(text) LIKE '%.bgsm' "
        "OR lower(text) LIKE '%.bgem'")]
    mat_hit = mat_miss = 0
    for p in mats:
        full = p.replace("/", "\\")
        hit = ("materials\\" + full).lower() in assets
        if hit:
            mat_hit += 1
        else:
            mat_miss += 1
    print(f"\nSNAM/MNAM material strings : {len(mats):,}")
    print(f"  resolved to a BA2 entry  : {mat_hit:,} "
          f"({100*mat_hit/len(mats):.1f}%)")
    print(f"  unresolved               : {mat_miss:,}")

    # 3. on-disk -----------------------------------------------------------
    t1 = time.time()
    disk = set()
    for dp, _dn, fn in os.walk(ROOT):
        rel = os.path.relpath(dp, ROOT)
        for f in fn:
            disk.add((f if rel == "." else os.path.join(rel, f)).replace(os.sep, "/").lower())
    print(f"\ndisk index                 : {len(disk):,} files "
          f"({time.time() - t1:.1f}s under {ROOT})")

    on_disk = sum(1 for p in rows if ("meshes\\" + p).lower() in assets
                  and assets[("meshes\\" + p).lower()][0].replace("\\", "/").lower() in disk)
    print(f"  mesh refs that name a real file : {on_disk:,} "
          f"({100*on_disk/len(rows):.1f}% of all MODL refs)")

    # 4. sample chains -----------------------------------------------------
    print("\nsample chains (record -> mesh -> file):")
    q = con.execute("""
        SELECT pl.name, r.record_type, r.edid, s.raw
        FROM subrecords s
        JOIN records r ON r.id = s.record_id
        JOIN plugins pl ON pl.id = r.plugin_id
        WHERE s.sub_type='MODL' AND length(s.raw)>8 AND r.edid IS NOT NULL
        LIMIT 4000
    """)
    shown = 0
    for plugin, rtype, edid, raw in q:
        p = bs(raw)
        hit = assets.get(("meshes\\" + p).lower())
        if not hit:
            continue
        rel = hit[0].replace("\\", "/")
        if rel.lower() not in disk:
            continue
        print(f"  {plugin:22} {rtype:5} {edid[:34]:34} -> "
              f"{os.path.join(ROOT, rel)}")
        shown += 1
        if shown >= 6:
            break
    print(f"\ntotal elapsed {(time.time() - t0):.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
