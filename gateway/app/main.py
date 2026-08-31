"""Gateway service entrypoint.

Runs in front of protected resources and enforces authorization using one of
four modes configured via AUTH_MODE: baseline, redis_ttl, redis_pubsub,
jwt_embedded.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request

from .config import get_settings
from .middleware import AuthMiddleware
from .redis_client import get_redis_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logger.info("Starting gateway with AUTH_MODE=%s", settings.auth_mode)

    # Pooled async HTTP client for calls to auth_service.
    app.state.http_client = httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=30.0,
        ),
    )

    if settings.caching_enabled:
        manager = get_redis_manager()
        await manager.connect()
        if settings.invalidation_enabled:
            manager.start_invalidation_listener()

    yield

    if settings.caching_enabled:
        await get_redis_manager().close()
    await app.state.http_client.aclose()
    logger.info("Gateway shutdown complete")


app = FastAPI(
    title="Authorization Benchmark - Gateway",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(AuthMiddleware)



@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "auth_mode": get_settings().auth_mode}


@app.get("/api/v1/resources/{resource_id}")
async def get_resource(request: Request, resource_id: str) -> dict[str, str]:
    """Protected endpoint; authorization is enforced by AuthMiddleware."""
    return {
        "resource_id": resource_id,
        "user_id": getattr(request.state, "user_id", "unknown"),
        "status": "granted",
    }
