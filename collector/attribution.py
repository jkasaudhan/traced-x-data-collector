"""Decide whether an item came from the subject's own account or someone else's.

Order of checks (first match wins), each returning an auditable reason string:

  1. platform_user_id matches a registered account, and the post falls inside
     that account's validity window        -> subject_own / subject_affiliated
  2. handle matches a registered account (ID unknown on either side)
     -> same, but the reason records that it was a handle match (weaker)
  3. handle is confusingly similar to a registered handle but the ID does not
     match                                  -> impersonation_suspect
  4. author resolved, no match              -> third_party
  5. no author                              -> unknown

Point 1 beats point 2 because handles are reassignable; point 3 exists because
the whole downstream product is about impersonation.
"""
from __future__ import annotations

import difflib
import re
from datetime import datetime
from typing import Iterable, Optional

from .config import SubjectAccount
from .models import ContentItem

# Roles that count as "the subject speaking", vs. the subject's organisation.
_OWN_ROLES = {"personal"}
_AFFILIATED_ROLES = {"campaign", "office", "staff"}

# How close a handle must be before we call it a possible impersonation.
SIMILARITY_THRESHOLD = 0.86


def _normalise_handle(handle: Optional[str]) -> str:
    if not handle:
        return ""
    return re.sub(r"[^a-z0-9]", "", handle.lstrip("@").lower())


def _in_window(acct: SubjectAccount, when: Optional[datetime]) -> bool:
    """An office handle only belongs to the subject while they hold the office."""
    if when is None:
        return True
    if acct.valid_from and when < acct.valid_from:
        return False
    if acct.valid_until and when > acct.valid_until:
        return False
    return True


def _attribution_for_role(role: str) -> str:
    if role in _OWN_ROLES:
        return "subject_own"
    if role in _AFFILIATED_ROLES:
        return "subject_affiliated"
    if role == "known_impersonator":
        return "impersonation_suspect"
    return "third_party"


def classify(item: ContentItem, accounts: Iterable[SubjectAccount]) -> tuple[str, str]:
    """Return (attribution, reason)."""
    accounts = list(accounts)
    author = item.author

    if author is None or not (author.platform_user_id or author.handle):
        return "unknown", "no author data on item"

    author_id = str(author.platform_user_id) if author.platform_user_id else None
    author_handle = _normalise_handle(author.handle)

    # 1. authoritative ID match
    if author_id:
        for acct in accounts:
            if acct.platform_user_id and str(acct.platform_user_id) == author_id:
                if _in_window(acct, item.posted_at):
                    return (
                        _attribution_for_role(acct.role),
                        f"user_id match on registered {acct.role} account @{acct.handle}",
                    )
                return (
                    "third_party",
                    f"user_id matches @{acct.handle} but post is outside that "
                    f"account's validity window for this subject",
                )

    # 2. handle match
    for acct in accounts:
        if author_handle and author_handle == _normalise_handle(acct.handle):
            # If both IDs are known and they disagree, this is a red flag, not a match.
            if acct.platform_user_id and author_id and str(acct.platform_user_id) != author_id:
                return (
                    "impersonation_suspect",
                    f"handle @{author.handle} matches registered {acct.role} account "
                    f"but user_id {author_id} != registered {acct.platform_user_id}",
                )
            if _in_window(acct, item.posted_at):
                return (
                    _attribution_for_role(acct.role),
                    f"handle match on registered {acct.role} account @{acct.handle} "
                    f"(no user_id on file — weaker evidence)",
                )
            return (
                "third_party",
                f"handle matches @{acct.handle} but post is outside that account's "
                f"validity window for this subject",
            )

    # 3. look-alike handle
    if author_handle:
        for acct in accounts:
            target = _normalise_handle(acct.handle)
            if not target:
                continue
            ratio = difflib.SequenceMatcher(None, author_handle, target).ratio()
            contains = target in author_handle and author_handle != target
            if ratio >= SIMILARITY_THRESHOLD or contains:
                return (
                    "impersonation_suspect",
                    f"handle @{author.handle} resembles registered @{acct.handle} "
                    f"(similarity {ratio:.2f}) but is not a registered account",
                )

    return "third_party", "author resolved and not in the registered account list"
