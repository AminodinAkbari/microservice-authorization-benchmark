"""Pydantic request/response schemas for the auth service."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CheckRequest(BaseModel):
    """Authorization check request body."""

    user_id: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    action: str = Field(min_length=1, pattern="^(read|write|delete)$")


class CheckResponse(BaseModel):
    """Authorization check response body: allow is 1 or 0."""

    allow: int


class RoleUpdateRequest(BaseModel):
    """Role assignment request body."""

    role: str = Field(min_length=1)
