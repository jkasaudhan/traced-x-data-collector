"""Read-only database access for the evidence viewer.

Reuses the collector's .env (one directory up) so there is a single place for
connection strings. WEBAPP_DB=local|cloud picks the target; local by default,
for the same reason the collector never defaults to cloud.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def database_url(target: str | None = None) -> str:
    target = (target or os.getenv("WEBAPP_DB") or "local").lower()
    if target == "cloud":
        url = os.getenv("DATABASE_URL_CLOUD")
    else:
        url = os.getenv("DATABASE_URL_LOCAL") or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError(f"no database URL configured for target {target!r} in {ROOT / '.env'}")
    return url


def describe(url: str) -> str:
    """Host only — the URL contains the password."""
    host = url.split("@", 1)[-1].split("/", 1)[0]
    return host


_pool: ConnectionPool | None = None


def _configure(conn: Any) -> None:
    # All timestamps leave the API in UTC, whatever the server's timezone.
    conn.execute("SET TIME ZONE 'UTC'")
    conn.commit()


def init_pool(target: str | None = None) -> str:
    global _pool
    url = database_url(target)
    kwargs: dict[str, Any] = {"row_factory": dict_row}
    # Supabase poolers (pgbouncer/supavisor) break server-side prepared statements.
    if "pooler.supabase.com" in url or ":6543" in url:
        kwargs["prepare_threshold"] = None
    _pool = ConnectionPool(url, min_size=1, max_size=5, kwargs=kwargs,
                           configure=_configure, open=True)
    return describe(url)


def close_pool() -> None:
    if _pool is not None:
        _pool.close()


def fetch_all(sql: str, params: Any = None) -> list[dict[str, Any]]:
    assert _pool is not None, "pool not initialised"
    with _pool.connection() as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())


def fetch_one(sql: str, params: Any = None) -> dict[str, Any] | None:
    rows = fetch_all(sql, params)
    return rows[0] if rows else None
