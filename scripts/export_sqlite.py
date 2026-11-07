"""Export the local `disinfo` Postgres database to a single SQLite file.

    .venv/bin/python scripts/export_sqlite.py                 # -> dumps/disinfo-<date>.sqlite
    .venv/bin/python scripts/export_sqlite.py --out my.sqlite

Same tables and columns as Postgres, so queries written against one mostly
work on the other. Type mapping:

    uuid, enum, text      -> TEXT
    timestamptz           -> TEXT, ISO-8601 UTC ('2025-11-04T23:03:24Z');
                             sorts correctly and works with SQLite date functions
    jsonb, text[]         -> TEXT holding JSON; query with json_extract()
    boolean               -> INTEGER 0/1
    integer, bigint       -> INTEGER
    numeric               -> REAL

Extras that make the file useful on its own: a `v_posts` view (post + author
+ is_real), an FTS5 full-text index over post text and author, and an
`export_info` table describing the export and the attribution labels.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collector.config import Settings  # noqa: E402

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE cases (
    id              TEXT PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    subject_name    TEXT NOT NULL,
    subject_role    TEXT,
    description     TEXT,
    window_start    TEXT NOT NULL,
    window_end      TEXT NOT NULL,
    about_terms     TEXT NOT NULL DEFAULT '[]',   -- JSON array
    config          TEXT NOT NULL DEFAULT '{}',   -- JSON (the case YAML)
    created_at      TEXT NOT NULL
);

CREATE TABLE subject_accounts (
    id                TEXT PRIMARY KEY,
    case_id           TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    platform          TEXT NOT NULL,
    handle            TEXT NOT NULL,
    platform_user_id  TEXT,
    role              TEXT NOT NULL CHECK (role IN ('personal','campaign','office','staff','known_impersonator')),
    verified_genuine  INTEGER NOT NULL,
    valid_from        TEXT,
    valid_until       TEXT,
    source_url        TEXT,
    note              TEXT,
    created_at        TEXT NOT NULL,
    UNIQUE (case_id, platform, handle)
);

CREATE TABLE collection_runs (
    id               TEXT PRIMARY KEY,
    case_id          TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    platform         TEXT NOT NULL,
    tier             TEXT,
    actor_id         TEXT,
    query            TEXT NOT NULL,
    slice_start      TEXT,
    slice_end        TEXT,
    max_items        INTEGER,
    items_returned   INTEGER NOT NULL,
    items_inserted   INTEGER NOT NULL,
    est_cost_usd     REAL NOT NULL,
    status           TEXT NOT NULL,
    external_run_id  TEXT,
    error            TEXT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT
);

CREATE TABLE authors (
    id                  TEXT PRIMARY KEY,
    platform            TEXT NOT NULL,
    platform_user_id    TEXT NOT NULL,
    handle              TEXT,
    display_name        TEXT,
    description         TEXT,
    account_created_at  TEXT,
    followers_count     INTEGER,
    following_count     INTEGER,
    post_count          INTEGER,
    is_verified         INTEGER,
    is_blue_verified    INTEGER,
    profile_image_url   TEXT,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    raw                 TEXT,                     -- JSON
    UNIQUE (platform, platform_user_id)
);

CREATE TABLE content_items (
    id                  TEXT PRIMARY KEY,
    case_id             TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    platform            TEXT NOT NULL,
    platform_item_id    TEXT NOT NULL,
    author_id           TEXT REFERENCES authors(id) ON DELETE SET NULL,
    attribution         TEXT NOT NULL CHECK (attribution IN
                          ('subject_own','subject_affiliated','impersonation_suspect','third_party','unknown')),
    attribution_reason  TEXT,
    posted_at           TEXT,
    collected_at        TEXT NOT NULL,
    url                 TEXT,
    text                TEXT,
    lang                TEXT,
    like_count          INTEGER,
    reply_count         INTEGER,
    repost_count        INTEGER,
    quote_count         INTEGER,
    view_count          INTEGER,
    bookmark_count      INTEGER,
    is_reply            INTEGER NOT NULL,
    is_repost           INTEGER NOT NULL,
    is_quote            INTEGER NOT NULL,
    parent_item_id      TEXT,
    conversation_id     TEXT,
    mentions_subject    INTEGER,
    archive_url         TEXT,
    content_sha256      TEXT,
    labels              TEXT NOT NULL DEFAULT '{}',   -- JSON
    collection_run_id   TEXT REFERENCES collection_runs(id) ON DELETE SET NULL,
    raw                 TEXT NOT NULL,                -- JSON, as returned by the scraper
    UNIQUE (case_id, platform, platform_item_id)
);

CREATE TABLE media_assets (
    id               TEXT PRIMARY KEY,
    content_item_id  TEXT NOT NULL REFERENCES content_items(id) ON DELETE CASCADE,
    media_type       TEXT,
    source_url       TEXT NOT NULL,
    local_path       TEXT,
    sha256           TEXT,
    phash            TEXT,
    duration_sec     REAL,
    width            INTEGER,
    height           INTEGER,
    raw              TEXT,                            -- JSON
    UNIQUE (content_item_id, source_url)
);
"""

