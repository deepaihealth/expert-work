"""Console plane is closed to API keys — third parties use /v1/agents/... only.

#1153 gave these endpoints a scope gate; P1 closes them to machine principals
outright. A tenant employee's JWT keeps its previous access unchanged.
"""

from __future__ import annotations

import re
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
    # Fix wave (P1 final review, deferred Minor #5) — the prefix table below
    # grew to cover five more routers, but this endpoint-level list did not,
    # so "an API key is denied AND an employee JWT is unaffected" was only
    # ever pinned on the original ten. The newly-closed prefixes are exactly
    # the ones most likely to over-close, so give each a live probe.
    ("GET", "/v1/conversations"),
    ("GET", "/v1/users"),
    ("GET", "/v1/artifacts"),
    ("GET", "/v1/workspace"),
    ("GET", "/v1/triggers"),
    ("GET", "/v1/skill-evolution/promote-requests"),
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
#: Fix wave (P1 final review, C2 addendum) added /v1/skill-evolution: an admin
#: governance surface (promote queue, eval evidence, skill lineage, and the
#: kill-switch engage/release writes) with no third-party story, which carried
#: no ``require(...)`` at all — a zero-scope service-account key read all of it
#: and could flip the kill-switch.
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
    "/v1/skill-evolution",
)

#: Console routes that a **prefix** can never reach, because they live under
#: ``/v1/agents`` — the one mount point the console plane shares with the
#: third-party plane. Closing that prefix wholesale would 403 the external
#: surface, so these carry ``console_only()`` per route instead, and this
#: table is what keeps the self-audit below from having a blind spot there
#: (P1 final review, Critical C2: all five answered 200 to a zero-scope
#: service-account key, and the ``rollback`` one is a **write**).
#:
#: Add a console route under ``/v1/agents``? Add it here too — otherwise the
#: audit cannot see it. ``test_agents_prefix_is_partitioned_exactly`` forces
#: that: any ``/v1/agents`` route missing from all three tables fails it.
_CONSOLE_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/v1/agents"),
        ("GET", "/v1/agents/{name}/{version}/revisions"),
        ("GET", "/v1/agents/{name}/{version}/revisions/{revision}"),
        ("POST", "/v1/agents/{name}/{version}/revisions/{revision}/rollback"),
        ("GET", "/v1/agents/{agent_name}/{agent_version}/users"),
    }
)

#: The third-party surface: every ``/v1/agents`` route an API key IS meant to
#: reach (design spec §四 endpoints 1-7, plus the pre-existing session bind).
#: The head risk of the route-level lockdown above is over-reach, so these are
#: probed with a real request and must never come back 403.
_EXTERNAL_AGENT_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/v1/agents/{agent_code}/runs"),
        ("POST", "/v1/agents/{agent_code}/runs/{run_id}:cancel"),
        ("GET", "/v1/agents/{agent_code}/runs/{run_id}/events"),
        ("GET", "/v1/agents/{agent_code}/sessions"),
        ("POST", "/v1/agents/{agent_code}/sessions"),
        ("GET", "/v1/agents/{agent_code}/sessions/{session_id}/messages"),
        ("POST", "/v1/agents/{agent_code}/uploads"),
        ("POST", "/v1/agents/{agent_code}/runs/{run_id}:decide"),
    }
)

#: ``/v1/agents`` routes deliberately left open to API keys because they hold
#: no tenant data and no per-tenant state: the AgentSpec JSON Schema is the
#: same static document for every caller. Kept as an explicit third bucket so
#: "open" is a decision recorded here, not a route that quietly fell through.
_OPEN_AGENT_ROUTES: frozenset[tuple[str, str]] = frozenset({("GET", "/v1/agents/schema")})

