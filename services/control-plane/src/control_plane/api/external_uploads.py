"""External file upload for third-party apps — ``/v1/agents/{agent_code}/uploads/...``.

Filled in by the external-API P1 plan, Task 5.
"""

from __future__ import annotations

from fastapi import APIRouter


def build_external_uploads_router() -> APIRouter:
    """Mount the external upload endpoints."""
    return APIRouter(prefix="/v1/agents", tags=["external"])
