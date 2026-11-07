#!/usr/bin/env bash
# Restore a dump made by scripts/dump_db.sh into an EMPTY database.
#
#   scripts/restore_db.sh dumps/disinfo-20261107.sql.gz
#   scripts/restore_db.sh dumps/disinfo-20261107.sql.gz postgresql://user:pass@host:5432/disinfo
#
# Without a URL it uses DATABASE_URL_LOCAL from .env, or
# postgresql://postgres:postgres@localhost:5432/disinfo if there is no .env.
# The database is created if it does not exist. It refuses to restore over
# existing tables, so it can never mix two datasets or clobber one.
set -euo pipefail
cd "$(dirname "$0")/.."

dump="${1:-}"
[ -f "$dump" ] || { echo "usage: $0 <dump.sql.gz> [database-url]" >&2; exit 2; }

url="${2:-}"
if [ -z "$url" ] && [ -f .env ]; then set -a; . ./.env; set +a; url="${DATABASE_URL_LOCAL:-}"; fi
url="${url:-postgresql://postgres:postgres@localhost:5432/disinfo}"

PSQL="$(command -v psql || echo /Applications/Postgres.app/Contents/Versions/latest/bin/psql)"
[ -x "$PSQL" ] || { echo "psql not found — install the PostgreSQL client" >&2; exit 1; }

# Verify the checksum if the manifest is next to the dump.
manifest="${dump%.sql.gz}.txt"
if [ -f "$manifest" ]; then
  want="$(awk '/^sha256:/{print $2}' "$manifest")"
  have="$(shasum -a 256 "$dump" | cut -d' ' -f1)"
  [ "$want" = "$have" ] || { echo "checksum mismatch: $dump is corrupt or incomplete" >&2; exit 1; }
  echo "checksum ok"
fi

# Create the database if needed, via the 'postgres' maintenance database.
db="$(echo "$url" | sed -E 's#^[^/]*//[^/]*/([^?]*).*#\1#')"
admin="$(echo "$url" | sed -E "s#^([^/]*//[^/]*/)[^?]*#\1postgres#")"
if ! "$PSQL" "$url" -Atc "SELECT 1" >/dev/null 2>&1; then
  echo "creating database $db"
  "$PSQL" "$admin" -v ON_ERROR_STOP=1 -qc "CREATE DATABASE \"$db\""
fi

existing="$("$PSQL" "$url" -Atc "SELECT count(*) FROM information_schema.tables
                                  WHERE table_schema = 'public' AND table_name = 'content_items'")"
if [ "$existing" != "0" ]; then
  echo "database $db already has a content_items table — refusing to restore over it." >&2
  echo "Restore into a new database name, or drop this one first:" >&2
  echo "  psql \"\$ADMIN_URL\" -c 'DROP DATABASE \"$db\"'" >&2
  exit 1
fi

echo "restoring $dump into $db …"
# One transaction: either everything lands or nothing does.
gunzip -c "$dump" | "$PSQL" "$url" -q -v ON_ERROR_STOP=1 --single-transaction >/dev/null

"$PSQL" "$url" -c "
  SELECT 'content_items' AS table_name, count(*) FROM content_items UNION ALL
  SELECT 'authors', count(*) FROM authors UNION ALL
  SELECT 'media_assets', count(*) FROM media_assets UNION ALL
  SELECT 'collection_runs', count(*) FROM collection_runs UNION ALL
  SELECT 'subject_accounts', count(*) FROM subject_accounts UNION ALL
  SELECT 'cases', count(*) FROM cases"
echo "done — compare the counts with ${manifest}"
