"""Every route belongs to exactly one plane — 阶段 1.3 的地基自审。

`test_console_lockdown.py` proves the *console* plane is closed to API keys,
route by route. It cannot tell you about a route that belongs to no plane at
all: a new router mounted without `console_only()` / `external_only()` is
invisible to it, and there is no third category to fall into by accident.

This module closes that hole. It walks **every** compiled `APIRoute` in the
real app and classifies it from evidence in the code — never from a table
somebody hand-maintains. The pinned sets below are change *detectors*: a new
route lands in whatever plane its gates put it in, and if that shifts a
pinned set the test fails and forces the decision to be made explicitly.

Why this is not the black box it replaces
-----------------------------------------
The deleted `_SELF_GATED_AGENT_ROUTES` / `_OPEN_AGENT_ROUTES` tables (removed
in `85abdb39`) were pure declaration: a route was "gated" because the table
said so, so a zero-authz route stayed green by being added to the table.
Here the classification is *derived* — `_classify` reads the route's actual
dependency graph and the source of the functions that graph reaches — and the
derivation itself is pinned by `test_classifier_positive_controls`, so a
scanner that goes blind fails loudly instead of reclassifying the world as
"gated".
"""

from __future__ import annotations

import inspect
import re
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import uuid4

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import Role
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

# ---------------------------------------------------------------- the planes

#: Authentication is skipped entirely for these prefixes — ``AuthMiddleware``
#: never resolves a principal, so no route under them can carry a plane gate.
#: Mirrors ``Settings.auth_exempt_path_prefixes``; asserted against it below so
#: the two cannot drift.
PUBLIC_PREFIXES = ("/healthz", "/metrics", "/v1/webhooks", "/v1/setup")

#: Qualname marker → plane. Factory-built dependencies only.
_PLANE_DEPENDENCIES: tuple[tuple[str, str], ...] = (
    ("console_only", "console"),
    ("external_only", "external"),
    # 阶段 1.4 hoisted the platform gate out of handler bodies into a shared
    # dependency. The inline patterns below stay: they still cover whatever
    # has not been converted, and their absence is what proves a conversion
    # actually landed rather than silently dropping the gate.
    ("platform_only", "platform"),
)

#: Source pattern → plane, for gates written inline in a handler body.
#:
#: These match a **guard**, not a mention. A bare ``is_system_admin`` search
#: classifies ``GET /v1/me`` as platform-gated because that endpoint *reports*
#: the flag in its response body — a false positive in the direction that hides
#: real holes. The two positive-form uses in the codebase
#: (``if principal.is_system_admin and payload.tenant_id != ...`` in
#: ``quota.py``) are cross-tenant *widening* branches, not gates, and are
#: deliberately not matched.
_PLANE_SOURCE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"_require_system_admin\("), "platform"),
    (re.compile(r"not\s+\w+\.is_system_admin"), "platform"),
)


def _dep_callables(dependant: Dependant) -> list[Callable[..., Any]]:
    """Every callable FastAPI resolves for this route, depth-first."""
    out: list[Callable[..., Any]] = [dependant.call] if dependant.call is not None else []
    for sub in dependant.dependencies:
        out.extend(_dep_callables(sub))
    return out


def _reachable(
    fn: Callable[..., Any],
    *,
    home: str | None = None,
    depth: int = 0,
    seen: set[int] | None = None,
) -> list[Callable[..., Any]]:
    """Functions reachable from ``fn`` **without leaving its own module**.

    ``inspect.getclosurevars`` is what makes this work on this codebase: the
    gate is as often in a closure of the router factory (``tenants.py``'s
    ``_set_tenant_status``) as in a module-level helper
    (``platform_skills.py``'s ``_require_system_admin``), and ``getattr(module,
    name)`` finds only the latter.

    The same-module restriction is load-bearing, not tidiness. Following
    imports reaches ``rbac.is_allowed``, whose body names ``is_system_admin``
    — so every ``require()`` route would be classified platform-gated. That is
    a false *positive* for "this route has a gate", the direction that hides
    real holes.
    """
    seen = seen if seen is not None else set()
    home = home if home is not None else getattr(fn, "__module__", "")
    if id(fn) in seen or depth > 3 or not callable(fn):
        return []
    if getattr(fn, "__module__", "") != home:
        return []
    seen.add(id(fn))
    out = [fn]
    try:
        closure_vars = inspect.getclosurevars(fn)
    except (TypeError, ValueError):
        return out
    for value in (*closure_vars.nonlocals.values(), *closure_vars.globals.values()):
        if callable(value):
            out.extend(_reachable(value, home=home, depth=depth + 1, seen=seen))
    return out


def _source(fn: Callable[..., Any]) -> str:
    try:
        return inspect.getsource(fn)
    except (OSError, TypeError):  # C builtins, exec'd code
        return ""


