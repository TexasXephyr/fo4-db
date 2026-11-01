"""fo4db.masters — a plugin's master list and the file-local formID map.

A plugin file addresses records with a 32-bit formID whose top byte is an index
into that plugin's OWN master list:

    idx <  len(masters)  -> override of masters[idx]
    idx == len(masters)  -> a record this plugin creates
    idx >  len(masters)  -> invalid (non-light plugin)

The engine renumbers these into runtime load-order prefixes — `CannabisWorld.esp`
is 0x0B in one profile and 0x61 in another — so the file-local form is the only
key that survives a profile change. Every reference in the database is resolved
through this map, never by matching a bare integer against a global ID set.
"""
from __future__ import annotations

from . import parser

LIGHT_FLAG = 0x00000200  # TES4 header flag: ESL (light) plugin


def read_tes4(path: str) -> tuple[int, list[str]]:
    """Return (TES4 flags, ordered MAST names) for a plugin file."""
    for rec in parser.walk(path):
        if rec.is_grup:
            continue
        if rec.rtype != "TES4":
            return 0, []
        mast = [
            sub.data.rstrip(b"\x00").decode("latin1")
            for sub in parser.subrecords(rec.data)
            if sub.stype == "MAST"
        ]
        return rec.flags, mast
    return 0, []


def own_slot(masters: list[str]) -> int:
    """Top-byte index a plugin uses for records it creates itself."""
    return len(masters)


def split(form_id: int, masters: list[str], self_name: str) -> tuple[str | None, int]:
    """Map a raw formID to (target plugin name or None, local id)."""
    idx = (form_id >> 24) & 0xFF
    local = form_id & 0x00FFFFFF
    if idx < len(masters):
        return masters[idx], local
    if idx == len(masters):
        return self_name, local
    return None, local
