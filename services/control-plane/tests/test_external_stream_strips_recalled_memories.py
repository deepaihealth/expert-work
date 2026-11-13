"""对外事件流不带召回的长期记忆 —— ``memory_recall`` 这一步的 ``updates`` 帧。

``memory_recall`` 节点把召回的整条 ``MemoryItem`` 写进 ``recalled_memories``,
``run_agent`` 把这一步的写入原样发上 bridge 并落 ``run_event``。控制台要靠它画
「召回了哪些记忆」,所以帧本身不能改;但第三方(API key)拿到的是同一帧 —— 记忆
正文明文、``tenant_id`` / ``user_id``、来源会话 / run 的内部 id 全在里面。

这里走真端点 + 真图(``build_react_graph`` 接一个返回固定记忆的召回节点)+ 真
``run_agent``,逐条钉对外出帧路径:

* ``POST /v1/agents/{code}/runs`` 的 stream 模式(默认 legacy 格式)—— 实时;
* ``GET …/runs/{run_id}/events`` —— 终态 run 的回放(读 ``run_event``);
* 同一 ``Idempotency-Key`` 的重试 —— 幂等重放(``build_events_response``);
* ``…/runs/{run_id}:regenerate`` 的 stream 模式 —— 另一条走 ``spawn_run`` 的路径。

对外每条都只能看到 ``{"memory_recall": {"_duration_ms": N}}``;``run_event`` 里
的落库行和控制台读到的帧保持完整。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from control_plane.api._run_event_stream import (
    EXTERNAL_HIDDEN_EVENTS,
    EXTERNAL_STRIPPED_STATE_KEYS,
    build_event_producer,
)
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.runtime import AgentRuntime
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AgentSpec
from expert_work.protocol.memory_item import MemoryItem
from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunEventStore,
    InMemoryRunStore,
    RunManager,
    RunStatus,
)
from expert_work.runtime.runs.schemas import TERMINAL_RUN_STATUSES
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from orchestrator import BuiltAgent, GraphRunner, ToolRegistry, ToolSpec, build_react_graph
from orchestrator.sse import (
    _BACKGROUND_PERSIST_WRITERS,
    run_agent,
    sse_consumer,
    strip_frame_state_keys,
)
from orchestrator.state import AgentState
from orchestrator.stream_items import STREAM_FORMAT_LEGACY
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier, make_test_jwt

_SECRET_CONTENT = "recalled-memory-body-5c1e"
_MEMORY_ID = UUID("00000000-0000-4000-8000-00000000a11c")
_MEMORY_TENANT = UUID("00000000-0000-4000-8000-00000000b22d")
_MEMORY_USER = UUID("00000000-0000-4000-8000-00000000c33e")
_SOURCE_THREAD = "source-thread-7d2f"
_SOURCE_RUN = "source-run-8e3a"
_CONTENT_HASH = "content-hash-9f4b"
#: B-300 PR 2 —— 记忆对象上新加的两个内部字段(设计 §6.3):来源与向量模型名。
_EMBEDDING_MODEL = "embed-vendor-0a6c/model-1d7e"

#: 对外 wire 上一个都不能出现的东西 —— 正文、各种 id、内部字段名。
_FORBIDDEN = (
    _SECRET_CONTENT,
    str(_MEMORY_ID),
    str(_MEMORY_TENANT),
    str(_MEMORY_USER),
    _SOURCE_THREAD,
    _SOURCE_RUN,
    _CONTENT_HASH,
    "recalled_memories",
    "assistant_derived",
    '"origin"',
    _EMBEDDING_MODEL,
    '"embedding_model"',
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


def _memory() -> MemoryItem:
    return MemoryItem(
        id=_MEMORY_ID,
        tenant_id=_MEMORY_TENANT,
        user_id=_MEMORY_USER,
        kind="fact",
        content=_SECRET_CONTENT,
        embedding=(),
        source_thread_id=_SOURCE_THREAD,
        source_run_id=_SOURCE_RUN,
        content_hash=_CONTENT_HASH,
        origin="assistant_derived",
        embedding_model=_EMBEDDING_MODEL,
    )


async def _recall(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """与真 ``memory_recall`` 节点同形:写 ``recalled_memories``(整条 ``MemoryItem``)。"""
    del state, config
    return {"recalled_memories": [_memory()]}


async def _fake_llm(
    *,
    messages: Sequence[BaseMessage],
    tools: Sequence[ToolSpec],
    on_delta: Callable[[Any], Awaitable[None]] | None = None,
) -> AIMessage:
    del messages, tools, on_delta
    return AIMessage(content="stub agent reply", id="ai-stub")


def _runtime(
    *, run_store: InMemoryRunStore, run_event_store: InMemoryRunEventStore
) -> AgentRuntime:
    graph = GraphRunner(checkpointer=InMemorySaver()).compile(
        build_react_graph(
            llm_caller=_fake_llm,  # type: ignore[arg-type]
            tool_registry=ToolRegistry(),
            memory_recall_node=_recall,
        )
    )

    async def _build(
        spec: object, *, tenant_id: object | None = None, user_id: str | None = None
    ) -> BuiltAgent:
        del spec, tenant_id, user_id
        return BuiltAgent(graph=graph, system_prompt="stub system prompt", max_steps=5)

    return AgentRuntime(
        run_manager=RunManager(store=run_store),
        stream_bridge=InMemoryStreamBridge(),
        agent_builder=_build,  # type: ignore[arg-type]
        run_event_store=run_event_store,
    )


class _Ctx:
    def __init__(
        self,
        client: AsyncClient,
        tenant_id: UUID,
        run_store: InMemoryRunStore,
        run_event_store: InMemoryRunEventStore,
    ) -> None:
        self.client = client
        self.tenant_id = tenant_id
        self.run_store = run_store
        self.run_event_store = run_event_store

    async def wait_terminal(self, run_id: UUID, *, timeout: float = 5.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            row = await self.run_store.get(run_id=run_id, tenant_id=self.tenant_id)
            if row is not None and row.status in TERMINAL_RUN_STATUSES:
                break
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"run {run_id} did not reach a terminal status: {row}")
            await asyncio.sleep(0.01)
        # 落库走后台攒批 writer;读 ``run_event`` 之前先等它冲干净。
        if _BACKGROUND_PERSIST_WRITERS:
            await asyncio.gather(*_BACKGROUND_PERSIST_WRITERS, return_exceptions=True)


@pytest.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    run_store = InMemoryRunStore()
    run_event_store = InMemoryRunEventStore()
    app = create_app(
        settings=Settings(
            service_name="control_plane_test",
            env="dev",
            auth_mode="dev",
            db_dsn="postgresql+asyncpg://test@localhost/test",
            rate_limit_burst=10_000,
            rate_limit_per_second=10_000.0,
            oidc_issuer=TEST_ISSUER,
            oidc_audience=[TEST_AUDIENCE],
        ),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        agent_runtime=_runtime(run_store=run_store, run_event_store=run_event_store),
        run_repo=run_store,
        run_event_repo=run_event_store,
    )
    tenant_id = uuid4()
    await app.state.agent_spec_repo.create(
        tenant_id=tenant_id,
        spec=AgentSpec.model_validate(deepcopy(_SPEC)),
        spec_sha256="a" * 64,
        created_by="seed",
    )
    jwt = make_test_jwt(
        tenant_id=tenant_id,
        subject="sa-test",
        sub_type="service_account",
        roles=(),
        scopes=("admin",),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://cp.test",
        headers={"Authorization": f"Bearer {jwt}"},
    ) as client:
        yield _Ctx(client, tenant_id, run_store, run_event_store)


def _updates_frames(body: str) -> list[dict[str, Any]]:
    """wire 上全部 ``updates`` 帧的 ``data``(按出现顺序)。"""
    frames: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        lines = block.splitlines()
        if "event: updates" not in lines:
            continue
        data = next(line for line in lines if line.startswith("data: "))
        frames.append(json.loads(data[len("data: ") :]))
    return frames


def _assert_external_memory_recall_frame(body: str) -> None:
    """对外:``memory_recall`` 这一步还在(帧形状不变),值里只剩 ``_duration_ms``。"""
    assert "event: end" in body  # sanity: 流真的跑完了
    recall = [f["memory_recall"] for f in _updates_frames(body) if "memory_recall" in f]
    assert len(recall) == 1, body
    assert set(recall[0]) == {"_duration_ms"}, recall[0]
    assert isinstance(recall[0]["_duration_ms"], int)
    leaked = [s for s in _FORBIDDEN if s in body]
    assert not leaked, f"external wire leaked {leaked}"


async def _stream_turn(ctx: _Ctx, **extra: Any) -> tuple[str, UUID]:
    body: dict[str, Any] = {"user_id": "cust-77", "input": "hi", **extra}
    resp = await ctx.client.post("/v1/agents/support-bot/runs", json=body)
    assert resp.status_code == 200, resp.text
    run_id = UUID(resp.headers["X-Expert-Work-Run-Id"])
    await ctx.wait_terminal(run_id)
    return resp.text, run_id


@pytest.mark.asyncio
async def test_live_legacy_stream_omits_recalled_memories(ctx: _Ctx) -> None:
    body, _ = await _stream_turn(ctx)
    _assert_external_memory_recall_frame(body)


@pytest.mark.asyncio
async def test_replay_omits_recalled_memories(ctx: _Ctx) -> None:
    _, run_id = await _stream_turn(ctx)
    resp = await ctx.client.get(
        f"/v1/agents/support-bot/runs/{run_id}/events", params={"user_id": "cust-77"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Expert-Work-Stream-Mode"] == "replay"
    _assert_external_memory_recall_frame(resp.text)


@pytest.mark.asyncio
async def test_idempotent_stream_replay_omits_recalled_memories(ctx: _Ctx) -> None:
    headers = {"Idempotency-Key": "memory-strip-1"}
    first = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi"},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    run_id = UUID(first.headers["X-Expert-Work-Run-Id"])
    await ctx.wait_terminal(run_id)
    _assert_external_memory_recall_frame(first.text)

    second = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi"},
        headers=headers,
    )
    assert second.status_code == 200, second.text
    assert UUID(second.headers["X-Expert-Work-Run-Id"]) == run_id
    _assert_external_memory_recall_frame(second.text)


@pytest.mark.asyncio
async def test_regenerate_stream_omits_recalled_memories(ctx: _Ctx) -> None:
    _, run_id = await _stream_turn(ctx, mode="stream")
    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{run_id}:regenerate",
        json={"user_id": "cust-77", "mode": "stream"},
    )
    assert resp.status_code == 200, resp.text
    _assert_external_memory_recall_frame(resp.text)


@pytest.mark.asyncio
async def test_console_data_keeps_full_recalled_memories(ctx: _Ctx) -> None:
    """控制台不受影响:落库行原样保留整条记忆,控制台用的回放(不传对外参数)
    读出来也是完整的 —— 对外裁剪只发生在对外出口,不在写入侧。"""
    _, run_id = await _stream_turn(ctx)

    rows = await ctx.run_event_store.list(run_id=run_id)
    stored = [
        r.data["memory_recall"]
        for r in rows
        if r.event_name == "updates" and "memory_recall" in r.data
    ]
    assert len(stored) == 1
    assert stored[0]["recalled_memories"][0]["content"] == _SECRET_CONTENT
    assert stored[0]["recalled_memories"][0]["id"] == str(_MEMORY_ID)
    # B-300 PR 2 —— 落库行里两个内部字段都在(证明对外那边是被裁掉、不是本来就没有)。
    assert stored[0]["recalled_memories"][0]["origin"] == "assistant_derived"
    assert stored[0]["recalled_memories"][0]["embedding_model"] == _EMBEDDING_MODEL

    plan = await build_event_producer(
        run_id=run_id,
        run_status=RunStatus.SUCCESS,
        event_store=ctx.run_event_store,
        stream_bridge=InMemoryStreamBridge(),
        since_seq=None,
        scope=None,
    )
    console = b"".join([c async for c in plan.producer]).decode()
    recall = [f["memory_recall"] for f in _updates_frames(console) if "memory_recall" in f]
    assert recall[0]["recalled_memories"][0]["content"] == _SECRET_CONTENT


class _RecallGraph:
    """一步召回 + 一步 agent —— 直接喂 ``run_agent``,给实时 consumer 层用。"""

    async def astream(
        self, input: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del input, config, stream_mode
        yield {"memory_recall": {"recalled_memories": [_memory()]}}
        yield {"agent": {"step_count": 1}}


async def _never() -> bool:
    return False


@pytest.mark.asyncio
async def test_live_consumer_strips_externally_and_console_keeps_full_items() -> None:
    """同一个 bridge 上先挂对外 consumer、再挂控制台 consumer:对外裁掉,控制台
    照旧拿到整条记忆 —— 也证明裁剪没有改写 bridge 上那份共享的帧对象。"""
    bridge, rm = InMemoryStreamBridge(), RunManager()
    record = await rm.create(
        run_id=uuid4(), thread_id=uuid4(), tenant_id=uuid4(), on_disconnect=DisconnectMode.CANCEL
    )
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_RecallGraph(),  # type: ignore[arg-type]
        graph_input={},
        config={"configurable": {"thread_id": str(record.thread_id)}},
    )

    async def _wire(strip: frozenset[str]) -> str:
        chunks = [
            c
            async for c in sse_consumer(
                bridge=bridge,
                record=record,
                run_manager=rm,
                is_disconnected=_never,
                heartbeat_interval=5.0,
                hide_events=EXTERNAL_HIDDEN_EVENTS if strip else frozenset(),
                strip_state_keys=strip,
                stream_format=STREAM_FORMAT_LEGACY,
            )
        ]
        return b"".join(chunks).decode()

    _assert_external_memory_recall_frame(await _wire(EXTERNAL_STRIPPED_STATE_KEYS))

    console = await _wire(frozenset())
    recall = [f["memory_recall"] for f in _updates_frames(console) if "memory_recall" in f]
    assert recall[0]["recalled_memories"][0]["content"] == _SECRET_CONTENT
    assert recall[0]["recalled_memories"][0]["tenant_id"] == str(_MEMORY_TENANT)


def test_projection_touches_only_updates_frames_and_never_mutates() -> None:
    frame = {"memory_recall": {"recalled_memories": [{"content": "x"}], "_duration_ms": 3}}
    out = strip_frame_state_keys("updates", frame, EXTERNAL_STRIPPED_STATE_KEYS)
    assert out == {"memory_recall": {"_duration_ms": 3}}
    assert frame["memory_recall"]["recalled_memories"] == [{"content": "x"}]
    # 其它帧原样放行;空集合(控制台)原样放行。
    assert strip_frame_state_keys("plan", frame, EXTERNAL_STRIPPED_STATE_KEYS) is frame
    assert strip_frame_state_keys("updates", frame, frozenset()) is frame
