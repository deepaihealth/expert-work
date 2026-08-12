"""Integration-level hardening for the shared external (third-party) API
resolution layer — External-API-v1 P1 Task 7b.

Four defects, each found during a per-endpoint review but only fixable in
the *shared* layer every ``/v1/agents/{agent_code}/...`` endpoint goes
through (``api/_external.py`` + ``api/agents.py:_resolve_session`` +
``app.py``'s validation-error handling):

1. A read endpoint (``GET .../sessions``) must never mint a ``tenant_user``
   row for an unrecognized ``user_id`` — only a write endpoint may.
2. ``user_id`` normalization (stripping whitespace) must apply identically
   on the mint (write) and lookup (read) paths, or a space-suffixed id
   silently becomes two different people.
3. A 422 from FastAPI's own request validation must still carry the
   external envelope (``{"success", "data", "error"}``), not the framework
   default ``{"detail": [...]}}``.

Fixture mirrors ``test_external_sessions.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.api._external import external_subject_id
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AgentSpec, Role
from expert_work.runtime.runs import (
    InMemoryRunEventStore,
    InMemoryRunStore,
    RunStatus,
    make_event_record,
)
from expert_work.runtime.runs.store import MAX_LIST_LIMIT
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
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
    def __init__(self, client: AsyncClient, app: Any, tenant_id: UUID, headers: dict[str, str]):
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.headers = headers

    async def seed_agent(self) -> None:
        await self.app.state.agent_spec_repo.create(
            tenant_id=self.tenant_id, spec=_spec(), spec_sha256="a" * 64, created_by="seed"
        )

    async def has_subject(self, subject_id: str) -> bool:
        """Whether a ``tenant_user`` row with this ``subject_id`` exists —
        the ground truth for "did we mint a ghost user"."""
        rows = await self.app.state.tenant_user_repo.list_by_tenant(
            self.tenant_id, subject_type="user", limit=MAX_LIST_LIMIT
        )
        return any(row.subject_id == subject_id for row in rows)


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
    jwt = make_test_jwt(tenant_id=tenant_id, subject=str(uuid4()), roles=(Role.ADMIN.value,))
    headers = {"Authorization": f"Bearer {jwt}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        yield _Ctx(client, app, tenant_id, headers)


@pytest.mark.asyncio
async def test_list_sessions_never_mints_a_ghost_user(ctx: _Ctx) -> None:
    """Step 1 — the core assertion: a GET with an unrecognized ``user_id``
    returns 200 + an empty list, AND leaves no ``tenant_user`` row behind.
    Before the fix this endpoint called the mint-on-use resolver, so any
    string a third party enumerated would upsert a row."""
    await ctx.seed_agent()
    subject_id = external_subject_id("never-seen-before")
    assert not await ctx.has_subject(subject_id)

    resp = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "never-seen-before"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["sessions"] == []

    assert not await ctx.has_subject(subject_id)


@pytest.mark.asyncio
async def test_run_submit_still_mints_the_end_user(ctx: _Ctx) -> None:
    """Step 1's counterpart: the write path (``POST .../runs``) is
    mint-on-use BY DESIGN — a third party never pre-registers its
    end-users. Proves the read-path fix didn't take the mint away from the
    path that needs it."""
    await ctx.seed_agent()
    subject_id = external_subject_id("fresh-cust")
    assert not await ctx.has_subject(subject_id)

    resp = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "fresh-cust", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert resp.status_code == 202, resp.text

    assert await ctx.has_subject(subject_id)


@pytest.mark.asyncio
async def test_user_id_normalizes_identically_on_write_and_read(ctx: _Ctx) -> None:
    """Step 2: a session written under ``"cust-77"`` must be findable via
    ``"cust-77 "`` (trailing space) — both paths normalize through the same
    ``external_subject_id``, so they can never drift into two identities."""
    await ctx.seed_agent()
    created = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert created.status_code == 202, created.text

    resp = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "cust-77 "},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    sessions = resp.json()["data"]["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == created.json()["data"]["thread_id"]


@pytest.mark.asyncio
async def test_blank_after_normalization_is_a_client_error(ctx: _Ctx) -> None:
    """Step 2's guard: ``user_id="   "`` passes ``min_length=1`` (3 raw
    characters) but normalizes to empty — must be rejected with a
    machine-readable code, not silently treated as an unknown user."""
    await ctx.seed_agent()
    resp = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "   "},
        headers=ctx.headers,
    )
    assert 400 <= resp.status_code < 500, resp.text
    body = resp.json()
    assert body["error"]["code"]


@pytest.mark.asyncio
async def test_missing_user_id_422_uses_the_external_envelope(ctx: _Ctx) -> None:
    """Step 3: FastAPI's own ``RequestValidationError`` (missing required
    query param) must still come back as ``{"success", "data", "error"}}``
    — the shape every other external response uses — not the framework
    default ``{"detail": [...]}}``, which has no ``error.code`` for an SDK
    to key off of."""
    await ctx.seed_agent()
    resp = await ctx.client.get("/v1/agents/support-bot/sessions", headers=ctx.headers)
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert "detail" not in body
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["message"]


# ---------------------------------------------------------------------------
# External-API-v1 P1 followup — lookup_external_user_id's 500-row scan ceiling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_lookup_survives_more_than_500_active_tenant_users(ctx: _Ctx) -> None:
    """The core correctness-ceiling assertion this task removes.

    Before the fix, ``lookup_external_user_id`` resolved ``user_id`` by
    scanning ``list_by_tenant(subject_type="user", limit=MAX_LIST_LIMIT)``
    — the top 500 rows ordered most-recently-active-first. A tenant with
    more than 500 active end users (the third-party mint-one-row-per-
    end-user model makes this the norm, not an edge case) would silently
    lose the ability to look up any user whose row fell outside that
    window: someone who registered a while ago and simply hasn't sent a
    new message recently, while 500+ *other* users in the tenant have —
    their own ``GET .../sessions`` call would come back empty, as if they
    had never used the agent.

    Reproduces exactly that: the target user's session is created first,
    then 510 other users are minted with strictly later ``last_active_at``
    timestamps (deterministically, via a patched clock — a bare loop of
    real ``datetime.now(UTC)`` calls ties far too often on this hardware to
    reliably rank the target last, and a stable ``sort(reverse=True)``
    breaks such ties in favor of whichever row was inserted first, i.e. the
    target — so an un-patched loop would not reliably reproduce the bug).
    This guarantees the target's row ranks 511th — well outside any
    500-row window — while ``get_by_subject`` (the fix) is a point lookup
    that is immune to rank entirely.
    """
    await ctx.seed_agent()

    target_user_id = "quiet-customer"
    created = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": target_user_id, "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert created.status_code == 202, created.text
    session_id = created.json()["data"]["thread_id"]

    users = ctx.app.state.tenant_user_repo
    # Every filler gets a last_active_at strictly after "now" at increasing
    # 1ms steps — guaranteed later than the target's (real-clock) timestamp
    # from the POST above, with no reliance on real-time tie-breaking.
    base = datetime.now(UTC) + timedelta(seconds=1)
    with patch("expert_work.persistence.tenant_user.memory.datetime") as mock_dt:
        for i in range(510):
            mock_dt.now.return_value = base + timedelta(milliseconds=i)
            await users.resolve(
                tenant_id=ctx.tenant_id, subject_type="user", subject_id=f"filler-{i}"
            )

    resp = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": target_user_id},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    sessions = resp.json()["data"]["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == session_id


@pytest.mark.asyncio
async def test_deactivated_user_sessions_stay_unreachable(ctx: _Ctx) -> None:
    """``lookup_external_user_id``'s ``row.deleted_at is not None`` gate
    (``_external.py``) is the only thing preserving this invariant now that
    the lookup goes through ``get_by_subject`` — which, unlike the old
    ``list_by_tenant`` scan, does NOT filter ``deleted_at`` itself (it
    mirrors ``get``'s semantics; see ``base.py``). Without that gate, a
    soft-deactivated (purged) end user's sessions would resurface through
    this read endpoint — reopening a retention/privacy hole the prior
    ``list_by_tenant``-based scan closed for free (both store implementations
    already filter ``deleted_at IS NULL`` there).

    Creates a session, deactivates its owning user directly via the store's
    ``deactivate`` (Phase 3a purge_user), then asserts the session is no
    longer visible through the read endpoint — matching the pre-fix
    behavior exactly, not a new restriction.
    """
    await ctx.seed_agent()

    target_user_id = "soon-to-be-purged"
    created = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": target_user_id, "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert created.status_code == 202, created.text

    users = ctx.app.state.tenant_user_repo
    subject_id = external_subject_id(target_user_id)
    row = await users.get_by_subject(
        tenant_id=ctx.tenant_id, subject_type="user", subject_id=subject_id
    )
    assert row is not None

    # Self-containment: prove the session WAS reachable before the deactivate,
    # so a later refactor that breaks the read path for an unrelated reason
    # can't leave this test passing for the wrong reason (P1 final review,
    # deferred Minor #6 — the assertion below is only meaningful against a
    # non-empty "before").
    before = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": target_user_id},
        headers=ctx.headers,
    )
    assert before.status_code == 200, before.text
    assert len(before.json()["data"]["sessions"]) == 1, before.text

    assert await users.deactivate(row.id, tenant_id=ctx.tenant_id, now=datetime.now(UTC)) is True

    resp = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": target_user_id},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["sessions"] == []


# ---------------------------------------------------------------------------
# P1 final review, Critical C1 — ``load_owned_run`` must never mint either
# ---------------------------------------------------------------------------


async def _terminal_run(ctx: _Ctx, user_id: str) -> tuple[str, str]:
    """Submit a run for ``user_id``, drive it terminal, seed one durable frame.

    Terminal matters: a non-terminal run makes the events endpoint live-attach
    to a bridge nothing is driving, so the request would hang instead of
    answering. The durable frame matters too — without it a successful replay
    is indistinguishable from the degenerate "no store wired, emit a bare
    ``end``" branch, so "the gate leaked" would look the same as "the gate
    held".
    """
    created = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": user_id, "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["run_id"]
    await ctx.app.state.run_event_store.append(
        make_event_record(run_id=UUID(run_id), seq=1, event_name="updates", data={"step": 1})
    )
    await ctx.app.state.run_store.set_status(
        run_id=UUID(run_id),
        tenant_id=ctx.tenant_id,
        status=RunStatus.SUCCESS,
        updated_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    return run_id, created.json()["data"]["thread_id"]


@pytest.mark.asyncio
async def test_run_events_never_mints_a_ghost_user(ctx: _Ctx) -> None:
    """``GET .../runs/{id}/events`` is the third read endpoint, and it went
    through ``load_owned_run`` — which resolved the end user with the
    *mint-on-use* default. So spraying arbitrary ``user_id``s at it returned
    404 every time while writing one ``tenant_user`` row per attempt (they
    then show up on the user-dimension ops page). Same invariant as
    ``test_list_sessions_never_mints_a_ghost_user``, on the endpoint that
    was missed.
    """
    await ctx.seed_agent()
    run_id, _thread_id = await _terminal_run(ctx, "cust-77")

    for ghost in ("ghost-1", "ghost-2", "ghost-3"):
        assert not await ctx.has_subject(external_subject_id(ghost))
        resp = await ctx.client.get(
            f"/v1/agents/support-bot/runs/{run_id}/events",
            params={"user_id": ghost},
            headers=ctx.headers,
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "RUN_NOT_FOUND"
        assert not await ctx.has_subject(external_subject_id(ghost)), (
            f"GET .../runs/{{id}}/events minted a tenant_user row for {ghost!r}"
        )


@pytest.mark.asyncio
async def test_run_events_neither_resurrect_nor_expose_a_purged_user(ctx: _Ctx) -> None:
    """The two remaining halves of C1, which only the events endpoint had.

    1. **Resurrection** — ``resolve`` clears ``deleted_at`` on purpose ("a
       returning user comes back clean"), so routing a *read* through it
       un-deleted a purged identity. Phase 3b's 90-day hard delete selects on
       ``deleted_at``, so that row also became permanently uncollectable.
    2. **Contradiction** — because the resurrection happened *before* the
       ownership check, the purged user's own run events came back 200 with a
       real event stream while their messages 404'd and their session list was
       empty. Three read endpoints, same identity, three different answers.
       This asserts all three agree.
    """
    await ctx.seed_agent()
    run_id, thread_id = await _terminal_run(ctx, "soon-to-be-purged")

    users = ctx.app.state.tenant_user_repo
    row = await users.get_by_subject(
        tenant_id=ctx.tenant_id,
        subject_type="user",
        subject_id=external_subject_id("soon-to-be-purged"),
    )
    assert row is not None

    # Self-containment: the events endpoint must be reachable BEFORE the
    # purge, or the 404 below proves nothing about the purge.
    live = await ctx.client.get(
        f"/v1/agents/support-bot/runs/{run_id}/events",
        params={"user_id": "soon-to-be-purged"},
        headers=ctx.headers,
    )
    assert live.status_code == 200, live.text
    assert "event: updates" in live.text

    assert await users.deactivate(row.id, tenant_id=ctx.tenant_id, now=datetime.now(UTC)) is True
    purged_at = (await users.get(row.id, tenant_id=ctx.tenant_id)).deleted_at
    assert purged_at is not None

    events = await ctx.client.get(
        f"/v1/agents/support-bot/runs/{run_id}/events",
        params={"user_id": "soon-to-be-purged"},
        headers=ctx.headers,
    )
    assert events.status_code == 404, events.text
    assert events.json()["error"]["code"] == "RUN_NOT_FOUND"

    still_purged = (await users.get(row.id, tenant_id=ctx.tenant_id)).deleted_at
    assert still_purged == purged_at, "a read endpoint cleared deleted_at (resurrected the user)"

    messages = await ctx.client.get(
        f"/v1/agents/support-bot/sessions/{thread_id}/messages",
        params={"user_id": "soon-to-be-purged"},
        headers=ctx.headers,
    )
    assert messages.status_code == 404, messages.text
    sessions = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "soon-to-be-purged"},
        headers=ctx.headers,
    )
    assert sessions.status_code == 200, sessions.text
    assert sessions.json()["data"]["sessions"] == []


# ---------------------------------------------------------------------------
# P1 wrap-up, N1 — the same defect family on ``_resolve_session``'s two callers
#
# ``POST .../runs`` and ``POST .../sessions`` share ``agents.py:_resolve_session``,
# which minted the end user *before* branching on ``session_id``. The
# ``session_id is None`` branch genuinely creates the session it addresses, so
# its mint is intentional product behavior (a third party never pre-registers
# its end users) — but the branch that is *handed* a ``session_id`` addresses an
# already-existing session, exactly like the upload path fixed in C1's second
# half. Both halves are covered here: the mint must survive, the ghost row must
# not.
# ---------------------------------------------------------------------------


async def _owned_session(ctx: _Ctx, user_id: str = "cust-77") -> str:
    """Create a real session owned by ``user_id`` and return its id."""
    created = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": user_id, "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert created.status_code == 202, created.text
    return str(created.json()["data"]["thread_id"])


async def _address_session(ctx: _Ctx, endpoint: str, *, user_id: str, session_id: str) -> Any:
    """Call one of the two ``_resolve_session`` callers with an explicit
    ``session_id`` — the branch that must NOT mint."""
    if endpoint == "runs":
        return await ctx.client.post(
            "/v1/agents/support-bot/runs",
            json={
                "user_id": user_id,
                "session_id": session_id,
                "input": "hi",
                "mode": "queue",
            },
            headers=ctx.headers,
        )
    return await ctx.client.post(
        "/v1/agents/support-bot/sessions",
        json={"user_id": user_id, "session_id": session_id},
        headers=ctx.headers,
    )


@pytest.mark.asyncio
async def test_session_bind_without_session_id_still_mints_the_end_user(ctx: _Ctx) -> None:
    """The half that must NOT change: ``POST .../sessions`` with no
    ``session_id`` creates the session it addresses, so it mints the
    ``tenant_user`` row on first use. Killing this mint would break the whole
    integration model (third parties do not pre-register end users), which is
    the headline risk of the N1 fix. ``test_run_submit_still_mints_the_end_user``
    is the same guard for the other caller.
    """
    await ctx.seed_agent()
    subject_id = external_subject_id("fresh-binder")
    assert not await ctx.has_subject(subject_id)

    resp = await ctx.client.post(
        "/v1/agents/support-bot/sessions",
        json={"user_id": "fresh-binder"},
        headers=ctx.headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["session_id"]

    assert await ctx.has_subject(subject_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["runs", "sessions"])
async def test_addressing_a_foreign_session_never_mints_a_ghost_user(
    ctx: _Ctx, endpoint: str
) -> None:
    """Point an existing ``session_id`` at enumerated ``user_id``s: every call
    must 404 *and* leave no ``tenant_user`` row behind. Before the fix
    ``_resolve_session`` resolved (upserted) the end user before checking
    ownership, so each rejected attempt still wrote a ghost row that surfaces on
    the user-dimension ops page — the same shape as C1 on the read endpoints and
    on the upload path.
    """
    await ctx.seed_agent()
    session_id = await _owned_session(ctx)

    for ghost in ("ghost-1", "ghost-2"):
        subject_id = external_subject_id(ghost)
        assert not await ctx.has_subject(subject_id)

        resp = await _address_session(ctx, endpoint, user_id=ghost, session_id=session_id)

        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "SESSION_NOT_FOUND"
        assert not await ctx.has_subject(subject_id), (
            f"POST .../{endpoint} minted a tenant_user row for {ghost!r}, "
            "who does not own the supplied session"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["runs", "sessions"])
async def test_addressing_a_foreign_session_never_resurrects_a_purged_user(
    ctx: _Ctx, endpoint: str
) -> None:
    """The second half of the same defect: ``resolve`` clears ``deleted_at`` on
    purpose ("a returning user comes back clean"), so a call that ends in 404
    still un-deleted a purged identity — and Phase 3b's 90-day hard delete
    selects on ``deleted_at``, so that row became permanently uncollectable.
    A rejected call must leave the purge intact.
    """
    await ctx.seed_agent()
    session_id = await _owned_session(ctx)

    users = ctx.app.state.tenant_user_repo
    # Give the purged identity a real row first (its own run), so the assertion
    # below is about the purge surviving — not about a row that never existed.
    await _owned_session(ctx, "soon-to-be-purged")
    row = await users.get_by_subject(
        tenant_id=ctx.tenant_id,
        subject_type="user",
        subject_id=external_subject_id("soon-to-be-purged"),
    )
    assert row is not None
    assert await users.deactivate(row.id, tenant_id=ctx.tenant_id, now=datetime.now(UTC)) is True
    purged_at = (await users.get(row.id, tenant_id=ctx.tenant_id)).deleted_at
    assert purged_at is not None

    resp = await _address_session(ctx, endpoint, user_id="soon-to-be-purged", session_id=session_id)
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "SESSION_NOT_FOUND"

    still_purged = (await users.get(row.id, tenant_id=ctx.tenant_id)).deleted_at
    assert still_purged == purged_at, (
        f"POST .../{endpoint} cleared deleted_at (resurrected a purged user) "
        "on a call it rejected with 404"
    )
