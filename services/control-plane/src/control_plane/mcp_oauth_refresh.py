"""Lazy MCP OAuth token refresh — Stream MCP-OAUTH (OA-6).

:class:`McpOAuthRefresher.ensure_fresh` is called per connection while the
per-user pool builds (under that pool's per-(tenant, user) lock, so refreshes
are serialized). It returns a *usable* connection record — refreshing the
access token when it is within ``skew`` of expiry — or ``None`` when the
connector cannot be made usable, in which case the pool simply doesn't attach
it (the rest of the agent still builds).

Failure taxonomy (so a transient fault doesn't burn down a still-valid token):

* ``invalid_grant`` from the token endpoint → the refresh token is revoked /
  expired → status ``revoked``; the user must reconnect.
* any other refresh failure (network, 5xx, other OAuth error) → only a problem
  if the access token is *already* expired → status ``error`` (retried on the
  next build); if it's merely near-expiry, the current token is served as-is.

Tokens never touch the DB / logs — new values overwrite the existing
``secret://`` refs in place. Logs carry only the ``connection_id`` (a UUID).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

import httpx

from control_plane.mcp_oauth import (
    McpOAuthError,
    discover_oauth_metadata,
)
from control_plane.mcp_oauth import (
    refresh_token as oauth_refresh_token,
)
from control_plane.tenant_scope import bypass_rls_session
from expert_work.persistence import McpConnectorCatalogStore, McpOAuthConnectionStore
from expert_work.protocol import McpOAuthConnectionPatch, McpOAuthConnectionRecord
from expert_work.runtime.secret_store import SecretStore, parse_secret_ref

logger = logging.getLogger("expert_work.control_plane.mcp_oauth_refresh")

Clock = Callable[[], datetime]
HttpClientFactory = Callable[[], httpx.AsyncClient]


class OAuthRefreshLock(Protocol):
    """Cross-replica per-(tenant, user) refresh lock (PgMcpOAuthRefreshLock)."""

    def acquire(self, *, tenant_id: UUID, user_id: str) -> AbstractAsyncContextManager[None]:
        """Hold the lock for one user's refresh; released on context exit."""


