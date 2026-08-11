"""Console plane is closed to API keys — third parties use /v1/agents/... only.

#1153 gave these endpoints a scope gate; P1 closes them to machine principals
outright. A tenant employee's JWT keeps its previous access unchanged.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from control_plane.api._authz import console_only
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AgentSpec, Role
from expert_work.runtime.runs import InMemoryRunEventStore, InMemoryRunStore
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TID = uuid4()
_RID = uuid4()

CONSOLE_ENDPOINTS: list[tuple[str, str]] = [
    ("GET", "/v1/sessions"),
    ("GET", f"/v1/sessions/{_TID}"),
    ("GET", f"/v1/sessions/{_TID}/messages"),
    ("GET", f"/v1/sessions/{_TID}/runs"),
    ("GET", f"/v1/sessions/{_TID}/runs/{_RID}"),
    ("GET", f"/v1/sessions/{_TID}/runs/{_RID}/events"),
    ("GET", "/v1/runs"),
    ("GET", "/v1/approvals"),
    ("GET", f"/v1/sessions/{_TID}/plan"),
    ("GET", f"/v1/sessions/{_TID}/workspace"),
]

#: Path prefixes that make up the console plane (Step 4's programmatic
#: lockdown audit walks every route under these). Kept in sync with the
#: brief's rationale: /v1/sessions, /v1/approvals, /v1/runs, /v1/uploads.
#: Fix round 2 (review Critical C1 / Important I1-I3) widened this: the
#: original prefix list left /v1/conversations (the read-only twin of
#: /v1/sessions + /v1/runs, zero scope needed a 200), /v1/users (roster +
#: :purge), /v1/artifacts + /v1/workspace (the second, thread-independent
#: entry point onto the same per-user data /v1/sessions/{id}/workspace/*
#: already locked), and /v1/triggers (list/get/patch/delete/:fire, plus
#: create — locked as a whole prefix rather than leaving one verb open,
#: since the self-audit below has no notion of "half a prefix") wide open
#: to any API key. /v1/memory is deliberately NOT here — it already has its
#: own machine-principal gate (memory.py::_require_caller_user). /v1/webhooks
#: is deliberately NOT here either — it's the inbound webhook receiver,
#: exempt from AuthMiddleware entirely (authenticated by a per-trigger
#: secret, not a JWT/API key), so it was never part of this plane.
_CONSOLE_PREFIXES: tuple[str, ...] = (
    "/v1/sessions",
    "/v1/approvals",
    "/v1/runs",
    "/v1/uploads",
    "/v1/conversations",
    "/v1/users",
    "/v1/artifacts",
    "/v1/workspace",
    "/v1/triggers",
)

#: The qualname ``console_only()``'s inner dependency carries — stable across
#: every call to ``console_only()`` (a fresh closure each time) since it comes
#: from the nested function's definition, not the specific instance. Used to
#: detect "this route depends on console_only()" without needing all routers
#: to share one Depends object.
_CONSOLE_ONLY_DEP_QUALNAME = f"{console_only.__qualname__}.<locals>._dep"


_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "support-bot", "version": "1.0.0", "tenant": "acme"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
        "system_prompt": {"template": "you are support"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}


def _spec() -> AgentSpec:
    return AgentSpec.model_validate(deepcopy(_SPEC))


def _build_settings() -> Settings:
    return Settings(
        service_name="control_plane_test",
        env="dev",
        auth_mode="dev",
        db_dsn="postgresql+asyncpg://test@localhost/test",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )


class _Ctx:
    def __init__(
        self,
        client: AsyncClient,
        app: Any,
        tenant_id: UUID,
        headers: dict[str, str],
        key_headers: dict[str, str],
    ) -> None:
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.headers = headers
        self.key_headers = key_headers

    async def seed_agent(self) -> None:
        await self.app.state.agent_spec_repo.create(
            tenant_id=self.tenant_id, spec=_spec(), spec_sha256="a" * 64, created_by="seed"
        )


@pytest.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    run_store = InMemoryRunStore()
    run_event_store = InMemoryRunEventStore()
    app = create_app(
        settings=_build_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=run_event_store),
        run_repo=run_store,
        run_event_repo=run_event_store,
    )
    tenant_id = uuid4()
    employee_jwt = make_test_jwt(
        tenant_id=tenant_id, subject=str(uuid4()), roles=(Role.ADMIN.value,)
    )
    # A real API-key caller resolves to a ``service_account`` principal
    # (matches ``test_api_key_scope_gate.py``'s ``_key_headers``). Scoped to
    # ``admin`` — the widest scope — so a 403 here can only be the console
    # lockdown, never an ``require_key_scope`` scope shortfall underneath it.
    key_jwt = make_test_jwt(
        tenant_id=tenant_id,
        subject="sa-test",
        sub_type="service_account",
        roles=(),
        scopes=("admin",),
    )
    headers = {"Authorization": f"Bearer {employee_jwt}"}
    key_headers = {"Authorization": f"Bearer {key_jwt}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        yield _Ctx(client, app, tenant_id, headers, key_headers)


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), CONSOLE_ENDPOINTS)
async def test_api_key_is_denied_on_the_console_plane(ctx: _Ctx, method: str, path: str) -> None:
    resp = await ctx.client.request(method, path, headers=ctx.key_headers)
    assert resp.status_code == 403, resp.text
    # Fix round 2 (review M4) — pin console_only()'s exact message text. It's
    # the only pointer a third party gets toward the external plane; a typo
    # in it would ship silently since nothing else asserts on it. ctx.key_headers
    # carries the widest (admin) scope, so require_key_scope (which runs first
    # in the dependency list) always passes and this 403 can only be console_only's.
    assert resp.json()["detail"]["message"] == (
        "console API is not available to API keys; use /v1/agents/{agent_code}/…"
    ), resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), CONSOLE_ENDPOINTS)
async def test_employee_jwt_is_unaffected(ctx: _Ctx, method: str, path: str) -> None:
    resp = await ctx.client.request(method, path, headers=ctx.headers)
    assert resp.status_code != 403, resp.text


@pytest.mark.asyncio
async def test_api_key_still_reaches_the_external_plane(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    resp = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "cust-77"},
        headers=ctx.key_headers,
    )
    assert resp.status_code == 200, resp.text


def test_every_console_route_carries_the_lockdown_dependency() -> None:
    """Programmatic self-check for Step 4 — no route can go missing silently.

    A ``rg`` grep for the decorator shape is brittle (line-wrapping,
    multi-line ``dependencies=[...]`` lists, etc. all defeat it). This
    instead builds the real app and inspects each compiled route's
    dependency graph — the same structure FastAPI itself resolves per
    request — so a route that forgot ``Depends(console_only())`` fails
    this test immediately instead of silently reopening the console plane
    to API keys.
    """
    app = create_app(
        settings=_build_settings(),
        lifecycle=Lifecycle(),
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        agent_runtime=stub_agent_runtime(),
        enable_reaper=False,
        enable_scheduler=False,
        enable_curation_worker=False,
    )
    checked: list[str] = []
    checked_paths: list[str] = []
    missing: list[str] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.path.startswith(_CONSOLE_PREFIXES):
            continue
        checked.append(f"{sorted(route.methods or ())} {route.path}")
        checked_paths.append(route.path)
        dep_qualnames = {dep.call.__qualname__ for dep in route.dependant.dependencies}
        if _CONSOLE_ONLY_DEP_QUALNAME not in dep_qualnames:
            missing.append(f"{sorted(route.methods or ())} {route.path}")
    assert checked, "no console-plane routes discovered — prefix list is stale"
    # Fix round 2 (review M1) — ``assert checked`` only catches every prefix
    # going stale at once. A single prefix going stale (a router's mount
    # point renamed, say) would silently drop that whole surface from the
    # audit while ``checked`` stays non-empty from the others. Pin each
    # prefix individually.
    for prefix in _CONSOLE_PREFIXES:
        assert any(p.startswith(prefix) for p in checked_paths), (
            f"no routes discovered under {prefix!r} — prefix is stale"
        )
    assert not missing, f"routes missing console_only(): {missing}"
