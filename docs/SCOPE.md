# P3 scope — what the reference library is allowed to contain

**Rule (Xephyr, 2026-10-31).** Nothing enters `fo4-db` or the unpacked asset
tree unless it touches the work: the mod being authored, a mod it depends on, a
mod whose assets/records have been mastered into it, or a master an in-scope
plugin needs in order to resolve. DLCs and Creation Club are present only because
they are already the vanilla foundation; their *asset archives* stay
unextracted. Plugins that are referenced but not in scope are recorded as **open
references**, not ingested.

`fo4-db ingest <dir>` takes every `.esp/.esm/.esl` in a directory, so scope must
be an explicit plugin list with md5s. Read the shared mod store
`~/.config/AmethystModManager/Profiles/Fallout 4/mods/<mod>/`, **not** game
`Data/` — `deploy_state.json` has `last_deployed = CW Tester` while
`last_active_profile = default`, so `Data/` is a symlink farm for a different
profile.

## Decisions (2026-10-31)

| # | decision |
|---|---|
| D1 | In scope: `CannabisWorld.esp`, `Cannabis Commonwealth.esp` (no-AWKCR/SMM build only), `CROSS_Halo.esp`, `Smoke-able Cigars.esp`. |
| D2 | `CannabisCommonwealthExpansion.esp` **excluded** — carries no unique record names (P3-B). |
| D3 | The SKE-AWKCR-AE-VIS v1.3 CC build is not read at all; it would pull `ArmorKeywords.esm`, `SettlementKeywords.esm`, `Armorsmith Extended.esp` into scope. |
| D4 | No load order is invented for reference-only plugins. Provenance comes from the profile's `plugins.txt` (active flag + declared index); reference-only plugins are recorded inactive. |
| D5 | Assets are indexed, never extracted: BA2 directory tables plus a loose-file pass per in-scope mod. Same-mod loose files outrank the same mod's archives (`bInvalidateOlderFiles=1`). |
| D6 | Re-ingest is per-plugin with purge, so a CW rebuild is a one-command refresh. |
| D7 | The scope is the checked-in `scope.tsv` manifest (name + folder). Name lookup refuses to resolve an ambiguous plugin; it lists the candidates instead. |

## P3-A — in scope now (measured 2026-10-31)

| plugin | bytes | md5 | masters | own recs | overrides |
|---|---|---|---|---|---|
| CannabisWorld.esp | 1,584,939 | `264053442619cc2516e17579c1d83f34` | Fallout4.esm | 4,452 | 1,323 |
| Cannabis Commonwealth.esp | 146,692 | `d264e9a54ca5aad53b39866948854270` | Fallout4.esm | 483 | 23 |
| CROSS_Halo.esp | 11,191 | `c1bf9e3c30bc11f99d689a1acc2363a2` | Fallout4.esm | 56 | 0 |
| Smoke-able Cigars.esp | 25,552 | `dc981e48f861874c21a03f2761f5dd99` | Fallout4.esm | 87 | 0 |

5,078 new records + 1,346 overrides + 40,801 subrecords. Against 2,197,919
existing records and 31.8M refs that is 6,424 records — 0.29% growth; the DB is
2.8 GB, so size is not the consideration. Correctness is.

`own`/`override` are read from the top byte of each record's formID as an index
into the plugin's own MAST list: `idx < len(masters)` = override of that master,
`idx == len(masters)` = a record the plugin creates. File-local, load-order
independent, and the only key that survives the same plugin loading at 0x0B in
one profile and 0x61 in another.

Active state in `default` today: CW, CROSS_Halo, Smoke-able active;
`Cannabis Commonwealth.esp` inactive. Ingestion is of the file on disk, so
inactive is acceptable for reference — recorded, not implied.

## P3-B — why Cannabis Commonwealth Expansion is out

| | records | EDIDs | EDIDs also in CW | EDIDs unique to it |
|---|---|---|---|---|
| Cannabis Commonwealth | 506 | 506 | 377 | **129** |
| Cannabis Commonwealth Expansion | 344 | 344 | **344** | **0** |

