"""班车 2 —— 作废收口在真 Postgres checkpointer 上的形态,以及它与 P-1 取代的衔接。

跑法::

    export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
    uv run --no-sync pytest \
        services/control-plane/tests/test_approval_void_integration.py -m integration -q
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.common.conversation_channel import SUPERSEDED_BY
from expert_work.persistence.database import DatabaseConfig, create_async_engine_from_config
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.runs import RunStatus
from orchestrator import close_voided_turn, sanitize_dangling_tool_calls
from orchestrator.approval_turn import VOIDED_APPROVAL_CONTENT
from tests.test_supersede_kernel_integration import (
    TENANT,
    _async_dsn,
    _cfg,
    _stack,
    _sync_dsn,
    _tool_call,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def engine(postgres_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_voided_turn_closes_on_postgres_and_the_next_turn_supersedes_cleanly(
    postgres_container: PostgresContainer, engine: AsyncEngine
) -> None:
    gated_call = AIMessage(
        content="", tool_calls=[_tool_call("set_plan", {"goal": "G-A"}, "tc-void")]
    )
    async with make_checkpointer("postgres", _sync_dsn(postgres_container)) as cp:
        st = await _stack(
            cp,
            engine,
            [gated_call, AIMessage(content="B-final")],
            approval_required=frozenset({"set_plan"}),
        )
        run_a, run_b, run_c = uuid4(), uuid4(), uuid4()
        await st.run_turn(run_a, "UA", status=RunStatus.PAUSED)
        cfg = _cfg(st.thread_id)
        assert (await st.compiled.aget_state(cfg)).values["pending_approval"] is not None

        assert await close_voided_turn(st.compiled, cfg, run_id=str(run_a)) == 1

        snap = await st.compiled.aget_state(cfg)
        assert snap.next == ()
        assert snap.values.get("pending_approval") is None
        tail = snap.values["messages"][-1]
        assert isinstance(tail, ToolMessage)
        assert (tail.tool_call_id, tail.content) == ("tc-void", VOIDED_APPROVAL_CONTENT)
        # 控制面随后把 A 收成 INTERRUPTED。
        assert await st.runs.set_status(
            run_id=run_a,
            tenant_id=TENANT,
            status=RunStatus.INTERRUPTED,
            updated_at=datetime.now(UTC),
            error="new_turn",
            expected_statuses=(RunStatus.PAUSED,),
        )

        await st.run_turn(run_b, "UB")
        assert sanitize_dangling_tool_calls(st.llm.prompts[-1]) == []

        # 下一轮照常可被取代:区间从补上的那条结果之后开始,A 那一轮不受牵连。
        result = await st.supersede(run_b, run_c)
        assert (result.location.start, result.location.end) == (4, 7)
        assert result.superseded_run_ids == (run_b,)
        marks = [m.additional_kwargs.get(SUPERSEDED_BY) for m in await st.messages()]
        assert marks == [None] * 4 + [str(run_c)] * 3
