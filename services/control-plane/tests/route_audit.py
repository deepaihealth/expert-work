"""Single source for "walk the routes the app actually serves".

Every route self-audit in this suite (console lockdown, external-only gate,
NUL path guard, external reachability, plane partition) used to enumerate
``app.routes`` and keep the ``APIRoute`` instances. FastAPI 0.137 made
``include_router`` **lazy**: ``app.routes`` now holds ``_IncludedRouter``
branch objects and ``sum(1 for r in app.routes if isinstance(r, APIRoute))``
is ``0``. Nothing raises — every one of those audits just walks an empty
collection and passes. "Blind but green" is the exact failure mode these
audits exist to prevent, so the enumeration is centralised here and made
loud in three separate ways:

1. **No defaulted attribute reads.** There is not one
   ``getattr(x, "dependencies", [])`` in this module, and there must never
   be. Every attribute below is read as a plain attribute, so the next time
   FastAPI renames or drops one, every audit raises ``AttributeError`` on
   the spot. A default would hand back an empty list and turn a security
   gate audit green again — which is how this class of bug survived a
   version bump the last time.
2. **Non-empty enumeration.** :func:`mounted_routes` refuses to return an
   empty list. Each audit additionally asserts its own filtered subset is
   non-empty (one prefix or one tag going stale leaves the global
   enumeration healthy).
3. **Composed values only.** ``RouteContext`` exposes what the router will
   actually apply at request time: ``path`` carries every enclosing
   ``include_router(prefix=...)``, ``tags`` and ``dependencies`` are the
   outer-to-inner concatenation of router-level and route-level values.
   ``original_route`` is the *undecorated* object — its ``path`` has no
   prefix and its ``dependencies`` lack every router-level entry — so it is
   deliberately NOT what the audits read. It is exposed only because
   production reads it too (see :attr:`MountedRoute.original_route`).
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import Any

from fastapi import FastAPI, params
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, RouteContext, iter_route_contexts
from starlette.routing import BaseRoute


class MountedRoute:
    """One route as the live router will serve it.

    A thin, strict view over FastAPI's :class:`~fastapi.routing.RouteContext`.
    The properties are deliberately narrow — an audit that needs something
    else should add it here (strictly, no default) rather than reach around
    the wrapper into ``._ctx``.
    """

    def __init__(self, ctx: RouteContext) -> None:
        self._ctx = ctx

    # -- identity -----------------------------------------------------------

    @property
    def is_api_route(self) -> bool:
        """Whether this is a FastAPI ``APIRoute`` (vs. a bare Starlette route).

        ``/openapi.json``, ``/docs``, ``/docs/oauth2-redirect`` and ``/redoc``
        are plain ``starlette.routing.Route`` objects that FastAPI mounts on
        the app itself. They have no ``tags``/``dependencies``/``dependant``
        at all, so reading those off them raises — which is why every audit
        filters on this first.
        """
        return isinstance(self._ctx.original_route, APIRoute)

    @property
    def original_route(self) -> BaseRoute:
        """The undecorated route object, WITHOUT any ``include_router`` layer.

        Its ``path`` is missing every enclosing prefix and its
        ``dependencies``/``dependant`` are missing every router-level entry,
        so it must never be used to decide whether a gate is present. It is
        exposed for exactly one reason: this is the object FastAPI puts in
        ``request.scope["route"]``, so it is what production's
        ``route_is_external()`` actually inspects. An audit that wants to
        pin production behaviour has to feed it this, not the composed view.
        """
        return self._ctx.original_route

    # -- composed (what the router applies at request time) -----------------

    @property
    def path(self) -> str:
        """Full mounted path, e.g. ``/v1/agents/{agent_code}/runs``."""
        path = self._ctx.path
        # RouteContext.path is one of the few properties FastAPI implements
        # with a defaulted getattr, so it answers None instead of raising for
        # a route shape it does not know how to compose (an included
        # websocket route composes to ``""``, for instance). Turn that back
        # into a loud failure here — an audit keyed on "" or None silently
        # matches no prefix and no table.
        assert path, f"route context has no composed path: {self._ctx!r}"
        return path

    @property
    def methods(self) -> frozenset[str]:
        """HTTP methods, ``HEAD``/``OPTIONS`` included if the route declares them."""
        methods = self._ctx.methods
        assert methods, f"route context has no methods: {self._ctx!r}"
        return frozenset(methods)

    @property
    def verbs(self) -> frozenset[str]:
        """:attr:`methods` minus ``HEAD``/``OPTIONS`` — what the audits enumerate."""
        return frozenset(m for m in self.methods if m not in ("HEAD", "OPTIONS"))

    @property
    def name(self) -> str:
        name = self._ctx.name
        assert name, f"route context has no name: {self._ctx!r}"
        return name

    @property
    def endpoint(self) -> Callable[..., Any]:
        endpoint = self._ctx.endpoint
        assert endpoint is not None, f"route context has no endpoint: {self._ctx!r}"
        return endpoint

    @property
    def tags(self) -> list[str | Enum]:
        """Router-level + route-level tags, composed."""
        tags: list[str | Enum] = self._ctx.tags
        return tags

    @property
    def dependencies(self) -> list[params.Depends]:
        """Router-level + route-level ``dependencies=[...]``, outer to inner."""
        dependencies: list[params.Depends] = self._ctx.dependencies
        return dependencies

    @property
    def dependant(self) -> Dependant:
        """The resolved dependency graph FastAPI itself builds per request."""
        dependant: Dependant = self._ctx.dependant
        return dependant

    # -- matching -----------------------------------------------------------

    def full_matches(self, path: str, method: str) -> bool:
        """Whether a concrete ``method path`` request FULLY matches this route.

        Uses the router's own compiled regex (``RouteContext.path_regex``,
        the same object ``_IncludedRouter._match`` matches against), so
        "which route wins" here is decided by the production matcher, not by
        a re-implementation of it. ``Match.PARTIAL`` (path matches, method
        does not) is not a win and answers ``False``, exactly as
        ``Match.FULL`` semantics require.
        """
        if self._ctx.path_regex.match(path) is None:
            return False
        return method in self.methods

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<MountedRoute {sorted(self.verbs)} {self.path}>"


def mounted_routes(app: FastAPI) -> list[MountedRoute]:
    """Every route the app mounts, in the router's own resolution order.

    Order matters: ``_IncludedRouter._match`` walks its candidates depth
    first and returns the first ``Match.FULL``, and
    ``iter_route_contexts`` flattens the same tree in the same order — so
    index order here IS registration/shadowing order.
    """
    routes = [MountedRoute(ctx) for ctx in iter_route_contexts(app.routes)]
    assert routes, (
        "no routes enumerated from the app — the enumeration is broken, not the app. "
        "FastAPI's lazy include_router means a wrong walk yields an empty list "
        "instead of raising, so every audit built on it would pass vacuously."
    )
    return routes


def mounted_api_routes(app: FastAPI) -> list[MountedRoute]:
    """:func:`mounted_routes` filtered to ``APIRoute``-backed routes."""
    routes = [route for route in mounted_routes(app) if route.is_api_route]
    assert routes, "no APIRoute enumerated from the app — the enumeration is broken, not the app"
    return routes
