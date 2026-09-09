# FO4 Data Wiki — Plan

**Goal:** A full technical wiki of Fallout 4, built from the local install.
Know everything about the game; modify anything not nailed down.

**Source of truth:** the local game install (`Data_Core/`). The DB is a
derived artifact — it must prove it matches the live game.

## Architecture

```
Data_Core/*.esm + active plugins
        │  (ingest once — the ONLY place parsing happens)
        ▼
fo4-db ingestion (harvested espdump walker → library)
        │
        ▼
SQLite: fo4.db  (raw materialized layer: records, subrecords, strings, refs)
        │
        ▼
Query CLI + wiki generation (thin tools, all read the DB)
```

## Layers

1. **Raw materialized layer** (this build): faithful extraction of every
   record, subrecord, string, and formID reference from every plugin.
   Schema is deliberately generic — record-type-specific decoding evolves
   on top of it.
2. **Schema layer** (next): per-record-type field decoding (ALCH value,
   MGEF flags, LVLI entries...) — driven by actual queries.
3. **Asset index** (next): BA2 contents (path, hash, size, archive) —
   index, never copy.
4. **Wiki generation** (next): markdown pages generated from the DB.

## Refresh mechanism

Every ingestion records a fingerprint: game version (Fallout4.exe strings),
plugins.txt hash, per-plugin md5. Freshness check = compare live vs
recorded. Bethesda ships updates → fingerprint changes → re-ingest.

## Schema (v1)

- `ingest_runs` — fingerprint + timestamps
- `plugins` — name, path, md5, size, load_order
- `records` — plugin, form_id, record_type, edid, flags, data_size
- `subrecords` — record, sub_type, raw blob, seq
- `strings` — record, sub_type, decoded text (printable heuristic)
- `references` — record, sub_type, offset, target formID (resolved vs known set)

## Phases

- [x] P0: inventory existing tools (espdump.py, fo4-plugin-toolkit, BSArch)
- [x] P1: fo4-db package + ingestion of vanilla esms (7 files) → fo4.db
- [x] P1.5: localized lstring resolution (204K strings)
- [x] P1.6: BA2 asset index (507K assets, metadata only)
- [x] P2: schema layer — typed field decoding (ALCH/MGEF/LVLI/COBJ/MISC/KYWD)
- [ ] P3: DLC + CC + active mods ingestion
- [ ] P4: wiki generation (markdown pages from the DB)
