"""Reachability guardrail for the ``/v1/agents/...`` external routes.

Starlette's router matches by **registration order, not specificity**
(``app.py``'s comment above the ``build_external_*_router()`` calls). The
seven external routers must stay registered *before* ``build_agents_router()``:
its ``GET /{name}/{version}`` is a two-segment, fully-parameterised path that
silently swallows any two-segment ``/{agent_code}/<literal>`` external route
registered after it (``version`` would just bind to the literal, e.g.
``"sessions"``) — the endpoint would then be unreachable in the real app even
though its own unit tests (which call the handler directly / build a
router-only app) stay green.

This test walks ``app.routes`` in the real ``create_app(...)`` order and
proves every external route is still the *first* route that matches a
concrete instance of its own path — so a future two-segment external route
added in the wrong place (or before the wrong router) fails loudly here
instead of degrading into a silent 404 at runtime. A second test proves the
fix didn't break the reverse direction: ``agents.py``'s own
``GET /{name}/{version}`` must still win for a real agent/version pair.
"""

from __future__ import annotations

import re
from typing import Any

from starlette.routing import BaseRoute, Match, Route

from control_plane.app import create_app
from control_plane.settings import Settings
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier


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


def _build_routes() -> list[BaseRoute]:
    app = create_app(settings=_build_settings(), jwt_verifier=build_test_jwt_verifier())
    return list(app.routes)


# Sample values for every path-param name that appears in a `/v1/agents/...`
# route's path template (as of this writing). A new param name added to a
# future route trips the KeyError below in `_concretize` — treat that as a
# prompt to add a sample value here, not a reason to skip this test.
_SAMPLE_PARAMS = {
    "agent_code": "sample-agent",
    "name": "sample-agent",
    "version": "1.0.0",
    "agent_name": "sample-agent",
    "agent_version": "1.0.0",
    "session_id": "11111111-1111-1111-1111-111111111111",
    "run_id": "22222222-2222-2222-2222-222222222222",
    "revision": "1",
    "upload_id": "upl_00000000-0000-4000-8000-000000000013",
}

_PARAM_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(?::[a-zA-Z_]+)?\}")


def _concretize(path_template: str) -> str:
    """Fill a route path template's ``{param}`` placeholders with sample values."""

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        assert name in _SAMPLE_PARAMS, (
            f"no sample value registered for path param {name!r} "
            f"(seen in {path_template!r}) — add one to _SAMPLE_PARAMS"
        )
        return _SAMPLE_PARAMS[name]

    return _PARAM_RE.sub(_sub, path_template)


def _first_match(routes: list[BaseRoute], path: str, method: str) -> BaseRoute | None:
    """The first route in registration order that FULLY matches (path + method)."""
    scope: dict[str, Any] = {"type": "http", "method": method, "path": path, "root_path": ""}
    for route in routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return route
    return None


def _external_routes(routes: list[BaseRoute]) -> list[Route]:
    """Discovery is by ``tags=["external"]`` alone — see the same-named
    function in ``test_external_only_gate.py`` for why the path-prefix
    condition this used to also require was dropped."""
    return [
        route
        for route in routes
        if isinstance(route, Route) and "external" in (getattr(route, "tags", None) or [])
    ]


def test_every_external_agents_route_is_reachable() -> None:
    """Every external ``/v1/agents/...`` route must win the match for its own path.

    If an earlier-registered route (e.g. ``agents.py``'s
    ``GET /{name}/{version}``) intercepts the request first, the external
    route is dead code in the real app — this is exactly the bug this test
    guards against.
    """
    routes = _build_routes()
    external_routes = _external_routes(routes)
    assert external_routes, "expected at least one tags=['external'] route"

    for route in external_routes:
        for method in sorted(route.methods or set()):
            if method in ("HEAD", "OPTIONS"):
                continue
            concrete_path = _concretize(route.path)
            winner = _first_match(routes, concrete_path, method)
            assert winner is route, (
                f"{method} {route.path} (tagged external) is shadowed: the router "
                f"resolves {method} {concrete_path} to "
                f"{getattr(winner, 'path', winner)!r} instead — registration order "
                f"in app.py must put the external routers before build_agents_router()"
            )


def test_name_version_route_still_wins_for_a_real_agent_and_version() -> None:
    """Reverse check: the reordering must not break ``agents.py`` itself.

    A real ``GET /v1/agents/{agent}/{version}`` request (where ``version`` is
    an actual version string, not one of the external routes' literal second
    segments like ``sessions``/``runs``) must still resolve to ``agents.py``'s
    own ``GET /{name}/{version}`` route.
    """
    routes = _build_routes()
    winner = _first_match(routes, "/v1/agents/some-agent/1.0.0", "GET")
    assert winner is not None
    assert isinstance(winner, Route)
    assert winner.path == "/v1/agents/{name}/{version}"
    assert "agents" in (winner.tags or [])
    assert "external" not in (winner.tags or [])


def test_the_discovery_is_not_tied_to_the_agents_path_prefix() -> None:
    """See ``test_external_only_gate.py``'s same-named test's docstring."""
    paths = {r.path for r in _external_routes(_build_routes())}
    assert "/v1/agent-catalog" in paths, (
        "external route /v1/agent-catalog was not picked up by the "
        "discoverer — it is still filtering by path prefix"
    )