def _classify(route: APIRoute) -> str:
    """The plane this route's own code puts it in.

    Two signals, both needed. A factory-built gate
    (``Depends(console_only())``) hands FastAPI the *inner* ``_dep``, whose
    source never names the factory — only the qualname does. An inline gate
    (``if not principal.is_system_admin``) has no distinguishing qualname —
    only the source shows it.
    """
    if route.path.startswith(PUBLIC_PREFIXES):
        return "public"
    roots = [route.endpoint, *_dep_callables(route.dependant)]
    qualnames = " ".join(getattr(fn, "__qualname__", "") for fn in roots)
    seen: set[int] = set()
    functions: list[Callable[..., Any]] = []
    for root in roots:
        functions.extend(_reachable(root, seen=seen))
    for marker, plane in _PLANE_DEPENDENCIES:
        if marker in qualnames:
            return plane
    blob = "".join(_source(fn) for fn in functions)
    for pattern, plane in _PLANE_SOURCE_PATTERNS:
        if pattern.search(blob):
            return plane
    return "tenant"


def _routes(app: Any) -> list[APIRoute]:
    return [r for r in app.routes if isinstance(r, APIRoute)]


def _label(route: APIRoute) -> str:
    methods = sorted(m for m in (route.methods or ()) if m not in ("HEAD", "OPTIONS"))
    return f"{','.join(methods)} {route.path}"


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


def _build_app() -> Any:
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


# ------------------------------------------------------------- the pinned set

#: Routes carrying **no plane gate** — reachable by an API key whose scopes
#: cover them, and by any employee JWT.
#:
#: 阶段 1.2 emptied this set down to one deliberate entry. What used to be here
#: was the tenant-management surface: minting / revealing / rotating sibling API
#: keys, inviting members and resetting their passwords, purging members, the
#: audit trail, tenant config and quota, the MCP registry, long-term memory.
#: All of it carried RBAC but no *plane* gate, so an ``admin``-scope key walked
#: straight in — the concrete form of backlog P-3 / P-4.
#:
#: The set is *derived*, never declared: adding a line grants nothing, it only
#: acknowledges a route the classifier already found ungated. Removing the last
#: entry is not the goal — ``/v1/me`` reports the caller's own principal and
#: nothing else, which is exactly what a machine principal may ask.
TENANT_PLANE_ROUTES: frozenset[str] = frozenset({"GET /v1/me"})


def test_every_route_belongs_to_exactly_one_plane() -> None:
    """No route escapes classification, and the ungated set does not grow.

    ``_classify`` is total — every route gets a plane — so the assertion that
    matters is the *composition*: anything the classifier cannot place a gate
    on lands in ``tenant``, and that set is pinned. A new router mounted
    without a plane gate fails here with its own path in the message.
    """
    app = _build_app()
    routes = _routes(app)
    assert len(routes) > 200, f"only {len(routes)} routes — app build is degraded"

    tenant_plane = {_label(r) for r in routes if _classify(r) == "tenant"}

    unexpected = sorted(tenant_plane - TENANT_PLANE_ROUTES)
    assert not unexpected, (
        "route(s) carry no plane gate and are not in TENANT_PLANE_ROUTES — "
        "decide the plane, do not just add the line: " + repr(unexpected)
    )
    stale = sorted(TENANT_PLANE_ROUTES - tenant_plane)
    assert not stale, (
        "TENANT_PLANE_ROUTES entries match no ungated route (gated since, or "
        "path renamed) — drop them: " + repr(stale)
    )


def test_public_prefixes_match_the_auth_middleware_config() -> None:
    """The public plane is whatever ``AuthMiddleware`` skips — not a second
    opinion maintained here. Drift makes every other assertion in this module
    classify against the wrong baseline."""
    assert tuple(_build_settings().auth_exempt_path_prefixes) == PUBLIC_PREFIXES


def test_classifier_positive_controls() -> None:
    """The classifier must keep *finding* gates.

    Every other test here passes trivially if ``_classify`` silently stops
    detecting things — a renamed dependency, a refactor that moves a gate
    across modules, an ``inspect`` behaviour change. These four routes pin one
    known member of each plane, so a blind classifier fails here first.
    """
    by_label = {_label(r): r for r in _routes(_build_app())}
    for label, expected in (
        ("GET /v1/sessions", "console"),
        ("GET /v1/agents/{agent_code}/sessions", "external"),
        ("GET /v1/platform/skills", "platform"),
        ("GET /v1/me", "tenant"),
        ("GET /healthz/live", "public"),
    ):
        assert label in by_label, f"{label} no longer exists — control is stale"
        assert _classify(by_label[label]) == expected, (
            f"{label} classified as {_classify(by_label[label])!r}, expected {expected!r}"
        )


def test_console_and_external_planes_are_disjoint() -> None:
    """A route carrying both gates would 403 every caller — dead surface that
    still looks reachable in the OpenAPI schema."""
    both = []
    for route in _routes(_build_app()):
        quals = " ".join(getattr(fn, "__qualname__", "") for fn in _dep_callables(route.dependant))
        if "console_only" in quals and "external_only" in quals:
            both.append(_label(route))
    assert not both, f"routes carrying BOTH plane gates (unreachable): {both}"