CCX carries 344 record names, every one of which already exists in CW, and 321
of the 344 have a *different body* in CW (`420co_HashPurpleKushfromSensi` is a
97-byte COBJ in CCX, 135 bytes in CW). It is a generation, not a component. Two
records with one name and two definitions, in plugins that can never load
together (CW's only master is `Fallout4.esm`; CCX masters CC), is the worst shape
for a lookup table.

CC keeps its place: 129 of its names exist nowhere else, and CCX names it as a
master — if CCX is ever wanted, CC must already be resolvable.

Consequence to build for: any query that names a record must be able to say
*which plugin's* definition it returned. `fo4-db field <EDID>` returns the first
match today and would silently alternate between generations.

## P3-C — open references

A reference is a 4-byte word whose top byte indexes the referring plugin's MAST
list. Resolve it to `(target_plugin_name, local_id)`; if that plugin is not in
`plugins`, store the reference with `resolved_record_id = NULL` and mark it
**open**. No placeholder record, no silent drop.

Payoff: `fo4-db open-refs` becomes the ledger of what the library points at but
does not contain, with counts per target plugin — precisely the list that would
be retired if another mod were later ingested. Nothing in scope today depends on
an absent plugin (all four masters are `Fallout4.esm`), so the ledger starts
empty; it exists for what is excluded.

## P3-D — load order

Not required for reference-only plugins, and not obtainable for them: they are
never loaded, so they have no runtime index. The requirement is that the library
never needs one — the file-local key above does that.

Where provenance is wanted, Amethyst supplies it and it can be read:

- `profiles/<profile>/plugins.txt` — authoritative; the engine reads this file.
  Starred = active, line order = load order. `default` has 142 active.
- `profiles/<profile>/loadorder.txt` — **stale, do not use.** Dated 2026-09-04
  against a `plugins.txt` of 2026-09-19, and missing two active plugins
  (`skkhandyfarmers.esp`, `basementliving.esp`). Where it overlaps it agrees with
  `plugins.txt`, which is why it looks trustworthy and is not.

So the DB gains `profile_plugins(profile, plugin, active, declared_index)` filled
from `plugins.txt`, and nothing else. An override is reported as "version 2 of
record X from plugin Y"; the winner is decided only when a reader asks about an
active profile.

## P3-E — assets: two passes, one owner column

1. **Archives.** BA2 directory tables only (seek, never decompress). One row per
   (owner mod, archive md5, path); archive md5 stored once so an identical archive
   under a different name is recognised instead of re-indexed.
2. **Loose files.** Walk `Data/` under each in-scope mod. Not optional:
   - Cannabis World ships **205 loose files** (111 nif, 51 dds, 20 pex, 10 hkx,
     7 bgsm, 2 swf, 2 wav, 1 uvd, 1 r2bak), **33 of which shadow entries in its
     own `CannabisWorld - Main.ba2`** (`Materials/Cannabis/*.BGSM`,
     `Meshes/Cannabis/*.nif`).
   - Smoke-able Cigars ships **no BA2 at all** — 57 asset files beside the .esp,
     12 MB, loose only.
   - CC (no-AWKCR) ships **no BA2 either** — 2 files, 147 KB.
3. **Precedence column** on every asset row: `loose` > `archive` within one
   owner. Cross-owner winner is a load-order question, answered only for active
   profiles.

Loose work in current scope: ~279 files. Metadata + md5; no extraction.

Duplication control: the excluded v1.3 CC build ships
`Cannabis Commonwealth - Main.ba2` / `- Textures.ba2` that are md5-identical to
CW's own archives (`c6bbf2be…`, `2847b7b3…`) — 108 MB, zero new bytes. Because
D3 excludes that build, no duplicate enters the index. The archive-md5 column is
the guard should another mod ever ship the same payload.

## Ingest blockers in the current code

1. **Per-plugin purge.** `db.ingest_plugin` does `INSERT OR REPLACE` on
   `plugins`/`records` but appends `subrecords`, `strings`, `refs` with no
   uniqueness and deletes nothing. Re-ingesting CW after a rebuild duplicates
   every subrecord and leaves records the new build dropped.
2. **Master-mapped refs.** `db.build_references` matches raw words against one
   flat set of every formID in the DB. 5,706 of CW's 5,775 records share an
   integer with an existing record (CC 506/506, CROSS_Halo 56/56, Smoke-able
   84/87), so flat matching attributes refs to the wrong record.
3. **Asset index owns nothing.** `assets.index_all` `DROP TABLE`s the index,
   globs `*.ba2` in one directory, has no owner and no loose pass.
4. **EDID ambiguity.** No query can distinguish CW's definition of a name from
   CC's.

## P3-F — built and applied (2026-10-31)

Code: `fo4db/masters.py` (MAST list + file-local arithmetic), `fo4db/schema_v2.py`
(migration, per-plugin purge, profile state, manifest lookup), `fo4db/refs.py`
(master-mapped reference scan), `fo4db/assets.py` (`index_source`: BA2 + loose),
`fo4db/cli.py` (`ingest --manifest/--purge/--dry-run`, `migrate`, `refs`, `out`,
`open-refs`, `assets-mod`, `profile`, `freshness`). Tests:
`tools/test_refs_mapping.py` (end-to-end on synthetic plugin binaries),
`tools/p3_acceptance.sh` (double ingest + count diff).

Applied to `fo4.db` after a `.backup` to `fo4.db.bak-preP3-20261031`.

| measure | value |
|---|---|
| plugins | 16 → 20 |
| records | 2,197,919 → 2,204,347 (+6,428, incl. 4 TES4) |
| subrecords | 9,893,532 → 9,934,355 |
| refs | 31,782,444 → **19,769,042** (17,288,101 high-confidence, 2,480,941 low) |
| assets | 507,570 → 508,100 |
| ingest + rebuild wall time | **205 s** (full re-scan of every plugin body) |
| words skipped as zero (formID 0 = plugin header) | 11,650,730 |
| refs contributed by the mods | CW 263,392 · CC 6,445 · Cigars 507 · CROSS_Halo 343 |
| db size | 2.8 GB → 4.5 GB (`refs` carries plugin/local/record/confidence per row) |

The ref count fell by 12M because the v1 map counted every zero-valued field as
a reference to a plugin header, and every small count as a reference to
`DoorMarker`/`TravelMarker`. Those rows are now excluded or demoted, not deleted:
`refs` and `out` read `confidence='high'` unless `--all` is passed.

**Verified 2026-11-01 (final code).** `tools/p3_acceptance.sh` two purged
ingests: counts, per-plugin record/subrecord totals and per-plugin ref counts all
identical; mods contribute CW 263,392 · CC 6,445 · Cigars 507 · CROSS_Halo 343.
`freshness`: 20 of 20 plugin files unchanged since ingest. Integrity: 0 refs to a
plugin header, 0 dangling, 0 orphan subrecords, 0 duplicate `(record, sub_type,
seq)`, 0 open references. `ingest_runs` now records the real game version
(`1.11.240.0`, read from the PE resource section — the old first-1-MB scan
returned `unknown` for every run).

### The defect this round found

The first `refs` v2 implementation re-mapped the *existing* rows through the
master map instead of re-scanning the plugins. It passed the idempotence test —
two runs, identical counts — while every mod contributed **zero** references,
because purge had deleted those rows and nothing re-derived them. A test that
cannot fail is not a test. `refs.rebuild` now walks each plugin body and calls
`refs.resolve_word()` per word, and the acceptance script asserts refs are
non-zero per mod.

A second defect, caught the same way: the first automated ingest silently read
the **excluded** `SKE-AWKCR-AE-VIS v1.3` CC build because name lookup took the
first filesystem match. Caught by md5 against the manifest expectation; lookup
now refuses ambiguity and lists candidates.

## P3-G — the typed layer covers the mods (2026-11-01)

`fo4-db fields` re-decodes every plugin, mods included, and resolves each
record's references in its own plugin's master-list context (`schema.RefResolver`,
same identity rule as `refs.resolve_word`). Coverage is the six P2 types —
ALCH, MGEF, LVLI, COBJ, MISC, KYWD.

| | before | after |
|---|---|---|
| fields rows | 71,706 | **127,724** |
| records decoded | 17,282 (vanilla only) | **18,327** (all 20 plugins) |
| reference values resolved | — | 42,345 of 42,347 |
| decoded per mod | — | CW 1,045 · CC 407 · Cigars 37 · CROSS_Halo 27 |

Two defects in the P2 decoder were exposed and fixed by running it against real
mod content:

1. **COBJ components were never decoded.** Fallout 4 stores recipe components as
   `FVPA` pairs (ref u32, count u32), 1–5 pairs per subrecord; the decoder
   looked for the Skyrim-era `CNTO`, which appears **zero** times in this
   database. Every recipe in the library read "result, no components". Now a
   recipe resolves components, workbench keyword, category keyword and produced
   count.
2. **Reference resolution was flat**, so `0x0002F3` in a mod was looked up as a
   bare formID and could return a vanilla record at the same number. It now
   resolves to `PLUGIN:LOCAL TYPE:EDID`, and the two values that do not resolve
   are named `(absent)` rather than filled with a wrong record.

The two absent references are real findings, not artefacts: `Cannabis
Commonwealth.esp` `LLS_Armor_Raider_Headpart_Facewear` entry[4] points at its own
`0x003F1A`, which exists in neither the mod nor `Fallout4.esm` (the flat resolver
would have quietly returned DLCCoast's REFR at `0x01003F1A`); and
`DLCCoast.esm` has one leveled-list entry pointing at `0x048856`, which that DLC
does not contain.

`ingest` now rebuilds `fields` as part of the run (~3 s), so a purge+ingest after
a Cannabis World rebuild keeps the typed layer consistent without a second
command.

## Acceptance test for the refresh

```
cd ~/dev/fo4-db
bash tools/p3_acceptance.sh            # purge+ingest twice, diff counts
.venv/bin/python tools/test_refs_mapping.py
```

The script diffs totals **and** per-plugin ref counts between the two runs, and
fails if an in-scope mod contributes zero references. That second assertion is
the one the earlier version would have failed.
