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
from expert_work.protocol import (
    AgentSpec,
    AuditQuery,
    PlatformAgentTemplateStatus,
    PlatformAgentTemplateUpsert,
    Role,
    TenantPlan,
)
from expert_work.runtime.runs import InMemoryRunEventStore, InMemoryRunStore
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

# Fixed, NOT ``uuid4()`` — these ids get interpolated into the endpoint paths
# below, which become the ``parametrize`` ids. A fresh random value per
# collection makes the test ids non-deterministic, so ``pytest-xdist`` refuses
# to run ("Different tests were collected between gw0 and gw1") and ``--lf`` /
# ``-k`` / CI report diffing silently stop matching. The values are arbitrary:
# every case asserts the plane gate fires *before* resource resolution, so
# these resources need not exist. (``uuid4()`` inside test bodies is fine —
# only module-level values that reach a parametrize id matter.)
_TID = UUID("00000000-0000-4000-8000-000000000011")
_RID = UUID("00000000-0000-4000-8000-000000000012")

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
    # Backlog Task 3 (security fix, spec/external-api-v1-p2) — the module's
    # own docstring says it mirrors triggers' console_only() shape, but the
    # gate itself was missing on all 5 routes; a zero-scope API key could
    # register a delivery URL and receive every run.completed /
    # artifact.saved event thereafter (an ongoing exfiltration channel, not
    # just an over-broad read). Live probe here per the rationale above.
    ("GET", "/v1/webhook-endpoints"),
    # Backlog Task 5 (security fix, spec/external-api-v1-p2b) — six more
    # prefixes landing across separate commits, one router file each
    # (curation.py builds two routers): /v1/skills, /v1/knowledge,
    # /v1/curation, /v1/eval-datasets, /v1/eval-runs, /v1/quality. All 42
    # routes between them carried zero require(...)/console_only(); only
    # ensure_tenant_scope/ensure_single_tenant_scope (which parse "which
    # tenant", not "is this caller allowed"), so a zero-scope API key
    # reached every one. One live probe per prefix, added alongside that
    # prefix's own commit. The curation-candidate probe is the brief's
    # worst case: it returns another end user's full conversation
    # trajectory via TrajectoryReader with no owner check.
    ("GET", "/v1/skills"),
    ("GET", "/v1/knowledge/bases"),
    ("GET", f"/v1/curation/candidates/{_TID}"),
    ("GET", "/v1/eval-datasets"),
    ("GET", "/v1/eval-runs"),
    ("GET", "/v1/quality/scores"),
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
#: Backlog Task 3 (security fix, spec/external-api-v1-p2) added
#: /v1/webhook-endpoints — NOT to be confused with /v1/webhooks above: this is
#: the outbound registration CRUD (``webhook_endpoints.py``), whose own
#: docstring says it mirrors the triggers CRUD but was missing console_only()
#: on all 5 routes. Locked as a whole prefix, same as /v1/triggers.
#: Backlog Task 5 (security fix, spec/external-api-v1-p2b) — /v1/skills:
#: zero RBAC on all 15 routes (see the CONSOLE_ENDPOINTS comment above for
#: the full six-prefix batch this belongs to). skills.py's own inline role
#: checks (``_require_subscribe_role``, and the ADMIN/SYSTEM_ADMIN branches
#: inside PATCH for pinning/activating high-risk skills) are untouched and
#: orthogonal — console_only() only closes the API-key axis; those checks
#: still gate which *employee* role can do what.
#: Backlog Task 5 (security fix, spec/external-api-v1-p2b) — /v1/knowledge:
#: zero RBAC on all 12 routes (base + document CRUD, reindex/reingest,
#: chunk read, retrieval test) — same shape as /v1/skills above.
#: Backlog Task 5 also added /v1/curation and /v1/eval-datasets
#: (``curation.py`` builds two routers, both zero-RBAC): candidate list/get/
#: promote/dismiss (4 routes) + eval-dataset CRUD (5 routes). ``GET
#: /v1/curation/candidates/{id}`` is the worst finding in this whole batch —
#: it returns the full conversation trajectory (``messages``, via
#: ``TrajectoryReader``) for *any* end user's candidate in the tenant, no
#: owner filter, so any same-tenant credential — including a zero-scope API
#: key — could read any end user's complete conversation verbatim.
#: Backlog Task 5 also added /v1/eval-runs: zero RBAC on all 4 routes
#: (list/enqueue/get/cases) — same shape as the rest of this batch.
#: Backlog Task 5's last prefix: /v1/quality — zero RBAC on both routes
#: (quality scores + drift alerts). Scores carry run_id/thread_id (can be
#: traced back to a specific end-user conversation, though the content
#: itself is a score + rationale, not the raw transcript); drift alerts are
#: tenant-level aggregates with no per-session content. This closes the
#: last of the six prefixes named in the CONSOLE_ENDPOINTS comment above —
#: all 42 routes across skills/knowledge/curation/eval-datasets/eval-runs/
#: quality now carry console_only(), verified by the structural self-audit
#: below (test_every_console_route_carries_the_lockdown_dependency), which
#: walks every live route under these prefixes and checks its dependency
#: graph directly rather than relying on any hand-maintained route list.
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
    "/v1/webhook-endpoints",
    "/v1/skill-evolution",
    "/v1/skills",
    "/v1/knowledge",
    "/v1/curation",
    "/v1/eval-datasets",
    "/v1/eval-runs",
    "/v1/quality",
    # 阶段 1.2 — the tenant-management surface. These carried RBAC but no
    # *plane* gate, so an ``admin``-scope API key reached them: minting and
    # revealing sibling keys, inviting members and resetting their passwords,
    # reading the audit trail, rewriting tenant config and quota.
    "/v1/members",
    "/v1/api_keys",
    "/v1/service_accounts",
    "/v1/role_bindings",
    "/v1/audit",
    "/v1/sandbox-egress-audit",
    "/v1/usage",
    "/v1/quota",
    "/v1/memory",
    "/v1/mcp-servers",
    "/v1/mcp-oauth",
    "/v1/model-catalog",
)

