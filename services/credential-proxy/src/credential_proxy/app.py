"""aiohttp application — the proxy's HTTP surface (STREAM-F-DESIGN § 4.4).

``create_app`` builds the real service (DB-backed stores, aiohttp
forwarder); tests inject a pre-built :class:`CredentialProxy` so the
routes are exercised without a DB or real upstreams.
"""

from __future__ import annotations

import asyncio
import logging
import time
from hmac import compare_digest
from uuid import UUID

from aiohttp import ClientError, web
from aiohttp.typedefs import Handler

from credential_proxy.allowlist import AllowlistStore, DbAllowlistStore
from credential_proxy.audit import DbEgressAuditStore, DbProxyAuditStore
from credential_proxy.cache import SecretCache
from credential_proxy.domain import (
    AllowlistDeniedError,
    AllowlistKey,
    BadForwardRequestError,
    ForwardRequest,
    SecretMissingError,
)
from credential_proxy.egress_proxy import EgressProxyServer
from credential_proxy.forwarder import AiohttpForwarder
from credential_proxy.proxy import CredentialProxy
from credential_proxy.settings import CredentialProxySettings
from expert_work.persistence import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.runtime.secret_store import make_secret_store

logger = logging.getLogger(__name__)

PROXY_KEY: web.AppKey[CredentialProxy] = web.AppKey("proxy", CredentialProxy)
ALLOWLIST_KEY: web.AppKey[AllowlistStore] = web.AppKey("allowlist", AllowlistStore)
CACHE_KEY: web.AppKey[SecretCache] = web.AppKey("cache", SecretCache)
_ENGINE_KEY: web.AppKey[object] = web.AppKey("engine", object)
_FORWARDER_KEY: web.AppKey[AiohttpForwarder] = web.AppKey("forwarder", AiohttpForwarder)
_EGRESS_SERVER_KEY: web.AppKey[asyncio.AbstractServer] = web.AppKey(
    "egress_server", asyncio.AbstractServer
)

#: Hop-by-hop / body-framing response headers the proxy must not relay
#: verbatim — aiohttp recomputes framing from the (decompressed) body.
_RESPONSE_DROP_HEADERS = frozenset(
    {"content-length", "content-encoding", "transfer-encoding", "connection"}
)

_ADMIN_TOKEN_KEY: web.AppKey[str] = web.AppKey("admin_token", str)

#: The one ``/admin`` route that stays open — the kubelet's startup /
#: liveness / readiness probes all hit it, before the pod is trusted by
#: anything, and it reveals nothing but "the process answers".
_ADMIN_OPEN_PATHS = frozenset({"/admin/health"})


def create_app(
    settings: CredentialProxySettings | None = None,
    *,
    proxy: CredentialProxy | None = None,
    allowlist: AllowlistStore | None = None,
    cache: SecretCache | None = None,
) -> web.Application:
    """Build the aiohttp app.

    ``proxy`` / ``allowlist`` / ``cache`` inject pre-built collaborators
    (tests) — the app then skips all DB / forwarder wiring.
    """
    resolved_settings = settings or CredentialProxySettings()
    app = web.Application(middlewares=[_admin_auth])
    if resolved_settings.admin_token:
        app[_ADMIN_TOKEN_KEY] = resolved_settings.admin_token

    if proxy is not None and allowlist is not None and cache is not None:
        app[PROXY_KEY] = proxy
        app[ALLOWLIST_KEY] = allowlist
        app[CACHE_KEY] = cache
    else:
        app.on_startup.append(_make_startup(resolved_settings))
        app.on_cleanup.append(_cleanup)

    _register_routes(app)
    return app


def _make_startup(settings: CredentialProxySettings):  # type: ignore[no-untyped-def]
    async def _startup(app: web.Application) -> None:
        engine = create_async_engine_from_config(
            DatabaseConfig(dsn=settings.db_dsn, echo_sql=settings.db_echo)
        )
        session_factory = create_async_session_factory(engine)
        forwarder = AiohttpForwarder(timeout_s=settings.upstream_timeout_s)
        cache = SecretCache(max_size=settings.cache_max_size, ttl_s=settings.cache_ttl_s)
        allowlist = DbAllowlistStore(session_factory)
        app[_ENGINE_KEY] = engine
        app[_FORWARDER_KEY] = forwarder
        app[CACHE_KEY] = cache
        app[ALLOWLIST_KEY] = allowlist
        app[PROXY_KEY] = CredentialProxy(
            allowlist=allowlist,
            secret_store=make_secret_store(
                settings.secret_store_backend,
                env_file=settings.secret_store_env_file,
            ),
            cache=cache,
            audit=DbProxyAuditStore(session_factory),
            forwarder=forwarder,
        )
        # The backend name is config, not a secret — but it is not logged:
        # CodeQL's clear-text-logging query taints any `secret*`-named field.
        logger.info("credential_proxy.start")

        # Transparent egress proxy on its own port + asyncio listener, same
        # event loop (sandbox-egress §3.1). Audited, on by default.
        if settings.egress_enabled:
            egress = EgressProxyServer(
                token_secret=settings.egress_token_secret,
                audit=DbEgressAuditStore(session_factory),
                now=_wall_clock,
                connect_timeout_s=settings.egress_connect_timeout_s,
            )
            app[_EGRESS_SERVER_KEY] = await egress.start(settings.host, settings.egress_port)

    return _startup


def _wall_clock() -> float:
    return time.time()