# ------------------------------------------------------- behavioural evidence


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as ac:
        yield ac


def _api_key_headers() -> dict[str, str]:
    """An ``admin``-scope API key — the widest scope a key can carry, so a 403
    below can only be the plane gate, never a scope shortfall underneath it."""
    return {
        "Authorization": "Bearer "
        + make_test_jwt(
            tenant_id=uuid4(),
            subject="sa-partition-probe",
            sub_type="service_account",
            roles=(),
            scopes=("admin",),
        )
    }


def _is_plane_denial(resp: Any) -> bool:
    if resp.status_code != 403:
        return False
    detail = resp.json().get("detail")
    message = detail.get("message", "") if isinstance(detail, dict) else str(detail)
    return "not available to API keys" in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/v1/members", "/v1/api_keys", "/v1/audit", "/v1/usage/cost", "/v1/mcp-servers"],
    ids=["members", "api_keys", "audit", "usage", "mcp_servers"],
)
async def test_api_key_is_plane_denied_on_the_tenant_management_surface(
    client: AsyncClient, path: str
) -> None:
    """阶段 1.2 in one assertion.

    Before this, an ``admin``-scope key answered here: it could list and mint
    sibling keys, read the tenant's audit trail, see its cost, rewrite the MCP
    registry, and reset an employee's password. The RBAC gates these routes
    already carried did not help — ``rbac._collect_roles`` maps an ``admin``
    scope straight onto ``Role.ADMIN``.

    The static partition test proves *every* such route is now gated; this
    proves the gate actually fires, on a sample spanning five routers.
    """
    resp = await client.get(path, headers=_api_key_headers())
    assert _is_plane_denial(resp), f"{path} answered {resp.status_code}: {resp.text[:200]}"


@pytest.mark.asyncio
async def test_the_one_remaining_tenant_plane_route_stays_open() -> None:
    """``TENANT_PLANE_ROUTES`` claims ``/v1/me`` has no plane gate. Prove it,
    so the pinned set cannot quietly describe a route that is in fact closed —
    the failure mode that made the deleted tolerance tables worthless."""
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as ac:
        resp = await ac.get("/v1/me", headers=_api_key_headers())
    assert not _is_plane_denial(resp), "/v1/me IS plane-gated — drop it from TENANT_PLANE_ROUTES"


@pytest.mark.asyncio
async def test_employee_jwt_is_denied_on_the_external_plane(client: AsyncClient) -> None:
    """The dual of ``test_console_lockdown``: the external plane must reject a
    human. Pins the ``external`` arm of the partition to real behaviour, so
    ``external`` cannot decay into "console with a different prefix"."""
    employee_jwt = make_test_jwt(tenant_id=uuid4(), subject=str(uuid4()), roles=(Role.ADMIN.value,))
    resp = await client.get(
        "/v1/agents/probe/sessions", headers={"Authorization": f"Bearer {employee_jwt}"}
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "FORBIDDEN", resp.text


@pytest.mark.asyncio
async def test_every_platform_route_actually_refuses_a_tenant_admin() -> None:
    """阶段 1.4's net: sweep **every** platform-classified route with a tenant
    admin and require the 403.

    This assertion was impossible before the refactor. The gate used to live
    inside each handler body, which runs *after* FastAPI validates the request
    — so a POST with no body answered 422, and a sweep could not tell "refused
    me" from "did not like my payload". As a route dependency it runs first,
    so an empty request is enough and every verb is reachable.

    A tenant ``admin`` is the prover: it is the strongest non-platform
    principal there is, so a 403 here can only be the platform gate.
    """
    app = _build_app()
    jwt = make_test_jwt(tenant_id=uuid4(), subject=str(uuid4()), roles=(Role.ADMIN.value,))
    headers = {"Authorization": f"Bearer {jwt}"}
    platform_routes = [r for r in _routes(app) if _classify(r) == "platform"]
    assert len(platform_routes) > 40, (
        f"only {len(platform_routes)} platform routes — classifier broke"
    )

    transport = ASGITransport(app=app)
    bad: list[str] = []
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        for route in platform_routes:
            path = re.sub(r"\{[^}]+\}", "00000000-0000-4000-8000-000000000001", route.path)
            for method in sorted(m for m in (route.methods or ()) if m not in ("HEAD", "OPTIONS")):
                resp = await client.request(method, path, json={}, headers=headers)
                if resp.status_code != 403:
                    bad.append(f"{method} {route.path} → {resp.status_code}")
                    continue
                detail = resp.json().get("detail")
                code = detail.get("code") if isinstance(detail, dict) else None
                if code != "PLATFORM_SCOPE_FORBIDDEN":
                    bad.append(f"{method} {route.path} → 403 but code={code!r}")
    assert not bad, "platform routes that did not refuse a tenant admin: " + repr(bad)