#: Console routes that a **prefix** can never reach, because they live under
#: ``/v1/agents`` — the one mount point the console plane shares with the
#: third-party plane. Closing that prefix wholesale would 403 the external
#: surface, so these carry ``console_only()`` per route instead, and this
#: table is what keeps the self-audit below from having a blind spot there
#: (P1 final review, Critical C2: the original five answered 200 to a
#: zero-scope service-account key, and the ``rollback`` one is a **write**).
#:
#: Backlog Task 2 (spec/external-api-v1-p2b) added the remaining eight:
#: ``create_agent`` / ``list_templates`` / ``fork_template`` / ``get_agent`` /
#: ``update_agent`` / ``delete_agent`` / ``disable_agent`` / ``enable_agent``
#: previously relied on ``require(...)`` / ``ensure_resource_access(...)``
#: alone (or, for ``fork_template``, nothing at all) — RBAC checks a
#: ``write``- or ``admin``-scope API key satisfies via the OPERATOR/ADMIN
#: role mapping (``rbac.py``), since API-key scopes stand in for roles. That
#: was ``_SELF_GATED_AGENT_ROUTES`` below (now removed — every route under
#: ``/v1/agents`` is classified console or external, no third bucket).
#: ``GET /v1/agents/schema`` (``agent_schema.py``) also moved here from
#: ``_OPEN_AGENT_ROUTES``, closing the last unclassified route.
#:
#: Add a console route under ``/v1/agents``? Add it here too — otherwise the
#: audit cannot see it. ``test_agents_prefix_is_partitioned_exactly`` forces
#: that: any ``/v1/agents`` route missing from both tables fails it.
_CONSOLE_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/v1/agents"),
        ("GET", "/v1/agents/{name}/{version}/revisions"),
        ("GET", "/v1/agents/{name}/{version}/revisions/{revision}"),
        ("POST", "/v1/agents/{name}/{version}/revisions/{revision}/rollback"),
        ("GET", "/v1/agents/{agent_name}/{agent_version}/users"),
        ("POST", "/v1/agents"),
        ("GET", "/v1/agents/templates"),
        ("POST", "/v1/agents/fork"),
        ("GET", "/v1/agents/{name}/{version}"),
        ("PUT", "/v1/agents/{name}/{version}"),
        ("DELETE", "/v1/agents/{name}/{version}"),
        ("POST", "/v1/agents/{name}/disable"),
        ("POST", "/v1/agents/{name}/enable"),
        ("GET", "/v1/agents/schema"),
    }
)