async def _cleanup(app: web.Application) -> None:
    egress_server = app.get(_EGRESS_SERVER_KEY)
    if egress_server is not None:
        egress_server.close()
        await egress_server.wait_closed()
    await app[_FORWARDER_KEY].aclose()
    engine = app[_ENGINE_KEY]
    await engine.dispose()  # type: ignore[attr-defined]
    logger.info("credential_proxy.stop")


@web.middleware
async def _admin_auth(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Bearer-token gate on the ``/admin`` write surface (B-31 ②).

    The design doc claims these routes are reachable only over mTLS with
    ``SAN=control-plane``; no such restriction was ever implemented, the
    Service is a plain ClusterIP, and there is no NetworkPolicy — so the
    gate has to live here.

    Why it matters more than "an admin API without auth": ``/admin/allowlist``
    *writes* the allowlist, and the allowlist is the only thing ``/forward``
    checks before resolving a secret and injecting it as ``Authorization``
    on a request to a caller-named upstream. Registering a row is therefore
    enough to have the proxy deliver a tenant's secret to an address of the
    caller's choosing.

    Unset token → 503, not "open". A gate that disappears when unconfigured
    is the bug, not the fallback.
    """
    path = request.path
    if not path.startswith("/admin/") or path in _ADMIN_OPEN_PATHS:
        return await handler(request)

    expected = request.app.get(_ADMIN_TOKEN_KEY)
    if not expected:
        return web.json_response({"detail": "admin API is not configured"}, status=503)

    scheme, _, presented = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not compare_digest(presented, expected):
        return web.json_response({"detail": "admin API requires a bearer token"}, status=401)
    return await handler(request)


def _register_routes(app: web.Application) -> None:
    app.router.add_route("*", "/forward", _forward)
    app.router.add_post("/admin/allowlist", _admin_add_allowlist)
    app.router.add_delete("/admin/allowlist/{tenant}/{agent}/{version}", _admin_remove_allowlist)
    app.router.add_post("/admin/cache/invalidate", _admin_invalidate_cache)
    app.router.add_get("/admin/health", _admin_health)


async def _forward(request: web.Request) -> web.Response:
    try:
        forward_request = await _parse_forward_request(request)
    except BadForwardRequestError as exc:
        return web.json_response({"detail": str(exc)}, status=400)

    proxy = request.app[PROXY_KEY]
    try:
        result = await proxy.forward(forward_request)
    except AllowlistDeniedError as exc:
        return web.json_response({"detail": str(exc)}, status=403)
    except SecretMissingError as exc:
        return web.json_response({"detail": str(exc)}, status=502)
    except ClientError as exc:
        return web.json_response({"detail": f"upstream request failed: {exc}"}, status=502)

    relayed = {k: v for k, v in result.headers.items() if k.lower() not in _RESPONSE_DROP_HEADERS}
    return web.Response(status=result.status, headers=relayed, body=result.body)


async def _parse_forward_request(request: web.Request) -> ForwardRequest:
    headers = request.headers
    tenant_raw = _require_header(headers, "X-Expert-Work-Tenant")
    try:
        tenant_id = UUID(tenant_raw)
    except ValueError as exc:
        msg = f"X-Expert-Work-Tenant is not a valid UUID: {tenant_raw!r}"
        raise BadForwardRequestError(msg) from exc

    session_raw = headers.get("X-Expert-Work-Session")
    return ForwardRequest(
        tenant_id=tenant_id,
        agent_name=_require_header(headers, "X-Expert-Work-Agent"),
        agent_version=_require_header(headers, "X-Expert-Work-Agent-Version"),
        secret_ref=_require_header(headers, "X-Expert-Work-Secret-Ref"),
        upstream_url=_require_header(headers, "X-Expert-Work-Upstream"),
        method=request.method,
        headers=dict(headers),
        body=await request.read(),
        session_id=UUID(session_raw) if session_raw else None,
        sandbox_id=headers.get("X-Expert-Work-Sandbox"),
    )


def _require_header(headers: object, name: str) -> str:
    value = headers.get(name)  # type: ignore[attr-defined]
    if not value:
        msg = f"missing required header: {name}"
        raise BadForwardRequestError(msg)
    return str(value)


async def _admin_add_allowlist(request: web.Request) -> web.Response:
    body = await request.json()
    try:
        key = AllowlistKey(
            tenant_id=UUID(body["tenant_id"]),
            agent_name=body["agent_name"],
            agent_version=body["agent_version"],
            secret_ref=body["secret_ref"],
            purpose=body.get("purpose"),
        )
    except (KeyError, ValueError) as exc:
        return web.json_response({"detail": f"invalid allowlist entry: {exc}"}, status=400)
    await request.app[ALLOWLIST_KEY].add(key)
    return web.json_response({"status": "registered"}, status=201)


async def _admin_remove_allowlist(request: web.Request) -> web.Response:
    try:
        tenant_id = UUID(request.match_info["tenant"])
    except ValueError as exc:
        return web.json_response({"detail": f"invalid tenant id: {exc}"}, status=400)
    removed = await request.app[ALLOWLIST_KEY].remove_agent_version(
        tenant_id,
        request.match_info["agent"],
        request.match_info["version"],
    )
    logger.info("credential_proxy.allowlist_revoked count=%d", removed)
    return web.Response(status=204)


async def _admin_invalidate_cache(request: web.Request) -> web.Response:
    request.app[CACHE_KEY].invalidate_all()
    return web.Response(status=204)


async def _admin_health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})
