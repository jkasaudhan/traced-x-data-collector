from .base import PlatformAdapter, get_adapter, register, REGISTRY  # noqa: F401
from . import twitter_apify  # noqa: F401  (registers the adapter)

__all__ = ["PlatformAdapter", "get_adapter", "register", "REGISTRY"]
