"""Create the target database if it does not exist yet.

Run inside the container during setup. Connects to the `postgres` maintenance
database on the same server, so it works whether or not the target exists.

    python scripts/ensure_db.py            # uses DATABASE_URL_LOCAL
    python scripts/ensure_db.py cloud      # uses DATABASE_URL_CLOUD
"""
from __future__ import annotations

import sys
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

sys.path.insert(0, ".")
from collector.config import Settings  # noqa: E402


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "local"
    url = Settings.load(target).database_url

    parts = urlsplit(url)
    dbname = parts.path.lstrip("/")
    if not dbname:
        print("no database name in the connection URL", file=sys.stderr)
        return 1

    # Supabase hands you a database that already exists and a role that cannot
    # create another one. Nothing to do.
    if "supabase" in (parts.hostname or ""):
        print(f"{dbname}: managed by Supabase, skipping creation")
        return 0

    admin_url = urlunsplit(parts._replace(path="/postgres"))
    try:
        with psycopg.connect(admin_url, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
            if cur.fetchone():
                print(f"{dbname}: already exists")
                return 0
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
            print(f"{dbname}: created")
            return 0
    except psycopg.OperationalError as exc:
        print(f"could not reach Postgres: {str(exc).strip().splitlines()[0]}",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
