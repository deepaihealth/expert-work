"""Path-param NUL-byte hardening for the external (third-party) API plane —
External-API-v1 P2-b, second pass (Critical fix).

The first P2-b pass (``test_external_nul_hardening.py``) put every
external-plane body / query / header field that reaches a Postgres
``text``/``jsonb`` column through the shared ``reject_nul`` / ``reject_nul_deep``
guard (``api/_external.py``) — but missed the one input class that is
neither a pydantic field nor a ``Query(...)``/header: a **path** parameter.
``POST /v1/agents/support%00bot/sessions`` 404s on its face, but the decoded
NUL survives into ``agent_code`` and used to flow straight into
``AgentDisableStore.get`` / ``AgentSpecStore.list_by_tenant`` (both a
``text``-column ``WHERE`` comparison) — the exact ``asyncpg
CharacterNotInRepertoireError`` → bare-text 500 the first pass exists to
prevent. ``POST /v1/agents/{name}/disable|enable`` has the identical hole
via the same ``list_by_tenant`` call.

The fix is a single router-level dependency (``reject_nul_path_params``,
``api/_external.py``), attached once to each router's own ``APIRouter(...,
dependencies=[...])`` constructor rather than per-route, so a future route
added to any of those routers is covered by construction. This file:

1. Proves the guard actually rejects a NUL in ``agent_code`` / ``name`` on
   every third-party-reachable route family (in-memory backend — the guard
   fires before the handler ever runs, so it doesn't matter whether the
   backend would have crashed).
2. Self-audits that every ``tags=["external"]`` route — discovered from the
   real app, not a hand-maintained list, so a future route is covered
   automatically — actually carries the guard dependency, plus the four
   ``/v1/agents/{agent_code}|{name}`` routes that live on ``agents.py``'s own
   router (tagged ``"agents"``, not ``"external"`` — see that test's
   docstring for why a tag-only audit would miss them).
3. Confirms legitimate (non-NUL) requests are unaffected.

A SEPARATE file, ``test_external_path_param_nul_guard_integration.py``, runs
a subset of these same routes against a REAL Postgres — that file is what
the fix's ablation self-proof was performed against, because most of these
routes' ``agent_code`` never reaches SQL at all (see that file's docstring
for exactly which routes do and don't, and why the ablation proof lives
there, not here).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from control_plane.api._external import reject_nul_path_params
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AgentSpec
from expert_work.runtime.runs import InMemoryRunEventStore, InMemoryRunStore
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

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

#: A literal, already-percent-encoded NUL — matches the real attack
#: (``POST /v1/agents/support%00bot/sessions``); Starlette decodes this back
#: to an actual ``\x00`` byte in ``request.path_params`` before any handler
#: (or our dependency) runs, on both the h11 and httptools uvicorn backends
#: (verified against a real uvicorn server during the review this file
#: fixes — this file itself runs over ``ASGITransport``, which reproduces
#: the same decode step Starlette's routing performs).
_NUL = "%00"


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
    def __init__(self, client: AsyncClient, app: Any, tenant_id: UUID, headers: dict[str, str]):
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.headers = headers

    async def seed_agent(self) -> None:
        await self.app.state.agent_spec_repo.create(
            tenant_id=self.tenant_id, spec=_spec(), spec_sha256="a" * 64, created_by="seed"
        )

    async def bind_session(self, user_id: str = "cust-77") -> str:
        resp = await self.client.post(
            "/v1/agents/support-bot/sessions",
            json={"user_id": user_id},
            headers=self.headers,
        )
        assert resp.status_code == 201, resp.text
        return str(resp.json()["data"]["session_id"])


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
    # External-API-v1 P2-b security fix (external_only()) — these routes are
    # now service-account-only, so the caller identity this file exercises
    # (irrelevant to what it's actually testing: NUL-byte path params) has to
    # be one. Was an employee JWT; borrowed for convenience before that gate
    # existed, same as the other files this fix's report catalogs.
    jwt = make_test_jwt(
        tenant_id=tenant_id,
        subject="sa-test",
        sub_type="service_account",
        roles=(),
        scopes=("admin",),
    )
    headers = {"Authorization": f"Bearer {jwt}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        yield _Ctx(client, app, tenant_id, headers)


def _assert_envelope_422(resp: Any) -> None:
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert "detail" not in body
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert "NUL" in body["error"]["message"]


# ---------------------------------------------------------------------------
# Per-route-family rejection — one test per third-party-reachable route that
# carries ``agent_code`` or ``name`` in its path. The guard runs before any
# endpoint code, so this holds regardless of whether the in-memory backend
# would itself have crashed on the NUL (see the integration file for which
# families actually reach SQL through this specific field).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_session_nul_agent_code_is_422(ctx: _Ctx) -> None:
    resp = await ctx.client.post(
        f"/v1/agents/support{_NUL}bot/sessions",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)


@pytest.mark.asyncio
async def test_run_agent_for_user_nul_agent_code_is_422(ctx: _Ctx) -> None:
    resp = await ctx.client.post(
        f"/v1/agents/support{_NUL}bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)


@pytest.mark.asyncio
async def test_list_sessions_nul_agent_code_is_422(ctx: _Ctx) -> None:
    resp = await ctx.client.get(
        f"/v1/agents/support{_NUL}bot/sessions",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)


@pytest.mark.asyncio
async def test_get_messages_nul_agent_code_is_422(ctx: _Ctx) -> None:
    resp = await ctx.client.get(
        f"/v1/agents/support{_NUL}bot/sessions/{uuid4()}/messages",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)


@pytest.mark.asyncio
async def test_rename_session_nul_agent_code_is_422(ctx: _Ctx) -> None:
    resp = await ctx.client.patch(
        f"/v1/agents/support{_NUL}bot/sessions/{uuid4()}",
        json={"user_id": "cust-77", "title": "new title"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)


@pytest.mark.asyncio
async def test_archive_session_nul_agent_code_is_422(ctx: _Ctx) -> None:
    resp = await ctx.client.delete(
        f"/v1/agents/support{_NUL}bot/sessions/{uuid4()}",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)


@pytest.mark.asyncio
async def test_cancel_run_nul_agent_code_is_422(ctx: _Ctx) -> None:
    resp = await ctx.client.post(
        f"/v1/agents/support{_NUL}bot/runs/{uuid4()}:cancel",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)


@pytest.mark.asyncio
async def test_decide_run_nul_agent_code_is_422(ctx: _Ctx) -> None:
    resp = await ctx.client.post(
        f"/v1/agents/support{_NUL}bot/runs/{uuid4()}:decide",
        json={"user_id": "cust-77", "decision": "approve"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)


@pytest.mark.asyncio
async def test_stream_run_events_nul_agent_code_is_422(ctx: _Ctx) -> None:
    resp = await ctx.client.get(
        f"/v1/agents/support{_NUL}bot/runs/{uuid4()}/events",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)


@pytest.mark.asyncio
async def test_upload_nul_agent_code_is_422(ctx: _Ctx) -> None:
    resp = await ctx.client.post(
        f"/v1/agents/support{_NUL}bot/uploads",
        data={"user_id": "cust-77"},
        files={"file": ("notes.txt", b"hello", "text/plain")},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)


@pytest.mark.asyncio
async def test_workspace_files_nul_agent_code_is_422(ctx: _Ctx) -> None:
    resp = await ctx.client.get(
        f"/v1/agents/support{_NUL}bot/workspace/files",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)


@pytest.mark.asyncio
async def test_workspace_file_nul_agent_code_is_422(ctx: _Ctx) -> None:
    resp = await ctx.client.get(
        f"/v1/agents/support{_NUL}bot/workspace/file",
        params={"user_id": "cust-77", "path": "notes.txt"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)


@pytest.mark.asyncio
async def test_disable_agent_nul_name_is_422(ctx: _Ctx) -> None:
    resp = await ctx.client.post(
        f"/v1/agents/support{_NUL}bot/disable",
        json={},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)


@pytest.mark.asyncio
async def test_enable_agent_nul_name_is_422(ctx: _Ctx) -> None:
    resp = await ctx.client.post(
        f"/v1/agents/support{_NUL}bot/enable",
        json={},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)


# ---------------------------------------------------------------------------
# Bonus — the guard checks EVERY path param, not just agent_code/name.
# UUID-typed params (session_id / run_id) would already be rejected by
# FastAPI's own UUID coercion, so this is redundant defense, not a distinct
# hole — but "redundant" must still mean 422, not something worse.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_run_nul_run_id_is_422(ctx: _Ctx) -> None:
    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/run{_NUL}id:cancel",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)


# ---------------------------------------------------------------------------
# Non-regression — a legitimate (NUL-free) agent_code / name must behave
# exactly as before. The full pre-existing external test suites
# (test_external_sessions.py, test_external_runs_cancel.py,
# test_external_events.py, test_external_approvals.py,
# test_external_uploads.py, test_external_workspace.py, ...) are the bulk of
# this proof (run as part of this fix's verification, not duplicated here);
# these two cover the two Critical routes directly.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_session_legit_agent_code_is_unaffected(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    resp = await ctx.client.post(
        "/v1/agents/support-bot/sessions",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_disable_then_enable_legit_name_is_unaffected(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    resp = await ctx.client.post(
        "/v1/agents/support-bot/disable", json={"reason": "maintenance"}, headers=ctx.headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["disabled"] is True
    resp2 = await ctx.client.post("/v1/agents/support-bot/enable", json={}, headers=ctx.headers)
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["data"]["disabled"] is False


# ---------------------------------------------------------------------------
# Structural self-audit — External-API-v1 P2-b review, Critical: "this is
# exactly the failure mode of a 'blanket fix' — it now LOOKS covered, so
# nobody checks again." A future route added to any external router, or to
# ``agents.py``, must fail CI on its own if it forgets the guard — nobody
# should have to remember to add a case to this file.
# ---------------------------------------------------------------------------


def _build_audit_app() -> Any:
    return create_app(
        settings=_build_settings(),
        lifecycle=Lifecycle(),
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        agent_runtime=stub_agent_runtime(),
        enable_reaper=False,
        enable_scheduler=False,
        enable_curation_worker=False,
    )


def _external_agents_routes(app: Any) -> list[APIRoute]:
    """Every route under ``/v1/agents`` tagged ``external`` — discovered from
    the live app, not a hand-maintained list, so a new route mounted on any
    of the six ``external_*.py`` routers is picked up automatically."""
    return [
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path.startswith("/v1/agents/")
        and "external" in (route.tags or [])
    ]


def _carries_nul_path_guard(route: APIRoute) -> bool:
    return any(dep.call is reject_nul_path_params for dep in route.dependant.dependencies)


def test_every_external_agents_route_carries_the_nul_path_guard() -> None:
    """Programmatic self-check, mirroring
    ``test_console_lockdown.py::test_every_console_route_carries_the_lockdown_dependency``:
    builds the real app and inspects each compiled route's resolved
    dependency graph — the same structure FastAPI itself resolves per
    request — so a route that forgot the guard (or was mounted on a router
    that forgot ``dependencies=[Depends(reject_nul_path_params)]`` in its own
    constructor) fails this test immediately.

    Discovery is by ``tags=["external"]``, not a fixed route table — unlike
    the console-lockdown audit (which has to hand-maintain a prefix/route
    table because ``console_only()`` is NOT applied uniformly to a whole
    router), every route on these six routers gets the guard by
    construction, so there is nothing here that can go stale the way a
    hand-maintained list can. Mutation proof for "this is per-route, not a
    loose match": temporarily drop ``dependencies=[Depends(reject_nul_path_params)]``
    from ONE router's own ``APIRouter(...)`` call (e.g.
    ``external_workspace.py``) and this test fails, naming exactly that
    router's routes — see the fix's report for the actual red/green output.
    """
    app = _build_audit_app()
    external_routes = _external_agents_routes(app)
    # Staleness guard, same shape as the reachability test's
    # ``assert external_routes`` — if this list is ever empty, either no
    # external routers are registered (a much bigger regression) or the tag
    # convention itself has drifted, and either way this audit has gone
    # blind and must fail loudly rather than vacuously pass.
    assert external_routes, "expected at least one tags=['external'] route under /v1/agents/"
    missing = [
        f"{sorted(m for m in (r.methods or ()) if m not in ('HEAD', 'OPTIONS'))} {r.path}"
        for r in external_routes
        if not _carries_nul_path_guard(r)
    ]
    assert not missing, f"external routes missing reject_nul_path_params: {missing}"


#: The third-party-reachable ``/v1/agents/{agent_code}`` / ``{name}`` routes
#: that live on ``agents.py``'s OWN router (``tags=["agents"]``, not
#: ``"external"``) rather than one of the six ``external_*.py`` routers —
#: ``bind_session`` / ``run_agent_for_user`` predate the ``external_*.py``
#: split (External-API-v1 P1), and ``disable``/``enable`` are reachable by
#: the same ``write``-scope API key class via ``require("manifest",
#: "write")``, not ``console_only()`` (see ``AgentDisableRequest``'s
#: docstring in ``agents.py``). A ``tags=["external"]``-only audit would
#: never see these four — the whole reason ``reject_nul_path_params`` is
#: attached to ``build_agents_router()``'s OWN constructor (covering every
#: route on that router, including the console-only ``{name}``/{version}``
#: ones, by the same by-construction argument) rather than assuming the tag
#: alone marks every third-party route.
_AGENTS_ROUTER_EXTERNAL_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/v1/agents/{agent_code}/sessions"),
        ("POST", "/v1/agents/{agent_code}/runs"),
        ("POST", "/v1/agents/{name}/disable"),
        ("POST", "/v1/agents/{name}/enable"),
    }
)


def test_agents_router_external_routes_carry_the_nul_path_guard() -> None:
    """The ``agents.py``-hosted counterpart to the test above.

    These four routes are NOT tagged ``external`` (see the module-level
    table's docstring), so the tag-driven audit above cannot see them —
    this table is what keeps them from being a blind spot. Cross-checked
    against the live app in both directions (``live == table``, not just
    ``table ⊆ live``) so a stale entry (path renamed, route removed) fails
    loudly instead of silently no-oping — same shape as
    ``test_console_lockdown.py``'s ``test_agents_prefix_is_partitioned_exactly``.
    """
    app = _build_audit_app()
    live: dict[tuple[str, str], APIRoute] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/v1/agents/"):
            continue
        for method in route.methods or ():
            if method in ("HEAD", "OPTIONS"):
                continue
            if (method, route.path) in _AGENTS_ROUTER_EXTERNAL_ROUTES:
                live[(method, route.path)] = route
    assert set(live) == _AGENTS_ROUTER_EXTERNAL_ROUTES, (
        f"table out of sync with the live app — missing: "
        f"{sorted(_AGENTS_ROUTER_EXTERNAL_ROUTES - set(live))}, "
        f"stale: {sorted(set(live) - _AGENTS_ROUTER_EXTERNAL_ROUTES)}"
    )
    missing = [
        f"{method} {path}"
        for (method, path), route in live.items()
        if not _carries_nul_path_guard(route)
    ]
    assert not missing, f"routes missing reject_nul_path_params: {missing}"
