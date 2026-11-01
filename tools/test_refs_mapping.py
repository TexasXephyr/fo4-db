#!/usr/bin/env python3
"""End-to-end test for fo4db.refs on synthetic plugin files.

Builds two tiny real plugin binaries (correct 24-byte record headers, GRUP
wrapping, MAST-tagged TES4) and runs the real ingest + rebuild path against
them. It exists because the first version of the v2 rebuild re-mapped the
existing `refs` rows instead of scanning the plugins — every count looked
identical twice in a row while the mods contributed zero references. A test
that cannot fail is not a test; this one can.

Run: .venv/bin/python tools/test_refs_mapping.py
Exit 0 = pass.
"""
from __future__ import annotations

import os
import sqlite3
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fo4db import db, refs, schema_v2  # noqa: E402

HDR = 24


def sub(stype: str, data: bytes) -> bytes:
    return stype.encode("ascii") + struct.pack("<H", len(data)) + data


def rec(rtype: str, form_id: int, body: bytes, flags: int = 0) -> bytes:
    # type, dataSize, flags, formID, then 8 bytes of timestamp/version control
    return (
        rtype.encode("ascii")
        + struct.pack("<III", len(body), flags, form_id)
        + b"\x00" * 8
        + body
    )


def grup(children: bytes) -> bytes:
    return b"GRUP" + struct.pack("<I", HDR + len(children)) + b"XXXX" + b"\x00" * 12 + children


def tes4(edid: str, masters: list[str], light: bool = False) -> bytes:
    body = sub("EDID", edid.encode() + b"\x00")
    for m in masters:
        body += sub("MAST", m.encode() + b"\x00")
    return rec("TES4", 0, body, flags=0x00000200 if light else 0)


def alch(edid: str, form_id: int, words: list[int]) -> bytes:
    body = sub("EDID", edid.encode() + b"\x00")
    if words:
        body += sub("DATA", b"".join(struct.pack("<I", w) for w in words))
    return rec("ALCH", form_id, body)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="fo4refs-")
    target = os.path.join(tmp, "TARGET.esm")
    user = os.path.join(tmp, "USER.esp")
    missing = os.path.join(tmp, "MISSING.esm")

    # TARGET.esm: no masters, own slot 0
    with open(target, "wb") as f:
        f.write(grup(tes4("TARGET", []) + alch("TargetThing", 0x00000800, [])))

    # USER.esp: masters [TARGET.esm, MISSING.esm], own slot 2
    words = [
        0x00000800,  # idx 0 -> TARGET.esm:0x800   -> resolved
        0x01001234,  # idx 1 -> MISSING.esm:0x1234 -> OPEN
        0x02000800,  # idx 2 -> self               -> resolved
        0x00000999,  # idx 0 -> TARGET.esm:0x999   -> absent, dropped
        0x07000001,  # idx 7 -> beyond own slot    -> unmappable, dropped
    ]
    with open(user, "wb") as f:
        f.write(
            grup(
                tes4("USER", ["TARGET.esm", "MISSING.esm"])
                + alch("UserThing", 0x02000800, words)
            )
        )

    dbfile = os.path.join(tmp, "fixture.db")
    conn = db.connect(dbfile)
    schema_v2.ensure(conn)
    for path in (target, user):
        db.ingest_plugin(conn, path)

    stats = refs.rebuild(conn, log=lambda *_: None)
    print("stats:", {k: stats[k] for k in ("resolved", "open", "dropped_absent_id", "dropped_unmappable", "refs_after")})

    rows = conn.execute(
        "SELECT raw_word, target_plugin_id, target_local, resolved_record_id, open_plugin "
        "FROM refs ORDER BY offset"
    ).fetchall()
    for r in rows:
        print("  ", [f"0x{r[0]:08X}", r[1], hex(r[2]), r[3], r[4]])

    # MISSING.esm is a master that was never created: its reference must be
    # kept and named, not silently dropped and not invented as a record.
    by_word = {r[0]: r for r in rows}
    checks = {
        "0x00000800 -> TARGET record": by_word.get(0x00000800, (0,) * 5)[3] is not None,
        "0x01001234 -> OPEN MISSING.esm":
            by_word.get(0x01001234, (None,) * 5)[4] == "MISSING.esm"
            and by_word.get(0x01001234)[3] is None
            and by_word.get(0x01001234)[1] is None,
        "0x02000800 -> self record": by_word.get(0x02000800, (0,) * 5)[3] is not None,
        "0x00000999 dropped (no such local id)": 0x00000999 not in by_word,
        "0x07000001 dropped (beyond own slot)": 0x07000001 not in by_word,
        "exactly 3 reference rows": stats["refs_after"] == 3,
        "no file for MISSING.esm": not os.path.exists(missing),
    }
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    ok = all(checks.values())
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
