"""P-1 —— 控制台三个读面把「已被取代 / 重发」投影出来。

对外那两个面(``/messages`` / ``/items`` / 对外 ``/runs``)各自在自己的文件里
测;这里管员工侧的三个:``GET /v1/sessions/{id}/messages``、
``GET /v1/sessions/{id}/runs``、``GET /v1/conversations/{id}`` 的 ``runs[]``。
三处与对外面共用同一份 ``MessageTurn`` / ``RunInfo`` 投影,漏掉任何一处,
对话页就会把被取代的一轮画成普通轮。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.common.message_stamp import STAMP_CREATED_AT, STAMP_RUN_ID
from expert_work.common.supersede import mark_superseded, tombstone_message
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunEventStore,
    InMemoryRunStore,
    RunInfo,
    RunStatus,
)
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID
_NOW = datetime(2026, 9, 10, 9, 0, 0, tzinfo=UTC)


class _SeedState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


async def _seed_thread_messages(
    checkpointer: InMemorySaver, thread_id: str, messages: list[BaseMessage]
) -> None:
    graph = StateGraph(_SeedState)
    graph.add_node("n", _noop)
    graph.add_edge(START, "n")
    seeded = graph.compile(checkpointer=checkpointer)
    await seeded.ainvoke(
        {"messages": messages},
        config={"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
    )


def _noop(_state: _SeedState) -> dict[str, list[BaseMessage]]:
    return {"messages": []}


def _stamp(run_id: UUID, at: datetime) -> dict[str, str]:
    return {STAMP_RUN_ID: str(run_id), STAMP_CREATED_AT: at.isoformat()}


def _run(
    *,
    run_id: UUID,
    thread_id: UUID,
    created_at: datetime,
    regenerated_from: UUID | None = None,
) -> RunInfo:
    return RunInfo(
        run_id=run_id,
        tenant_id=_TENANT,
        thread_id=thread_id,
        user_id=None,
        status=RunStatus.SUCCESS,
        on_disconnect=DisconnectMode.CANCEL,
        is_resume=False,
        error=None,
        created_at=created_at,
        updated_at=created_at,
        finished_at=created_at + timedelta(seconds=2),
        regenerated_from_run_id=regenerated_from,
    )


class _Ctx:
    def __init__(self, client: AsyncClient, app: Any, thread_id: UUID) -> None:
        self.client = client
        self.app = app
        self.thread_id = thread_id
        self.old_run = uuid4()
        self.new_run = uuid4()


@pytest.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    run_store = InMemoryRunStore()
    run_event_store = InMemoryRunEventStore()
    checkpointer = InMemorySaver()
    runtime = stub_agent_runtime(run_store=run_store, run_event_store=run_event_store)
    runtime.durable_checkpointer = checkpointer
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
        agent_runtime=runtime,
        run_repo=run_store,
        run_event_repo=run_event_store,
    )
    thread_id = uuid4()
    await app.state.thread_meta_repo.create(
        thread_id=thread_id,
        tenant_id=_TENANT,
        created_by="seed",
        agent_name="alpha",
        agent_version="1.0.0",
    )
    c = _Ctx(client=None, app=app, thread_id=thread_id)  # type: ignore[arg-type]
    await run_store.create(_run(run_id=c.old_run, thread_id=thread_id, created_at=_NOW))
    await run_store.create(
        _run(
            run_id=c.new_run,
            thread_id=thread_id,
            created_at=_NOW + timedelta(seconds=10),
            regenerated_from=c.old_run,
        )
    )
    await run_store.mark_superseded(
        run_id=c.old_run, tenant_id=_TENANT, superseded_by_run_id=c.new_run
    )
    old = [
        mark_superseded(m, new_run_id=str(c.new_run), now=_NOW)
        for m in (
            HumanMessage(content="U1", additional_kwargs=_stamp(c.old_run, _NOW)),
            AIMessage(content="A1", additional_kwargs=_stamp(c.old_run, _NOW)),
        )
    ]
    stone = tombstone_message(
        mark_superseded(
            HumanMessage(content="U0", additional_kwargs=_stamp(c.old_run, _NOW)),
            new_run_id=str(c.new_run),
            now=_NOW,
        )
    )
    await _seed_thread_messages(
        checkpointer,
        str(thread_id),
        [
            stone,
            *old,
            HumanMessage(
                content="U2", additional_kwargs=_stamp(c.new_run, _NOW + timedelta(seconds=10))
            ),
            AIMessage(
                content="A2", additional_kwargs=_stamp(c.new_run, _NOW + timedelta(seconds=11))
            ),
        ],
    )
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT)}"}
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://control-plane.test",
        headers=headers,
    ) as client:
        c.client = client
        yield c


@pytest.mark.asyncio
async def test_console_messages_expose_superseded_by_and_tombstone(ctx: _Ctx) -> None:
    resp = await ctx.client.get(f"/v1/sessions/{ctx.thread_id}/messages")
    assert resp.status_code == 200, resp.text
    rows = resp.json()["data"]["messages"]
    assert [(r["content"], r["superseded_by"], r["tombstone"]) for r in rows] == [
        ("", str(ctx.new_run), True),
        ("U1", str(ctx.new_run), False),
        ("A1", str(ctx.new_run), False),
        ("U2", None, False),
        ("A2", None, False),
    ]


@pytest.mark.asyncio
async def test_console_thread_runs_expose_supersede_links(ctx: _Ctx) -> None:
    resp = await ctx.client.get(f"/v1/sessions/{ctx.thread_id}/runs")
    assert resp.status_code == 200, resp.text
    by_run = {r["run_id"]: r for r in resp.json()["data"]["runs"]}
    assert by_run[str(ctx.old_run)]["superseded_by"] == str(ctx.new_run)
    assert by_run[str(ctx.old_run)]["regenerated_from"] is None
    assert by_run[str(ctx.new_run)]["regenerated_from"] == str(ctx.old_run)
    assert by_run[str(ctx.new_run)]["superseded_by"] is None


@pytest.mark.asyncio
async def test_conversation_detail_runs_expose_supersede_links(ctx: _Ctx) -> None:
    resp = await ctx.client.get(f"/v1/conversations/{ctx.thread_id}")
    assert resp.status_code == 200, resp.text
    by_run = {r["run_id"]: r for r in resp.json()["data"]["runs"]}
    assert by_run[str(ctx.old_run)]["superseded_by"] == str(ctx.new_run)
    assert by_run[str(ctx.old_run)]["regenerated_from"] is None
    assert by_run[str(ctx.new_run)]["regenerated_from"] == str(ctx.old_run)
    assert by_run[str(ctx.new_run)]["superseded_by"] is None
