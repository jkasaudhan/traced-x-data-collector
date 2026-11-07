"""PostgreSQL persistence. Plain SQL via psycopg3 — no ORM."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import CaseConfig, SubjectAccount
from .models import ContentItem


def _as_param(value: Any) -> Any:
    """JSONB columns read back as Python dicts and must be re-wrapped on the
    way in. Lists are deliberately left alone: `cases.about_terms` is a
    native TEXT[], and wrapping it would turn it into a JSON string."""
    if isinstance(value, dict):
        return _json(value)
    return value


def _json(obj: Any) -> Jsonb:
    """JSONB wrapper that tolerates dates, Decimals and other stragglers that
    arrive from YAML configs and third-party actor payloads."""
    return Jsonb(obj, dumps=lambda o: json.dumps(o, default=str, ensure_ascii=False))


def _connect_kwargs(dsn: str) -> dict[str, Any]:
    """Connection tweaks that Supabase needs and a local socket does not.

    Supabase's TRANSACTION pooler (port 6543) multiplexes several clients onto
    one backend and rejects prepared statements. psycopg3 starts preparing a
    query automatically after it has seen it five times, so a long run fails
    partway through rather than at connect time — disable preparation on any
    pooled connection. The SESSION pooler (port 5432) is fine either way, so
    this errs toward the safe setting for both.

    sslmode is appended only when the DSN does not already specify it; a plain
    `host=/tmp` socket URL gets neither.
    """
    kw: dict[str, Any] = {"row_factory": dict_row, "autocommit": False}
    pooled = "pooler.supabase.com" in dsn or ":6543" in dsn
    if pooled:
        kw["prepare_threshold"] = None
    if "supabase.co" in dsn or "supabase.com" in dsn:
        if "sslmode=" not in dsn:
            kw["sslmode"] = "require"
        # Pooled Supabase connections drop idle sockets; fail fast instead of hanging.
        kw["connect_timeout"] = 15
        kw["keepalives"] = 1
        kw["keepalives_idle"] = 30
    return kw


class Database:
    def __init__(self, dsn: str):
        self.conn = psycopg.connect(dsn, **_connect_kwargs(dsn))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- schema ------------------------------------------------------------

    def init_schema(self, schema_path: str | Path = "schema.sql") -> None:
        sql = Path(schema_path).read_text(encoding="utf-8")
        with self.conn.cursor() as cur:
            cur.execute(sql)
        self.conn.commit()

    # -- cases -------------------------------------------------------------

    def upsert_case(self, cfg: CaseConfig) -> UUID:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cases (slug, subject_name, subject_role, description,
                                   window_start, window_end, about_terms, config)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (slug) DO UPDATE SET
                    subject_name = EXCLUDED.subject_name,
                    subject_role = EXCLUDED.subject_role,
                    description  = EXCLUDED.description,
                    window_start = EXCLUDED.window_start,
                    window_end   = EXCLUDED.window_end,
                    about_terms  = EXCLUDED.about_terms,
                    config       = EXCLUDED.config
                RETURNING id
                """,
                (cfg.slug, cfg.subject_name, cfg.subject_role, cfg.description,
                 cfg.window_start, cfg.window_end, cfg.about_terms, _json(cfg.raw)),
            )
            case_id = cur.fetchone()["id"]

            for acct in cfg.accounts:
                cur.execute(
                    """
                    INSERT INTO subject_accounts
                        (case_id, platform, handle, platform_user_id, role,
                         valid_from, valid_until, source_url, note)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (case_id, platform, handle) DO UPDATE SET
                        platform_user_id = COALESCE(EXCLUDED.platform_user_id,
                                                    subject_accounts.platform_user_id),
                        role        = EXCLUDED.role,
                        valid_from  = EXCLUDED.valid_from,
                        valid_until = EXCLUDED.valid_until,
                        source_url  = EXCLUDED.source_url,
                        note        = EXCLUDED.note
                    """,
                    (case_id, acct.platform, acct.handle, acct.platform_user_id,
                     acct.role, acct.valid_from, acct.valid_until,
                     acct.source_url, acct.note),
                )
        self.conn.commit()
        return case_id

    def get_case_id(self, slug: str) -> Optional[UUID]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT id FROM cases WHERE slug = %s", (slug,))
            row = cur.fetchone()
        return row["id"] if row else None

    def load_subject_accounts(self, case_id: UUID, platform: str) -> list[SubjectAccount]:
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT platform, handle, platform_user_id, role,
                          valid_from, valid_until, source_url, note
                   FROM subject_accounts WHERE case_id = %s AND platform = %s""",
                (case_id, platform),
            )
            rows = cur.fetchall()
        return [
            SubjectAccount(
                platform=r["platform"], handle=r["handle"],
                platform_user_id=r["platform_user_id"], role=r["role"],
                valid_from=r["valid_from"], valid_until=r["valid_until"],
                source_url=r["source_url"], note=r["note"],
            )
            for r in rows
        ]

    def learn_account_id(self, case_id: UUID, platform: str, handle: str,
                         platform_user_id: str) -> None:
        """Backfill a registered account's user_id the first time we see it."""
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE subject_accounts SET platform_user_id = %s
                   WHERE case_id = %s AND platform = %s AND handle = %s
                     AND platform_user_id IS NULL""",
                (platform_user_id, case_id, platform, handle.lstrip("@").lower()),
            )
        self.conn.commit()

    # -- runs --------------------------------------------------------------

    def start_run(self, case_id: UUID, platform: str, tier: str, actor_id: str,
                  query: str, slice_start: datetime, slice_end: datetime,
                  max_items: int) -> UUID:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO collection_runs
                      (case_id, platform, tier, actor_id, query,
                       slice_start, slice_end, max_items, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'running')
                   RETURNING id""",
                (case_id, platform, tier, actor_id, query,
                 slice_start, slice_end, max_items),
            )
            run_id = cur.fetchone()["id"]
        self.conn.commit()
        return run_id

    def finish_run(self, run_id: UUID, *, status: str, items_returned: int = 0,
                   items_inserted: int = 0, est_cost_usd: float = 0.0,
                   external_run_id: Optional[str] = None,
                   error: Optional[str] = None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE collection_runs
                      SET status = %s, items_returned = %s, items_inserted = %s,
                          est_cost_usd = %s, external_run_id = %s, error = %s,
                          finished_at = now()
                    WHERE id = %s""",
                (status, items_returned, items_inserted, est_cost_usd,
                 external_run_id, error, run_id),
            )
        self.conn.commit()

    def spent_usd(self, case_id: UUID) -> float:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(est_cost_usd), 0) AS s FROM collection_runs WHERE case_id = %s",
                (case_id,),
            )
            return float(cur.fetchone()["s"])

    def succeeded_queries(self, case_id: UUID, platform: str) -> set[str]:
        """Queries already collected successfully — skipped on re-run."""
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT query FROM collection_runs
                   WHERE case_id = %s AND platform = %s AND status = 'succeeded'""",
                (case_id, platform),
            )
            return {r["query"] for r in cur.fetchall()}

    # -- content -----------------------------------------------------------

    def upsert_author(self, item: ContentItem) -> Optional[UUID]:
        a = item.author
        if a is None or not a.platform_user_id:
            return None
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO authors (platform, platform_user_id, handle, display_name,
                                     description, account_created_at, followers_count,
                                     following_count, post_count, is_verified,
                                     is_blue_verified, profile_image_url, raw)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (platform, platform_user_id) DO UPDATE SET
                    handle            = COALESCE(EXCLUDED.handle, authors.handle),
                    display_name      = COALESCE(EXCLUDED.display_name, authors.display_name),
                    description       = COALESCE(EXCLUDED.description, authors.description),
                    followers_count   = COALESCE(EXCLUDED.followers_count, authors.followers_count),
                    following_count   = COALESCE(EXCLUDED.following_count, authors.following_count),
                    post_count        = COALESCE(EXCLUDED.post_count, authors.post_count),
                    is_verified       = COALESCE(EXCLUDED.is_verified, authors.is_verified),
                    is_blue_verified  = COALESCE(EXCLUDED.is_blue_verified, authors.is_blue_verified),
                    profile_image_url = COALESCE(EXCLUDED.profile_image_url, authors.profile_image_url),
                    last_seen_at      = now(),
                    raw               = EXCLUDED.raw
                RETURNING id
                """,
                (a.platform, str(a.platform_user_id), a.handle, a.display_name,
                 a.description, a.account_created_at, a.followers_count,
                 a.following_count, a.post_count, a.is_verified,
                 a.is_blue_verified, a.profile_image_url, _json(a.raw)),
            )
            return cur.fetchone()["id"]

    def upsert_item(self, case_id: UUID, item: ContentItem, attribution: str,
                    reason: str, run_id: Optional[UUID],
                    mentions_subject: Optional[bool]) -> bool:
        """Returns True if a new row was inserted (False if it already existed)."""
        author_id = self.upsert_author(item)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO content_items
                    (case_id, platform, platform_item_id, author_id, attribution,
                     attribution_reason, posted_at, url, text, lang,
                     like_count, reply_count, repost_count, quote_count,
                     view_count, bookmark_count, is_reply, is_repost, is_quote,
                     parent_item_id, conversation_id, mentions_subject,
                     collection_run_id, raw)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (case_id, platform, platform_item_id) DO UPDATE SET
                    like_count    = EXCLUDED.like_count,
                    reply_count   = EXCLUDED.reply_count,
                    repost_count  = EXCLUDED.repost_count,
                    quote_count   = EXCLUDED.quote_count,
                    view_count    = EXCLUDED.view_count,
                    attribution   = EXCLUDED.attribution,
                    attribution_reason = EXCLUDED.attribution_reason
                RETURNING (xmax = 0) AS inserted
                """,
                (case_id, item.platform, item.platform_item_id, author_id,
                 attribution, reason, item.posted_at, item.url, item.text, item.lang,
                 item.like_count, item.reply_count, item.repost_count,
                 item.quote_count, item.view_count, item.bookmark_count,
                 item.is_reply, item.is_repost, item.is_quote,
                 item.parent_item_id, item.conversation_id, mentions_subject,
                 run_id, _json(item.raw)),
            )
            inserted = bool(cur.fetchone()["inserted"])
            content_item_id = None
            if item.media:
                cur.execute(
                    """SELECT id FROM content_items
                       WHERE case_id=%s AND platform=%s AND platform_item_id=%s""",
                    (case_id, item.platform, item.platform_item_id),
                )
                content_item_id = cur.fetchone()["id"]
                for m in item.media:
                    cur.execute(
                        """INSERT INTO media_assets
                               (content_item_id, media_type, source_url,
                                width, height, duration_sec, raw)
                           VALUES (%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (content_item_id, source_url) DO NOTHING""",
                        (content_item_id, m.media_type, m.source_url,
                         m.width, m.height, m.duration_sec, _json(m.raw)),
                    )
        return inserted

    # -- local -> cloud promotion -----------------------------------------

    #: copied in FK order; primary keys are carried across unchanged so the
    #: foreign keys need no remapping.
    _COPY_ORDER = [
        ("cases", "id = %(case_id)s"),
        ("subject_accounts", "case_id = %(case_id)s"),
        ("collection_runs", "case_id = %(case_id)s"),
        ("authors", """id IN (SELECT DISTINCT author_id FROM content_items
                              WHERE case_id = %(case_id)s AND author_id IS NOT NULL)"""),
        ("content_items", "case_id = %(case_id)s"),
        ("media_assets", """content_item_id IN (SELECT id FROM content_items
                                                WHERE case_id = %(case_id)s)"""),
    ]

    def copy_case_to(self, target: "Database", case_id: UUID,
                     batch_size: int = 500) -> dict[str, int]:
        """Copy one case and everything hanging off it into another database.

        Idempotent: rows that already exist on the target are left alone, so
        re-running after adding more local data only moves the new rows.
        """
        moved: dict[str, int] = {}
        for table, where in self._COPY_ORDER:
            with self.conn.cursor() as src:
                src.execute(f"SELECT * FROM {table} WHERE {where}", {"case_id": case_id})
                cols = [d.name for d in src.description]
                collist = ", ".join(f'"{c}"' for c in cols)
                placeholders = ", ".join(["%s"] * len(cols))
                sql = (f"INSERT INTO {table} ({collist}) VALUES ({placeholders}) "
                       f"ON CONFLICT DO NOTHING")
                n = 0
                while True:
                    rows = src.fetchmany(batch_size)
                    if not rows:
                        break
                    payload = [tuple(_as_param(r[c]) for c in cols) for r in rows]
                    with target.conn.cursor() as dst:
                        dst.executemany(sql, payload)
                        # ON CONFLICT DO NOTHING means rowcount is the number
                        # actually written, not the number offered.
                        n += max(dst.rowcount, 0)
                    target.conn.commit()
            moved[table] = n
        return moved

    # -- reporting ---------------------------------------------------------

    def summary(self, case_id: UUID) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT platform, attribution, count(*) AS n,
                          min(posted_at) AS earliest, max(posted_at) AS latest
                     FROM content_items WHERE case_id = %s
                    GROUP BY platform, attribution ORDER BY platform, n DESC""",
                (case_id,),
            )
            return cur.fetchall()

    def export_jsonl(self, case_id: UUID, path: str | Path) -> int:
        with self.conn.cursor() as cur, open(path, "w", encoding="utf-8") as fh:
            cur.execute(
                """SELECT ci.platform, ci.platform_item_id, ci.attribution,
                          ci.attribution_reason, ci.posted_at, ci.url, ci.text,
                          ci.lang, ci.like_count, ci.repost_count, ci.reply_count,
                          ci.view_count, a.handle AS author_handle,
                          a.platform_user_id AS author_id, a.followers_count
                     FROM content_items ci
                     LEFT JOIN authors a ON a.id = ci.author_id
                    WHERE ci.case_id = %s ORDER BY ci.posted_at""",
                (case_id,),
            )
            n = 0
            for row in cur:
                row["posted_at"] = row["posted_at"].isoformat() if row["posted_at"] else None
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                n += 1
        return n