# Refresh when the access token is within this window of expiry.
_DEFAULT_SKEW_S = 60.0
# If the token endpoint omits ``expires_in`` on refresh, assume this TTL so we
# don't re-refresh on every build (a past ``expires_at`` would loop forever).
_REFRESH_FALLBACK_TTL_S = 3600
# RFC 6749 error that means the refresh token itself is no longer valid.
_REVOKED_OAUTH_ERROR = "invalid_grant"


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class McpOAuthRefresher:
    """Refreshes near-expiry OAuth access tokens during pool build (OA-6)."""

    def __init__(
        self,
        *,
        oauth_store: McpOAuthConnectionStore,
        catalog_store: McpConnectorCatalogStore,
        secret_store: SecretStore,
        http_factory: HttpClientFactory,
        clock: Clock = _utc_now,
        skew_s: float = _DEFAULT_SKEW_S,
        refresh_lock: OAuthRefreshLock | None = None,
        invalidate_user: Callable[[UUID, str], None] | None = None,
    ) -> None:
        self._oauth_store = oauth_store
        self._catalog_store = catalog_store
        self._secret_store = secret_store
        self._http_factory = http_factory
        self._clock = clock
        self._skew_s = skew_s
        # Cross-replica lock (OA-6 hardening). None = single-replica / tests:
        # the pool's in-process asyncio.Lock is the only serialisation.
        self._refresh_lock = refresh_lock
        # 二期 PR2 T2 — a refresh overwrites the token secrets IN PLACE (same
        # refs), so any cached per-user built agent still holds the old token
        # baked in; after a successful refresh this evicts those builds
        # (``AgentRuntime.invalidate_user``). None = tests / no runtime wired.
        self._invalidate_user = invalidate_user

    async def ensure_fresh(
        self, record: McpOAuthConnectionRecord
    ) -> McpOAuthConnectionRecord | None:
        """Return a usable (possibly refreshed) record, or ``None`` if unusable."""
        if record.status != "connected" or not record.access_token_ref:
            return None

        now = self._clock()
        expires_at = record.token_expires_at
        if expires_at is None or expires_at > now + timedelta(seconds=self._skew_s):
            return record  # no expiry, or comfortably valid — serve as-is.

        truly_expired = expires_at <= now
        if not record.refresh_token_ref:
            if truly_expired:
                await self._mark(record, status="expired", error="token_expired")
                return None
            return record  # near-expiry but no refresh token — serve while valid.

        return await self._refresh(record, truly_expired=truly_expired)

    async def _refresh(
        self, record: McpOAuthConnectionRecord, *, truly_expired: bool
    ) -> McpOAuthConnectionRecord | None:
        """Refresh under the cross-replica lock (OA-6 hardening).

        Without a lock (single replica / tests) refresh directly. With one,
        hold it for this user, then **reload** the connection — another replica
        may have refreshed while we blocked, in which case we serve its token
        instead of refreshing again (which would race the rotated refresh token
        and falsely revoke the connection)."""
        if self._refresh_lock is None:
            return await self._do_refresh(record, truly_expired=truly_expired)
        async with self._refresh_lock.acquire(tenant_id=record.tenant_id, user_id=record.user_id):
            fresh = await self._oauth_store.get(
                connection_id=record.id, tenant_id=record.tenant_id, user_id=record.user_id
            )
            if fresh is None or fresh.status != "connected" or not fresh.access_token_ref:
                return None
            now = self._clock()
            if fresh.token_expires_at is None or fresh.token_expires_at > now + timedelta(
                seconds=self._skew_s
            ):
                logger.info("mcp_oauth_refresh.already_fresh connection_id=%s", fresh.id)
                return fresh  # refreshed by another replica while we blocked
            return await self._do_refresh(fresh, truly_expired=fresh.token_expires_at <= now)

    async def _do_refresh(
        self, record: McpOAuthConnectionRecord, *, truly_expired: bool
    ) -> McpOAuthConnectionRecord | None:
        # oauth_client_id lives on the (NULL-tenant) catalog entry.
        async with bypass_rls_session():
            entry = await self._catalog_store.get_by_id(record.catalog_id)
        if entry is None or not entry.oauth_client_id:
            if truly_expired:
                await self._mark(record, status="error", error="catalog_missing")
                return None
            return record

        refresh_secret = await self._secret_store.get(
            parse_secret_ref(record.refresh_token_ref or "")
        )
        if not refresh_secret:
            if truly_expired:
                await self._mark(record, status="expired", error="refresh_token_missing")
                return None
            return record

        try:
            async with self._http_factory() as http:
                metadata = await discover_oauth_metadata(mcp_url=record.resolved_url, http=http)
                tokens = await oauth_refresh_token(
                    metadata=metadata,
                    client_id=entry.oauth_client_id,
                    refresh_token=refresh_secret,
                    http=http,
                )
        except McpOAuthError as exc:
            if exc.oauth_error == _REVOKED_OAUTH_ERROR:
                logger.warning("mcp_oauth_refresh.revoked connection_id=%s", record.id)
                await self._mark(record, status="revoked", error=exc.code)
                return None
            logger.warning("mcp_oauth_refresh.failed connection_id=%s", record.id)
            if truly_expired:
                await self._mark(record, status="error", error=exc.code)
                return None
            return record  # transient + still valid → serve current token.

        # Overwrite the access token (and any rotated refresh token) in place.
        await self._secret_store.put(
            parse_secret_ref(record.access_token_ref or ""), tokens.access_token
        )
        if tokens.refresh_token and record.refresh_token_ref:
            await self._secret_store.put(
                parse_secret_ref(record.refresh_token_ref), tokens.refresh_token
            )
        # 二期 PR2 T2 — the token refs were just overwritten in place; evict the
        # user's cached built agents so the next build resolves the new token.
        if self._invalidate_user is not None:
            self._invalidate_user(record.tenant_id, record.user_id)
        ttl = tokens.expires_in if tokens.expires_in else _REFRESH_FALLBACK_TTL_S
        now = self._clock()
        updated = await self._oauth_store.update(
            connection_id=record.id,
            tenant_id=record.tenant_id,
            user_id=record.user_id,
            patch=McpOAuthConnectionPatch(
                status="connected",
                token_expires_at=now + timedelta(seconds=ttl),
                scopes=tokens.scope if tokens.scope is not None else None,
                last_refresh_at=now,
                clear_last_error=True,
            ),
        )
        logger.info("mcp_oauth_refresh.ok connection_id=%s", record.id)
        return updated

    async def _mark(self, record: McpOAuthConnectionRecord, *, status: str, error: str) -> None:
        await self._oauth_store.update(
            connection_id=record.id,
            tenant_id=record.tenant_id,
            user_id=record.user_id,
            patch=McpOAuthConnectionPatch(status=status, last_error=error),  # type: ignore[arg-type]
        )
        return None
