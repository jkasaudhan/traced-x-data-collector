"""Twitter / X adapter backed by Apify actors.

Why advanced-search slicing instead of profile scraping: X caps a user
timeline at roughly the last 3,200 posts, but `from:handle since:A until:B`
has no such ceiling, so a long back-window is collected slice by slice.

Two actors are supported out of the box; pick one in the case YAML.

  jacquemus/x-tweet-scraper   ~$0.10 / 1,000 results, pay-per-event
  apidojo/tweet-scraper       ~$0.40 / 1,000 tweets, minimum 50 per query,
                              API blocked on the Apify Free plan

Prices change — confirm on the actor page and set `price_per_1k` in the YAML
so the budget guard stays honest.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional

from apify_client import ApifyClient
from dateutil import parser as dateparser

from ..config import CaseConfig
from ..models import Author, ContentItem, Media, QuerySpec
from .base import PlatformAdapter, register

log = logging.getLogger(__name__)

DEFAULT_ACTOR = "jacquemus/x-tweet-scraper"
DEFAULT_PRICE_PER_1K = 0.10


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Actors disagree on field names; take the first key that is present."""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _to_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_dt(v: Any) -> Optional[datetime]:
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        # handles both ISO-8601 and Twitter's "Tue Nov 25 12:00:00 +0000 2025"
        dt = dateparser.parse(str(v))
    except (ValueError, OverflowError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def _slices(start: datetime, end: datetime, mode: str) -> list[tuple[datetime, datetime]]:
    """Cut the window into '<n>h', day, week or month slices (UTC)."""
    out: list[tuple[datetime, datetime]] = []
    cur = start
    while cur < end:
        if mode.endswith("h") and mode[:-1].isdigit():
            nxt = cur + timedelta(hours=int(mode[:-1]))
        elif mode == "day":
            nxt = cur + timedelta(days=1)
        elif mode == "week":
            nxt = cur + timedelta(days=7)
        else:
            # first day of next month
            nxt = (cur.replace(day=1) + timedelta(days=32)).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
        out.append((cur, min(nxt, end)))
        cur = nxt
    return out


# ---------------------------------------------------------------------------
# adapter
# ---------------------------------------------------------------------------

@register
class TwitterApifyAdapter(PlatformAdapter):
    platform = "twitter"

    def __init__(self, token: str, options: dict[str, Any] | None = None):
        super().__init__(token, options)
        self.actor_id = self.options.get("actor_id", DEFAULT_ACTOR)
        self.price_per_1k = float(self.options.get("price_per_1k", DEFAULT_PRICE_PER_1K))
        self.sort = self.options.get("sort", "Latest")
        self.language = self.options.get("language")
        self.client = ApifyClient(token)
        self._last_run_id: Optional[str] = None

    # -- planning ---------------------------------------------------------

    def plan(self, case: CaseConfig) -> list[QuerySpec]:
        specs: list[QuerySpec] = []
        handles = [a.handle for a in case.accounts_for("twitter")]

        # Tier 1 — everything the subject's own/affiliated accounts posted.
        tier = case.tiers.get("subject_own")
        if tier and tier.enabled and handles:
            for s, e in _slices(case.window_start, case.window_end, tier.slice):
                for acct in case.accounts_for("twitter"):
                    # An office handle belongs to someone else outside its
                    # validity window: do not pay to collect those posts.
                    if acct.role == "known_impersonator":
                        continue
                    if (acct.valid_from and e <= acct.valid_from) or \
                       (acct.valid_until and s > acct.valid_until):
                        continue
                    s2 = max(s, acct.valid_from) if acct.valid_from else s
                    h = acct.handle
                    specs.append(QuerySpec(
                        tier="subject_own",
                        query=self._q(f"from:{h}", s2, e, tier),
                        slice_start=s2, slice_end=e,
                        max_items=tier.max_items_per_slice,
                        sort=tier.sort,
                        note=f"{acct.role} account @{h}",
                    ))

        # Tier 2 — what everyone else said. Unbounded, so engagement-floored.
        tier = case.tiers.get("mentions")
        if tier and tier.enabled:
            terms = tier.queries or [self._default_mentions_term(case, handles)]
            for s, e in _slices(case.window_start, case.window_end, tier.slice):
                for t in terms:
                    specs.append(QuerySpec(
                        tier="mentions",
                        query=self._q(t, s, e, tier),
                        slice_start=s, slice_end=e,
                        max_items=tier.max_items_per_slice,
                        sort=tier.sort,
                        note="third-party mentions",
                    ))

        # Tier 3 — targeted queries for specific claims you already know about.
        tier = case.tiers.get("claims")
        if tier and tier.enabled:
            for s, e in _slices(case.window_start, case.window_end, tier.slice):
                for t in tier.queries:
                    specs.append(QuerySpec(
                        tier="claims",
                        query=self._q(t, s, e, tier),
                        slice_start=s, slice_end=e,
                        max_items=tier.max_items_per_slice,
                        sort=tier.sort,
                        note="known claim cluster",
                    ))
        return specs

    @staticmethod
    def _default_mentions_term(case: CaseConfig, handles: list[str]) -> str:
        parts = [f'"{t}"' if " " in t else t for t in case.about_terms]
        parts += [f"@{h}" for h in handles]
        joined = " OR ".join(parts) or case.subject_name
        return f"({joined}) -filter:retweets"

    def _q(self, term: str, start: datetime, end: datetime, tier: Any) -> str:
        # since_time/until_time are exact Unix seconds (until exclusive). The
        # date-level since:/until: operators were observed to leak a day past
        # `until`, which would put posts in the wrong slice or outside the case.
        q = f"{term} since_time:{int(start.timestamp())} until_time:{int(end.timestamp())}"
        if tier.exclude_reposts and "-filter:retweets" not in term:
            q += " -filter:retweets"
        if tier.min_faves:
            q += f" min_faves:{tier.min_faves}"
        if self.language:
            q += f" lang:{self.language}"
        return q

    # -- fetching ---------------------------------------------------------

    def _actor_input(self, spec: QuerySpec) -> dict[str, Any]:
        base: dict[str, Any] = {
            "searchTerms": [spec.query],
            "maxItems": spec.max_items,
        }
        sort = spec.sort or self.sort
        if self.actor_id.startswith("apidojo/"):
            base["sort"] = sort
        else:
            base["searchMode"] = sort
        base.update(self.options.get("extra_input", {}))
        return base

    def fetch(self, spec: QuerySpec) -> Iterator[ContentItem]:
        run = self.client.actor(self.actor_id).call(
            run_input=self._actor_input(spec),
            timeout_secs=self.options.get("timeout_secs", 900),
        )
        self._last_run_id = run.get("id")
        dataset_id = run.get("defaultDatasetId")
        if not dataset_id:
            log.warning("actor run returned no dataset: %s", run.get("status"))
            return
        for raw in self.client.dataset(dataset_id).iterate_items():
            item = self.normalise(raw)
            if item is not None:
                yield item

    def last_external_run_id(self) -> Optional[str]:
        return self._last_run_id

    # -- normalisation ----------------------------------------------------

    @staticmethod
    def normalise(raw: dict[str, Any]) -> Optional[ContentItem]:
        """Map one actor row onto the canonical ContentItem. Tolerant by design:
        actor output schemas drift, so every field is looked up under aliases."""
        if not isinstance(raw, dict):
            return None
        # Some actors emit non-tweet rows (errors, profiles) into the dataset.
        if raw.get("type") in {"mock_tweet", "error"} or raw.get("error"):
            return None

        item_id = _first(raw, "id", "id_str", "tweetId", "rest_id")
        if not item_id:
            return None

        a = _first(raw, "author", "user", default={}) or {}
        author = None
        author_uid = _first(a, "id", "id_str", "userId", "rest_id")
        if author_uid or _first(a, "userName", "screen_name", "username"):
            author = Author(
                platform="twitter",
                platform_user_id=str(author_uid) if author_uid else "",
                handle=(_first(a, "userName", "screen_name", "username") or "").lstrip("@").lower() or None,
                display_name=_first(a, "name", "displayName"),
                description=_first(a, "description", "bio"),
                account_created_at=_to_dt(_first(a, "createdAt", "created_at")),
                followers_count=_to_int(_first(a, "followers", "followersCount", "followers_count")),
                following_count=_to_int(_first(a, "following", "followingCount", "friends_count")),
                post_count=_to_int(_first(a, "statusesCount", "statuses_count", "tweetsCount")),
                is_verified=_first(a, "isVerified", "verified"),
                is_blue_verified=_first(a, "isBlueVerified", "is_blue_verified"),
                profile_image_url=_first(a, "profilePicture", "profileImageUrl",
                                         "profile_image_url_https"),
                raw=a,
            )

        media: list[Media] = []
        ent = _first(raw, "extendedEntities", "entities", default={}) or {}
        # jacquemus/x-tweet-scraper puts [{type, url, poster?}] at the top level.
        top = raw.get("media") if isinstance(raw.get("media"), list) else []
        for m in (ent.get("media") or top or []):
            url = _first(m, "media_url_https", "media_url", "url")
            if url:
                sizes = (m.get("original_info") or {})
                media.append(Media(
                    source_url=url,
                    media_type=m.get("type"),
                    width=_to_int(sizes.get("width")),
                    height=_to_int(sizes.get("height")),
                    raw=m,
                ))

        return ContentItem(
            platform="twitter",
            platform_item_id=str(item_id),
            author=author,
            posted_at=_to_dt(_first(raw, "createdAt", "created_at", "date")),
            url=_first(raw, "url", "twitterUrl", "tweetUrl"),
            text=_first(raw, "text", "full_text", "fullText", "content"),
            lang=_first(raw, "lang", "language"),
            like_count=_to_int(_first(raw, "likeCount", "favorite_count", "favoriteCount")),
            reply_count=_to_int(_first(raw, "replyCount", "reply_count")),
            repost_count=_to_int(_first(raw, "retweetCount", "retweet_count")),
            quote_count=_to_int(_first(raw, "quoteCount", "quote_count")),
            view_count=_to_int(_first(raw, "viewCount", "views", "view_count")),
            bookmark_count=_to_int(_first(raw, "bookmarkCount", "bookmark_count")),
            is_reply=bool(_first(raw, "isReply", "is_reply", default=False)),
            is_repost=bool(_first(raw, "isRetweet", "is_retweet", default=False)),
            is_quote=bool(_first(raw, "isQuote", "isQuoteStatus", "is_quote_status",
                                 default=False)),
            parent_item_id=(lambda v: str(v) if v else None)(
                _first(raw, "inReplyToId", "in_reply_to_status_id_str",
                       "quotedTweetId", "retweetedTweetId")),
            conversation_id=(lambda v: str(v) if v else None)(
                _first(raw, "conversationId", "conversation_id")),
            media=media,
            raw=raw,
        )
