# Are `fo4.db` and the unpacked gamedata tree connected?

**Short answer: yes, and the question is well-formed — but the useful link is not
what it first looks like.** The database knows *what files the game contains*;
the library is *those files on disk*. What is missing is a pointer, and the
right pointer is one row of configuration, not a column of paths.

Measured 2026-10-31 against `fo4.db` (2.8 GB, 16 vanilla plugins) and
`gamedata/Data` (225,588 files, 31 GB, now physically on
`/media/xephyr/Local Data/fallout4lib/gamedata/Data`).

## The connection as it stands

| coupling | where |
|---|---|
| The DB is the **oracle** for the unpacked tree: `assets` records every archive entry with its internal path and unpacked size, and `tools/verify_gamedata.py` (in fo4-modkit) diffs the tree against it | `assets` — 507,570 rows over 84 archives |
| Both are derived from the same source of truth, `Data_Core/*.esm` + `*.ba2` — the DB parses the plugins, the extractor unpacks the archives | `docs/PLAN.md` |
| The archive ledger (`gamedata/extracted.tsv`, 19 archives) says *which archive was unpacked*; `assets` says *which path came from which archive at what size*. The verifier joins them at run time | `assets.archive`, `assets.path`, `assets.size` |

That is a real, working coupling — it is the reason the move this morning could
be proven correct rather than assumed. But it is **ephemeral**: nothing
persists a link from a row to a file. The join is re-derived every run.

## Where a persistent link would help — three places, not one

### 1. `assets` — one row per file that exists on disk

The only table whose rows map 1:1 onto files. Measured: 507,570 entries, of
which **225,588** are present in the unpacked tree (the extracted subset; the
rest are unextracted voices/animations/DLC).

`assets.path` is *already* the library-relative path, e.g.
`Meshes\Actors\Character\CharacterAssets\Hair\Female\FemaleHair35.nif`. So the
"link" is half-built. The missing half is two things:

- a **type-root prefix rule** — plugin subrecords store `Actors\Character\...`,
  archives store `Meshes\Actors\Character\...`. Prefix by extension:
  `.nif → Meshes\`, `.bgsm/.bgem → Materials\`, `.dds → Textures\`;
- a **root**: where those relative paths start today.

### 2. `subrecords` where `sub_type='MODL'` — mesh references from records

48,262 rows hold a **literal mesh path** (not a formID; the 4-byte MODL rows in
ARMA/ARMO are formIDs and must be excluded by `length(raw) > 8`).

Measured resolution of those 44,795 text paths (case-folded, `.egt` facegen
files excluded):

| | count | share |
|---|---|---|
| resolve to a `assets` row | 44,179 | **98.6 %** |
| name a file actually present in the unpacked tree | 36,703 | **81.9 %** |

The 18 % gap is not a defect: those meshes live in `DLCCoast/DLCRobot/
DLCNukaWorld - Main.ba2`, which were never extracted (MANIFEST "Deliberate
omissions"). The DB knows them; the disk does not have them yet.

### 3. `strings` where the text ends `.bgsm`/`.bgem` — material references

9,585 rows (9,395 from `SNAM`, 190 from `MNAM`). **9,404 (98.1 %)** resolve to a
`Materials\…` archive entry. `MODS` subrecords are **not** paths — they are
4-byte formIDs into the 4,028 `MSWP` material-swap records, and those are what
point at a `.bgsm`.

`refs` (31.8 M rows) carries no files: formID edges only. `TXST` (511 records)
names texture *sets*, not files.

## The design point that the move this morning proved

Storing an absolute path per row would have been the obvious implementation and
it would have been wrong: moving 31 GB to another drive would have meant
rewriting **507,570 rows**. The relative path is already there; only the root
moved. So:

```sql
-- one row. moving the library again is ONE update, not half a million.
CREATE TABLE IF NOT EXISTS lib_root (
    name  TEXT PRIMARY KEY,
    root  TEXT NOT NULL,          -- '/media/xephyr/Local Data/fallout4lib/gamedata/Data'
    note  TEXT
);

CREATE VIEW IF NOT EXISTS asset_file AS
SELECT a.archive, a.path, a.ext, a.size,
       r.root || '/' || replace(a.path, '\', '/') AS local_path
FROM assets a, lib_root r;

-- record -> mesh -> file.  98.6% of textual MODL refs join today.
CREATE VIEW IF NOT EXISTS record_mesh AS
SELECT pl.name AS plugin, rec.record_type, rec.edid,
       substr(s.raw,1,length(s.raw)-1) AS mesh_rel,     -- strips the NUL
       a.archive, a.path AS archive_path
FROM subrecords s
JOIN records rec ON rec.id = s.record_id
JOIN plugins pl  ON pl.id  = rec.plugin_id
LEFT JOIN assets a
       ON lower(a.path) = lower('Meshes\' || substr(s.raw,1,length(s.raw)-1))
WHERE s.sub_type = 'MODL' AND length(s.raw) > 8;
```

Two implementation facts that cost time to discover, recorded so they are not
rediscovered:

1. **`trim(raw, char(0))` returns a BLOB in SQLite, not TEXT.** Comparing it to
   `assets.path` yields zero matches for all 44,795 rows, silently. Use
   `substr(raw, 1, length(raw)-1)`.
2. **The join must be case-folded.** Records store some paths lowercase
   (`actors\character\...\femaleeyes.nif`) where the archive stores
   `Actors\Character\...`. Exact-case matching returns 43,015 of 44,874 (95.9 %);
   folding returns 98.6 %. Folding needs an expression index to stay fast:
   `CREATE INDEX idx_assets_path_lc ON assets(lower(path));` (~507k rows).
3. `fo4.db` is a **derived artifact**: add views and indexes, never edit rows,
   and expect them to vanish on the next full re-ingest.

## What the DB cannot see yet

`plugins` holds 16 rows — the 7 vanilla esms, 6 DLC esms, and 9 CC esls.
**No mod plugin is ingested**, so `records` cannot answer a single question
about `CannabisWorld.esp`'s own meshes, materials or texture sets. That is P3 in
`docs/PLAN.md`, and it is the step that makes the link layer worth building:
P3 + this view turns "where is the mesh for this record" into one query for the
mod we actually work on, instead of `grep` through NIF filenames.

## Verdict

- The two **are** connected, by an on-demand join in the verifier.
- A persistent link **is** worth having, at `assets` (file↔row), `MODL`
  (record→mesh) and `SNAM` (record→material).
- Implement it as **one root row + views**, not as stored absolute paths.
- Do it **with** P3 (mod ingestion), not before: the value is concentrated in
  the mod plugins the DB does not yet contain.

The probe behind every number above is `tools/asset_link_probe.py`:

```
python3 tools/asset_link_probe.py "/media/xephyr/Local Data/fallout4lib/gamedata/Data"
```
