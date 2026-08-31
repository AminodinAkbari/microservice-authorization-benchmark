"""Authorization middleware for the gateway.

Measures T_auth_overhead with time.perf_counter_ns() and appends the
measurement to the response as the X-Auth-Overhead-Ns header.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
import jwt as pyjwt
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from .config import get_settings
from .jwt_utils import decode_token, extract_user_id
from .redis_client import get_redis_manager

logger = logging.getLogger(__name__)

PROTECTED_PREFIX = "/api/v1/resources/"
VALID_ACTIONS = ("read", "write", "delete")


class AuthMiddleware(BaseHTTPMiddleware):
    """Verifies the JWT and enforces the authorization decision for protected routes."""

    def __init__(self, app: Any) -> None:
        super().__init__(app)
        self.settings = get_settings()
        self._http_client: httpx.AsyncClient | None = None

    def set_http_client(self, client: httpx.AsyncClient) -> None:
        self._http_client = client

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if not request.url.path.startswith(PROTECTED_PREFIX):
            return await call_next(request)

        start_ns = time.perf_counter_ns()
        try:
            decision, cache_hit = await self._authorize(request)
            overhead_ns = time.perf_counter_ns() - start_ns
        except pyjwt.PyJWTError:
            overhead_ns = time.perf_counter_ns() - start_ns
            return self._build_response(
                401, {"detail": "Invalid or expired token"}, overhead_ns, None
            )
        except httpx.HTTPError as exc:
            overhead_ns = time.perf_counter_ns() - start_ns
            logger.error("auth_service call failed: %s", exc)
            return self._build_response(
                502,
                {"detail": "Authorization service unavailable"},
                overhead_ns,
                None,
            )
        except Exception:  # noqa: BLE001 - convert auth-path failures to 500
            overhead_ns = time.perf_counter_ns() - start_ns
            logger.exception("Unexpected authorization failure")
            return self._build_response(
                500, {"detail": "Internal authorization error"}, overhead_ns, None
            )

        request.state.auth_overhead_ns = overhead_ns
        request.state.cache_hit = cache_hit

        if not decision:
            return self._build_response(
                403, {"detail": "Forbidden"}, overhead_ns, cache_hit
            )

        response = await call_next(request)
        response.headers["X-Auth-Overhead-Ns"] = str(overhead_ns)
        if cache_hit is not None:
            response.headers["X-Cache-Hit"] = "1" if cache_hit else "0"
        logger.info(
            "auth_mode=%s path=%s allowed=%s overhead_ns=%d cache_hit=%s",
            self.settings.auth_mode,
            request.url.path,
            decision,
            overhead_ns,
            cache_hit,
        )
        return response

    # ------------------------------------------------------------------
    # Authorization paths
    # ------------------------------------------------------------------

    async def _authorize(self, request: Request) -> tuple[bool, bool | None]:
        """Return (allowed, cache_hit). cache_hit is None when no cache is used."""
        auth_header = request.headers.get("Authorization", "")
        scheme, _, token = auth_header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise pyjwt.InvalidTokenError("Missing bearer token")

        claims = decode_token(token, self.settings.secret_key, self.settings.jwt_algorithm)
        user_id = extract_user_id(claims)
        request.state.user_id = user_id

        resource_id = request.url.path[len(PROTECTED_PREFIX):].strip("/")
        action = request.headers.get("X-Action", "").strip().lower()
        if action not in VALID_ACTIONS:
            raise pyjwt.InvalidTokenError("X-Action header must be read, write or delete")

        mode = self.settings.auth_mode
        if mode == "jwt_embedded":
            return self._check_embedded(claims, resource_id, action), None
        if mode == "baseline":
            return await self._call_auth_service(user_id, resource_id, action), None

        # redis_ttl / redis_pubsub: consult cache first, fall back to auth_service.
        manager = get_redis_manager()
        cached = await manager.get_decision(user_id, resource_id, action)
        if cached is not None:
            return cached, True
        allowed = await self._call_auth_service(user_id, resource_id, action)
        await manager.set_decision(user_id, resource_id, action, allowed)
        return allowed, False

    def _check_embedded(
        self, claims: dict[str, Any], resource_id: str, action: str
    ) -> bool:
        permissions = claims.get("permissions")
        if not isinstance(permissions, list):
            logger.warning("JWT lacks a valid 'permissions' claim for jwt_embedded mode")
            return False
        required = f"{resource_id}:{action}"
        return required in permissions

    async def _call_auth_service(
        self, user_id: str, resource_id: str, action: str
    ) -> bool:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=self.settings.http_timeout_seconds,
                limits=httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=20,
                    keepalive_expiry=30.0,
                ),
            )
        response = await self._http_client.post(
            f"{self.settings.auth_service_url}/check",
            json={"user_id": user_id, "resource_id": resource_id, "action": action},
        )
        response.raise_for_status()
        body = response.json()
        return body.get("allow") == 1

    @staticmethod
    def _build_response(
        status_code: int, content: dict[str, Any], overhead_ns: int, cache_hit: bool | None
    ) -> Response:
        response = JSONResponse(status_code=status_code, content=content)
        response.headers["X-Auth-Overhead-Ns"] = str(overhead_ns)
        if cache_hit is not None:
            response.headers["X-Cache-Hit"] = "1" if cache_hit else "0"
        return response

