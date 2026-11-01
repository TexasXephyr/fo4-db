#!/usr/bin/env bash
# Acceptance test for P3 ingest: purge+ingest twice, counts must match exactly.
set -euo pipefail
cd "$HOME/dev/fo4-db"
export FO4_DB="${FO4_DB:-$HOME/dev/fo4-db/fo4.db}"
M="$HOME/.config/AmethystModManager/Profiles/Fallout 4/mods"
OUT="${1:-/tmp/p3}"
mkdir -p "$OUT"

snapshot() {
  sqlite3 "$FO4_DB" \
    "select 'plugins', count(*) from plugins" \
    "select 'records', count(*) from records" \
    "select 'subrecords', count(*) from subrecords" \
    "select 'strings', count(*) from strings" \
    "select 'refs', count(*) from refs" \
    "select 'refs_open', count(*) from refs where open_plugin is not null" \
    "select 'assets', count(*) from assets" \
    > "$OUT/counts-$1.txt"
  sqlite3 "$FO4_DB" \
    "select p.name, count(*), sum(length(s.raw)) from records r join plugins p on p.id=r.plugin_id join subrecords s on s.record_id=r.id group by p.name order by p.name" \
    > "$OUT/perplugin-$1.txt"
  # refs contributed by each plugin -- the count that exposed a rebuild which
  # re-mapped rows instead of scanning plugins (mods showed 0 here)
  sqlite3 "$FO4_DB" \
    "select p.name, count(*) from refs x join records r on r.id=x.record_id join plugins p on p.id=r.plugin_id group by p.name order by p.name" \
    > "$OUT/refs-by-plugin-$1.txt"
}

echo "== run A =="
.venv/bin/fo4-db ingest --manifest scope.tsv --mods-root "$M" --purge | tail -12
snapshot A

echo "== run B (idempotence) =="
.venv/bin/fo4-db ingest --manifest scope.tsv --mods-root "$M" --purge | tail -6
snapshot B

echo "== diff counts =="
diff -u "$OUT/counts-A.txt" "$OUT/counts-B.txt" && echo "COUNTS IDENTICAL"
echo "== diff per-plugin =="
diff -u "$OUT/perplugin-A.txt" "$OUT/perplugin-B.txt" && echo "PER-PLUGIN IDENTICAL"
echo "== diff refs by plugin =="
diff -u "$OUT/refs-by-plugin-A.txt" "$OUT/refs-by-plugin-B.txt" && echo "REFS-BY-PLUGIN IDENTICAL"
echo "== mod refs must be non-zero =="
grep -E '^(Cannabis|CROSS|Smoke)' "$OUT/refs-by-plugin-A.txt" | awk -F'|' '{print "  "$0; if ($2+0==0) bad=1} END{exit bad+0}'
echo "MODS CONTRIBUTE REFERENCES"
