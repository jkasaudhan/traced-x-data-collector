"""Case configuration loading (YAML) + environment settings."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

load_dotenv()


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------

@dataclass
class Settings:
    database_url: str
    apify_token: str
    db_target: str = "local"

    @classmethod
    def load(cls, db_target: Optional[str] = None) -> "Settings":
        """Resolve which database to talk to.

        Precedence: explicit --db flag > DB_TARGET env var > 'local'.
        Cloud is never the default: writing to Supabase has to be a decision
        someone made, not something that happens because a var was exported.
        """
        target = (db_target or os.getenv("DB_TARGET") or "local").lower()
        if target not in ("local", "cloud"):
            raise RuntimeError(f"DB_TARGET must be 'local' or 'cloud', got {target!r}")

        if target == "cloud":
            url = os.getenv("DATABASE_URL_CLOUD")
            if not url:
                raise RuntimeError(
                    "DB_TARGET=cloud but DATABASE_URL_CLOUD is not set. "
                    "Get it from Supabase: Project Settings -> Database -> "
                    "Connection string -> Session pooler."
                )
        else:
            url = os.getenv("DATABASE_URL_LOCAL") or os.getenv("DATABASE_URL")
            if not url:
                raise RuntimeError(
                    "DATABASE_URL_LOCAL is not set (copy .env.example to .env)"
                )

        # Not required here: `plan`, `status`, `export` and `copy` never call
        # Apify. Only `collect` does, and it checks for itself.
        token = os.getenv("APIFY_TOKEN", "")
        return cls(database_url=url, apify_token=token, db_target=target)

    def require_apify_token(self) -> str:
        t = self.apify_token
        if not t or t.startswith("apify_api_xxxx") or t.startswith("dummy"):
            raise RuntimeError(
                "APIFY_TOKEN is not set in .env. Get one at "
                "https://console.apify.com/settings/integrations"
            )
        return t

    @staticmethod
    def url_for(target: str) -> str:
        """Resolve one specific target's URL — used by the `copy` command."""
        return Settings.load(target).database_url

    def describe(self) -> str:
        """Host only. Never print the URL: it contains the database password."""
        import re
        m = re.search(r"@([^/?]+)", self.database_url)
        if not m:  # unix-socket style URL, e.g. postgres@/mydb?host=/tmp
            m = re.search(r"@/([^?]+)", self.database_url)
        return f"{self.db_target} ({m.group(1) if m else 'unknown host'})"


# --------------------------------------------------------------------------
# Case config
# --------------------------------------------------------------------------

def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


@dataclass
class SubjectAccount:
    platform: str
    handle: str
    role: str = "personal"
    platform_user_id: Optional[str] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    source_url: Optional[str] = None
    note: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SubjectAccount":
        return cls(
            platform=d["platform"],
            handle=d["handle"].lstrip("@").lower(),
            role=d.get("role", "personal"),
            platform_user_id=str(d["platform_user_id"]) if d.get("platform_user_id") else None,
            valid_from=_parse_dt(d["valid_from"]) if d.get("valid_from") else None,
            valid_until=_parse_dt(d["valid_until"]) if d.get("valid_until") else None,
            source_url=d.get("source_url"),
            note=d.get("note"),
        )


@dataclass
class TierConfig:
    enabled: bool = True
    slice: str = "month"              # '<n>h' | 'day' | 'week' | 'month'
    max_items_per_slice: int = 500
    queries: list[str] = field(default_factory=list)
    min_faves: Optional[int] = None
    sort: Optional[str] = None        # overrides the platform-level sort
    exclude_reposts: bool = False     # append -filter:retweets to every query


@dataclass
class CaseConfig:
    slug: str
    subject_name: str
    window_start: datetime
    window_end: datetime
    subject_role: Optional[str] = None
    description: Optional[str] = None
    about_terms: list[str] = field(default_factory=list)
    accounts: list[SubjectAccount] = field(default_factory=list)
    tiers: dict[str, TierConfig] = field(default_factory=dict)
    budget_usd: float = 10.0
    platforms: dict[str, dict[str, Any]] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "CaseConfig":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        tiers = {
            name: TierConfig(
                enabled=t.get("enabled", True),
                slice=t.get("slice", "month"),
                max_items_per_slice=int(t.get("max_items_per_slice", 500)),
                queries=list(t.get("queries", [])),
                min_faves=t.get("min_faves"),
                sort=t.get("sort"),
                exclude_reposts=bool(t.get("exclude_reposts", False)),
            )
            for name, t in (data.get("tiers") or {}).items()
        }
        return cls(
            slug=data["slug"],
            subject_name=data["subject_name"],
            subject_role=data.get("subject_role"),
            description=data.get("description"),
            window_start=_parse_dt(data["window_start"]),
            window_end=_parse_dt(data["window_end"]),
            about_terms=list(data.get("about_terms", [])),
            accounts=[SubjectAccount.from_dict(a) for a in data.get("accounts", [])],
            tiers=tiers,
            budget_usd=float(data.get("budget_usd", 10.0)),
            platforms=data.get("platforms", {}) or {},
            raw=data,
        )

    def accounts_for(self, platform: str) -> list[SubjectAccount]:
        return [a for a in self.accounts if a.platform == platform]
