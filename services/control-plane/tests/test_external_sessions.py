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

from control_plane.api.runs import _build_human_message
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.common.message_stamp import STAMP_CREATED_AT, STAMP_RUN_ID
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.feedback_store import FeedbackRecord
from expert_work.protocol import AgentSpec
from expert_work.protocol.multimodal import IMAGE_REF_PREFIX, ImageRef
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

    async def bind_session(self, user_id: str) -> UUID:
        """Bind a session for ``user_id`` against ``support-bot`` and return its thread id."""
        bound = await self.client.post(
            "/v1/agents/support-bot/sessions", json={"user_id": user_id}, headers=self.headers
        )
        assert bound.status_code == 201, bound.text
        return UUID(bound.json()["data"]["session_id"])


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
    # External-API-v1 P2-b security fix (external_only()) — this file's whole
    # point is proving the external plane works for a machine caller; the
    # employee JWT here was a borrowed-fixture incidental, not a deliberate
    # test of console-JWT access (this file predates the gate).
    jwt = make_test_jwt(
        tenant_id=tenant_id,
        subject="sa-test",
        sub_type="service_account",
        roles=(),
        scopes=("admin",),
    )
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
    assert sessions[0]["session_id"] == a.json()["data"]["thread_id"]


@pytest.mark.asyncio
async def test_first_external_run_auto_titles_the_session(ctx: _Ctx) -> None:
    """对外首条 run 的 input 截成会话标题(照控制台 ``runs.py`` 同款先例)。

    此前对外平面从不写 title,project-service 建的会话在对话页整页
    「未命名对话」(2026-08-26 用户反馈)。第二条 run 不覆盖已有标题。
    """
    await ctx.seed_agent()
    a = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "帮我生成一份 7 天的康复训练方案", "mode": "queue"},
        headers=ctx.headers,
    )
    assert a.status_code == 202, a.text
    session_id = a.json()["data"]["thread_id"]

    listed = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    sessions = {s["session_id"]: s for s in listed.json()["data"]["sessions"]}
    assert sessions[session_id]["title"] == "帮我生成一份 7 天的康复训练方案"

    # 第二条 run 不覆盖。
    await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={
            "user_id": "cust-77",
            "session_id": session_id,
            "input": "换个说法再来一份",
            "mode": "queue",
        },
        headers=ctx.headers,
    )
    again = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    still = {s["session_id"]: s for s in again.json()["data"]["sessions"]}
    assert still[session_id]["title"] == "帮我生成一份 7 天的康复训练方案"


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
        run_id=UUID(pending_run.json()["data"]["run_id"]),
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
    assert queued_run.json()["data"]["status"] == "queued"

    running_run = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert running_run.status_code == 202, running_run.text
    await ctx.run_store.set_status(
        run_id=UUID(running_run.json()["data"]["run_id"]),
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
        run_id=UUID(done_run.json()["data"]["run_id"]),
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
    assert by_id[pending_run.json()["data"]["thread_id"]]["running"] is True
    assert by_id[queued_run.json()["data"]["thread_id"]]["running"] is True
    assert by_id[running_run.json()["data"]["thread_id"]]["running"] is True
    assert by_id[done_run.json()["data"]["thread_id"]]["running"] is False


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
    session_id = started.json()["data"]["thread_id"]
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
    session_id = started.json()["data"]["thread_id"]
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
            "feedback": None,
            "superseded_by": None,
            "tombstone": False,
        },
        {
            "role": "assistant",
            "content": "turn1 assistant",
            "channel": "final",
            "created_at": None,
            "run_id": None,
            "feedback": None,
            "superseded_by": None,
            "tombstone": False,
        },
        {
            "role": "user",
            "content": "turn2 user",
            "channel": None,
            "created_at": None,
            "run_id": None,
            "feedback": None,
            "superseded_by": None,
            "tombstone": False,
        },
        {
            "role": "assistant",
            "content": "turn2 assistant",
            "channel": "final",
            "created_at": None,
            "run_id": None,
            "feedback": None,
            "superseded_by": None,
            "tombstone": False,
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
            "feedback": None,
            "superseded_by": None,
            "tombstone": False,
        },
        {
            "role": "user",
            "content": "turn2 user",
            "channel": None,
            "created_at": None,
            "run_id": None,
            "feedback": None,
            "superseded_by": None,
            "tombstone": False,
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
    session_id = started.json()["data"]["thread_id"]
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
            "feedback": None,
            "superseded_by": None,
            "tombstone": False,
        },
        {
            "role": "assistant",
            "content": "stamped answer",
            "channel": "final",
            "created_at": None,
            "run_id": None,
            "feedback": None,
            "superseded_by": None,
            "tombstone": False,
        },
    ]


