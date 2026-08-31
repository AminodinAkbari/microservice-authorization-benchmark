"""Auth service configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings for the authorization service."""

    database_url: str
    redis_url: str = "redis://localhost:6379/0"
    invalidate_channel: str = "authz:invalidate"
    db_pool_min_size: int = 5
    db_pool_max_size: int = 20

    @property
    def db_dsn(self) -> str:
        """asyncpg accepts standard libpq-style connection strings/URLs."""
        return self.database_url


def _get_env(key: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(key, default)
    if value is None and required:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return value  # type: ignore[return-value]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build (and cache) the settings object from the process environment."""
    return Settings(
        database_url=_get_env("DATABASE_URL", required=True),
        redis_url=_get_env("REDIS_URL", "redis://localhost:6379/0"),
        invalidate_channel=_get_env("INVALIDATE_CHANNEL", "authz:invalidate"),
        db_pool_min_size=int(_get_env("DB_POOL_MIN_SIZE", "5")),
        db_pool_max_size=int(_get_env("DB_POOL_MAX_SIZE", "20")),
    )
