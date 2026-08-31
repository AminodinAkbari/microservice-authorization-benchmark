"""Redis client management, caching helpers and pub/sub invalidation listener."""

from __future__ import annotations

import asyncio
import json
import logging

import redis.asyncio as aioredis
from redis.asyncio import Redis
from redis.exceptions import RedisError

from .config import Settings, get_settings

logger = logging.getLogger(__name__)

# Cache decisions are stored as bytes b"1" (allow) or b"0" (deny).
ALLOW_BYTE = b"1"
DENY_BYTE = b"0"


class RedisManager:
    """Owns the data-plane Redis connection and the pub/sub listener task."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Redis | None = None
        self._listener_task: asyncio.Task[None] | None = None

    @property
    def client(self) -> Redis:
        if self._client is None:
            raise RuntimeError("Redis client is not initialised")
        return self._client

    async def connect(self) -> None:
        self._client = aioredis.from_url(
            self._settings.redis_url,
            decode_responses=False,  # raw bytes: cache values are b"1"/b"0"
            max_connections=50,
        )
        try:
            await self._client.ping()
            logger.info("Connected to Redis at %s", self._settings.redis_url)
        except RedisError as exc:
            logger.error("Redis connection failed: %s", exc)
            raise

    async def close(self) -> None:
        await self.stop_invalidation_listener()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("Redis connection closed")

    # ------------------------------------------------------------------
    # Cache helpers (data plane)
    # ------------------------------------------------------------------

    def cache_key(self, user_id: str, resource_id: str, action: str) -> str:
        return f"{self._settings.cache_key_prefix}:{user_id}:{resource_id}:{action}"

    async def get_decision(
        self, user_id: str, resource_id: str, action: str
    ) -> bool | None:
        """Return cached allow/deny, or None on cache miss."""
        if self._client is None:
            return None
        try:
            raw = await self._client.get(self.cache_key(user_id, resource_id, action))
        except RedisError as exc:
            logger.warning("Redis GET failed (falling through to auth_service): %s", exc)
            return None
        if raw is None:
            return None
        # Values are stored as bytes b"1"/b"0"; decode and compare.
        value = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        return value == "1"

    async def set_decision(
        self, user_id: str, resource_id: str, action: str, allowed: bool
    ) -> None:
        if self._client is None:
            return
        try:
            await self._client.set(
                self.cache_key(user_id, resource_id, action),
                ALLOW_BYTE if allowed else DENY_BYTE,
                ex=self._settings.cache_ttl_seconds,
            )
        except RedisError as exc:
            logger.warning("Redis SET failed (continuing without cache write): %s", exc)

    async def delete_user_keys(self, user_id: str) -> int:
        """Delete all keys matching ``authz:{user_id}:*`` using SCAN and DEL."""
        if self._client is None:
            return 0
        pattern = f"{self._settings.cache_key_prefix}:{user_id}:*"
        deleted = 0
        try:
            batch: list[bytes] = []
            async for key in self._client.scan_iter(match=pattern, count=200):
                batch.append(key)
                if len(batch) >= 200:
                    deleted += await self._client.delete(*batch)
                    batch.clear()
            if batch:
                deleted += await self._client.delete(*batch)
        except RedisError as exc:
            logger.error("Failed to invalidate keys for user %s: %s", user_id, exc)
            return deleted
        logger.info("Invalidated %d cache keys for user %s", deleted, user_id)
        return deleted

    # ------------------------------------------------------------------
    # Pub/sub invalidation listener (control plane, separate connection)
    # ------------------------------------------------------------------

    def start_invalidation_listener(self) -> None:
        if self._listener_task is None or self._listener_task.done():
            self._listener_task = asyncio.create_task(
                self._invalidation_loop(), name="authz-invalidation-listener"
            )
            logger.info("Started pub/sub invalidation listener")

    async def stop_invalidation_listener(self) -> None:
        if self._listener_task is not None and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None
            logger.info("Stopped pub/sub invalidation listener")

    async def _invalidation_loop(self) -> None:
        """Subscribe on a dedicated connection and invalidate per-user keys."""
        assert self._client is not None
        pubsub = self._client.pubsub()  # redis-py uses a separate connection
        channel = self._settings.invalidate_channel
        try:
            await pubsub.subscribe(channel)
            logger.info("Subscribed to Redis channel %r", channel)
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                raw = message.get("data")
                if raw is None:
                    continue
                try:
                    payload = json.loads(
                        raw.decode("utf-8") if isinstance(raw, bytes) else raw
                    )
                    user_id = payload["user_id"]
                except (ValueError, KeyError, AttributeError):
                    logger.warning("Ignoring malformed invalidation message: %r", raw)
                    continue
                await self.delete_user_keys(user_id)
        except asyncio.CancelledError:
            raise
        except RedisError as exc:
            logger.error("Invalidation listener crashed: %s", exc)
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
            except RedisError:
                pass


_manager: RedisManager | None = None


def get_redis_manager() -> RedisManager:
    """Return the process-wide RedisManager singleton."""
    global _manager
    if _manager is None:
        _manager = RedisManager(get_settings())
    return _manager

