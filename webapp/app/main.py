"""Evidence viewer: browse, search and compare what the subject really posted
against what other accounts posted about them.

    cd webapp && .venv/bin/python -m app.main            # local Postgres
    cd webapp && .venv/bin/python -m app.main --db cloud # Supabase

Read-only: every query is a SELECT and the connections are opened read-only.
"""
from __future__ import annotations

import argparse
import os
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db

STATIC = Path(__file__).resolve().parents[1] / "static"

# The subject's genuine accounts: personal (subject_own) and office/campaign
# (subject_affiliated). Everything else is "someone other than the subject",
# which is NOT the same as false — the viewer says so in the UI.
REAL = ("subject_own", "subject_affiliated")

Side = Literal["all", "real", "others"]
Sort = Literal["recent", "oldest", "likes", "views", "reposts", "replies"]
MediaFilter = Literal["any", "with", "photo", "video", "none"]

SORT_SQL = {
    "recent": "ci.posted_at DESC NULLS LAST",
    "oldest": "ci.posted_at ASC NULLS LAST",
    "likes": "ci.like_count DESC NULLS LAST",
    "views": "ci.view_count DESC NULLS LAST",
    "reposts": "ci.repost_count DESC NULLS LAST",
    "replies": "ci.reply_count DESC NULLS LAST",
}

# One item, with its author and media folded in as JSON so the list endpoint
# is a single round trip.
ITEM_SELECT = """
SELECT
    ci.id, ci.platform::text AS platform, ci.platform_item_id,
    ci.attribution::text AS attribution, ci.attribution_reason,
    ci.posted_at, ci.url, ci.text, ci.lang,
    ci.like_count, ci.reply_count, ci.repost_count, ci.quote_count,
    ci.view_count, ci.bookmark_count,
    ci.is_reply, ci.is_repost, ci.is_quote, ci.parent_item_id, ci.conversation_id,
    ci.mentions_subject,
    jsonb_build_object(
        'id', a.id, 'handle', a.handle, 'name', a.display_name,
        'description', a.description, 'followers', a.followers_count,
        'following', a.following_count, 'verified', a.is_verified,
        'avatar', a.profile_image_url, 'platform_user_id', a.platform_user_id
    ) AS author,
    COALESCE((
        SELECT jsonb_agg(jsonb_build_object(
                   'type', ma.media_type, 'url', ma.source_url,
                   'poster', ma.raw->>'poster') ORDER BY ma.source_url)
        FROM media_assets ma WHERE ma.content_item_id = ci.id
    ), '[]'::jsonb) AS media
FROM content_items ci
LEFT JOIN authors a ON a.id = ci.author_id
"""


def _case_id(slug: Optional[str]) -> UUID:
    row = (db.fetch_one("SELECT id FROM cases WHERE slug = %s", (slug,)) if slug
           else db.fetch_one("SELECT id FROM cases ORDER BY created_at LIMIT 1"))
    if not row:
        raise HTTPException(404, "case not found")
    return row["id"]


def _side_clause(side: Side) -> tuple[str, list[Any]]:
    if side == "real":
        return "ci.attribution = ANY(%s::attribution_t[])", [list(REAL)]
    if side == "others":
        return "NOT (ci.attribution = ANY(%s::attribution_t[]))", [list(REAL)]
    return "TRUE", []


