"""B-52 —— 对外用量的单一装配口。

五个消费点(四个 ``end`` 帧分支 + ``GET .../runs/{id}/usage``)全部经过这里,
所以口径(取哪些 ``usage_kind``、露哪些字段、无记录时返回什么)只有一处可能弄错。
``end_frame_data`` 的 docstring 记着两条 SSE 流的字段集合**已经分叉过一次**。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from control_plane.api._run_usage import (
    EXTERNAL_USAGE_KINDS,
    make_usage_loader,
    usage_buckets_to_wire,
)
from expert_work.persistence.token_usage_store import (
    InMemoryTokenUsageStore,
    ModelTokenTotals,
    TokenUsageRecord,
)
from expert_work.runtime.runs import DisconnectMode, RunInfo, RunStatus
from expert_work.runtime.runs.store import InMemoryRunStore

_TRACE = "a" * 32


def test_wire_shape_drops_llm_calls_and_keeps_four_token_fields() -> None:
    """``llm_calls`` 是我方内部 LLM 调用次数,不对外;四档 token 一个都不能少。"""
    wire = usage_buckets_to_wire(
        [
            ModelTokenTotals(
                provider="glm",
                model="glm-5.3",
                input_tokens=100,
                output_tokens=20,
                cache_creation_tokens=0,
                cache_read_tokens=80,
                llm_calls=7,
            )
        ]
    )
    assert wire == [
        {
            "provider": "glm",
            "model": "glm-5.3",
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 80,
            "cache_creation_tokens": 0,
        }
    ]
    assert "llm_calls" not in wire[0]


def test_external_usage_kinds_is_conversation_only() -> None:
    assert EXTERNAL_USAGE_KINDS == ("conversation",)


async def _seed_run(runs: InMemoryRunStore, *, tenant_id: UUID, trace_id: str | None) -> UUID:
    run_id = uuid4()
    now = datetime.now(UTC)
    await runs.create(
        RunInfo(
            run_id=run_id,
            tenant_id=tenant_id,
            thread_id=uuid4(),
            user_id=None,
            status=RunStatus.SUCCESS,
            on_disconnect=DisconnectMode.CONTINUE,
            is_resume=False,
            error=None,
            created_at=now,
            updated_at=now,
            finished_at=now,
            trace_id=trace_id,
        )
    )
    return run_id


async def _seed_usage(
    usage: InMemoryTokenUsageStore, *, tenant_id: UUID, kind: str, model: str, inp: int
) -> None:
    await usage.insert(
        TokenUsageRecord(
            tenant_id=tenant_id,
            agent_name="bot",
            agent_version="1.0.0",
            model=model,
            provider="glm",
            trace_id=_TRACE,
            input_tokens=inp,
            output_tokens=1,
            usage_kind=kind,
        )
    )


@pytest.mark.asyncio
async def test_loader_counts_only_conversation_kind() -> None:
    """端到端钉住过滤 —— 常量断言不够,那条只看常量不看用法。"""
    tenant = uuid4()
    runs, usage = InMemoryRunStore(), InMemoryTokenUsageStore()
    run_id = await _seed_run(runs, tenant_id=tenant, trace_id=_TRACE)
    await _seed_usage(usage, tenant_id=tenant, kind="conversation", model="glm-5.3", inp=100)
    await _seed_usage(usage, tenant_id=tenant, kind="quality_sampling", model="glm-5.2", inp=7)
    await _seed_usage(usage, tenant_id=tenant, kind="skill_evolution", model="glm-5.1", inp=3)

    buckets = await make_usage_loader(usage=usage, runs=runs, run_id=run_id, tenant_id=tenant)()

    assert buckets is not None
    assert [b["model"] for b in buckets] == ["glm-5.3"]
    assert buckets[0]["input_tokens"] == 100


@pytest.mark.asyncio
async def test_loader_returns_none_when_run_has_no_trace_yet() -> None:
    """排队中的 run 还没绑 trace —— ``None``(字段缺席),不是 ``[]``(零用量)。"""
    tenant = uuid4()
    runs, usage = InMemoryRunStore(), InMemoryTokenUsageStore()
    run_id = await _seed_run(runs, tenant_id=tenant, trace_id=None)

    assert (
        await make_usage_loader(usage=usage, runs=runs, run_id=run_id, tenant_id=tenant)() is None
    )


@pytest.mark.asyncio
async def test_loader_returns_none_when_run_row_vanished() -> None:
    """run 行被 purge —— 同样是 ``None``,且**不能抛**:终局路径上抛会带崩整条流。"""
    loader = make_usage_loader(
        usage=InMemoryTokenUsageStore(),
        runs=InMemoryRunStore(),
        run_id=uuid4(),
        tenant_id=uuid4(),
    )
    assert await loader() is None


@pytest.mark.asyncio
async def test_loader_returns_none_when_trace_has_no_usage_rows() -> None:
    """有 trace 但没有 usage 行(历史 run)—— ``None``,不是空列表。"""
    tenant = uuid4()
    runs, usage = InMemoryRunStore(), InMemoryTokenUsageStore()
    run_id = await _seed_run(runs, tenant_id=tenant, trace_id=_TRACE)

    assert (
        await make_usage_loader(usage=usage, runs=runs, run_id=run_id, tenant_id=tenant)() is None
    )


@pytest.mark.asyncio
async def test_loader_swallows_store_failure_instead_of_raising() -> None:
    """store 抛了,loader 也只能返回 ``None``。

    它跑在 SSE 的终局路径上 —— 把异常放出去会让整条流断在 ``end`` 帧之前,
    调用方连 run 是怎么结束的都拿不到。用量查不到远没有那么严重。
    """

    class _Exploding(InMemoryTokenUsageStore):
        async def totals_by_trace_ids(self, trace_ids, *, usage_kinds=None):  # type: ignore[no-untyped-def]
            raise RuntimeError("db is down")

    tenant = uuid4()
    runs = InMemoryRunStore()
    run_id = await _seed_run(runs, tenant_id=tenant, trace_id=_TRACE)

    assert (
        await make_usage_loader(usage=_Exploding(), runs=runs, run_id=run_id, tenant_id=tenant)()
        is None
    )
