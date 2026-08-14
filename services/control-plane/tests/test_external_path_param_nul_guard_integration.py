"""Real-Postgres proof for the path-param NUL guard — External-API-v1 P2-b.

``test_external_path_param_nul_guard.py`` proves the guard rejects a NUL in
``agent_code`` / ``name`` on every third-party-reachable route, using the
fast in-memory backend — sufficient there because the guard runs before any
handler code, so it doesn't matter whether the backend underneath would
actually have crashed.

This file exists to answer a narrower, sharper question: does removing the
guard reproduce the ACTUAL ``asyncpg.CharacterNotInRepertoireError`` → 500
the review reported, or would the request have failed some other, more
boring way (a 404, or nothing at all) regardless? Tracing every handler
shows the answer differs by route:

- ``bind_session`` / ``run_agent_for_user`` (``agents.py``) and
  ``upload_for_user`` (``external_uploads.py``) all call
  ``_resolve_session``, which checks ``disable_service.is_disabled(tenant_id,
  agent_code)`` FIRST — i.e. ``AgentDisableStore.get`` — a ``text``-column
  ``WHERE`` comparison. A NUL here really does reach SQL and really does
  500 without the guard.
- ``list_sessions`` (``GET .../sessions``) passes ``agent_code`` into
  ``ThreadMetaStore.list_by_tenant(..., agent_name=agent_code, ...)`` — but
  ONLY once ``lookup_external_user_id`` has resolved a KNOWN ``user_id`` to
  a real ``tenant_user`` row; for an unseen ``user_id`` the function returns
  an empty list before ever reaching that query. So this route needs a
  pre-existing user (seeded via a prior legitimate ``bind_session`` call) to
  actually reach the crash.
- ``disable_agent`` / ``enable_agent`` call ``_agent_exists`` →
  ``AgentSpecStore.list_by_tenant(tenant_id=..., name=name, limit=1)`` —
  also a direct ``WHERE`` comparison. Also really crashes.
- ``get_messages`` / ``rename_session`` / ``archive_session``
  (``external_sessions.py``) and ``cancel_run`` / ``decide_run`` /
  ``stream_run_events`` (which all route through ``load_owned_run`` →
  ``load_owned_session``) only ever compare ``agent_code`` against an
  ALREADY-FETCHED row's ``meta.agent_name`` in Python — ``threads.get(...)``
  is keyed on ``session_id`` alone. A NUL ``agent_code`` on these routes
  simply never matches (Python string comparison, not SQL), so removing the
  guard downgrades them to a 404 (``SESSION_NOT_FOUND`` / ``RUN_NOT_FOUND``),
  not a 500.
- ``list_workspace_files`` / ``download_workspace_file``
  (``external_workspace.py``) explicitly ``del agent_code`` — the workspace
  is ``(tenant, user)``-scoped, not per-agent (see that module's docstring).
  Removing the guard there does not change behavior AT ALL for a NUL
  ``agent_code`` — it was always discarded.

So this file's tests cover exactly the routes that provably reach SQL
through ``agent_code`` / ``name``: ``bind_session``, ``run_agent_for_user``,
``upload_for_user``, ``list_sessions`` (with a pre-seeded user), and
``disable_agent`` / ``enable_agent``. These are also exactly the routes the
review's own citations name (``AgentDisableStore.get`` /
``AgentSpecStore.list_by_tenant``) — the review's "all 11 external routes"
framing was a generalization from "agent_code appears in the path" rather
than a claim that all 11 individually reach SQL through it; see the fix
report for the corrected count. The guard is still attached uniformly to
every route (defense in depth + one less thing to reason about per-route),
but the 500-reproduction proof below is scoped to where a 500 is actually
reachable.

Ablation self-proof (performed by hand, not encoded as a permanent test):
temporarily remove ``dependencies=[Depends(reject_nul_path_params)]`` from
a router's ``APIRouter(...)`` call, rerun this file, and the affected tests
here fail with ``resp.status_code == 500`` (verified during this fix — see
the report for the actual captured output) instead of the expected 422.
Restoring the dependency turns them green again.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from control_plane.app import create_app
from control_plane.settings import Settings
from expert_work.protocol import AgentSpec
from expert_work.runtime.runs import InMemoryRunEventStore, InMemoryRunStore
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier, make_test_jwt

pytestmark = pytest.mark.integration

# Migrations live in the expert-work-persistence package, not control-plane —
# same path shape as test_sql_store_wiring_integration.py.
_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "packages/expert-work-persistence/alembic.ini"

_NUL = "%00"

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


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
def sql_settings(postgres_container: PostgresContainer) -> Iterator[Settings]:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    yield Settings(
        service_name="control_plane_sql_test",
        store_backend="sql",
        db_dsn=_async_dsn(postgres_container),
        # testcontainers Postgres is a direct connection, not PgBouncer.
        db_pgbouncer_mode=False,
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
        console_headers: dict[str, str],
    ):
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.headers = headers
        #: Employee JWT — ``headers`` is a service account, which
        #: ``console_only()`` bars from console routes. See the fixture.
        self.console_headers = console_headers

    async def seed_agent(self) -> None:
        await self.app.state.agent_spec_repo.create(
            tenant_id=self.tenant_id, spec=_spec(), spec_sha256="a" * 64, created_by="seed"
        )


@pytest.fixture
async def ctx(sql_settings: Settings) -> AsyncIterator[_Ctx]:
    run_store = InMemoryRunStore()
    run_event_store = InMemoryRunEventStore()
    app = create_app(
        settings=sql_settings,
        jwt_verifier=build_test_jwt_verifier(),
        agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=run_event_store),
        run_repo=run_store,
        run_event_repo=run_event_store,
        enable_reaper=False,
        enable_scheduler=False,
        enable_curation_worker=False,
    )
    tenant_id = uuid4()
    # External-API-v1 P2-b security fix (external_only()) — the external
    # plane is now service-account-only; this file's employee JWT was a
    # borrowed fixture (predates the gate), not a deliberate test of
    # console-JWT access.
    jwt = make_test_jwt(
        tenant_id=tenant_id,
        subject="sa-test",
        sub_type="service_account",
        roles=(),
        scopes=("admin",),
    )
    headers = {"Authorization": f"Bearer {jwt}"}
    # The comment above used to continue: "``disable``/``enable`` (also
    # exercised here) are NOT gated and remain reachable either way." The
    # console lockdown later in P2-b made that false — both are
    # ``console_only()`` now, so the service account gets 403. The NUL cases
    # on those two routes still pass with the key, because
    # ``reject_nul_path_params`` is a ROUTER-level dependency and runs before
    # the route-level gate; only the "legitimate name still 200s"
    # non-regression case reaches the handler and therefore needs an employee.
    console_jwt = make_test_jwt(
        tenant_id=tenant_id,
        subject="employee-test",
        sub_type="user",
        roles=("admin",),
        scopes=(),
    )
    console_headers = {"Authorization": f"Bearer {console_jwt}"}
    # ``raise_app_exceptions=False`` matches a real deployment: Starlette's
    # ``ServerErrorMiddleware`` (always the outermost middleware) turns an
    # uncaught exception into a bare-text 500 response AND re-raises it for
    # the ASGI server's own logging — a real uvicorn process swallows that
    # re-raise per-connection, but ``httpx.ASGITransport``'s default
    # (``raise_app_exceptions=True``) lets it propagate into the TEST
    # process instead of the response object. With the guard in place this
    # never fires either way; it only matters for the ablation exercise.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        yield _Ctx(client, app, tenant_id, headers, console_headers)


def _assert_envelope_422(resp: Any) -> None:
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert "detail" not in body
    assert body["success"] is False
    assert body["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_sql_bind_session_nul_agent_code_is_422_not_500(ctx: _Ctx) -> None:
    """``_resolve_session``'s FIRST call is ``disable_service.is_disabled`` —
    ``AgentDisableStore.get(tenant_id, agent_name='support\\x00bot')`` — a
    real ``WHERE`` comparison against Postgres. Ablated, this is the exact
    crash the review reported."""
    resp = await ctx.client.post(
        f"/v1/agents/support{_NUL}bot/sessions",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)


@pytest.mark.asyncio
async def test_sql_run_agent_for_user_nul_agent_code_is_422_not_500(ctx: _Ctx) -> None:
    resp = await ctx.client.post(
        f"/v1/agents/support{_NUL}bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)


@pytest.mark.asyncio
async def test_sql_upload_nul_agent_code_is_422_not_500(ctx: _Ctx) -> None:
    """Mint branch (``session_id`` omitted) — ``upload_for_user`` calls the
    same ``_resolve_session`` as ``bind_session``."""
    resp = await ctx.client.post(
        f"/v1/agents/support{_NUL}bot/uploads",
        data={"user_id": "cust-77"},
        files={"file": ("notes.txt", b"hello", "text/plain")},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)


@pytest.mark.asyncio
async def test_sql_list_sessions_nul_agent_code_is_422_not_500(ctx: _Ctx) -> None:
    """Needs a user this tenant has already seen — ``lookup_external_user_id``
    must resolve a real row so the handler actually reaches
    ``ThreadMetaStore.list_by_tenant(..., agent_name=agent_code, ...)``; an
    unseen ``user_id`` would return an empty list before ever touching that
    query, which would falsely 200 instead of exercising the crash."""
    await ctx.seed_agent()
    bound = await ctx.client.post(
        "/v1/agents/support-bot/sessions", json={"user_id": "cust-77"}, headers=ctx.headers
    )
    assert bound.status_code == 201, bound.text

    resp = await ctx.client.get(
        f"/v1/agents/support{_NUL}bot/sessions",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)


@pytest.mark.asyncio
async def test_sql_disable_agent_nul_name_is_422_not_500(ctx: _Ctx) -> None:
    """``_agent_exists`` → ``AgentSpecStore.list_by_tenant(tenant_id=...,
    name='support\\x00bot', limit=1)`` — the second crash site the review
    cites, on a route outside every ``external_*.py`` router."""
    resp = await ctx.client.post(
        f"/v1/agents/support{_NUL}bot/disable",
        json={},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)


@pytest.mark.asyncio
async def test_sql_enable_agent_nul_name_is_422_not_500(ctx: _Ctx) -> None:
    resp = await ctx.client.post(
        f"/v1/agents/support{_NUL}bot/enable",
        json={},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)


# ---------------------------------------------------------------------------
# Non-regression under the real backend — same routes, legitimate params.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sql_bind_session_legit_agent_code_still_201s(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    resp = await ctx.client.post(
        "/v1/agents/support-bot/sessions", json={"user_id": "cust-77"}, headers=ctx.headers
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_sql_disable_agent_legit_name_still_200s(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    # Console route — needs the employee identity (see the fixture).
    resp = await ctx.client.post(
        "/v1/agents/support-bot/disable",
        json={"reason": "maintenance"},
        headers=ctx.console_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["disabled"] is True
    # Guard the premise: the API key must NOT reach this handler. Without
    # this, someone removing the gate would see every assertion here still
    # green and the fixture comment above silently become wrong again.
    via_key = await ctx.client.post(
        "/v1/agents/support-bot/disable", json={"reason": "maintenance"}, headers=ctx.headers
    )
    assert via_key.status_code == 403, via_key.text