INDEXES = """
CREATE INDEX idx_subject_accounts_lookup ON subject_accounts (case_id, platform, platform_user_id);
CREATE INDEX idx_runs_case          ON collection_runs (case_id, started_at DESC);
CREATE INDEX idx_authors_handle     ON authors (platform, lower(handle));
CREATE INDEX idx_items_case_time    ON content_items (case_id, posted_at DESC);
CREATE INDEX idx_items_attribution  ON content_items (case_id, attribution);
CREATE INDEX idx_items_author       ON content_items (author_id);
CREATE INDEX idx_items_parent       ON content_items (parent_item_id);
CREATE INDEX idx_items_conversation ON content_items (conversation_id);
CREATE INDEX idx_media_item         ON media_assets (content_item_id);

-- One row per post with its author and a plain is_real flag:
-- 1 = posted by one of the subject's registered accounts.
CREATE VIEW v_posts AS
SELECT
    ci.id, c.slug AS case_slug, ci.platform, ci.platform_item_id,
    ci.attribution,
    CASE WHEN ci.attribution IN ('subject_own','subject_affiliated') THEN 1 ELSE 0 END AS is_real,
    ci.attribution_reason, ci.posted_at, ci.url, ci.text, ci.lang,
    a.handle AS author_handle, a.display_name AS author_name,
    a.platform_user_id AS author_user_id, a.followers_count AS author_followers,
    a.is_verified AS author_verified,
    ci.like_count, ci.repost_count, ci.reply_count, ci.quote_count,
    ci.view_count, ci.bookmark_count,
    ci.is_reply, ci.is_quote, ci.parent_item_id, ci.conversation_id,
    (SELECT count(*) FROM media_assets m WHERE m.content_item_id = ci.id) AS media_count
FROM content_items ci
JOIN cases c ON c.id = ci.case_id
LEFT JOIN authors a ON a.id = ci.author_id;

-- Full-text search (stemmed: 'freeze' also finds 'freezes'):
--   SELECT * FROM posts_fts WHERE posts_fts MATCH 'rent freeze';
CREATE VIRTUAL TABLE posts_fts USING fts5(
    text, author_handle, author_name,
    item_id UNINDEXED,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
INSERT INTO posts_fts (text, author_handle, author_name, item_id)
SELECT ci.text, a.handle, a.display_name, ci.id
FROM content_items ci LEFT JOIN authors a ON a.id = ci.author_id;
"""

TABLES = ["cases", "subject_accounts", "collection_runs", "authors",
          "content_items", "media_assets"]

ATTRIBUTION_HELP = {
    "subject_own": "posted by the subject's personal account (matched on platform user id)",
    "subject_affiliated": "posted by the subject's office/campaign/staff account inside its validity window",
    "impersonation_suspect": "handle resembles a registered account but the user id does not match",
    "third_party": "posted by any other account - not the subject; NOT a judgement that it is false",
    "unknown": "no author information on the item",
}


def _select_list(conn: psycopg.Connection, table: str) -> tuple[list[str], str]:
    """Column names plus a SELECT list that converts each Postgres type to its
    SQLite representation server-side, so Python only moves plain values."""
    cols = conn.execute(
        """SELECT column_name, data_type, udt_name FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position""",
        (table,)).fetchall()
    names, exprs = [], []
    for name, data_type, udt in cols:
        q = f'"{name}"'
        if udt == "timestamptz":
            e = f"""to_char({q} AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')"""
        elif udt in ("jsonb", "json"):
            e = f"{q}::text"
        elif data_type == "ARRAY":
            e = f"to_jsonb({q})::text"
        elif udt == "bool":
            e = f"{q}::int"
        elif udt == "numeric":
            e = f"{q}::float8"
        elif udt == "uuid" or data_type == "USER-DEFINED":
            e = f"{q}::text"
        else:
            e = q
        names.append(name)
        exprs.append(e)
    return names, ", ".join(exprs)


def export(out: Path) -> dict[str, int]:
    url = Settings.load("local").database_url
    if not url.split("?")[0].endswith("/disinfo"):
        raise SystemExit("DATABASE_URL_LOCAL does not point at the disinfo database")

    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    counts: dict[str, int] = {}

    with psycopg.connect(url) as pg:
        pg.read_only = True
        lite = sqlite3.connect(tmp)
        try:
            lite.executescript(SCHEMA)
            for table in TABLES:          # parent tables first, for the foreign keys
                names, select = _select_list(pg, table)
                placeholders = ", ".join("?" * len(names))
                insert = f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders})"
                with pg.cursor(name=f"export_{table}") as cur:   # server-side, streams
                    cur.execute(f"SELECT {select} FROM {table} ORDER BY 1")
                    n = 0
                    while rows := cur.fetchmany(2000):
                        lite.executemany(insert, rows)
                        n += len(rows)
                counts[table] = n
                print(f"  {table:<17} {n:>6}")

            lite.executescript(INDEXES)

            lite.execute("CREATE TABLE export_info (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            info = {
                "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": "PostgreSQL database 'disinfo' (collector)",
                "timestamps": "ISO-8601 UTC text",
                "json_columns": "raw, labels, config, about_terms - query with json_extract()",
                "real_posts": "attribution IN ('subject_own','subject_affiliated'), or v_posts.is_real = 1",
                **{f"attribution.{k}": v for k, v in ATTRIBUTION_HELP.items()},
                **{f"rows.{k}": str(v) for k, v in counts.items()},
            }
            lite.executemany("INSERT INTO export_info VALUES (?, ?)", info.items())
            lite.commit()

            problems = lite.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise SystemExit(f"foreign key check failed: {problems[:5]}")
            if lite.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise SystemExit("sqlite integrity check failed")
            lite.execute("VACUUM")
        finally:
            lite.close()

    tmp.replace(out)
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description="Export the local disinfo database to SQLite.")
    ap.add_argument("--out", type=Path,
                    default=Path("dumps") / f"disinfo-{datetime.now(timezone.utc):%Y%m%d}.sqlite")
    args = ap.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    print(f"exporting local disinfo -> {args.out}")
    export(args.out)
    print(f"wrote {args.out} ({args.out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
