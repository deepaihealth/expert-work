"""P-1 supersede 内核 —— 真 Postgres checkpointer + 真 ReAct 图(spike 的生产形态)。

跑法::

    export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
    uv run --no-sync pytest \
        services/control-plane/tests/test_supersede_kernel_integration.py -m integration -q
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from control_plane.api.runs import build_run_graph_input
from control_plane.supersede import (
    MAX_SUPERSEDED_VERSIONS,
    SupersedeError,
    _bounds_history,
    _bounds_sql,
    _checkpoint_pool,
    supersede_run,
    supersede_thread_lock,
)
from control_plane.transcript import extract_turns, read_messages
from expert_work.common.conversation_channel import SUPERSEDED_BY, TOMBSTONE
from expert_work.persistence.approval import InMemoryApprovalStore
from expert_work.persistence.database import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.thread_message import InMemoryThreadMessageStore
from expert_work.persistence.thread_meta import InMemoryThreadMetaStore
from expert_work.protocol.approval import ApprovalRecord, ApprovalStatus
from expert_work.protocol.plan import Plan, PlanStep
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.runs import DisconnectMode, InMemoryRunStore, RunInfo, RunStatus
from orchestrator import (
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
)

pytestmark = pytest.mark.integration

GOAL_ONE, GOAL_TWO = "PLAN-GOAL-ONE", "PLAN-GOAL-TWO"
TENANT = uuid4()


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "").replace("postgresql+psycopg://", "postgresql://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
async def engine(postgres_container: PostgresContainer) -> Any:
    eng = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    try:
        yield eng
    finally:
        await eng.dispose()


@dataclass
class _ScriptedLLM:
    script: list[AIMessage]
    prompts: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec], **_: Any
    ) -> AIMessage:
        del tools
        self.prompts.append(list(messages))
        if not self.script:
            msg = "scripted LLM exhausted"
            raise RuntimeError(msg)
        return self.script.pop(0)


@dataclass
class _PlanTool:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="set_plan",
            description="plan writer",
            parameters={"type": "object", "properties": {"goal": {"type": "string"}}},
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del ctx
        goal = str(args["goal"])
        return ToolResult(
            content="ok",
            state_updates={"plan": Plan(goal=goal, steps=(PlanStep(id="1", description=goal),))},
        )


def _built_stub() -> Any:
    return SimpleNamespace(
        supports_vision=False,
        spotlight_nonce=None,
        max_steps=8,
        max_no_progress=0,
        system_prompt="You are the kernel-test agent.",
        prompt_jinja=False,
    )


def _tool_call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _turn_script(goal: str | None, final: str) -> list[AIMessage]:
    if goal is None:
        return [AIMessage(content=final)]
    return [
        AIMessage(
            content="",
            tool_calls=[_tool_call("set_plan", {"goal": goal}, f"tc-{goal}-{uuid4().hex[:6]}")],
        ),
        AIMessage(content=final),
    ]


def _cfg(thread_id: UUID) -> RunnableConfig:
    return {"configurable": {"thread_id": str(thread_id), "tenant_id": str(TENANT)}}


@dataclass
class _Stack:
    """一套内核依赖:真 checkpointer 上的图 + 三个内存 store + 真 advisory lock。"""

    compiled: Any
    llm: _ScriptedLLM
    runs: InMemoryRunStore
    approvals: InMemoryApprovalStore
    thread_messages: InMemoryThreadMessageStore
    threads: InMemoryThreadMetaStore
    session_factory: Any
    thread_id: UUID

    async def run_turn(
        self,
        run_id: UUID,
        text: str,
        *,
        status: RunStatus = RunStatus.SUCCESS,
        regenerated_from: UUID | None = None,
    ) -> None:
        graph_input = build_run_graph_input(
            _built_stub(),
            input_text=text,
            image_refs=[],
            untrusted_content=None,
            run_id=run_id,
        )
        cfg: RunnableConfig = {
            "configurable": {**dict(_cfg(self.thread_id)["configurable"]), "run_id": str(run_id)}
        }
        async for _ in self.compiled.astream(graph_input, cfg, stream_mode="updates"):
            pass
        await self.add_run_row(run_id, status=status, regenerated_from=regenerated_from)

    async def add_run_row(
        self, run_id: UUID, *, status: RunStatus, regenerated_from: UUID | None = None
    ) -> None:
        now = datetime.now(UTC)
        await self.runs.create(
            RunInfo(
                run_id=run_id,
                tenant_id=TENANT,
                thread_id=self.thread_id,
                user_id=None,
                status=status,
                on_disconnect=DisconnectMode.CONTINUE,
                is_resume=False,
                error=None,
                created_at=now,
                updated_at=now,
                finished_at=now if status is not RunStatus.RUNNING else None,
                regenerated_from_run_id=regenerated_from,
            )
        )
        await asyncio.sleep(0.001)  # list_by_thread 按 created_at 排,保证严格递增

    async def messages(self) -> list[BaseMessage]:
        snap = await self.compiled.aget_state(_cfg(self.thread_id))
        return list(snap.values["messages"])

    async def supersede(self, target: UUID, new: UUID, *, require_replay: bool = True) -> Any:
        async with supersede_thread_lock(self.session_factory, self.thread_id):
            return await supersede_run(
                graph=self.compiled,
                thread_id=self.thread_id,
                tenant_id=TENANT,
                target_run_id=target,
                new_run_id=new,
                runs=self.runs,
                approvals=self.approvals,
                thread_messages=self.thread_messages,
                threads=self.threads,
                require_replay=require_replay,
            )


async def _stack(
    cp: Any,
    engine: AsyncEngine,
    script: list[AIMessage],
    *,
    approval_required: frozenset[str] = frozenset(),
) -> _Stack:
    llm = _ScriptedLLM(script=script)
    registry = ToolRegistry()
    registry.register(_PlanTool())
    compiled = GraphRunner(checkpointer=cp).compile(
        build_react_graph(
            llm_caller=llm, tool_registry=registry, approval_required_tools=approval_required
        )
    )
    threads = InMemoryThreadMetaStore()
    thread_id = uuid4()
    await threads.create(thread_id=thread_id, tenant_id=TENANT, created_by="p1-kernel-test")
    return _Stack(
        compiled=compiled,
        llm=llm,
        runs=InMemoryRunStore(),
        approvals=InMemoryApprovalStore(),
        thread_messages=InMemoryThreadMessageStore(),
        threads=threads,
        session_factory=create_async_session_factory(engine),
        thread_id=thread_id,
    )


def _marks(msgs: Sequence[BaseMessage]) -> list[str | None]:
    return [m.additional_kwargs.get(SUPERSEDED_BY) for m in msgs]


def _dump(msgs: Sequence[BaseMessage]) -> str:
    return json.dumps([m.model_dump() for m in msgs], ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# 主场景:两轮 → 取代第二轮 → 标记/下标/内容/plan/next/SQL 三表
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supersede_marks_in_place_reverts_plan_and_links_rows(
    postgres_container: PostgresContainer, engine: AsyncEngine
) -> None:
    async with make_checkpointer("postgres", _sync_dsn(postgres_container)) as cp:
        st = await _stack(
            cp, engine, [*_turn_script(GOAL_ONE, "A1-final"), *_turn_script(GOAL_TWO, "A2-final")]
        )
        r1, r2, r3 = uuid4(), uuid4(), uuid4()
        await st.run_turn(r1, "U1")
        await st.run_turn(r2, "U2")
        before = await st.messages()
        assert len(before) == 10
        # 这套集成测必须真的走 SQL 取法,不是内存回退 —— 否则整条 SQL 路径可能
        # 一次都没被执行过就全绿了。
        assert _checkpoint_pool(st.compiled) is not None
        # 镜像先同步一次(模拟 sweep 已跑过),再看 supersede 是否显式更新它
        await st.thread_messages.sync_thread(
            thread_id=st.thread_id,
            tenant_id=TENANT,
            turns=extract_turns(before),
            synced_at=datetime.now(UTC),
        )
        await st.threads.update_message_count(st.thread_id, 4, tenant_id=TENANT)

        result = await st.supersede(r2, r3)

        assert (result.location.start, result.location.end) == (5, 10)
        assert result.superseded_run_ids == (r2,)
        assert result.replay_messages is not None
        assert isinstance(result.replay_messages[0], SystemMessage)
        assert isinstance(result.replay_messages[1], HumanMessage)
        snap = await st.compiled.aget_state(_cfg(st.thread_id))
        after = list(snap.values["messages"])
        assert [m.id for m in after] == [m.id for m in before]  # 条数 / 下标不变
        assert [m.content for m in after] == [m.content for m in before]  # 内容不变
        assert _marks(after) == [None] * 5 + [str(r3)] * 5  # 标记只在 [5,10)
        assert snap.values["plan"].goal == GOAL_ONE  # plan 回退
        assert snap.next == ()  # as_node="agent"
        # SQL 三表
        old_row = await st.runs.get(run_id=r2, tenant_id=TENANT)
        assert old_row is not None and old_row.superseded_by_run_id == r3
        mirror = {
            seq: t.superseded_by
            for (tid, seq), (_x, t) in st.thread_messages._turns.items()
            if tid == st.thread_id
        }
        assert mirror == {1: None, 4: None, 6: r3, 9: r3}
        meta = await st.threads.get(st.thread_id, tenant_id=TENANT)
        # 与 /messages 同口径:被取代轮仍计
        assert meta is not None and meta.message_count == 4
    # 新 saver 再读,标记仍在
    async with make_checkpointer("postgres", _sync_dsn(postgres_container)) as cp2:
        raw = await read_messages(cp2, st.thread_id)
        assert _marks(raw) == [None] * 5 + [str(r3)] * 5


@pytest.mark.asyncio
async def test_plan_reverts_to_none_when_first_turn_had_no_plan(
    postgres_container: PostgresContainer, engine: AsyncEngine
) -> None:
    async with make_checkpointer("postgres", _sync_dsn(postgres_container)) as cp:
        st = await _stack(cp, engine, [*_turn_script(None, "A1"), *_turn_script(GOAL_TWO, "A2")])
        r1, r2, r3 = uuid4(), uuid4(), uuid4()
        await st.run_turn(r1, "U1")
        await st.run_turn(r2, "U2")
        assert (await st.compiled.aget_state(_cfg(st.thread_id))).values["plan"].goal == GOAL_TWO
        loc = (await st.supersede(r2, r3)).location
        assert (loc.start, loc.end) == (3, 8)
        assert loc.plan_before is None
        assert (await st.compiled.aget_state(_cfg(st.thread_id))).values.get("plan") is None


# ---------------------------------------------------------------------------
# 墓碑:第 6 次取代,最老版本正文清空,id / 下标 / 条数不变
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sixth_version_tombstones_the_oldest(
    postgres_container: PostgresContainer, engine: AsyncEngine
) -> None:
    async with make_checkpointer("postgres", _sync_dsn(postgres_container)) as cp:
        st = await _stack(
            cp,
            engine,
            [*_turn_script(None, "A1")]
            + [
                m
                for i in range(MAX_SUPERSEDED_VERSIONS + 2)
                for m in _turn_script(None, f"A2-v{i}")
            ],
        )
        r1, v0 = uuid4(), uuid4()
        await st.run_turn(r1, "U1")
        await st.run_turn(v0, "U2-v0")
        prev = v0
        # 跑到 v6 = 恰好 6 个被取代版本,比上限多一个 —— 最老的那个必须成墓碑。
        for i in range(1, MAX_SUPERSEDED_VERSIONS + 2):
            new = uuid4()
            await st.supersede(prev, new)
            await st.run_turn(new, f"U2-v{i}", regenerated_from=prev)
            prev = new
        msgs = await st.messages()
        ids = [m.id for m in msgs]
        # v0..v5 六个被取代版本 → 超过 5 份的最老一个(v0)成墓碑;v1..v5 仍带正文
        assert msgs[3].content == ""
        assert msgs[3].additional_kwargs[TOMBSTONE] is True  # v0 的 System
        assert msgs[4].content == ""
        assert msgs[4].additional_kwargs[SUPERSEDED_BY]  # v0 的 Human,标记仍在
        assert "U2-v1" in msgs[7].content  # v1 未清理
        assert len(msgs) == 3 + 3 * (MAX_SUPERSEDED_VERSIONS + 2)  # 每轮 3 条
        assert ids == [m.id for m in await st.messages()]  # 下标稳定
        assert "U2-v0" not in _dump(msgs)
        assert "U2-v6" in _dump(msgs)


# ---------------------------------------------------------------------------
# 审批链:PAUSED run + continuation run 是一轮;取代 continuation 必须连 PAUSED 段一起标
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supersede_covers_the_approval_chain(
    postgres_container: PostgresContainer, engine: AsyncEngine
) -> None:
    async with make_checkpointer("postgres", _sync_dsn(postgres_container)) as cp:
        st = await _stack(
            cp,
            engine,
            [*_turn_script(None, "A1"), *_turn_script(GOAL_TWO, "A2-final")],
            approval_required=frozenset({"set_plan"}),
        )
        r1, paused, cont, r3 = uuid4(), uuid4(), uuid4(), uuid4()
        await st.run_turn(r1, "U1")
        await st.run_turn(paused, "U2", status=RunStatus.PAUSED)  # 停在 set_plan 的审批门
        pending = (await st.compiled.aget_state(_cfg(st.thread_id))).values.get("pending_approval")
        assert pending is not None
        now = datetime.now(UTC)
        await st.approvals.create(
            ApprovalRecord(
                id=uuid4(),
                tenant_id=TENANT,
                run_id=paused,
                thread_id=st.thread_id,
                request_id=str(pending.request_id),
                node="tools",
                reason_kind="policy_gate",
                action_summary="set_plan",
                requested_at=now,
                timeout_at=now,
                status=ApprovalStatus.APPROVED,
                continuation_run_id=cont,
            )
        )
        # 续跑:照 runs.py 审批续跑的写法 —— 写裁定、as_node="agent"、graph_input=None 起新 run
        await st.compiled.aupdate_state(
            _cfg(st.thread_id),
            {
                "pending_approval": None,
                "approval_resume": {
                    "decision": "approve",
                    "modified_args": None,
                    "reason": None,
                    "binding_digest": pending.binding_digest,
                },
            },
            as_node="agent",
        )
        cont_cfg: RunnableConfig = {
            "configurable": {**dict(_cfg(st.thread_id)["configurable"]), "run_id": str(cont)}
        }
        async for _ in st.compiled.astream(None, cont_cfg, stream_mode="updates"):
            pass
        await st.add_run_row(cont, status=RunStatus.SUCCESS)
        before = await st.messages()

        result = await st.supersede(cont, r3)

        assert result.superseded_run_ids == (cont, paused)
        assert result.location.start == 3  # U2 这一轮从第一轮之后开始
        after = await st.messages()
        assert _marks(after) == [None] * 3 + [str(r3)] * (len(before) - 3)
        paused_row = await st.runs.get(run_id=paused, tenant_id=TENANT)
        cont_row = await st.runs.get(run_id=cont, tenant_id=TENANT)
        assert paused_row is not None and paused_row.superseded_by_run_id == r3
        assert cont_row is not None and cont_row.superseded_by_run_id == r3
        assert (await st.compiled.aget_state(_cfg(st.thread_id))).values.get("plan") is None


# ---------------------------------------------------------------------------
# 拒绝语义
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejections(postgres_container: PostgresContainer, engine: AsyncEngine) -> None:
    async with make_checkpointer("postgres", _sync_dsn(postgres_container)) as cp:
        st = await _stack(
            cp,
            engine,
            [*_turn_script(None, "A1"), *_turn_script(None, "A2"), *_turn_script(None, "A3")],
        )
        r1, r2 = uuid4(), uuid4()
        await st.run_turn(r1, "U1")
        await st.run_turn(r2, "U2")
        with pytest.raises(SupersedeError) as e:
            await st.supersede(r1, uuid4())
        assert (e.value.code, e.value.status_code) == ("RUN_NOT_LAST", 422)
        with pytest.raises(SupersedeError) as e:
            await st.supersede(uuid4(), uuid4())
        assert e.value.code == "RUN_NOT_FOUND"
        # 有 run 在跑
        running = uuid4()
        await st.add_run_row(running, status=RunStatus.RUNNING)
        with pytest.raises(SupersedeError) as e:
            await st.supersede(running, uuid4())
        assert (e.value.code, e.value.status_code) == ("THREAD_BUSY", 409)
        await st.runs.set_status(
            run_id=running,
            tenant_id=TENANT,
            status=RunStatus.ERROR,
            error="x",
            updated_at=datetime.now(UTC),
        )
        # 图开始前就失败的 run(无检查点):regenerate 拿不到输入 → 422;edit 可以,只链接行
        with pytest.raises(SupersedeError) as e:
            await st.supersede(running, uuid4(), require_replay=True)
        assert (e.value.code, e.value.status_code) == ("RUN_INPUT_UNAVAILABLE", 422)
        r3 = uuid4()
        result = await st.supersede(running, r3, require_replay=False)
        assert (result.location.start, result.location.end) == (6, 6)
        running_row = await st.runs.get(run_id=running, tenant_id=TENANT)
        assert running_row is not None and running_row.superseded_by_run_id == r3
        await st.add_run_row(r3, status=RunStatus.SUCCESS, regenerated_from=running)
        # 已取代(后继存在)
        with pytest.raises(SupersedeError) as e:
            await st.supersede(running, uuid4(), require_replay=False)
        assert e.value.code == "RUN_ALREADY_SUPERSEDED"
        # 目标 PAUSED
        p = uuid4()
        await st.add_run_row(p, status=RunStatus.PAUSED)
        with pytest.raises(SupersedeError) as e:
            await st.supersede(p, uuid4(), require_replay=False)
        assert (e.value.code, e.value.status_code) == ("RUN_AWAITING_APPROVAL", 409)


@pytest.mark.asyncio
async def test_dangling_link_is_treated_as_not_superseded(
    postgres_container: PostgresContainer, engine: AsyncEngine
) -> None:
    """agent_run 指向一个不存在的 run(上次在建新行前崩了)→ 允许重做。"""
    async with make_checkpointer("postgres", _sync_dsn(postgres_container)) as cp:
        st = await _stack(cp, engine, [*_turn_script(None, "A1"), *_turn_script(None, "A2")])
        r1, r2 = uuid4(), uuid4()
        await st.run_turn(r1, "U1")
        await st.run_turn(r2, "U2")
        ghost = uuid4()
        await st.supersede(r2, ghost)  # ghost 行永远没建
        real = uuid4()
        result = await st.supersede(r2, real)  # 不该 409
        assert _marks(await st.messages())[-1] == str(real)
        assert result.superseded_run_ids == (r2,)


# ---------------------------------------------------------------------------
# 两副本并发:同一轮只成一条
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_replicas_concurrent_supersede_single_winner(
    postgres_container: PostgresContainer, engine: AsyncEngine
) -> None:
    async with make_checkpointer("postgres", _sync_dsn(postgres_container)) as cp:
        st = await _stack(cp, engine, [*_turn_script(None, "A1"), *_turn_script(None, "A2")])
        r1, r2 = uuid4(), uuid4()
        await st.run_turn(r1, "U1")
        await st.run_turn(r2, "U2")
        other_factory = create_async_session_factory(engine)  # 第二个「副本」自己的 factory

        async def attempt(factory: Any, new: UUID) -> str:
            try:
                async with supersede_thread_lock(factory, st.thread_id):
                    await supersede_run(
                        graph=st.compiled,
                        thread_id=st.thread_id,
                        tenant_id=TENANT,
                        target_run_id=r2,
                        new_run_id=new,
                        runs=st.runs,
                        approvals=st.approvals,
                        thread_messages=st.thread_messages,
                        threads=st.threads,
                        require_replay=True,
                    )
                    # 锁内建行,与 spawn_run 同序
                    await st.add_run_row(new, status=RunStatus.PENDING, regenerated_from=r2)
                return "ok"
            except SupersedeError as exc:
                return exc.code

        outcomes = await asyncio.gather(
            attempt(st.session_factory, uuid4()), attempt(other_factory, uuid4())
        )
        assert sorted(outcomes) == ["RUN_ALREADY_SUPERSEDED", "ok"]
        assert len({m.additional_kwargs.get(SUPERSEDED_BY) for m in (await st.messages())[3:]}) == 1


# ---------------------------------------------------------------------------
# 两条取法必须同义 —— SQL 路径与 history 回退路径给出逐字段相同的边界
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bounds_sql_and_history_agree_field_by_field(
    postgres_container: PostgresContainer, engine: AsyncEngine
) -> None:
    """`_bounds_sql`(生产)与 `_bounds_history`(内存回退)必须逐字段相等。

    仓库老教训:SQL 与内存实现的谓词 / 定序一旦漂了,单测全绿而生产走另一套。
    这条测试同时握着真池和真图,所以能把两条路摆在**同一批 checkpoint** 上对。

    三种形态各对一次:单 run、审批链(多 run)、链中夹一个压根没有 checkpoint
    的 run —— 第三种正是「按 ``run_ids`` 顺序取」会漂掉的那种。
    """
    async with make_checkpointer("postgres", _sync_dsn(postgres_container)) as cp:
        st = await _stack(
            cp,
            engine,
            [*_turn_script(None, "A1"), *_turn_script(GOAL_TWO, "A2"), *_turn_script(None, "A3")],
        )
        r1, r2, r3 = uuid4(), uuid4(), uuid4()
        await st.run_turn(r1, "U1")
        await st.run_turn(r2, "U2")
        await st.run_turn(r3, "U3")
        pool = _checkpoint_pool(st.compiled)
        assert pool is not None  # 没有池的话下面对的是同一条路,等于没对
        cfg = _cfg(st.thread_id)
        ghost = uuid4()  # 从来没跑过 → 一条 checkpoint 都没有

        # 判据的覆盖面(实测过,别照直觉写):把 `_bounds_history` 改回「按 run_ids
        # 顺序取」之后,**只有「顺序颠倒」这一种会红**。其余四种在老写法下也对得上
        # —— `_approval_chain` 产出的就是新在前,老写法的隐含约定正好被满足;空 run
        # 只是被 `continue` 跳过,不移动首尾。所以顺序无关这条同义性,全靠最后那种
        # 形态兜着。前四种留着钉常规形态不回归,不要以为它们在证顺序无关。
        shapes: list[tuple[str, list[UUID]]] = [
            ("单 run", [r2]),
            ("多 run 链(新在前)", [r3, r2, r1]),
            ("链中夹空 run", [r3, ghost, r1]),
            ("空 run 排在最后", [r2, r1, ghost]),
            ("顺序颠倒", [r1, r2, r3]),  # ← 唯一能照出顺序依赖的那种
        ]
        # 收齐所有形态再断言,失败信息才说得出「哪几种漂了」——
        # fail-fast 只会报第一种,看不出判据的覆盖面。
        disagreed: list[tuple[str, Any, Any]] = []
        for label, run_ids in shapes:
            via_sql = await _bounds_sql(pool, st.thread_id, run_ids)
            via_history = await _bounds_history(st.compiled, cfg, run_ids)
            assert via_sql is not None, label  # 形态本身得有快照,否则对的是两个 None
            if via_sql != via_history:
                disagreed.append((label, via_sql, via_history))
        assert disagreed == [], disagreed

        # 全空链两侧都得是 None(而不是一侧 None、一侧崩)。
        assert await _bounds_sql(pool, st.thread_id, [ghost]) is None
        assert await _bounds_history(st.compiled, cfg, [ghost]) is None
