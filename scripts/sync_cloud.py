"""Make Supabase match the local `disinfo` database.

    .venv/bin/python scripts/sync_cloud.py --dry-run   # show what would change, write nothing
    .venv/bin/python scripts/sync_cloud.py             # apply

Unlike `collector.cli copy` (insert-only), this also UPDATES rows that changed
locally. Per table:

  collector tables   upsert by primary key; rows that exist only on the cloud
                     are reported, never deleted (collection data is evidence)
  analytics_* tables mirrored: upsert, then delete cloud rows that are gone
                     locally — they are derived output of content-analytics,
                     and local is the source of truth

All tables are synced in ONE transaction on the cloud side: it either fully
applies or not at all. The analytics_* tables must already exist on the cloud
(`cd ../content-analytics && DATABASE_URL=<cloud> uv run content-analytics init-db`).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collector.config import Settings  # noqa: E402

# Parents before children, so foreign keys hold at every step.
COLLECTOR_TABLES = ["cases", "subject_accounts", "collection_runs", "authors",
                    "content_items", "media_assets"]
ANALYTICS_TABLES = ["analytics_runs", "analytics_thresholds", "analytics_content_labels",
                    "analytics_harassment_bursts", "analytics_subject_daily",
                    "analytics_profile_summary", "analytics_alerts"]


def columns(conn: psycopg.Connection, table: str) -> list[str]:
    return [r[0] for r in conn.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position""",
        (table,))]


def primary_key(conn: psycopg.Connection, table: str) -> list[str]:
    return [r[0] for r in conn.execute(
        """SELECT a.attname
           FROM pg_index i
           JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
           WHERE i.indrelid = %s::regclass AND i.indisprimary
           ORDER BY array_position(i.indkey, a.attnum)""",
        (f"public.{table}",))]


def sync_table(local: psycopg.Connection, cloud: psycopg.Connection, table: str,
               mirror: bool) -> dict[str, int]:
    lcols, ccols = columns(local, table), columns(cloud, table)
    if not ccols:
        raise SystemExit(f"{table} does not exist on the cloud — create the schema first")
    if lcols != ccols:
        raise SystemExit(f"{table}: columns differ\n  local {lcols}\n  cloud {ccols}")
    pk = primary_key(local, table)
    if not pk:
        raise SystemExit(f"{table} has no primary key; cannot sync it safely")

    t = sql.Identifier(table)
    tmp = sql.Identifier(f"_sync_{table}")
    cols = sql.SQL(", ").join(map(sql.Identifier, lcols))
    pkcols = sql.SQL(", ").join(map(sql.Identifier, pk))
    non_pk = [c for c in lcols if c not in pk]

    # Stage the full local table in a temp table on the cloud via COPY.
    cloud.execute(sql.SQL("CREATE TEMP TABLE {} (LIKE {} INCLUDING DEFAULTS) ON COMMIT DROP")
                  .format(tmp, t))
    with local.cursor().copy(sql.SQL("COPY {} ({}) TO STDOUT").format(t, cols)) as src, \
         cloud.cursor().copy(sql.SQL("COPY {} ({}) FROM STDIN").format(tmp, cols)) as dst:
        for block in src:
            dst.write(block)
    staged = cloud.execute(sql.SQL("SELECT count(*) FROM {}").format(tmp)).fetchone()[0]

    # Upsert; only touch rows whose content actually differs.
    if non_pk:
        on_conflict = sql.SQL("DO UPDATE SET {} WHERE ({}) IS DISTINCT FROM ({})").format(
            sql.SQL(", ").join(sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(c)) for c in non_pk),
            sql.SQL(", ").join(sql.SQL("{}.{}").format(t, sql.Identifier(c)) for c in non_pk),
            sql.SQL(", ").join(sql.SQL("EXCLUDED.{}").format(sql.Identifier(c)) for c in non_pk))
    else:
        on_conflict = sql.SQL("DO NOTHING")
    rows = cloud.execute(sql.SQL(
        "INSERT INTO {t} ({cols}) SELECT {cols} FROM {tmp} ON CONFLICT ({pk}) {oc} "
        "RETURNING (xmax = 0)").format(t=t, cols=cols, tmp=tmp, pk=pkcols, oc=on_conflict)).fetchall()
    inserted = sum(1 for (new,) in rows if new)
    updated = len(rows) - inserted

    match = sql.SQL(" AND ").join(
        sql.SQL("s.{0} = c.{0}").format(sql.Identifier(c)) for c in pk)
    if mirror:
        deleted = cloud.execute(sql.SQL(
            "DELETE FROM {t} c WHERE NOT EXISTS (SELECT 1 FROM {tmp} s WHERE {m})")
            .format(t=t, tmp=tmp, m=match)).rowcount
        cloud_only = 0
    else:
        deleted = 0
        cloud_only = cloud.execute(sql.SQL(
            "SELECT count(*) FROM {t} c WHERE NOT EXISTS (SELECT 1 FROM {tmp} s WHERE {m})")
            .format(t=t, tmp=tmp, m=match)).fetchone()[0]

    # Serial ids: keep the cloud sequence ahead of the copied ids.
    for (seq, col) in cloud.execute(
            """SELECT pg_get_serial_sequence(%s, column_name), column_name
               FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = %s
                 AND column_default LIKE 'nextval(%%'""", (f"public.{table}", table)).fetchall():
        cloud.execute(sql.SQL("SELECT setval(%s, GREATEST((SELECT max({}) FROM {}), 1))")
                      .format(sql.Identifier(col), t), (seq,))

    total = cloud.execute(sql.SQL("SELECT count(*) FROM {}").format(t)).fetchone()[0]
    return {"local": staged, "inserted": inserted, "updated": updated,
            "deleted": deleted, "cloud_only": cloud_only, "cloud_total": total}


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync local disinfo -> Supabase.")
    ap.add_argument("--dry-run", action="store_true", help="do everything, then roll back")
    ap.add_argument("--no-analytics", action="store_true", help="collector tables only")
    args = ap.parse_args()

    local_s, cloud_s = Settings.load("local"), Settings.load("cloud")
    print(f"from {local_s.describe()}\n  to {cloud_s.describe()}"
          + ("   [dry run — nothing will be saved]" if args.dry_run else ""))

    with psycopg.connect(local_s.database_url) as local, \
         psycopg.connect(cloud_s.database_url, prepare_threshold=None) as cloud:
        local.read_only = True
        # Repeatable read: every local table is read from the same snapshot.
        local.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ

        plan = [(t, False) for t in COLLECTOR_TABLES]
        if not args.no_analytics:
            plan += [(t, True) for t in ANALYTICS_TABLES]

        print(f"\n  {'table':<28}{'local':>7}{'insert':>8}{'update':>8}{'delete':>8}{'cloud':>8}")
        try:
            for table, mirror in plan:
                r = sync_table(local, cloud, table, mirror)
                note = f"  ({r['cloud_only']} cloud-only rows kept)" if r["cloud_only"] else ""
                print(f"  {table:<28}{r['local']:>7}{r['inserted']:>8}{r['updated']:>8}"
                      f"{r['deleted']:>8}{r['cloud_total']:>8}{note}")
        except BaseException:
            cloud.rollback()
            print("\nfailed — rolled back, the cloud is unchanged")
            raise
        if args.dry_run:
            cloud.rollback()
            print("\ndry run — rolled back")
        else:
            cloud.commit()
            print("\ncommitted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