#: Routes under ``/v1/agents`` whose authorization lives inside the handler
#: (``ensure_resource_access``) or on a ``require(...)`` dependency rather than
#: on ``console_only()``. Read the bucket name precisely: these reject an
#: **under-scoped** key, not every key. A zero-scope key gets 403 — but an
#: ``admin``-scope key passes, because ``rbac._collect_roles`` maps that scope
#: to ``Role.ADMIN``; measured on one: ``GET /v1/agents/templates`` → 200,
#: ``POST /v1/agents/{name}/disable`` → 200, ``DELETE /v1/agents/{name}/{version}``
#: → 204 (the agent really is deleted). That is the documented meaning of the
#: ``admin`` scope ("never hand it to a third-party integrator — it is the whole
#: tenant"), not a hole this table hides; it is spelled out because reading the
#: bucket as "API keys cannot reach these" would make the key-reachable surface
#: under ``/v1/agents`` look like exactly the eight external routes, and it is
#: not. Listed so the partition test below stays exhaustive without claiming
#: they are external.
_SELF_GATED_AGENT_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/v1/agents"),
        ("GET", "/v1/agents/templates"),
        ("POST", "/v1/agents/fork"),
        ("GET", "/v1/agents/{name}/{version}"),
        ("PUT", "/v1/agents/{name}/{version}"),
        ("DELETE", "/v1/agents/{name}/{version}"),
        ("POST", "/v1/agents/{name}/disable"),
        ("POST", "/v1/agents/{name}/enable"),
    }
)

#: Sample values for the path params that appear in ``/v1/agents`` route
#: templates, so the partition test can turn a template into a real request.
#: A new param name trips the assertion in ``_concretize`` — add it here.
_SAMPLE_PARAMS: dict[str, str] = {
    "agent_code": "support-bot",
    "name": "support-bot",
    "version": "1.0.0",
    "agent_name": "support-bot",
    "agent_version": "1.0.0",
    "session_id": str(_TID),
    "run_id": str(_RID),
    "revision": "1",
}

_PARAM_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(?::[a-zA-Z_]+)?\}")


def _concretize(path_template: str) -> str:
    def _sub(match: re.Match[str]) -> str:
        param = match.group(1)
        assert param in _SAMPLE_PARAMS, (
            f"no sample value for path param {param!r} (seen in {path_template!r})"
        )
        return _SAMPLE_PARAMS[param]

    return _PARAM_RE.sub(_sub, path_template)


def _agents_routes(app: Any) -> set[tuple[str, str]]:
    """Every ``(method, path)`` the real app mounts under ``/v1/agents``."""
    found: set[tuple[str, str]] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/v1/agents"):
            continue
        for method in route.methods or ():
            if method in ("HEAD", "OPTIONS"):
                continue
            found.add((method, route.path))
    return found


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
    seen_console_routes: set[tuple[str, str]] = set()
    missing: list[str] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        methods = {m for m in (route.methods or ()) if m not in ("HEAD", "OPTIONS")}
        # Fix wave (P1 final review, C2) — the audit is no longer purely
        # prefix-driven. ``/v1/agents`` hosts the console AND the third-party
        # plane, so its console routes are enumerated one by one in
        # ``_CONSOLE_ROUTES``; a prefix sweep would either miss them (what
        # happened) or 403 the external plane (what must not happen).
        route_level = {(m, route.path) for m in methods} & _CONSOLE_ROUTES
        if not route.path.startswith(_CONSOLE_PREFIXES) and not route_level:
            continue
        seen_console_routes |= route_level
        checked.append(f"{sorted(methods)} {route.path}")
        checked_paths.append(route.path)
        dep_qualnames = {dep.call.__qualname__ for dep in route.dependant.dependencies}
        if _CONSOLE_ONLY_DEP_QUALNAME not in dep_qualnames:
            missing.append(f"{sorted(methods)} {route.path}")
    assert checked, "no console-plane routes discovered — prefix list is stale"
    # Same staleness guard the prefixes get: an entry here that no longer
    # matches a real route (path renamed, verb changed) would silently stop
    # auditing anything while the rest of the table keeps the test green.
    assert seen_console_routes == _CONSOLE_ROUTES, (
        f"_CONSOLE_ROUTES entries match no live route: "
        f"{sorted(_CONSOLE_ROUTES - seen_console_routes)}"
    )
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


# ---------------------------------------------------------------------------
# P1 final review, Critical C2 — /v1/agents hosts BOTH planes
# ---------------------------------------------------------------------------


