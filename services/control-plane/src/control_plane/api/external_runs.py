"""External run control for third-party apps — ``/v1/agents/{agent_code}/runs/...``.

Filled in by the external-API P1 plan, Task 2.
"""

from __future__ import annotations

from fastapi import APIRouter


def build_external_runs_router() -> APIRouter:
    """Mount the external run-control endpoints."""
    return APIRouter(prefix="/v1/agents", tags=["external"])
