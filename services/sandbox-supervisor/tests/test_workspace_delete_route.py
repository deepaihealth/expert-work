"""Phase 3a — the ``POST /v1/workspaces/{tenant}/{user}:delete`` route +
``HTTPSupervisorRuntime.mark_workspace_deleted`` wire round-trip.

The route proxies the control-plane cascade purge to the supervisor (only the
supervisor can mutate a per-user docker volume). This exercises the full wire
path: client → route → ``supervisor.mark_workspace_deleted``.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from orchestrator.tools.sandbox import HTTPSupervisorRuntime
from sandbox_supervisor.app import _register_routes


class _FakeSupervisor:
    """Records ``mark_workspace_deleted`` calls; the route only needs this op."""

    def __init__(self) -> None:
        self.deletions: list[tuple[UUID, UUID]] = []

    async def mark_workspace_deleted(self, *, tenant_id: UUID, user_id: UUID) -> None:
        self.deletions.append((tenant_id, user_id))


def _app(supervisor: _FakeSupervisor) -> FastAPI:
    app = FastAPI()
    _register_routes(app)
    app.state.supervisor = supervisor
    return app


@pytest.mark.asyncio
async def test_route_invokes_mark_workspace_deleted() -> None:
    supervisor = _FakeSupervisor()
    app = _app(supervisor)
    tenant_id, user_id = uuid4(), uuid4()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://supervisor") as client:
        resp = await client.post(f"/v1/workspaces/{tenant_id}/{user_id}:delete")
    assert resp.status_code == 204
    assert supervisor.deletions == [(tenant_id, user_id)]


@pytest.mark.asyncio
async def test_http_client_mark_workspace_deleted_round_trip() -> None:
    supervisor = _FakeSupervisor()
    app = _app(supervisor)
    tenant_id, user_id = uuid4(), uuid4()
    client = HTTPSupervisorRuntime(
        base_url="http://supervisor", transport=httpx.ASGITransport(app=app)
    )
    await client.mark_workspace_deleted(tenant_id=tenant_id, user_id=user_id)
    assert supervisor.deletions == [(tenant_id, user_id)]
