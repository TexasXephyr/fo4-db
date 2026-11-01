# PLAN P3 — ingest the work's mods into fo4-db

Scope and decisions: `docs/SCOPE.md`. This file is the execution order; each step
is verified before the next starts.

## Target state

`fo4-db` answers, for the vanilla game **and** for `CannabisWorld.esp`,
`Cannabis Commonwealth.esp` (no-AWKCR), `CROSS_Halo.esp` and
`Smoke-able Cigars.esp`:

- what a record is, **and which plugin's definition was returned**;
- what points at it (`refs`), with the target named by `(plugin, local id)`;
- what it points at that the library does not hold (`open-refs`);
- which assets a mod ships, archive or loose, and which of those shadow the
  same mod's own archive;
- which profile declares a plugin active, and at what index, when it is.

## Steps

| # | step | verification |
|---|---|---|
| 1 | Schema v2: `plugin_masters`, `profile_plugins`, `refs` rebuilt with `target_plugin_id`/`target_local`/`resolved_record_id`/`open_plugin`, `assets` gains `owner`/`source_kind`/`precedence`/`source_md5`/`path_norm`, `plugins` gains `is_light`/`source` | `PRAGMA table_info` on each, `fo4-db stats` still answers |
| 2 | Master lists read for all 24 ingested plugins from their TES4 `MAST` subrecords | 24 rows in `plugin_masters`, index 0 = `Fallout4.esm` for every DLC/CC file |
| 3 | `ingest --plugin NAME --mod-dir DIR [--purge] [--dry-run]` | dry run prints counts and writes nothing; two consecutive purged ingests give identical counts |
| 4 | Rebuild of `refs` through the master map (all plugins, vanilla included) | row count before/after, false-positive drop measured, `fo4-db refs <EDID>` resolves |
| 5 | `open-refs` ledger | starts empty under D1–D3 |
| 6 | Asset index v2: mod BA2s (metadata only, archive md5 recorded) + loose pass per in-scope mod | CW: 133 + 112 archive entries, 204 loose, 33 shadows flagged; Cigars: 59 loose, 0 archives |
| 7 | `profile_plugins` from `default` and `CW Tester` `plugins.txt` | 142 active in `default`, inactive in-scope plugins recorded as such |

## Order of execution

1 → 2 → 3 (dry run, counts reported) → 3 (live, after `fo4.db` backup) → 4 →
5 → 6 → 7 → acceptance test.

## Acceptance

```
fo4-db ingest --plugin CannabisWorld.esp --mod-dir "<...>/Cannabis World" --purge
fo4-db ingest --plugin CannabisWorld.esp --mod-dir "<...>/Cannabis World" --purge
fo4-db stats
```

Identical counts after the second run; no orphan rows in
`subrecords`/`strings`/`refs`/`fields` (checked by SQL, not by assertion);
`open-refs` unchanged.

## Non-goals (this round)

- No asset extraction. Nothing is unpacked for the mods.
- No `fields` re-decode: P2's typed layer still resolves refs by bare formID and
  is left as-is until P4 asks for it (recorded as a known defect).
- No DLC/CC asset ingest. No other mod from the shared store.
- No git commit until the round is verified; `fo4.db` is gitignored and the
  2.8 GB backup stays out of the repo.