def _filters(case_id: UUID, *, side: Side, q: Optional[str], start: Optional[date],
             end: Optional[date], media: MediaFilter, author: Optional[str],
             attribution: Optional[str], kind: Optional[str]) -> tuple[str, list[Any]]:
    where = ["ci.case_id = %s"]
    params: list[Any] = [case_id]

    clause, p = _side_clause(side)
    where.append(clause)
    params += p

    if attribution:
        where.append("ci.attribution = %s::attribution_t")
        params.append(attribution)
    if q:
        # Plain substring search over text and author; pg_trgm indexes it.
        where.append("(ci.text ILIKE %s OR a.handle ILIKE %s OR a.display_name ILIKE %s)")
        like = f"%{q.strip()}%"
        params += [like, like, like]
    if start:
        where.append("ci.posted_at >= %s")
        params.append(start)
    if end:
        where.append("ci.posted_at < %s")
        params.append(end + timedelta(days=1))   # end date is inclusive
    if author:
        try:
            where.append("ci.author_id = %s")
            params.append(UUID(author))
        except ValueError:
            where[-1] = "lower(a.handle) = lower(%s)"
            params.append(author.lstrip("@"))
    if kind == "original":
        where.append("NOT ci.is_reply AND NOT ci.is_quote")
    elif kind == "reply":
        where.append("ci.is_reply")
    elif kind == "quote":
        where.append("ci.is_quote")

    has_media = "EXISTS (SELECT 1 FROM media_assets m WHERE m.content_item_id = ci.id{})"
    if media == "with":
        where.append(has_media.format(""))
    elif media == "photo":
        where.append(has_media.format(" AND m.media_type = 'photo'"))
    elif media == "video":
        where.append(has_media.format(" AND m.media_type IN ('video','animated_gif')"))
    elif media == "none":
        where.append("NOT " + has_media.format(""))
    return " AND ".join(where), params


def _shape(row: dict[str, Any]) -> dict[str, Any]:
    row["is_real"] = row["attribution"] in REAL
    a = row.get("author") or {}
    if a.get("avatar"):
        # _normal is 48px; _bigger is 73px and looks right on retina.
        a["avatar"] = a["avatar"].replace("_normal.", "_bigger.")
    if not row.get("url") and a.get("handle"):
        row["url"] = f"https://x.com/{a['handle']}/status/{row['platform_item_id']}"
    return row


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(_: FastAPI):
    app.state.db_host = db.init_pool(os.getenv("WEBAPP_DB"))
    yield
    db.close_pool()


app = FastAPI(title="Evidence Viewer", lifespan=lifespan)


@app.get("/api/meta")
def meta(case: Optional[str] = None) -> dict[str, Any]:
    cid = _case_id(case)
    c = db.fetch_one(
        """SELECT slug, subject_name, subject_role, description,
                  window_start, window_end FROM cases WHERE id = %s""", (cid,))
    accounts = db.fetch_all(
        """SELECT sa.platform::text AS platform, sa.handle, sa.role::text AS role,
                  sa.platform_user_id, sa.valid_from, sa.valid_until, sa.source_url,
                  a.display_name, a.followers_count, a.profile_image_url AS avatar
           FROM subject_accounts sa
           LEFT JOIN authors a ON a.platform = sa.platform
                              AND a.platform_user_id = sa.platform_user_id
           WHERE sa.case_id = %s ORDER BY sa.role, sa.handle""", (cid,))
    for acct in accounts:
        if acct.get("avatar"):
            acct["avatar"] = acct["avatar"].replace("_normal.", "_bigger.")
    by_attr = db.fetch_all(
        """SELECT attribution::text AS attribution, count(*)::int AS n,
                  min(posted_at) AS first, max(posted_at) AS last
           FROM content_items WHERE case_id = %s GROUP BY 1 ORDER BY 2 DESC""", (cid,))
    totals = db.fetch_one(
        """SELECT count(*)::int AS items,
                  count(DISTINCT author_id)::int AS authors,
                  min(posted_at) AS first, max(posted_at) AS last,
                  COALESCE(sum(view_count), 0)::bigint AS views
           FROM content_items WHERE case_id = %s""", (cid,))
    media = db.fetch_all(
        """SELECT ma.media_type AS type, count(*)::int AS n
           FROM media_assets ma JOIN content_items ci ON ci.id = ma.content_item_id
           WHERE ci.case_id = %s GROUP BY 1 ORDER BY 2 DESC""", (cid,))
    real = sum(r["n"] for r in by_attr if r["attribution"] in REAL)
    return {
        "case": c, "accounts": accounts, "by_attribution": by_attr,
        "totals": {**totals, "real": real, "others": totals["items"] - real},
        "media": media, "database": app.state.db_host,
    }


