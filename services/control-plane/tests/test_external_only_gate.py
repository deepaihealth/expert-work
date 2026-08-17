"""External plane is closed to employee (console) JWTs — third parties use a
service-account API key only. Dual of ``test_console_lockdown.py``.

External-API-v1 P2-b security fix: the external plane
(``/v1/agents/{agent_code}/...``) had no admin-only guard on cross-user
access (the console plane's ``resolve_target_user_id`` has one; the
external plane's ``lookup_external_user_id`` never did, because the
external plane's whole design assumes the caller is a machine principal
acting *for* the ``user_id`` it names, not an employee acting for
themselves). An employee JWT hitting this plane inherited only its RBAC
role — real, measured impact: a plain ``viewer`` read any end user's
workspace files (``GET .../workspace/file`` → 200 +
``SSN,salary\\n123-45-6789,220000`` for a real seeded file) and full
conversation history; an ``operator`` could run agents, decide approvals, or
rename/archive sessions as any end user in the tenant. ``external_only()``
(``api/_authz.py``) closes it: 403 any principal whose ``subject_type`` is
not ``"service_account"``.
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

from control_plane.api._authz import external_only
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AgentSpec, AuditQuery, Role
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

#: The full third-party surface under ``/v1/agents`` — same fifteen routes as
#: ``test_console_lockdown.py``'s ``_EXTERNAL_AGENT_ROUTES``. Kept as an
#: independent local table (not imported) — each security-gate test file in
#: this suite owns its own route table so it can't silently start passing
#: because a SIBLING file's table drifted.
_EXTERNAL_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/v1/agents/{agent_code}/sessions"),
        ("GET", "/v1/agents/{agent_code}/sessions"),
        ("GET", "/v1/agents/{agent_code}/sessions/{session_id}/messages"),
        ("PATCH", "/v1/agents/{agent_code}/sessions/{session_id}"),
        ("DELETE", "/v1/agents/{agent_code}/sessions/{session_id}"),
        ("POST", "/v1/agents/{agent_code}/runs"),
        ("POST", "/v1/agents/{agent_code}/runs/{run_id}:cancel"),
        ("POST", "/v1/agents/{agent_code}/runs/{run_id}:decide"),
        ("GET", "/v1/agents/{agent_code}/runs/{run_id}/events"),
        ("POST", "/v1/agents/{agent_code}/uploads"),
        ("GET", "/v1/agents/{agent_code}/workspace/files"),
        ("GET", "/v1/agents/{agent_code}/workspace/file"),
        # 阶段 3 PR-B — 对外产物视图(external_artifacts.py)三个端点。list +
        # download 两个端点最初漏登记在这张表里(评审 Important B),导致没有
        # 任何测试用真实员工 JWT 请求过它们并断言 403 —— tag 驱动的静态审计
        # 只查依赖有没有挂上,查不出 handler 内部对某种 principal 特殊放行这
        # 类问题。delete 端点(Task 3)登记时一并补齐,下面三条都已登记。
        ("GET", "/v1/agents/{agent_code}/artifacts"),
        ("GET", "/v1/agents/{agent_code}/artifacts/download"),
        ("DELETE", "/v1/agents/{agent_code}/artifacts"),
        # 附件模型统一 Task 4 — 对外下载端点(external_uploads.py)。
        ("GET", "/v1/agents/{agent_code}/uploads/{upload_id}"),
    }
)

_SAMPLE_PARAMS: dict[str, str] = {
    "agent_code": "support-bot",
    "session_id": str(_TID),
    "run_id": str(_RID),
    "upload_id": "upl_00000000-0000-4000-8000-000000000013",
}


def _concretize(path_template: str) -> str:
    out = path_template
    for name, value in _SAMPLE_PARAMS.items():
        out = out.replace(f"{{{name}}}", value)
    assert "{" not in out, f"no sample value for a path param in {path_template!r}"
    return out


_EXTERNAL_ONLY_MESSAGE = (
    "external API is not available to console credentials; use a service-account API key"
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
        employee_headers: dict[str, dict[str, str]],
        key_headers: dict[str, dict[str, str]],
        audit_store: InMemoryAuditLogStore,
    ) -> None:
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        #: keyed by role name: "viewer" / "operator" / "admin"
        self.employee_headers = employee_headers
        #: keyed by scope name: "read" / "write" / "admin"
        self.key_headers = key_headers
        self.audit_store = audit_store

    async def seed_agent(self) -> None:
        await self.app.state.agent_spec_repo.create(
            tenant_id=self.tenant_id, spec=_spec(), spec_sha256="a" * 64, created_by="seed"
        )

    async def bind_session(self, *, headers: dict[str, str], user_id: str = "cust-77") -> str:
        resp = await self.client.post(
            "/v1/agents/support-bot/sessions",
            json={"user_id": user_id},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        return str(resp.json()["data"]["session_id"])


@pytest.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    run_store = InMemoryRunStore()
    run_event_store = InMemoryRunEventStore()
    audit_store = InMemoryAuditLogStore()
    app = create_app(
        settings=_build_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(audit_store),
        agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=run_event_store),
        run_repo=run_store,
        run_event_repo=run_event_store,
    )
    tenant_id = uuid4()

    def _employee(role: Role) -> dict[str, str]:
        jwt = make_test_jwt(tenant_id=tenant_id, subject=str(uuid4()), roles=(role.value,))
        return {"Authorization": f"Bearer {jwt}"}

    def _key(scope: str) -> dict[str, str]:
        jwt = make_test_jwt(
            tenant_id=tenant_id,
            subject=f"sa-{scope}",
            sub_type="service_account",
            roles=(),
            scopes=(scope,),
        )
        return {"Authorization": f"Bearer {jwt}"}

    employee_headers = {
        "viewer": _employee(Role.VIEWER),
        "operator": _employee(Role.OPERATOR),
        "admin": _employee(Role.ADMIN),
    }
    key_headers = {
        "read": _key("read"),
        "write": _key("write"),
        "admin": _key("admin"),
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        yield _Ctx(client, app, tenant_id, employee_headers, key_headers, audit_store)


# ---------------------------------------------------------------------------
# Behavioral — employee JWTs of every role are denied on every external route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), sorted(_EXTERNAL_ROUTES))
@pytest.mark.parametrize("role", ["viewer", "operator", "admin"])
async def test_employee_jwt_is_denied_on_every_external_route(
    ctx: _Ctx, role: str, method: str, path: str
) -> None:
    """The C-level fact this fix closes: a role that could reach the
    console-side equivalent (or not) makes no difference here — every
    employee role is uniformly 403'd by ``external_only()``, which runs
    before the per-route RBAC check (``require(...)``). Pinning the message
    proves the 403 came from THIS gate, not a scope/role shortfall that
    would have produced a different message and could mask a route that
    never got the guard wired at all."""
    resp = await ctx.client.request(method, _concretize(path), headers=ctx.employee_headers[role])
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["message"] == _EXTERNAL_ONLY_MESSAGE, resp.text


@pytest.mark.asyncio
async def test_employee_jwt_denial_audit_includes_the_denied_path(ctx: _Ctx) -> None:
    """Dual of ``test_console_lockdown.py``'s same-named test. The deny audit's
    ``resource_id`` (``"external:non_service_account_denied"``) is a constant
    shared by every route this gate closes; ``details.path`` now carries the
    real request path so triage can tell routes apart.
    """
    resp = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "cust-77"},
        headers=ctx.employee_headers["viewer"],
    )
    assert resp.status_code == 403, resp.text

    page = await ctx.audit_store.query(AuditQuery(tenant_id=ctx.tenant_id, limit=1000))
    matches = [r for r in page.entries if r.resource_id == "external:non_service_account_denied"]
    assert len(matches) == 1, [r.model_dump() for r in page.entries]
    assert matches[0].details.get("path") == "/v1/agents/support-bot/sessions"
    # resource_id itself must stay the stable constant — see the brief: it
    # may already be relied on by queries/dashboards, only `details` is free.
    assert matches[0].resource_id == "external:non_service_account_denied"


# ---------------------------------------------------------------------------
# Behavioral — a service-account key is never blocked BY THIS GATE, at any
# scope. A scope shortfall (e.g. a read-only key on a write route) may still
# 403 from require_key_scope/require() underneath — that is pre-existing,
# unrelated behavior this fix must not change, so the assertion is precise:
# never the external_only() message, not "never 403".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), sorted(_EXTERNAL_ROUTES))
@pytest.mark.parametrize("scope", ["read", "write", "admin"])
async def test_service_account_key_is_never_blocked_by_the_external_only_gate(
    ctx: _Ctx, scope: str, method: str, path: str
) -> None:
    resp = await ctx.client.request(method, _concretize(path), headers=ctx.key_headers[scope])
    if resp.status_code == 403:
        assert resp.json()["detail"]["message"] != _EXTERNAL_ONLY_MESSAGE, (
            f"{method} {path} (scope={scope}) was blocked by external_only(), "
            f"not by a pre-existing scope/role check: {resp.text}"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), sorted(_EXTERNAL_ROUTES))
async def test_admin_scope_key_still_reaches_every_external_route(
    ctx: _Ctx, method: str, path: str
) -> None:
    """Same shape as ``test_console_lockdown.py``'s
    ``test_api_key_still_reaches_every_external_agents_route`` — admin scope
    is the widest, so a 403 here can only be ``external_only()`` (there is no
    seeded agent, so the happy status is 404, not 200; that is fine — 403 is
    the only status this test rules out)."""
    resp = await ctx.client.request(method, _concretize(path), headers=ctx.key_headers["admin"])
    assert resp.status_code != 403, f"{method} {path} -> {resp.status_code} {resp.text}"


# ---------------------------------------------------------------------------
# Behavioral — genuine happy-path smoke: prove "unaffected" means the exact
# same 2xx it always did, not merely "not 403". The exhaustive per-route
# happy-path assertions live in the dedicated external_*.py test suites
# (test_external_sessions.py, etc.) run as service-account callers; these
# two are a direct, no-detour confirmation in this file.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_scope_key_bind_session_still_201s(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    session_id = await ctx.bind_session(headers=ctx.key_headers["admin"])
    assert UUID(session_id)


@pytest.mark.asyncio
async def test_admin_scope_key_list_sessions_still_200s(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    await ctx.bind_session(headers=ctx.key_headers["admin"], user_id="cust-77")
    resp = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "cust-77"},
        headers=ctx.key_headers["admin"],
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]["sessions"]) == 1


# ---------------------------------------------------------------------------
# Behavioral — no credentials at all is still 401 (AuthMiddleware, upstream
# of every dependency including external_only()). Not a claim about this
# gate specifically; a guard against this fix accidentally reordering
# something ahead of authentication.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/agents/{agent_code}/sessions"),
        ("GET", "/v1/agents/{agent_code}/workspace/file"),
    ],
)
async def test_no_credentials_is_still_401(ctx: _Ctx, method: str, path: str) -> None:
    resp = await ctx.client.request(method, _concretize(path))
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Structural self-audit — mirrors
# test_console_lockdown.py::test_every_console_route_carries_the_lockdown_dependency
# and test_external_path_param_nul_guard.py's two-table pattern. Nobody
# should have to remember to add a case above when a new external route is
# mounted; this fails CI on its own if they forget the guard.
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
    """Every route tagged ``external`` — discovered from the live app, not a
    hand-maintained list, so a new route mounted on any of the seven
    ``external_*.py`` routers is picked up automatically.

    Discovery is by ``tags=["external"]`` **alone** — no path-prefix
    condition. All of this suite's tests used to also require
    ``route.path.startswith("/v1/agents/")``, which made ``GET
    /v1/agent-catalog`` (phase 3's first external route NOT under
    ``/v1/agents/``) invisible to this audit while it stayed green. Only
    ``external_*.py`` ever sets this tag, so the tag alone is already
    precise, and dropping the prefix means any future new prefix is covered
    automatically instead of silently falling outside this audit.
    """
    return [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and "external" in (route.tags or [])
    ]


_EXTERNAL_ONLY_DEP_QUALNAME = f"{external_only.__qualname__}.<locals>._dep"


def _carries_external_only_guard(route: APIRoute) -> bool:
    return any(
        dep.call.__qualname__ == _EXTERNAL_ONLY_DEP_QUALNAME for dep in route.dependant.dependencies
    )


def test_every_external_agents_route_carries_the_external_only_guard() -> None:
    """Programmatic self-check: builds the real app and inspects each
    compiled route's resolved dependency graph — the same structure FastAPI
    itself resolves per request — so a route that forgot the guard (or was
    mounted on a router that forgot ``Depends(external_only())`` in its own
    ``APIRouter(...)`` constructor) fails this test immediately.

    Mutation proof for "this is per-route, not a loose match": temporarily
    drop ``Depends(external_only())`` from ONE router's own ``APIRouter(...)``
    call (e.g. ``external_workspace.py``) and this test fails, naming exactly
    that router's routes — see the fix's report for the actual red/green
    output.
    """
    app = _build_audit_app()
    external_routes = _external_agents_routes(app)
    assert external_routes, "expected at least one tags=['external'] route"
    missing = [
        f"{sorted(m for m in (r.methods or ()) if m not in ('HEAD', 'OPTIONS'))} {r.path}"
        for r in external_routes
        if not _carries_external_only_guard(r)
    ]
    assert not missing, f"external routes missing external_only(): {missing}"


def test_the_discovery_is_not_tied_to_the_agents_path_prefix() -> None:
    """Discovery must key off ``tags=["external"]`` alone, not a path prefix.

    ``GET /v1/agent-catalog`` (phase 3) is the first external route NOT
    under ``/v1/agents/``. If the discoverer still carried a path-prefix
    condition, this route would fall out of the audit entirely — while the
    audit itself stayed green. That is the worst failure mode: it looks
    protective but covers nothing.
    """
    app = _build_audit_app()
    paths = {r.path for r in _external_agents_routes(app)}
    assert "/v1/agent-catalog" in paths, (
        "external route /v1/agent-catalog was not picked up by the "
        "discoverer — it is still filtering by path prefix"
    )


#: The third-party-reachable ``/v1/agents/{agent_code}`` routes that live on
#: ``agents.py``'s OWN router (``tags=["agents"]``, not ``"external"``)
#: rather than one of the seven ``external_*.py`` routers —
#: ``bind_session`` / ``run_agent_for_user`` predate the ``external_*.py``
#: split (External-API-v1 P1). A ``tags=["external"]``-only audit would never
#: see these two — the same blind spot ``test_external_path_param_nul_guard.py``
#: documents for the NUL-path-param guard on this router.
#:
#: ``disable`` / ``enable`` (``POST /v1/agents/{name}/disable|enable``) are
#: deliberately NOT here. They are reachable by an employee JWT BY DESIGN —
#: the admin-ui kill-switch button calls them directly
#: (``apps/admin-ui/src/api/agents.ts::disableAgent``) — and neither
#: operation is scoped to a ``user_id``: there is no per-end-user data or
#: action here for an under-privileged employee to reach that the console
#: side would have gated and the external side doesn't. Gating them with
#: ``external_only()`` would 403 the admin-ui kill switch for every
#: employee, a real functional regression, to close a vulnerability shape
#: (cross-user data/action access) that does not apply to a tenant-wide
#: manifest flag. (A ``write``-scope API key used to reach them too —
#: ``require("manifest", "write")`` maps that scope to OPERATOR — until
#: Backlog Task 2, spec/external-api-v1-p2b, gave both routes
#: ``console_only()``; that axis is closed now, which is orthogonal to the
#: ``external_only()`` question this table answers.)
_AGENTS_ROUTER_EXTERNAL_ONLY_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/v1/agents/{agent_code}/sessions"),
        ("POST", "/v1/agents/{agent_code}/runs"),
    }
)

#: The full universe this correspondence check walks: every third-party- (or
#: write-scope-key-) reachable ``/v1/agents`` route hosted on agents.py's OWN
#: router, NOT just the subset that must carry the guard. Same four routes as
#: ``test_external_path_param_nul_guard.py``'s ``_AGENTS_ROUTER_EXTERNAL_ROUTES``.
#: Kept separate from ``_AGENTS_ROUTER_EXTERNAL_ONLY_ROUTES`` on purpose (a
#: shrunk/stale table must fail loudly, not vacuously) — see
#: ``_agents_router_own_candidate_routes`` below for how ``live`` is now built:
#: a TRUE full enumeration of the app, independent of this table's contents.
#:
#: Self-audit blind-spot fix (found in review): the ORIGINAL version of the
#: enumeration below built ``live`` by walking ``app.routes`` and keeping only
#: entries that were ALREADY present in this table
#: (``if (method, route.path) in _AGENTS_ROUTER_UNIVERSE: live[...] = route``).
#: That construction makes ``set(live)`` a subset of ``_AGENTS_ROUTER_UNIVERSE``
#: BY CONSTRUCTION, so ``set(live) == _AGENTS_ROUTER_UNIVERSE`` can only ever
#: fail in the "stale" direction (table lists a route the app no longer has) —
#: the dangerous direction, a genuinely NEW route added to this router with no
#: guard and no tag, was unreachable by construction: it never enters ``live``
#: because it was never in the table to begin with, so it can never make the
#: sides differ. A live-verified repro: adding an unguarded, untagged
#: ``{agent_code}`` route to this router left this test (and the other 89 in
#: this file) all green. See ``_agents_router_own_candidate_routes`` for the
#: fix — enumeration keyed off route SHAPE, not table membership.
_AGENTS_ROUTER_UNIVERSE: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/v1/agents/{agent_code}/sessions"),
        ("POST", "/v1/agents/{agent_code}/runs"),
        ("POST", "/v1/agents/{name}/disable"),
        ("POST", "/v1/agents/{name}/enable"),
    }
)

#: Structural discriminator for "is this route a candidate agents.py owns
#: that the correspondence check below must see" — matches exactly ONE path
#: parameter immediately followed by exactly ONE literal (non-parameter)
#: segment, e.g. ``/{agent_code}/sessions`` or ``/{name}/disable``.
#:
#: This is what the four tracked routes (``sessions`` / ``runs`` / ``disable``
#: / ``enable``) have in common, and every OTHER route on agents.py's own
#: router is a different shape: ``""`` / ``"/fork"`` / ``"/templates"`` (no
#: path param at all), ``"/{name}/{version}"`` (two params, no literal
#: segment after — the manifest identity routes: get/put/delete), or
#: ``"/{name}/{version}/revisions[...]"`` (more than two segments — those
#: also carry ``console_only()`` and are not third-party-reachable at all).
#: It is a fact about this router's URL design, not a hand-maintained list of
#: names — a future route mounted directly on ``agents.py``'s own router in
#: this same "one param, one action segment" shape is picked up automatically
#: by matching the pattern, not by being named in advance.
#:
#: Known residual limitation (documented, not silently assumed): a
#: hypothetical future route in a DIFFERENT shape (e.g. a second segment
#: after ``sessions``) would not match this pattern and so would not be
#: forced into ``live`` — the same way the original table-membership bug
#: missed things, just a narrower gap. In practice a new third-party route is
#: expected to be added to one of the seven ``external_*.py`` routers (covered
#: by the tag-driven audit above, ``test_every_external_agents_route_carries_
#: the_external_only_guard``) rather than directly on ``agents.py``'s own
#: router; this table only exists because ``sessions``/``runs``/``disable``/
#: ``enable`` predate that split. The mutation proof this fix is required to
#: pass (see the fix's report) adds a route in exactly the tracked shape —
#: the realistic "someone copies the existing pattern and forgets the guard"
#: case — and confirms it goes red.
_AGENTS_ROUTER_CANDIDATE_SHAPE = re.compile(r"^/v1/agents/\{[^{}/]+\}/[^{}/]+$")


def _agents_router_own_candidate_routes(app: Any) -> dict[tuple[str, str], APIRoute]:
    """Every ``(method, path)`` matching ``_AGENTS_ROUTER_CANDIDATE_SHAPE`` on
    agents.py's OWN router (excludes ``tags=["external"]`` routes, which live
    on the seven ``external_*.py`` routers and match the same path shape but are
    already covered by the tag-driven audit above).

    A TRUE full enumeration of the live app: nothing here is filtered by
    membership in ``_AGENTS_ROUTER_UNIVERSE`` or any other table — that
    filtering was the exact construction bug this helper replaces (see the
    module-level docstring on ``_AGENTS_ROUTER_UNIVERSE``). A route that
    matches the shape and exists in the app always lands in the returned
    dict, whether or not any test-file table already knows about it.
    """
    live: dict[tuple[str, str], APIRoute] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/v1/agents/"):
            continue
        if "external" in (route.tags or []):
            continue
        if not _AGENTS_ROUTER_CANDIDATE_SHAPE.match(route.path):
            continue
        for method in route.methods or ():
            if method in ("HEAD", "OPTIONS"):
                continue
            live[(method, route.path)] = route
    return live


def test_agents_router_external_only_routes_carry_the_guard() -> None:
    """The ``agents.py``-hosted counterpart to the test above.

    Walks ``live`` — a full, table-independent enumeration of the app (see
    ``_agents_router_own_candidate_routes``) — cross-checks it against
    ``_AGENTS_ROUTER_UNIVERSE`` in both directions (so a stale table entry OR
    a genuinely new, untracked route fails loudly), then asserts an exact
    per-route correspondence between "is in
    ``_AGENTS_ROUTER_EXTERNAL_ONLY_ROUTES``" and "actually carries the guard"
    — not just "every listed route carries it", which a shrunk table would
    still satisfy vacuously.
    """
    app = _build_audit_app()
    live = _agents_router_own_candidate_routes(app)
    assert set(live) == _AGENTS_ROUTER_UNIVERSE, (
        f"universe table out of sync with the live app — missing: "
        f"{sorted(_AGENTS_ROUTER_UNIVERSE - set(live))}, "
        f"stale (route exists in the app but not in the table — a new route "
        f"was added to agents.py's own router without updating this audit): "
        f"{sorted(set(live) - _AGENTS_ROUTER_UNIVERSE)}"
    )
    mismatched = [
        f"{method} {path} (should_carry_guard={should_carry}, actually_carries={actually})"
        for (method, path), route in live.items()
        for should_carry in [(method, path) in _AGENTS_ROUTER_EXTERNAL_ONLY_ROUTES]
        for actually in [_carries_external_only_guard(route)]
        if should_carry != actually
    ]
    assert not mismatched, f"external_only() correspondence mismatch: {mismatched}"
