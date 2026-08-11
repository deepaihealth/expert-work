"""External session listing for third-party apps — ``/v1/agents/{agent_code}/sessions/...``.

Filled in by the external-API P1 plan, Task 3.
"""

from __future__ import annotations

from fastapi import APIRouter


def build_external_sessions_router() -> APIRouter:
    """Mount the external session-listing endpoints."""
    return APIRouter(prefix="/v1/agents", tags=["external"])
