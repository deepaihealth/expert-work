"""👎 当场进池 / 改票标记 —— ``control_plane.feedback_candidates``(P-2 PR2)。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from control_plane.feedback_candidates import CandidateSyncDeps, sync_candidate_for_feedback
from expert_work.persistence import InMemoryCurationCandidateStore, InMemoryThreadMetaStore
from expert_work.protocol import CandidateStatus
from expert_work.runtime.storage import InMemoryObjectStore
from orchestrator.trajectory import TrajectoryReader, TrajectoryRecord, TrajectoryRecorder

_AT = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


class _Fx:
    def __init__(self, *, with_reader: bool = True) -> None:
        self.object_store = InMemoryObjectStore()
        self.threads = InMemoryThreadMetaStore()
        self.candidates = InMemoryCurationCandidateStore()
        self.deps = CandidateSyncDeps(
            threads=self.threads,
            candidates=self.candidates,
            reader=TrajectoryReader(object_store=self.object_store) if with_reader else None,
        )

    async def seed_thread(
        self, tenant: UUID, thread: UUID, *, agent_name: str | None = "reporter"
    ) -> None:
        await self.threads.create(
            thread_id=thread,
            tenant_id=tenant,
            created_by="seed",
            user_id=uuid4(),
            agent_name=agent_name,
            agent_version="1.0.0",
        )

    async def seed_trajectory(
        self, tenant: UUID, thread: UUID, *, outcome: str = "success"
    ) -> None:
        await TrajectoryRecorder(object_store=self.object_store).record(
            TrajectoryRecord(
                thread_id=thread,
                tenant_id=tenant,
                outcome=outcome,  # type: ignore[arg-type]
                messages=[HumanMessage(content="hi"), AIMessage(content="bye")],
                run_id=uuid4(),
                finished_at=_AT,
            )
        )

    async def sync(
        self,
        tenant: UUID,
        thread: UUID,
        run: UUID,
        *,
        rating: str,
        previous: str | None = None,
        comment: str | None = None,
    ) -> str:
        return await sync_candidate_for_feedback(
            deps=self.deps,
            tenant_id=tenant,
            thread_id=thread,
            run_id=run,
            rating=rating,
            previous_rating=previous,
            comment=comment,
        )


@pytest.mark.asyncio
async def test_down_with_trajectory_inserts_candidate_immediately() -> None:
    fx = _Fx()
    tenant, thread, run = uuid4(), uuid4(), uuid4()
    await fx.seed_thread(tenant, thread)
    await fx.seed_trajectory(tenant, thread)
    assert await fx.sync(tenant, thread, run, rating="down", comment="太慢") == "inserted"
    rows = await fx.candidates.list_for_review(tenant_id=tenant)
    assert len(rows) == 1
    assert rows[0].signal == "negative_feedback" and rows[0].feedback_rating == "down"
    assert rows[0].feedback_run_id == run and rows[0].feedback_comment == "太慢"
    assert rows[0].agent_name == "reporter" and rows[0].status is CandidateStatus.PENDING
    # 重复 👎 不产生第二条候选。
    assert (
        await fx.sync(tenant, thread, run, rating="down", previous="down", comment="还是慢")
        == "upgraded"
    )
    rows = await fx.candidates.list_for_review(tenant_id=tenant)
    assert len(rows) == 1 and rows[0].feedback_comment == "还是慢"


@pytest.mark.asyncio
async def test_down_upgrades_existing_failed_outcome_candidate() -> None:
    fx = _Fx()
    tenant, thread, run = uuid4(), uuid4(), uuid4()
    await fx.seed_thread(tenant, thread)
    await fx.seed_trajectory(tenant, thread, outcome="failed")
    keys = await fx.deps.reader.list_keys(tenant_id=tenant)  # type: ignore[union-attr]
    from expert_work.protocol import CurationCandidateRecord

    await fx.candidates.upsert(
        CurationCandidateRecord(
            id=uuid4(),
            tenant_id=tenant,
            agent_name="reporter",
            agent_version="1.0.0",
            thread_id=thread,
            trajectory_key=keys[0],
            outcome="failed",
            signal="failed_outcome",
            detected_at=_AT,
        )
    )
    assert await fx.sync(tenant, thread, run, rating="down") == "upgraded"
    rows = await fx.candidates.list_for_review(tenant_id=tenant)
    assert len(rows) == 1
    assert rows[0].signal == "negative_feedback" and rows[0].feedback_run_id == run


@pytest.mark.asyncio
async def test_down_without_trajectory_or_meta_defers_and_creates_nothing() -> None:
    fx = _Fx()
    tenant, thread, run = uuid4(), uuid4(), uuid4()
    await fx.seed_thread(tenant, thread)
    assert await fx.sync(tenant, thread, run, rating="down") == "deferred"  # 没落盘
    await fx.seed_trajectory(tenant, thread)
    assert await fx.sync(tenant, uuid4(), run, rating="down") == "deferred"  # 没 meta
    no_reader = _Fx(with_reader=False)
    await no_reader.seed_thread(tenant, thread)
    assert await no_reader.sync(tenant, thread, run, rating="down") == "deferred"
    assert await fx.candidates.list_for_review(tenant_id=tenant) == []


@pytest.mark.asyncio
async def test_up_never_creates_and_marks_change_only_after_a_down() -> None:
    fx = _Fx()
    tenant, thread, run = uuid4(), uuid4(), uuid4()
    await fx.seed_thread(tenant, thread)
    await fx.seed_trajectory(tenant, thread)
    assert await fx.sync(tenant, thread, run, rating="up") == "noop"
    assert await fx.candidates.list_for_review(tenant_id=tenant) == []
    assert await fx.sync(tenant, thread, run, rating="down") == "inserted"
    assert await fx.sync(tenant, thread, run, rating="up", previous="down") == "changed"
    rows = await fx.candidates.list_for_review(tenant_id=tenant)
    assert len(rows) == 1 and rows[0].signal == "negative_feedback"  # 保留、不降级
    assert rows[0].feedback_changed_at is not None
    # 👍→👎:升级清掉改票标记。
    assert await fx.sync(tenant, thread, run, rating="down", previous="up") == "upgraded"
    rows = await fx.candidates.list_for_review(tenant_id=tenant)
    assert rows[0].feedback_changed_at is None