@app.get("/api/timeline")
def timeline(case: Optional[str] = None, q: Optional[str] = None) -> list[dict[str, Any]]:
    cid = _case_id(case)
    where, params = _filters(cid, side="all", q=q, start=None, end=None,
                             media="any", author=None, attribution=None, kind=None)
    return db.fetch_all(
        f"""SELECT (ci.posted_at AT TIME ZONE 'UTC')::date AS day,
                   count(*) FILTER (WHERE ci.attribution = ANY(%s::attribution_t[]))::int AS real,
                   count(*) FILTER (WHERE NOT ci.attribution = ANY(%s::attribution_t[]))::int AS others
            FROM content_items ci LEFT JOIN authors a ON a.id = ci.author_id
            WHERE {where} AND ci.posted_at IS NOT NULL
            GROUP BY 1 ORDER BY 1""",
        [list(REAL), list(REAL), *params])


@app.get("/api/items")
def items(
    case: Optional[str] = None,
    side: Side = "all",
    q: Optional[str] = None,
    start: Optional[date] = None,
    end: Optional[date] = None,
    media: MediaFilter = "any",
    author: Optional[str] = None,
    attribution: Optional[str] = None,
    kind: Optional[Literal["original", "reply", "quote"]] = None,
    sort: Sort = "recent",
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    cid = _case_id(case)
    where, params = _filters(cid, side=side, q=q, start=start, end=end, media=media,
                             author=author, attribution=attribution, kind=kind)
    total = db.fetch_one(
        f"SELECT count(*)::int AS n FROM content_items ci "
        f"LEFT JOIN authors a ON a.id = ci.author_id WHERE {where}", params)["n"]
    rows = db.fetch_all(
        f"{ITEM_SELECT} WHERE {where} ORDER BY {SORT_SQL[sort]}, ci.id LIMIT %s OFFSET %s",
        [*params, limit, offset])
    return {"total": total, "items": [_shape(r) for r in rows]}


@app.get("/api/items/{item_id}")
def item(item_id: UUID) -> dict[str, Any]:
    row = db.fetch_one(f"{ITEM_SELECT} WHERE ci.id = %s", (item_id,))
    if not row:
        raise HTTPException(404, "item not found")
    row = _shape(row)
    raw = db.fetch_one("SELECT raw FROM content_items WHERE id = %s", (item_id,))
    row["raw"] = raw["raw"] if raw else None

    # What this post replies to or quotes, if we collected it.
    parent = None
    if row["parent_item_id"]:
        p = db.fetch_one(
            f"{ITEM_SELECT} WHERE ci.platform_item_id = %s AND ci.case_id = "
            f"(SELECT case_id FROM content_items WHERE id = %s)",
            (row["parent_item_id"], item_id))
        parent = _shape(p) if p else None

    # Other posts in the same thread, or quoting this one. For a real post
    # this is "how other accounts reacted to it".
    reactions = [_shape(r) for r in db.fetch_all(
        f"""{ITEM_SELECT}
            WHERE ci.case_id = (SELECT case_id FROM content_items WHERE id = %s)
              AND ci.id <> %s
              AND (ci.parent_item_id = %s
                   OR (%s::text IS NOT NULL AND ci.conversation_id = %s))
            ORDER BY ci.like_count DESC NULLS LAST LIMIT 30""",
        (item_id, item_id, row["platform_item_id"],
         row["conversation_id"], row["conversation_id"]))]
    return {"item": row, "parent": parent, "reactions": reactions}


@app.get("/api/top-authors")
def top_authors(case: Optional[str] = None, side: Side = "others",
                limit: int = Query(10, ge=1, le=50)) -> list[dict[str, Any]]:
    cid = _case_id(case)
    clause, p = _side_clause(side)
    rows = db.fetch_all(
        f"""SELECT a.id, a.handle, a.display_name AS name, a.followers_count AS followers,
                   a.is_verified AS verified, a.profile_image_url AS avatar,
                   count(*)::int AS posts,
                   COALESCE(sum(ci.like_count), 0)::bigint AS likes,
                   COALESCE(sum(ci.view_count), 0)::bigint AS views
            FROM content_items ci JOIN authors a ON a.id = ci.author_id
            WHERE ci.case_id = %s AND {clause}
            GROUP BY a.id ORDER BY views DESC LIMIT %s""",
        [cid, *p, limit])
    for r in rows:
        if r.get("avatar"):
            r["avatar"] = r["avatar"].replace("_normal.", "_bigger.")
    return rows


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", choices=["local", "cloud"], default=None)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()
    if args.db:
        os.environ["WEBAPP_DB"] = args.db
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