@pytest.mark.asyncio
async def test_messages_never_leak_the_internal_image_ref(ctx: _Ctx) -> None:
    """B-14 — the last place an ``expert_work://`` URI could cross the
    external boundary.

    A text-only model (``supports_vision=False``) with an Agent that still
    declares a ``vision:`` block gets its images as inline text mentions
    (``api/runs.py::_build_human_message`` Path B: ``[image attached:
    expert_work://image/…]``) so the model can call ``ask_image``. That
    line is a prompt-engineering artefact for the model, not something the
    end user typed — and it carries the internal image URI the attachment
    unification (spec 2026-08-17) removed from every other external
    response in favour of the opaque ``upl_`` id.

    Seeds the REAL Path-B message (built by the production helper, not a
    hand-written string) so this test tracks the producer's exact format.
    The turn itself stays — ``message_count`` is documented as "the same
    messages 5.3 returns", so an image-only turn must remain a (now empty)
    entry rather than vanish and skew pagination.
    """
    await ctx.seed_agent()
    checkpointer = InMemorySaver()
    ctx.app.state.agent_runtime.durable_checkpointer = checkpointer
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    session_id = started.json()["data"]["thread_id"]
    refs = [
        ImageRef(
            tenant_id=ctx.tenant_id, thread_id=UUID(session_id), image_id=uuid4(), ext=".png"
        ).to_uri()
        for _ in range(2)
    ]
    assert all(r.startswith(IMAGE_REF_PREFIX) for r in refs)
    await _seed_thread_messages(
        checkpointer,
        session_id,
        [
            # Text + two images + one document — every Path-B part at once.
            _build_human_message(
                input_text="what's in these?\n\nsecond paragraph",
                image_refs=refs,
                supports_vision=False,
                document_names=["report.pdf"],
            ),
            AIMessage(content="two photos and a report"),
            # Image-only turn: nothing typed, only attachments.
            _build_human_message(input_text=None, image_refs=refs[:1], supports_vision=False),
            AIMessage(content="another photo"),
        ],
    )

    resp = await ctx.client.get(
        f"/v1/agents/support-bot/sessions/{session_id}/messages",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    assert IMAGE_REF_PREFIX not in resp.text
    assert "image attached" not in resp.text
    contents = [(m["role"], m["content"]) for m in resp.json()["data"]["messages"]]
    assert contents == [
        # User paragraphs (including the blank line the user typed) and the
        # document mention are untouched; only the image-ref lines go.
        ("user", "what's in these?\n\nsecond paragraph\n\n[file attached: report.pdf]"),
        ("assistant", "two photos and a report"),
        ("user", ""),
        ("assistant", "another photo"),
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
    thread_id = UUID(started.json()["data"]["thread_id"])
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
    thread_id = UUID(started.json()["data"]["thread_id"])
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
    never_computed_id = UUID(never_computed.json()["data"]["thread_id"])
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
    assert by_id[computed_zero.json()["data"]["thread_id"]]["message_count"] == 0
    assert by_id[never_computed.json()["data"]["thread_id"]]["message_count"] is None


# ---------------------------------------------------------------------------
# External-API-v1 P2-b Task 3 — rename / archive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_session(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert started.status_code == 202, started.text
    session_id = started.json()["data"]["thread_id"]

    resp = await ctx.client.patch(
        f"/v1/agents/support-bot/sessions/{session_id}",
        json={"user_id": "cust-77", "title": "改过的标题"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True

    listed = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"]["sessions"][0]["title"] == "改过的标题"


@pytest.mark.asyncio
async def test_rename_blank_title_is_422(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert started.status_code == 202, started.text
    session_id = started.json()["data"]["thread_id"]

    resp = await ctx.client.patch(
        f"/v1/agents/support-bot/sessions/{session_id}",
        json={"user_id": "cust-77", "title": "   "},
        headers=ctx.headers,
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "INVALID_TITLE"


@pytest.mark.asyncio
async def test_rename_foreign_session_is_404(ctx: _Ctx) -> None:
    """Renaming someone else's session must 404 — never 403 — so a third
    party cannot distinguish "not yours" from "doesn't exist"."""
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert started.status_code == 202, started.text
    session_id = started.json()["data"]["thread_id"]

    resp = await ctx.client.patch(
        f"/v1/agents/support-bot/sessions/{session_id}",
        json={"user_id": "someone-else", "title": "偷改"},
        headers=ctx.headers,
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "SESSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_archive_session_hides_from_list(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert started.status_code == 202, started.text
    session_id = started.json()["data"]["thread_id"]

    resp = await ctx.client.delete(
        f"/v1/agents/support-bot/sessions/{session_id}",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True

    listed = await ctx.client.get(
        "/v1/agents/support-bot/sessions",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"]["sessions"] == []


@pytest.mark.asyncio
async def test_archive_requires_at_least_write_scope(ctx: _Ctx) -> None:
    """A ``read``-only key cannot archive — archive is gated on ``"write"``
    (same tier as ``rename_session``), not the console side's ``"delete"``
    (User decision 2026-08-13: an external ``"delete"`` gate maps only to an
    ``admin``-scoped key — ``rbac.py``'s OPERATOR grant for ``session`` has
    no ``delete`` — which would force a third party to hold a key that can
    also rewrite service accounts / role bindings just to archive a
    session). This still proves the endpoint isn't open to every key: a
    ``read``-only (VIEWER) principal must be refused.
    """
    await ctx.seed_agent()
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert started.status_code == 202, started.text
    session_id = started.json()["data"]["thread_id"]

    read_only_jwt = make_test_jwt(
        tenant_id=ctx.tenant_id,
        subject="sa-read-only",
        sub_type="service_account",
        roles=(),
        scopes=("read",),
    )
    resp = await ctx.client.delete(
        f"/v1/agents/support-bot/sessions/{session_id}",
        params={"user_id": "cust-77"},
        headers={"Authorization": f"Bearer {read_only_jwt}"},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_messages_echo_only_the_callers_own_feedback(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    checkpointer = InMemorySaver()
    ctx.app.state.agent_runtime.durable_checkpointer = checkpointer
    session_id = await ctx.bind_session("cust-77")
    run_id = uuid4()
    stamp = {
        STAMP_RUN_ID: str(run_id),
        STAMP_CREATED_AT: datetime(2026, 9, 9, tzinfo=UTC).isoformat(),
    }
    await _seed_thread_messages(
        checkpointer,
        str(session_id),
        [
            HumanMessage(content="hi", additional_kwargs=dict(stamp)),
            AIMessage(content="hello", additional_kwargs=dict(stamp)),
        ],
    )
    me = await ctx.app.state.tenant_user_repo.resolve(
        tenant_id=ctx.tenant_id, subject_type="user", subject_id="ext:cust-77"
    )
    store = ctx.app.state.feedback_store
    await store.upsert(
        FeedbackRecord(
            tenant_id=ctx.tenant_id,
            thread_id=session_id,
            run_id=run_id,
            rating="up",
            source="external",
            actor_id=str(me.id),
        )
    )
    await store.upsert(
        FeedbackRecord(
            tenant_id=ctx.tenant_id,
            thread_id=session_id,
            run_id=run_id,
            rating="down",
            comment="nope",
            source="external",
            actor_id="someone-else",
        )
    )
async def test_messages_expose_superseded_by_and_tombstone(ctx: _Ctx) -> None:
    """P-1 —— 被取代轮在 ``/messages`` 上可见且带标记;墓碑正文为空但仍占一行。"""
    from expert_work.common.supersede import mark_superseded, tombstone_message

    await ctx.seed_agent()
    checkpointer = InMemorySaver()
    ctx.app.state.agent_runtime.durable_checkpointer = checkpointer
    started = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    session_id = started.json()["data"]["thread_id"]
    new_run = uuid4()
    now = datetime(2026, 9, 10, tzinfo=UTC)
    old = [
        mark_superseded(m, new_run_id=str(new_run), now=now)
        for m in (HumanMessage(content="U1"), AIMessage(content="A1"))
    ]
    stone = tombstone_message(
        mark_superseded(HumanMessage(content="U0"), new_run_id=str(new_run), now=now)
    )
    await _seed_thread_messages(
        checkpointer,
        session_id,
        [stone, *old, HumanMessage(content="U2"), AIMessage(content="A2")],
    )

    resp = await ctx.client.get(
        f"/v1/agents/support-bot/sessions/{session_id}/messages",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    msgs = resp.json()["data"]["messages"]
    assert len(msgs) == 2
    assert all(m["feedback"] == {"rating": "up", "comment": None, "item_id": None} for m in msgs)
    rows = resp.json()["data"]["messages"]
    assert [(r["content"], r["superseded_by"], r["tombstone"]) for r in rows] == [
        ("", str(new_run), True),
        ("U1", str(new_run), False),
        ("A1", str(new_run), False),
        ("U2", None, False),
        ("A2", None, False),
    ]
