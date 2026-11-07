"""Platform adapter contract.

To add TikTok/Instagram/YouTube later, implement this interface and register
it in REGISTRY. Nothing else in the codebase changes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterator

from ..config import CaseConfig
from ..models import ContentItem, QuerySpec


class PlatformAdapter(ABC):
    #: value stored in the `platform` enum column
    platform: str = "other"
    #: USD per 1,000 delivered items, used for the budget guard
    price_per_1k: float = 0.0
    #: identifier recorded in collection_runs.actor_id
    actor_id: str = ""

    def __init__(self, token: str, options: dict[str, Any] | None = None):
        self.token = token
        self.options = options or {}

    @abstractmethod
    def plan(self, case: CaseConfig) -> list[QuerySpec]:
        """Turn the case config into concrete, time-sliced query units."""

    @abstractmethod
    def fetch(self, spec: QuerySpec) -> Iterator[ContentItem]:
        """Run one query unit and yield normalised items."""

    def last_external_run_id(self) -> str | None:
        return None


REGISTRY: dict[str, type[PlatformAdapter]] = {}


def register(cls: type[PlatformAdapter]) -> type[PlatformAdapter]:
    REGISTRY[cls.platform] = cls
    return cls


def get_adapter(platform: str, token: str, options: dict[str, Any] | None = None) -> PlatformAdapter:
    if platform not in REGISTRY:
        raise KeyError(
            f"No adapter for platform {platform!r}. Available: {sorted(REGISTRY)}"
        )
    return REGISTRY[platform](token, options)
