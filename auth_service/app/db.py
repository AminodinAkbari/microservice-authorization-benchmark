"""asyncpg connection pool and data-access helpers for the auth service."""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg
import redis.asyncio as aioredis
from redis.asyncio import Redis

from .config import get_settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_redis: Redis | None = None


async def init_pool() -> asyncpg.Pool:
    """Create the asyncpg connection pool (min_size=5, max_size=20 by default)."""
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = await asyncpg.create_pool(
            dsn=settings.db_dsn,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
        )
        logger.info(
            "Created asyncpg pool (min_size=%d, max_size=%d)",
            settings.db_pool_min_size,
            settings.db_pool_max_size,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("asyncpg pool closed")


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialised")
    return _pool


async def get_redis() -> Redis:
    """Lazily create a Redis client used for publishing invalidation events."""
    global _redis
    if _redis is None:
        settings = get_settings()
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        await _redis.ping()
        logger.info("Connected to Redis at %s", settings.redis_url)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


async def fetch_role_id(user_id: str) -> int | None:
    """Return the role id for a user, or None if the user does not exist."""
    pool = get_pool()
    row: int | None = await pool.fetchval(
        "SELECT role_id FROM users WHERE id = $1", user_id
    )
    return row


async def fetch_permissions(role_id: int) -> list[str]:
    """Return all permissions granted to a role."""
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT permission FROM role_permissions WHERE role_id = $1", role_id
    )
    return [row["permission"] for row in rows]


async def get_role_name(role: str) -> int | None:
    """Resolve a role name to its id; None if unknown role."""
    pool = get_pool()
    return await pool.fetchval("SELECT id FROM roles WHERE name = $1", role)


async def update_user_role(user_id: str, role_id: int) -> bool:
    """Update a user's role and commit. Returns True if a row was updated."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            status: str = await conn.execute(
                "UPDATE users SET role_id = $1 WHERE id = $2", role_id, user_id
            )
    updated = status.endswith("1")
    logger.info("Updated role of user %s to role_id=%d (updated=%s)", user_id, role_id, updated)
    return updated


async def publish_invalidation(user_id: str) -> None:
    """Publish {user_id} to the authz:invalidate Redis channel."""
    redis = await get_redis()
    await redis.publish(
        get_settings().invalidate_channel, json.dumps({"user_id": user_id})
    )
    logger.info("Published invalidation event for user %s", user_id)


async def healthcheck_db() -> dict[str, Any]:
    pool = get_pool()
    version: str = await pool.fetchval("SELECT version()")
    return {"database": version.split()[0] if version else "unknown"}
