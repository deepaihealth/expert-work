"""External event replay for third-party apps — ``/v1/agents/{agent_code}/events/...``.

Filled in by the external-API P1 plan, Task 4.
"""

from __future__ import annotations

from fastapi import APIRouter


def build_external_events_router() -> APIRouter:
    """Mount the external event-replay endpoints."""
    return APIRouter(prefix="/v1/agents", tags=["external"])
