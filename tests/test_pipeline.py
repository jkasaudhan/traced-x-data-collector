"""End-to-end test with a stubbed actor — no Apify calls, no spend.

    DATABASE_URL=... APIFY_TOKEN=x python -m pytest tests/ -q

Covers the parts that are easy to get silently wrong: field normalisation
across actor schemas, the office-handle validity window, look-alike handle
detection, user-id learning, and insert idempotency.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from collector.config import CaseConfig, Settings, SubjectAccount
from collector.db import Database
from collector.pipeline import collect
from collector.platforms import twitter_apify
from collector.platforms.twitter_apify import TwitterApifyAdapter

CASE = "cases/mamdani.yaml"

MAMDANI_UID = "1234567890"
OFFICE_UID = "9876543210"


def _tweet(tid, uid, handle, when, text, **kw):
    return {
        "type": "tweet",
        "id": tid,
        "url": f"https://x.com/{handle}/status/{tid}",
        "text": text,
        "createdAt": when,
        "lang": "en",
        "likeCount": kw.get("likes", 10),
        "retweetCount": 2,
        "replyCount": 1,
        "quoteCount": 0,
        "viewCount": 1000,
        "isReply": False,
        "isRetweet": False,
        "author": {
            "id": uid,
            "userName": handle,
            "name": kw.get("name", handle),
            "followers": kw.get("followers", 500),
            "isBlueVerified": kw.get("blue", False),
            "createdAt": "Mon Jan 02 10:00:00 +0000 2017",
        },
    }


FIXTURES = [
    # genuine personal account
    _tweet("1", MAMDANI_UID, "ZohranKMamdani",
           "Tue Nov 25 12:00:00 +0000 2025", "Freeze the rent."),
    # office handle BEFORE he took office -> belongs to the predecessor
    _tweet("2", OFFICE_UID, "NYCMayor",
           "Mon Nov 10 09:00:00 +0000 2025", "Statement from the Mayor."),
    # office handle AFTER inauguration -> affiliated
    _tweet("3", OFFICE_UID, "NYCMayor",
           "Sun Feb 01 09:00:00 +0000 2026", "City Hall update."),
    # look-alike handle
    _tweet("4", "5555", "ZohranKMamdanii",
           "Wed Nov 26 08:00:00 +0000 2025", "I am resigning. (fake)"),
    # ordinary third party, snake_case schema variant from a different actor
    {
        "id_str": "5",
        "full_text": "Mamdani held a press conference today.",
        "created_at": "Thu Nov 27 15:30:00 +0000 2025",
        "favorite_count": 44,
        "retweet_count": 9,
        "user": {"id_str": "7777", "screen_name": "somereporter",
                 "name": "Some Reporter", "followers_count": 12000},
        "entities": {"media": [{"type": "photo",
                                "media_url_https": "https://pbs.twimg.com/media/x.jpg",
                                "original_info": {"width": 1200, "height": 800}}]},
    },
]


@pytest.fixture()
def db():
    settings = Settings.load()
    d = Database(settings.database_url)
    d.init_schema("schema.sql")
    with d.conn.cursor() as cur:
        cur.execute("DELETE FROM cases WHERE slug = 'mamdani-nyc-2025'")
        cur.execute("DELETE FROM authors WHERE platform = 'twitter'")
    d.conn.commit()
    yield d
    d.close()


@pytest.fixture()
def stubbed(monkeypatch):
    """One slice only, returning the fixtures, so nothing hits the network."""
    monkeypatch.setattr(TwitterApifyAdapter, "fetch",
                        lambda self, spec: iter(
                            [TwitterApifyAdapter.normalise(r) for r in FIXTURES]
                            if spec.tier == "subject_own" and
                            spec.slice_start.month == 10 else []))
    monkeypatch.setattr(TwitterApifyAdapter, "last_external_run_id",
                        lambda self: "stub-run")


def test_normalise_handles_both_actor_schemas():
    camel = TwitterApifyAdapter.normalise(FIXTURES[0])
    snake = TwitterApifyAdapter.normalise(FIXTURES[4])
    assert camel.platform_item_id == "1"
    assert camel.author.handle == "zohrankmamdani"
    assert camel.like_count == 10
    assert snake.platform_item_id == "5"
    assert snake.author.handle == "somereporter"
    assert snake.like_count == 44
    assert snake.posted_at == datetime(2025, 11, 27, 15, 30, tzinfo=timezone.utc)
    assert len(snake.media) == 1 and snake.media[0].width == 1200


def test_non_tweet_rows_are_dropped():
    assert TwitterApifyAdapter.normalise({"type": "error", "id": "9"}) is None
    assert TwitterApifyAdapter.normalise({"text": "no id"}) is None


def test_attribution_and_idempotency(db, stubbed):
    cfg = CaseConfig.load(CASE)
    # The shipped case file deliberately registers only the confirmed personal
    # handle. Register the office account here so the validity-window branch
    # stays covered without the production config having to assert it.
    cfg.accounts.append(SubjectAccount(
        platform="twitter", handle="nycmayor", role="office",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
    ))
    case_id = db.upsert_case(cfg)

    r1 = collect(db, cfg, case_id, "twitter", "x", tiers=["subject_own"])
    assert r1.items_inserted == 5

    with db.conn.cursor() as cur:
        cur.execute("""SELECT platform_item_id, attribution, attribution_reason
                       FROM content_items WHERE case_id=%s ORDER BY platform_item_id""",
                    (case_id,))
        rows = {r["platform_item_id"]: r for r in cur.fetchall()}

    assert rows["1"]["attribution"] == "subject_own"
    # office handle before inauguration must NOT be credited to the subject
    assert rows["2"]["attribution"] == "third_party"
    assert "validity window" in rows["2"]["attribution_reason"]
    assert rows["3"]["attribution"] == "subject_affiliated"
    assert rows["4"]["attribution"] == "impersonation_suspect"
    assert rows["5"]["attribution"] == "third_party"

    # the numeric id of the personal account was learned and written back
    accts = {a.handle: a for a in db.load_subject_accounts(case_id, "twitter")}
    assert accts["zohrankmamdani"].platform_user_id == MAMDANI_UID

    # re-running inserts nothing new
    r2 = collect(db, cfg, case_id, "twitter", "x", tiers=["subject_own"])
    assert r2.items_returned == r1.items_returned and r2.items_inserted == 0


def test_budget_guard_stops_the_run(db, monkeypatch):
    cfg = CaseConfig.load(CASE)
    cfg.budget_usd = 0.0
    case_id = db.upsert_case(cfg)
    monkeypatch.setattr(TwitterApifyAdapter, "fetch",
                        lambda self, spec: iter([]))
    r = collect(db, cfg, case_id, "twitter", "x")
    assert r.stopped_on_budget and r.specs_executed == 0
