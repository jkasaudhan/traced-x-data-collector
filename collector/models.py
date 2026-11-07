"""Canonical, platform-agnostic data model.

Every platform adapter normalises into these dataclasses. Nothing downstream
(attribution, storage, reporting) ever sees a platform-specific field name.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class Author:
    platform: str
    platform_user_id: str
    handle: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    account_created_at: Optional[datetime] = None
    followers_count: Optional[int] = None
    following_count: Optional[int] = None
    post_count: Optional[int] = None
    is_verified: Optional[bool] = None
    is_blue_verified: Optional[bool] = None
    profile_image_url: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Media:
    source_url: str
    media_type: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration_sec: Optional[float] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContentItem:
    platform: str
    platform_item_id: str
    author: Optional[Author] = None
    posted_at: Optional[datetime] = None
    url: Optional[str] = None
    text: Optional[str] = None
    lang: Optional[str] = None

    like_count: Optional[int] = None
    reply_count: Optional[int] = None
    repost_count: Optional[int] = None
    quote_count: Optional[int] = None
    view_count: Optional[int] = None
    bookmark_count: Optional[int] = None

    is_reply: bool = False
    is_repost: bool = False
    is_quote: bool = False
    parent_item_id: Optional[str] = None
    conversation_id: Optional[str] = None

    media: list[Media] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class QuerySpec:
    """One unit of collection work: a query over a time slice, with a cap."""
    tier: str                      # 'subject_own' | 'mentions' | 'claim'
    query: str                     # platform-native query string
    slice_start: datetime
    slice_end: datetime
    max_items: int
    note: str = ""
    sort: Optional[str] = None     # platform search mode, e.g. Latest | Top
