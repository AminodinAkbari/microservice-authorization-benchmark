"""Authorization service entrypoint.

Endpoints:
    POST /check                      -> {"allow": 1|0}
    POST /admin/users/{id}/role      -> assign role, commit, publish invalidation
    GET  /health                     -> liveness/readiness probe
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status

# Load .env file from project root before reading config (override shell exports)
load_dotenv(Path(__file__).parent.parent.parent / ".env", override=True)

from . import db
from .config import get_settings
from .models import (
    SELECT_PERMISSIONS_BY_ROLE,
    SELECT_ROLE_ID_BY_NAME,
    SELECT_ROLE_ID_BY_USER,
    UPDATE_USER_ROLE,
)
from .schemas import CheckRequest, CheckResponse, RoleUpdateRequest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logger.info("Starting auth service (db pool %d-%d)", settings.db_pool_min_size, settings.db_pool_max_size)
    await db.init_pool()
    yield
    await db.close_redis()
    await db.close_pool()
    logger.info("Auth service shutdown complete")


app = FastAPI(
    title="Authorization Benchmark - Auth Service",
    version="1.0.0",
    lifespan=lifespan,
)


@app.post("/check", response_model=CheckResponse)
async def check(payload: CheckRequest) -> CheckResponse:
    """Decide whether user_id is allowed to perform action on resource_id."""
    try:
        role_id = await db.get_pool().fetchval(SELECT_ROLE_ID_BY_USER, payload.user_id)
        if role_id is None:
            logger.warning("Unknown user %s", payload.user_id)
            return CheckResponse(allow=0)
        rows = await db.get_pool().fetch(SELECT_PERMISSIONS_BY_ROLE, role_id)
    except asyncpg.PostgresError:
        logger.exception("Database error while evaluating authorization")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authorization backend unavailable",
        ) from None

    required = f"{payload.resource_id}:{payload.action}"
    allowed = any(row["permission"] == required for row in rows)
    return CheckResponse(allow=1 if allowed else 0)


@app.post("/admin/users/{user_id}/role")
async def set_user_role(user_id: str, payload: RoleUpdateRequest) -> dict[str, object]:
    """Assign a role to a user, commit, then publish a cache invalidation event."""
    try:
        role_id = await db.get_pool().fetchval(SELECT_ROLE_ID_BY_NAME, payload.role)
        if role_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown role: {payload.role}",
            )
        updated = await db.get_pool().execute(UPDATE_USER_ROLE, role_id, user_id)
        await db.publish_invalidation(user_id)
    except asyncpg.PostgresError:
        logger.exception("Database error while updating role for user %s", user_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authorization backend unavailable",
        ) from None

    if not updated.endswith("1"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown user: {user_id}",
        )
    return {"user_id": user_id, "role": payload.role, "updated": True}


@app.get("/health")
async def health() -> dict[str, str]:
    info = await db.healthcheck_db()
    return {"status": "ok", "database": info["database"]}
