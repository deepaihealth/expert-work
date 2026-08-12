"""External session-listing + message-history API — external-API P1 Task 3.

``GET /v1/agents/{agent_code}/sessions`` and
``GET /v1/agents/{agent_code}/sessions/{session_id}/messages`` are third-party
facing: they must scope strictly to ``(tenant, user, agent)`` and never widen
to "every session in the tenant" the way the console's own listing endpoint
does when its ownership filter goes unset. Fixture mirrors
``test_agents_run_for_user.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from datetime import UTC, datetime
from typing import Annotated, Any, TypedDict
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.common.message_stamp import STAMP_CREATED_AT, STAMP_RUN_ID
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AgentSpec, Role
from expert_work.runtime.runs import InMemoryRunEventStore, InMemoryRunStore, RunStatus
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)


class _SeedState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


async def _seed_thread_messages(
    checkpointer: InMemorySaver, thread_id: str, messages: list[BaseMessage]
) -> None:
    """Write one checkpoint holding ``messages`` for ``thread_id`` (mirrors a
    real run leaving a durable checkpoint). Same pattern as
    ``test_sessions_api.py``'s helper of the same name."""
    graph = StateGraph(_SeedState)
    graph.add_node("n", lambda _state: {"messages": []})
    graph.add_edge(START, "n")
    seeded = graph.compile(checkpointer=checkpointer)
    await seeded.ainvoke(
        {"messages": messages},
        config={"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
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
        headers: dict[str, str],
        run_store: InMemoryRunStore,
    ):
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.headers = headers
        self.run_store = run_store

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
    jwt = make_test_jwt(tenant_id=tenant_id, subject=str(uuid4()), roles=(Role.ADMIN.value,))
    headers = {"Authorization": f"Bearer {jwt}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        yield _Ctx(client, app, tenant_id, headers, run_store)


@pytest.mark.asyncio
async def test_sessions_list_only_returns_this_users_sessions(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    a = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert a.status_code == 202, a.text
    await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-99", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )

    resp = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    sessions = resp.json()["data"]["sessions"]
    assert len(sessions) == 1
    # Queue-mode 202 carries the thread id in the body, not the
    # ``X-Expert-Work-Session-Id`` header (that's only set on the SSE path —
    # see runs.py:spawn_run's queue-mode branch, out of this task's scope).
    assert sessions[0]["session_id"] == a.json()["thread_id"]


@pytest.mark.asyncio
async def test_sessions_running_reflects_persistent_run_status(ctx: _Ctx) -> None:
    """``running`` must come from the durable ``RunStore`` — not
    ``RunManager.has_inflight``, a per-process in-memory registry (its own
    docstring says so) that a multi-replica deployment can't rely on: a run
    executing on another instance would falsely read ``running: false``.

    Covers all three active statuses independently — ``PENDING``,
    ``QUEUED``, ``RUNNING`` — each on its own session, plus a terminal one,
    so dropping any single status out of ``_ACTIVE_RUN_STATUSES`` fails
    exactly that session's assertion (review fix round 2 — the first version
    of this test only ever drove runs through ``RUNNING``/``SUCCESS``,
    leaving ``PENDING`` with zero mutation coverage).

    ``QUEUED`` is reached the way a third party actually gets there:
    ``mode="queue"`` (``runs.py:830-850``) creates the run already in that
    status — never claimed here, never hand-set. ``PENDING`` and ``RUNNING``
    are dispatched directly on the durable row (as if some other replica, or
    a ``RunQueueWorker``, owns the run) since nothing in this stub harness
    naturally parks a run in either for long enough to observe.
    """
    await ctx.seed_agent()

    pending_run = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert pending_run.status_code == 202, pending_run.text
    await ctx.run_store.set_status(
        run_id=UUID(pending_run.json()["run_id"]),
        tenant_id=ctx.tenant_id,
        status=RunStatus.PENDING,
        updated_at=datetime.now(UTC),
    )

    # Left exactly as ``mode="queue"`` creates it — status QUEUED, untouched.
    queued_run = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert queued_run.status_code == 202, queued_run.text
    assert queued_run.json()["status"] == "queued"

    running_run = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert running_run.status_code == 202, running_run.text
    await ctx.run_store.set_status(
        run_id=UUID(running_run.json()["run_id"]),
        tenant_id=ctx.tenant_id,
        status=RunStatus.RUNNING,
        updated_at=datetime.now(UTC),
    )

    done_run = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert done_run.status_code == 202, done_run.text
    await ctx.run_store.set_status(
        run_id=UUID(done_run.json()["run_id"]),
        tenant_id=ctx.tenant_id,
        status=RunStatus.SUCCESS,
        updated_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )

    resp = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    by_id = {s["session_id"]: s for s in resp.json()["data"]["sessions"]}
    assert by_id[pending_run.json()["thread_id"]]["running"] is True
    assert by_id[queued_run.json()["thread_id"]]["running"] is True
    assert by_id[running_run.json()["thread_id"]]["running"] is True
    assert by_id[done_run.json()["thread_id"]]["running"] is False


@pytest.mark.asyncio
async def test_sessions_list_requires_user_id(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    resp = await ctx.client.get("/v1/agents/support-bot/sessions", headers=ctx.headers)
    # Missing the required query param must be rejected, never silently widened
    # to "every session in the tenant".
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_sessions_list_is_scoped_to_the_agent(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    resp = await ctx.client.get(
        "/v1/agents/other-bot/sessions",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["sessions"] == []


@pytest.mark.asyncio
async def test_messages_404_for_another_user(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    session_id = started.json()["thread_id"]
    resp = await ctx.client.get(
        f"/v1/agents/support-bot/sessions/{session_id}/messages",
        params={"user_id": "someone-else"},
        headers=ctx.headers,
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "SESSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_messages_returns_envelope_for_its_owner(ctx: _Ctx) -> None:
    """Exercises the real ``read_turns`` path end to end — not the
    ``durable_checkpointer is None`` early return (``stub_agent_runtime``
    never sets one, which made the original version of this test a
    tautology: it asserted against a hard-coded ``{"messages": []}``
    literal without ever running the field-mapping / hidden-message-filter
    / pagination code). Wires an ``InMemorySaver`` in directly (mutating the
    runtime object the fixture already built — no change to
    ``agent_fixtures.py`` needed) and seeds a real checkpoint."""
    await ctx.seed_agent()
    checkpointer = InMemorySaver()
    ctx.app.state.agent_runtime.durable_checkpointer = checkpointer
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    session_id = started.json()["thread_id"]
    await _seed_thread_messages(
        checkpointer,
        session_id,
        [
            HumanMessage(content="turn1 user"),
            AIMessage(content="turn1 assistant"),
            # Orchestrator scaffolding (CM-1-style recovery advisory) — must
            # never reach a third-party app (``include_hidden=False``).
            HumanMessage(
                content="<recovery-advisory>internal only</recovery-advisory>",
                additional_kwargs={"expert_work_hide_from_ui": True},
            ),
            HumanMessage(content="turn2 user"),
            AIMessage(content="turn2 assistant"),
        ],
    )

    resp = await ctx.client.get(
        f"/v1/agents/support-bot/sessions/{session_id}/messages",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    # Hidden scaffolding dropped; role/content/channel mapped straight off
    # ``read_turns`` — proves this isn't the ``checkpointer is None`` stub path.
    # None of these messages carry a P2 stamp, so created_at/run_id must come
    # back null — pre-stamp history is never backfilled.
    assert body["data"]["messages"] == [
        {
            "role": "user",
            "content": "turn1 user",
            "channel": None,
            "created_at": None,
            "run_id": None,
        },
        {
            "role": "assistant",
            "content": "turn1 assistant",
            "channel": "final",
            "created_at": None,
            "run_id": None,
        },
        {
            "role": "user",
            "content": "turn2 user",
            "channel": None,
            "created_at": None,
            "run_id": None,
        },
        {
            "role": "assistant",
            "content": "turn2 assistant",
            "channel": "final",
            "created_at": None,
            "run_id": None,
        },
    ]

    # Pagination slices the (already hidden-filtered) turn list.
    paged = await ctx.client.get(
        f"/v1/agents/support-bot/sessions/{session_id}/messages",
        params={"user_id": "cust-77", "limit": 2, "offset": 1},
        headers=ctx.headers,
    )
    assert paged.status_code == 200, paged.text
    assert paged.json()["data"]["messages"] == [
        {
            "role": "assistant",
            "content": "turn1 assistant",
            "channel": "final",
            "created_at": None,
            "run_id": None,
        },
        {
            "role": "user",
            "content": "turn2 user",
            "channel": None,
            "created_at": None,
            "run_id": None,
        },
    ]


@pytest.mark.asyncio
async def test_messages_exposes_created_at_and_run_id_stamps(ctx: _Ctx) -> None:
    """P2 Task 5: stamped messages must surface ``created_at``/``run_id`` on
    the external endpoint, and a corrupt stamp on one message must not take
    down the whole session's read.

    Seeds real ``additional_kwargs`` stamps (not a hand-rolled ``MessageTurn``)
    so this exercises the actual ``extract_turns`` parsing path end to end —
    a test that only checked the field existed without ever setting a stamp
    would pass even if the parser were a no-op.
    """
    await ctx.seed_agent()
    checkpointer = InMemorySaver()
    ctx.app.state.agent_runtime.durable_checkpointer = checkpointer
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    session_id = started.json()["thread_id"]
    stamped_at = datetime(2026, 8, 12, 1, 2, 3, tzinfo=UTC)
    run_id = uuid4()
    await _seed_thread_messages(
        checkpointer,
        session_id,
        [
            HumanMessage(
                content="stamped question",
                additional_kwargs={
                    STAMP_CREATED_AT: stamped_at.isoformat(),
                    STAMP_RUN_ID: str(run_id),
                },
            ),
            # Corrupt stamp on this one message must degrade to null, not
            # blow up the whole session's read.
            AIMessage(
                content="stamped answer",
                additional_kwargs={
                    STAMP_CREATED_AT: "not-a-timestamp",
                    STAMP_RUN_ID: "not-a-uuid",
                },
            ),
        ],
    )

    resp = await ctx.client.get(
        f"/v1/agents/support-bot/sessions/{session_id}/messages",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    messages = resp.json()["data"]["messages"]
    assert messages == [
        {
            "role": "user",
            "content": "stamped question",
            "channel": None,
            "created_at": stamped_at.isoformat(),
            "run_id": str(run_id),
        },
        {
            "role": "assistant",
            "content": "stamped answer",
            "channel": "final",
            "created_at": None,
            "run_id": None,
        },
    ]


@pytest.mark.asyncio
async def test_sessions_list_exposes_message_count(ctx: _Ctx) -> None:
    """Task 8: ``thread_meta.message_count`` (written by run finalization's
    ``include_hidden=False`` recount — out of this task's scope, only read
    here) must round-trip into the external sessions list response.

    Seeds a distinctive non-zero value (7) rather than 0 — ``ThreadMeta``
    already defaults ``message_count`` to 0 on creation
    (``thread_meta/memory.py:49``), so asserting ``== 0`` here would pass
    even if the field were never wired through the endpoint at all.
    """
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert started.status_code == 202, started.text
    thread_id = UUID(started.json()["thread_id"])
    updated = await ctx.app.state.thread_meta_repo.update_message_count(
        thread_id, 7, tenant_id=ctx.tenant_id
    )
    assert updated is True

    resp = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()["data"]["sessions"][0]
    assert "message_count" in item
    assert item["message_count"] == 7


@pytest.mark.asyncio
async def test_sessions_list_message_count_null_when_never_computed(ctx: _Ctx) -> None:
    """A session whose run has never reached finalization has
    ``message_count IS NULL`` ("not yet computed") — the column has no
    ``server_default`` precisely so this is distinguishable from ``0``
    ("computed, genuinely empty"; ``0144_thread_meta_msg_count.py``). The
    response must serialize this as JSON ``null`` with the key present, not
    omit the field and not coerce it to ``0``.

    ``ThreadMetaStore.create()`` always writes ``0`` and
    ``update_message_count()`` only accepts ``int`` (by design — its
    docstring: "callers should never write 0 to mean not yet computed")
    — there is no public API to put a row back into the NULL state, so
    this reaches into the in-memory store's row dict directly, the same
    way ``test_runs_api.py``/``test_resume_idempotency_flow.py`` do for
    states with no public writer.
    """
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert started.status_code == 202, started.text
    thread_id = UUID(started.json()["thread_id"])
    repo = ctx.app.state.thread_meta_repo
    row = await repo.get(thread_id, tenant_id=ctx.tenant_id)
    assert row is not None
    repo._rows[thread_id] = row.model_copy(update={"message_count": None})

    resp = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()["data"]["sessions"][0]
    assert "message_count" in item
    assert item["message_count"] is None


@pytest.mark.asyncio
async def test_sessions_list_message_count_distinguishes_null_from_zero(ctx: _Ctx) -> None:
    """Both states in the same response: one session genuinely computed to
    0 (an agent run that finalized with no visible turns — the default
    ``ThreadMeta.create()`` leaves in place), one never computed (``None``,
    seeded the same way as the previous test). A regression that collapsed
    either state into the other — e.g. hard-coding the field, or a stray
    ``count or 0`` — would pass a test that only ever inspected one session
    in isolation; asserting both in the same list catches it.
    """
    await ctx.seed_agent()
    computed_zero = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert computed_zero.status_code == 202, computed_zero.text
    never_computed = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert never_computed.status_code == 202, never_computed.text
    never_computed_id = UUID(never_computed.json()["thread_id"])
    repo = ctx.app.state.thread_meta_repo
    row = await repo.get(never_computed_id, tenant_id=ctx.tenant_id)
    assert row is not None
    repo._rows[never_computed_id] = row.model_copy(update={"message_count": None})

    resp = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    by_id = {s["session_id"]: s for s in resp.json()["data"]["sessions"]}
    assert by_id[computed_zero.json()["thread_id"]]["message_count"] == 0
    assert by_id[never_computed.json()["thread_id"]]["message_count"] is None
