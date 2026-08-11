"""External approval decisions for third-party apps — ``/v1/agents/{agent_code}/approvals/...``.

Filled in by the external-API P1 plan, Task 6.
"""

from __future__ import annotations

from fastapi import APIRouter


def build_external_approvals_router() -> APIRouter:
    """Mount the external approval-decision endpoints."""
    return APIRouter(prefix="/v1/agents", tags=["external"])
