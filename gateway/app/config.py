"""Gateway configuration loaded from environment variables.

No hardcoded secrets or URLs: every value is supplied via the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

VALID_AUTH_MODES = ("baseline", "redis_ttl", "redis_pubsub", "jwt_embedded")


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings for the gateway service."""

    secret_key: str
    jwt_algorithm: str = "HS256"
    auth_mode: str = "baseline"
    auth_service_url: str = "http://localhost:8001"
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_seconds: int = 60
    invalidate_channel: str = "authz:invalidate"
    cache_key_prefix: str = "authz"
    http_timeout_seconds: float = 5.0

    @property
    def caching_enabled(self) -> bool:
        """True when the selected mode consults Redis before calling auth_service."""
        return self.auth_mode in ("redis_ttl", "redis_pubsub")

    @property
    def invalidation_enabled(self) -> bool:
        """True when the gateway must subscribe to the invalidation channel."""
        return self.auth_mode == "redis_pubsub"


def _get_env(key: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(key, default)
    if value is None and required:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return value  # type: ignore[return-value]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build (and cache) the settings object from the process environment."""
    auth_mode = _get_env("AUTH_MODE", "baseline").strip().lower()
    if auth_mode not in VALID_AUTH_MODES:
        raise RuntimeError(
            f"AUTH_MODE must be one of {VALID_AUTH_MODES}, got: {auth_mode!r}"
        )
    return Settings(
        secret_key=_get_env("SECRET_KEY", required=True),
        jwt_algorithm=_get_env("JWT_ALGORITHM", "HS256"),
        auth_mode=auth_mode,
        auth_service_url=_get_env("AUTH_SERVICE_URL", "http://localhost:8001").rstrip("/"),
        redis_url=_get_env("REDIS_URL", "redis://localhost:6379/0"),
        cache_ttl_seconds=int(_get_env("CACHE_TTL_SECONDS", "60")),
        invalidate_channel=_get_env("INVALIDATE_CHANNEL", "authz:invalidate"),
    )
