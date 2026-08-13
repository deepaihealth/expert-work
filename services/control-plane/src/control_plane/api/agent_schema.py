"""``GET /v1/agents/schema`` — the AgentSpec JSON Schema (Stream S, Mini-ADR S-1).

The visual manifest editor renders its form straight from this schema, so the
form never drifts from the backend contract. Read-only; ``by_alias=True`` emits
``apiVersion`` (the manifest's camelCase root field). Computed once at import.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from control_plane.api._authz import console_only
from expert_work.protocol import AgentSpec

_AGENT_SPEC_SCHEMA: dict[str, Any] = AgentSpec.model_json_schema(by_alias=True)


def build_agent_schema_router() -> APIRouter:
    router = APIRouter(prefix="/v1/agents", tags=["agents"])

    # Backlog Task 2 (spec/external-api-v1-p2b) — this router shares the
    # ``/v1/agents`` prefix with the console AND third-party planes (see
    # ``agents.py``'s ``_CONSOLE_ONLY``/``_EXTERNAL_ONLY`` docstrings), so it
    # must carry one of the two gates too or the partition self-audit
    # (``test_agents_prefix_is_partitioned_exactly``) flags it as
    # unclassified. The schema is a static document with no tenant data, so
    # leaking it isn't the concern — but a third-party client has no use for
    # the manifest editor's form schema either, so ``console_only()`` is the
    # correct side, not a new "open" carve-out.
    @router.get("/schema", dependencies=[Depends(console_only())])
    async def get_agent_schema() -> dict[str, object]:
        return {"success": True, "data": _AGENT_SPEC_SCHEMA, "error": None}

    return router
