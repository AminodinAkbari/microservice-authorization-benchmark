"""JWT utilities for the gateway (PyJWT, HS256)."""

from __future__ import annotations

import logging
from typing import Any

import jwt

logger = logging.getLogger(__name__)

CREDENTIALS_EXCEPTION = jwt.PyJWTError


def decode_token(token: str, secret_key: str, algorithm: str = "HS256") -> dict[str, Any]:
    """Decode and verify a JWT.

    Raises:
        jwt.InvalidTokenError: if the token is malformed, expired, or has a
            bad signature.
    """
    return jwt.decode(token, secret_key, algorithms=[algorithm])


def extract_user_id(claims: dict[str, Any]) -> str:
    """Extract the user id from the ``sub`` claim."""
    user_id = claims.get("sub")
    if not user_id or not isinstance(user_id, str):
        logger.warning("JWT is missing a valid 'sub' claim")
        raise jwt.InvalidTokenError("Token is missing the 'sub' claim")
    return user_id