def test_agents_prefix_is_partitioned_exactly() -> None:
    """Every ``/v1/agents`` route must be classified, one bucket each.

    This is the structural fix for how C2 happened: the console lockdown was
    prefix-driven, and ``/v1/agents`` is the one prefix a sweep cannot close,
    so five console routes under it were never audited by anything. A new
    route added here — either plane — now fails this test until someone
    decides which bucket it belongs in.
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
    live = _agents_routes(app)
    classified = (
        _CONSOLE_ROUTES | _EXTERNAL_AGENT_ROUTES | _OPEN_AGENT_ROUTES | _SELF_GATED_AGENT_ROUTES
    )
    assert live - classified == set(), (
        f"unclassified /v1/agents routes — each needs a table: {sorted(live - classified)}"
    )
    assert classified - live == set(), (
        f"table entries match no live route (stale): {sorted(classified - live)}"
    )
    # The buckets must not overlap, or "console" and "external" could both
    # claim a route and the two assertions below would contradict silently.
    buckets = [
        _CONSOLE_ROUTES,
        _EXTERNAL_AGENT_ROUTES,
        _OPEN_AGENT_ROUTES,
        _SELF_GATED_AGENT_ROUTES,
    ]
    for i, first in enumerate(buckets):
        for second in buckets[i + 1 :]:
            assert not (first & second), f"route classified twice: {sorted(first & second)}"


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), sorted(_CONSOLE_ROUTES))
async def test_api_key_is_denied_on_the_console_routes_under_agents(
    ctx: _Ctx, method: str, path: str
) -> None:
    """The C2 assertion itself: these five answered 200 to a zero-scope key.

    ``ctx.key_headers`` carries the widest (``admin``) scope, so a 403 here
    cannot be a scope shortfall — it can only be ``console_only()``. The
    message is pinned for the same reason the prefix test pins it: it is the
    only pointer a third party gets toward the external plane.
    """
    await ctx.seed_agent()
    resp = await ctx.client.request(method, _concretize(path), headers=ctx.key_headers)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["message"] == (
        "console API is not available to API keys; use /v1/agents/{agent_code}/…"
    ), resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), sorted(_CONSOLE_ROUTES))
async def test_employee_jwt_still_reaches_the_console_routes_under_agents(
    ctx: _Ctx, method: str, path: str
) -> None:
    """``console_only()`` keys off ``subject_type == "service_account"`` only —
    asserted, not assumed. An employee JWT must be completely unaffected."""
    await ctx.seed_agent()
    resp = await ctx.client.request(method, _concretize(path), headers=ctx.headers)
    assert resp.status_code != 403, resp.text


@pytest.mark.asyncio
async def test_rollback_under_scoped_key_gets_the_console_pointer_not_a_role_denial(
    ctx: _Ctx,
) -> None:
    """Wrapup2 N-B — pin the rollback route's ``dependencies=[...]`` order.

    ``console_only()`` must run before ``Depends(require("manifest", "write"))``
    so an **under-scoped** API key (here: zero-scope) still gets the
    console-plane pointer message — the only hint a third party gets toward
    ``/v1/agents/{agent_code}/...`` — instead of a bare role denial. This is
    deliberately NOT ``ctx.key_headers`` (admin scope): an admin-scope key
    passes the ``manifest:write`` role check either way, so it sees the same
    message regardless of dependency order and cannot catch a swap. A
    zero/under-scoped key is the population the order actually affects.
    """
    await ctx.seed_agent()
    zero_scope_jwt = make_test_jwt(
        tenant_id=ctx.tenant_id,
        subject="sa-zero-scope",
        sub_type="service_account",
        roles=(),
        scopes=(),
    )
    zero_scope_headers = {"Authorization": f"Bearer {zero_scope_jwt}"}
    resp = await ctx.client.post(
        "/v1/agents/support-bot/1.0.0/revisions/1/rollback", headers=zero_scope_headers
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["message"] == (
        "console API is not available to API keys; use /v1/agents/{agent_code}/…"
    ), resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), sorted(_EXTERNAL_AGENT_ROUTES))
async def test_api_key_still_reaches_every_external_agents_route(
    ctx: _Ctx, method: str, path: str
) -> None:
    """The head risk of a route-level lockdown under a shared prefix: catching
    the third-party plane in the blast radius.

    Every one of the eight external routes must stay reachable to an API key.
    A missing body / query param answers 4xx here — that is fine and expected;
    what must never appear is 403, which is the only thing ``console_only()``
    can produce.
    """
    await ctx.seed_agent()
    resp = await ctx.client.request(method, _concretize(path), headers=ctx.key_headers)
    assert resp.status_code != 403, f"{method} {path} → {resp.status_code} {resp.text}"