#: The third-party surface: every ``/v1/agents`` route an API key IS meant to
#: reach (design spec §四 endpoints 1-7, plus the pre-existing session bind).
#: The head risk of the route-level lockdown above is over-reach, so these are
#: probed with a real request and must never come back 403.
_EXTERNAL_AGENT_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/v1/agents/{agent_code}/runs"),
        ("GET", "/v1/agents/{agent_code}/runs"),
        ("POST", "/v1/agents/{agent_code}/runs/{run_id}:cancel"),
        ("GET", "/v1/agents/{agent_code}/runs/{run_id}/events"),
        ("GET", "/v1/agents/{agent_code}/sessions"),
        ("POST", "/v1/agents/{agent_code}/sessions"),
        ("GET", "/v1/agents/{agent_code}/sessions/{session_id}/messages"),
        ("POST", "/v1/agents/{agent_code}/uploads"),
        ("POST", "/v1/agents/{agent_code}/runs/{run_id}:decide"),
        # P2-b — session rename / archive (external_sessions.py) and the
        # workspace list / download mirror (external_workspace.py). All four
        # are mounted on ``APIRouter(prefix="/v1/agents", tags=["external"])``
        # and gated with ``Depends(require("session", ...))`` — never
        # ``console_only()`` — so they belong to the third-party surface,
        # same as the eight above.
        ("PATCH", "/v1/agents/{agent_code}/sessions/{session_id}"),
        ("DELETE", "/v1/agents/{agent_code}/sessions/{session_id}"),
        ("GET", "/v1/agents/{agent_code}/workspace/files"),
        ("GET", "/v1/agents/{agent_code}/workspace/file"),
        # 阶段 3 PR-B — 对外产物视图(external_artifacts.py)。同样挂在
        # ``APIRouter(prefix="/v1/agents", tags=["external"])`` 上,用
        # ``require("session", ...)`` 而非 ``console_only()``。
        ("GET", "/v1/agents/{agent_code}/artifacts"),
        ("GET", "/v1/agents/{agent_code}/artifacts/download"),
        ("DELETE", "/v1/agents/{agent_code}/artifacts"),
        # 附件模型统一 Task 4 — 对外下载端点(external_uploads.py),同样挂
        # ``require("session", "read")`` 而非 ``console_only()``。
        ("GET", "/v1/agents/{agent_code}/uploads/{upload_id}"),
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
    "upload_id": "upl_00000000-0000-4000-8000-000000000013",
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
        audit_store: InMemoryAuditLogStore,
    ) -> None:
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.headers = headers
        self.key_headers = key_headers
        self.audit_store = audit_store

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
        yield _Ctx(client, app, tenant_id, headers, key_headers, audit_store)


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
async def test_api_key_denial_audit_includes_the_denied_path(ctx: _Ctx) -> None:
    """Backlog item 3 — the deny audit's ``resource_id`` was a constant
    (``"console:api_key_denied"``) shared by every route this gate closes;
    with nothing else distinguishing rows, a triage pass through the audit
    log cannot tell which route a caller actually hit. ``details.path`` now
    carries the real request path so rows are distinguishable.
    """
    resp = await ctx.client.get("/v1/sessions", headers=ctx.key_headers)
    assert resp.status_code == 403, resp.text

    page = await ctx.audit_store.query(AuditQuery(tenant_id=ctx.tenant_id, limit=1000))
    matches = [r for r in page.entries if r.resource_id == "console:api_key_denied"]
    assert len(matches) == 1, [r.model_dump() for r in page.entries]
    assert matches[0].details.get("path") == "/v1/sessions"
    # resource_id itself must stay the stable constant — see the brief: it
    # may already be relied on by queries/dashboards, only `details` is free.
    assert matches[0].resource_id == "console:api_key_denied"


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

    Backlog Task 2 (spec/external-api-v1-p2b) tightened this from four
    buckets to two: every ``/v1/agents`` route is now either
    ``_CONSOLE_ROUTES`` or ``_EXTERNAL_AGENT_ROUTES``, no third "open" or
    "self-gated" class — the eight routes that used to tolerate an
    ``ensure_resource_access(...)``/``require(...)``-only gate (reachable by
    an ``admin``- or ``write``-scope API key) and the one genuinely-open
    schema route all carry ``console_only()`` now.
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
    classified = _CONSOLE_ROUTES | _EXTERNAL_AGENT_ROUTES
    assert live - classified == set(), (
        f"unclassified /v1/agents routes — each needs a table: {sorted(live - classified)}"
    )
    assert classified - live == set(), (
        f"table entries match no live route (stale): {sorted(classified - live)}"
    )
    # The buckets must not overlap, or "console" and "external" could both
    # claim a route and the two assertions below would contradict silently.
    assert not (_CONSOLE_ROUTES & _EXTERNAL_AGENT_ROUTES), (
        f"route classified twice: {sorted(_CONSOLE_ROUTES & _EXTERNAL_AGENT_ROUTES)}"
    )


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

    Every one of the external routes must stay reachable to an API key.
    A missing body / query param answers 4xx here — that is fine and expected;
    what must never appear is 403, which is the only thing ``console_only()``
    can produce.
    """
    await ctx.seed_agent()
    resp = await ctx.client.request(method, _concretize(path), headers=ctx.key_headers)
    assert resp.status_code != 403, f"{method} {path} → {resp.status_code} {resp.text}"


# ---------------------------------------------------------------------------
# Backlog Task 2 (spec/external-api-v1-p2b) — the eight routes above already
# get generic coverage from the two ``_CONSOLE_ROUTES``-parametrized tests
# (an ``admin``-scope key is denied, an employee JWT is unaffected). These
# pin the exact vulnerability shape the brief measured: a ``write``-scope key
# (not ``admin`` — ``write`` maps to OPERATOR, which ``rbac.py`` grants
# ``manifest: {read, write}``, the precise gap ``ensure_resource_access`` /
# ``require("manifest", "write")`` alone left open) and, for
# ``fork_template`` specifically, a **zero**-scope key (it carried no
# authorization check of its own at all before this fix).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/agents/fork"),
        ("POST", "/v1/agents/{name}/disable"),
        ("GET", "/v1/agents/{name}/{version}"),
        ("DELETE", "/v1/agents/{name}/{version}"),
    ],
)
async def test_write_scope_key_is_denied_on_the_newly_gated_agents_routes(
    ctx: _Ctx, method: str, path: str
) -> None:
    """``write`` scope maps to OPERATOR (``rbac.py: _collect_roles``), which is
    granted ``manifest: {read, write}`` — exactly the role ``ensure_resource_
    access`` / ``require("manifest", "write")`` alone let through before
    ``console_only()`` closed this axis. Covers the brief's named-risky
    subset: fork, disable (cancels every in-flight run for the agent), the
    read side (``get_agent``), and delete.

    DELETE is a special case, not this test's to prove: OPERATOR is NOT
    granted ``manifest:delete`` (``rbac.py``), so a write-scope key 403s on
    this route from RBAC alone — with or without ``console_only()`` on
    ``delete_agent``. This instance therefore only confirms the write-scope
    axis stays denied; it cannot, by itself, tell "the gate closed it" from
    "OPERATOR never had delete" (that would be a reintroduced coincidence,
    same shape as the pre-fix one). The causal proof that ``console_only()``
    — not the coincidence — now guards DELETE belongs to
    ``test_api_key_is_denied_on_the_console_routes_under_agents``: its
    ``ctx.key_headers`` carries ``admin`` scope (→ ADMIN role, which DOES
    have ``manifest:delete``), so removing ``console_only()`` from
    ``delete_agent`` turns that test's DELETE case green→red, which this one
    alone cannot.
    """
    await ctx.seed_agent()
    write_scope_jwt = make_test_jwt(
        tenant_id=ctx.tenant_id,
        subject="sa-write-scope",
        sub_type="service_account",
        roles=(),
        scopes=("write",),
    )
    write_scope_headers = {"Authorization": f"Bearer {write_scope_jwt}"}
    resp = await ctx.client.request(method, _concretize(path), headers=write_scope_headers)
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_zero_scope_key_is_denied_on_fork_template(ctx: _Ctx) -> None:
    """``fork_template`` carried no authorization dependency at all before
    Backlog Task 2 — not ``require(...)``, not an in-handler
    ``ensure_resource_access`` gate at the route-entry level (it does call
    one, but only after loading the template and checking tier
    entitlement) — so a zero-scope key reached further into the handler
    than any other route on this router. ``console_only()`` now rejects it
    before any of that runs.

    A published, tier-affordable template is pre-seeded here (same shape as
    ``tests/test_agents_fork.py``'s ``_upsert()``/``seed_template``) so the
    403 below cannot be riding on the fixture's ``InMemoryPlatformAgentTemplateStore``
    happening to be empty. Without a seed, pulling ``console_only()`` off
    this route would 404 at the handler's step-1 template lookup (``base is
    None``, ``api/agents.py``) before step 4's ``ensure_resource_access``
    ever runs, and the bare ``== 403`` assertion would pass for that wrong
    reason.

    Seeding is not enough by itself, though — verified by mutation, not just
    read off the source: a zero-scope key that reaches step 4 gets denied
    there too (no role, no binding — same final 403 as ``console_only()``
    would have produced), so a bare status-code assertion stays green with
    or without the gate; that is the exact tautology the seed was meant to
    close, just moved one step deeper. Pinning the denial **message**, the
    same idiom the other console-plane tests in this module use, is what
    actually discriminates: only ``console_only()`` produces this pointer
    text — strip it and the 403 survives (from
    ``ensure_resource_access``) but the message changes to that call's
    generic "principal lacks access to this resource".
    """
    await ctx.app.state.platform_agent_template_store.create(
        upsert=PlatformAgentTemplateUpsert(
            spec=_spec(),
            display_name="Support Bot",
            status=PlatformAgentTemplateStatus.PUBLISHED,
            required_tier=TenantPlan.FREE,
        ),
        created_by="sysadmin",
    )
    zero_scope_jwt = make_test_jwt(
        tenant_id=ctx.tenant_id,
        subject="sa-zero-scope",
        sub_type="service_account",
        roles=(),
        scopes=(),
    )
    zero_scope_headers = {"Authorization": f"Bearer {zero_scope_jwt}"}
    resp = await ctx.client.post(
        "/v1/agents/fork",
        json={"template_name": "support-bot", "template_version": "latest", "name": "x"},
        headers=zero_scope_headers,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["message"] == (
        "console API is not available to API keys; use /v1/agents/{agent_code}/…"
    ), resp.text
