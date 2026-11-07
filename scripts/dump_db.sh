#!/usr/bin/env bash
# Dump the local `disinfo` database to a portable, gzipped plain-SQL file.
#
#   scripts/dump_db.sh              # -> dumps/disinfo-<date>.sql.gz (+ .txt manifest)
#
# Plain SQL (not pg_dump's custom format) so it restores with nothing but psql,
# into any Postgres 13+. pg_dump 17+/18 adds a few lines older servers and
# older psql do not understand (\restrict, SET transaction_timeout); they are
# stripped here so the file is not tied to the version that produced it.
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a
url="${DATABASE_URL_LOCAL:-${DATABASE_URL:-}}"
[ -n "$url" ] || { echo "DATABASE_URL_LOCAL is not set in .env" >&2; exit 1; }
case "$url" in
  */disinfo|*/disinfo\?*) ;;
  *) echo "DATABASE_URL_LOCAL does not point at the disinfo database" >&2; exit 1 ;;
esac

PG_DUMP="$(command -v pg_dump || echo /Applications/Postgres.app/Contents/Versions/latest/bin/pg_dump)"
PSQL="$(command -v psql || echo /Applications/Postgres.app/Contents/Versions/latest/bin/psql)"

mkdir -p dumps
out="dumps/disinfo-$(date -u +%Y%m%d).sql.gz"

"$PG_DUMP" "$url" --no-owner --no-privileges \
  | grep -vE '^\\(restrict|unrestrict) |^SET transaction_timeout' \
  | gzip -9 > "$out"

counts="$("$PSQL" "$url" -Atc "
  SELECT 'cases',            count(*) FROM cases            UNION ALL
  SELECT 'subject_accounts', count(*) FROM subject_accounts UNION ALL
  SELECT 'collection_runs',  count(*) FROM collection_runs  UNION ALL
  SELECT 'authors',          count(*) FROM authors          UNION ALL
  SELECT 'content_items',    count(*) FROM content_items    UNION ALL
  SELECT 'media_assets',     count(*) FROM media_assets")"

# Manifest: what is in the file and a checksum to verify the download.
{
  echo "file:    $(basename "$out")"
  echo "created: $(date -u +%Y-%m-%dT%H:%M:%SZ) from the local disinfo database"
  echo "sha256:  $(shasum -a 256 "$out" | cut -d' ' -f1)"
  echo "rows:"
  echo "$counts" | awk -F'|' '{printf "  %-17s %s\n", $1, $2}'
} > "${out%.sql.gz}.txt"

echo "wrote $out ($(du -h "$out" | cut -f1))"
cat "${out%.sql.gz}.txt"
