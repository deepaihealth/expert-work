"""Unit tests for the curation worker — Stream J.12 (Mini-ADR J-43)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from control_plane.curation_worker import CurationWorker
from expert_work.persistence import InMemoryCurationCandidateStore, InMemoryThreadMetaStore
from expert_work.persistence.feedback_store import FeedbackRecord, InMemoryFeedbackStore
from expert_work.protocol import CandidateStatus, TrajectoryOutcome
from expert_work.runtime.runs import DisconnectMode, InMemoryRunStore, RunInfo, RunStatus
from expert_work.runtime.storage import InMemoryObjectStore
from orchestrator.trajectory import TrajectoryReader, TrajectoryRecord, TrajectoryRecorder

_BASE = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)


class _Fixture:
    """A worker wired over in-memory backends, with seed helpers."""

    def __init__(self, *, with_run_store: bool = False) -> None:
        self.object_store = InMemoryObjectStore()
        self.candidates = InMemoryCurationCandidateStore()
        self.threads = InMemoryThreadMetaStore()
        self.feedback = InMemoryFeedbackStore()
        self.runs = InMemoryRunStore() if with_run_store else None
        self.worker = CurationWorker(
            trajectory_reader=TrajectoryReader(object_store=self.object_store),
            candidate_store=self.candidates,
            thread_store=self.threads,
            feedback_store=self.feedback,
            run_store=self.runs,
            interval_s=60,
        )

    async def seed_run(
        self,
        *,
        tenant_id: UUID,
        thread_id: UUID,
        status: RunStatus = RunStatus.SUCCESS,
        created_at: datetime = _BASE,
        run_id: UUID | None = None,
    ) -> None:
        assert self.runs is not None
        await self.runs.create(
            RunInfo(
                run_id=run_id or uuid4(),
                tenant_id=tenant_id,
                thread_id=thread_id,
                user_id=None,
                status=status,
                on_disconnect=DisconnectMode.CANCEL,
                is_resume=False,
                error=None,
                created_at=created_at,
                updated_at=created_at,
                finished_at=created_at,
                trace_id=None,
            )
        )

    async def seed_trajectory(
        self,
        *,
        tenant_id: UUID,
        thread_id: UUID,
        outcome: TrajectoryOutcome,
        user_id: UUID | None = None,
        run_id: UUID | None = None,
        finished_at: datetime = _BASE,
    ) -> None:
        await TrajectoryRecorder(object_store=self.object_store).record(
            TrajectoryRecord(
                thread_id=thread_id,
                tenant_id=tenant_id,
                outcome=outcome,
                messages=[HumanMessage(content="hi"), AIMessage(content="bye")],
                user_id=user_id,
                run_id=run_id,
                finished_at=finished_at,
            )
        )

    async def seed_thread(
        self,
        *,
        tenant_id: UUID,
        thread_id: UUID,
        agent_name: str | None = "reporter",
        user_id: UUID | None = None,
    ) -> None:
        await self.threads.create(
            thread_id=thread_id,
            tenant_id=tenant_id,
            created_by="user@example.com",
            user_id=user_id,
            agent_name=agent_name,
            agent_version="1.0.0",
        )

    async def seed_feedback(
        self,
        *,
        tenant_id: UUID,
        thread_id: UUID,
        rating: str,
        run_id: UUID | None = None,
        comment: str | None = None,
    ) -> None:
        await self.feedback.insert(
            FeedbackRecord(
                tenant_id=tenant_id,
                thread_id=thread_id,
                run_id=run_id,
                rating=rating,
                comment=comment,
                actor_id="user@example.com",
            )
        )


@pytest.mark.asyncio
async def test_failed_outcome_becomes_candidate() -> None:
    fx = _Fixture()
    tenant, thread = uuid4(), uuid4()
    await fx.seed_thread(tenant_id=tenant, thread_id=thread)
    await fx.seed_trajectory(tenant_id=tenant, thread_id=thread, outcome="failed")

    detected = await fx.worker.run_once()
    assert detected == 1
    rows = await fx.candidates.list_for_review(tenant_id=tenant)
    assert len(rows) == 1
    assert rows[0].signal == "failed_outcome"
    assert rows[0].feedback_rating is None
    assert rows[0].status is CandidateStatus.PENDING


@pytest.mark.asyncio
async def test_negative_feedback_becomes_candidate() -> None:
    fx = _Fixture()
    tenant, thread = uuid4(), uuid4()
    await fx.seed_thread(tenant_id=tenant, thread_id=thread)
    await fx.seed_trajectory(tenant_id=tenant, thread_id=thread, outcome="success")
    await fx.seed_feedback(tenant_id=tenant, thread_id=thread, rating="down")

    detected = await fx.worker.run_once()
    assert detected == 1
    rows = await fx.candidates.list_for_review(tenant_id=tenant)
    assert rows[0].signal == "negative_feedback"
    assert rows[0].feedback_rating == "down"


@pytest.mark.asyncio
async def test_positive_feedback_becomes_golden_candidate() -> None:
    fx = _Fixture()
    tenant, thread = uuid4(), uuid4()
    await fx.seed_thread(tenant_id=tenant, thread_id=thread)
    await fx.seed_trajectory(tenant_id=tenant, thread_id=thread, outcome="success")
    await fx.seed_feedback(tenant_id=tenant, thread_id=thread, rating="up")

    await fx.worker.run_once()
    rows = await fx.candidates.list_for_review(tenant_id=tenant)
    assert rows[0].signal == "positive_feedback"
    assert rows[0].feedback_rating == "up"


@pytest.mark.asyncio
async def test_plain_success_without_feedback_is_skipped() -> None:
    fx = _Fixture()
    tenant, thread = uuid4(), uuid4()
    await fx.seed_thread(tenant_id=tenant, thread_id=thread)
    await fx.seed_trajectory(tenant_id=tenant, thread_id=thread, outcome="success")

    detected = await fx.worker.run_once()
    assert detected == 0
    assert await fx.candidates.list_for_review(tenant_id=tenant) == []


@pytest.mark.asyncio
async def test_negative_feedback_outranks_failed_outcome() -> None:
    """A 👎 is the most actionable signal even on an already-failed run."""
    fx = _Fixture()
    tenant, thread = uuid4(), uuid4()
    await fx.seed_thread(tenant_id=tenant, thread_id=thread)
    await fx.seed_trajectory(tenant_id=tenant, thread_id=thread, outcome="failed")
    await fx.seed_feedback(tenant_id=tenant, thread_id=thread, rating="down")

    await fx.worker.run_once()
    rows = await fx.candidates.list_for_review(tenant_id=tenant)
    assert rows[0].signal == "negative_feedback"


@pytest.mark.asyncio
async def test_trajectory_without_thread_meta_is_skipped() -> None:
    """No agent identity → cannot scope an agent-level dataset → skip."""
    fx = _Fixture()
    tenant, thread = uuid4(), uuid4()
    await fx.seed_trajectory(tenant_id=tenant, thread_id=thread, outcome="failed")

    detected = await fx.worker.run_once()
    assert detected == 0


@pytest.mark.asyncio
async def test_candidate_carries_agent_scope() -> None:
    fx = _Fixture()
    tenant, thread, user = uuid4(), uuid4(), uuid4()
    await fx.seed_thread(tenant_id=tenant, thread_id=thread, agent_name="auditor", user_id=user)
    await fx.seed_trajectory(tenant_id=tenant, thread_id=thread, outcome="failed", user_id=user)

    await fx.worker.run_once()
    rows = await fx.candidates.list_for_review(tenant_id=tenant)
    assert rows[0].agent_name == "auditor"
    assert rows[0].agent_version == "1.0.0"
    assert rows[0].user_id == user


@pytest.mark.asyncio
async def test_rescan_is_idempotent() -> None:
    fx = _Fixture()
    tenant, thread = uuid4(), uuid4()
    await fx.seed_thread(tenant_id=tenant, thread_id=thread)
    await fx.seed_trajectory(tenant_id=tenant, thread_id=thread, outcome="failed")

    assert await fx.worker.run_once() == 1
    assert await fx.worker.run_once() == 0
    assert len(await fx.candidates.list_for_review(tenant_id=tenant)) == 1


@pytest.mark.asyncio
async def test_start_stop_lifecycle() -> None:
    fx = _Fixture()
    assert fx.worker.is_running is False
    fx.worker.start()
    assert fx.worker.is_running is True
    await fx.worker.stop()
    assert fx.worker.is_running is False


# ---------------------------------------------------------------------------
# SE-16 (SE-A38) — implicit positive: unlabeled success + settled thread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settled_unlabeled_success_becomes_implicit_candidate() -> None:
    fx = _Fixture(with_run_store=True)
    tenant, thread = uuid4(), uuid4()
    await fx.seed_thread(tenant_id=tenant, thread_id=thread)
    await fx.seed_trajectory(tenant_id=tenant, thread_id=thread, outcome="success")
    # Last run finished long ago (>> quiet window) and the thread is clean.
    await fx.seed_run(tenant_id=tenant, thread_id=thread, created_at=_BASE)

    detected = await fx.worker.run_once()
    assert detected == 1
    rows = await fx.candidates.list_for_review(tenant_id=tenant, agent_name="reporter")
    assert [r.signal for r in rows] == ["implicit_success"]
    assert rows[0].status is CandidateStatus.PENDING


@pytest.mark.asyncio
async def test_active_thread_is_not_implicit_yet() -> None:
    fx = _Fixture(with_run_store=True)
    tenant, thread = uuid4(), uuid4()
    await fx.seed_thread(tenant_id=tenant, thread_id=thread)
    await fx.seed_trajectory(tenant_id=tenant, thread_id=thread, outcome="success")
    # A run just landed — conversation still in flight; not a candidate yet
    # (the next sweep re-evaluates once the thread settles).
    await fx.seed_run(tenant_id=tenant, thread_id=thread, created_at=datetime.now(UTC))

    assert await fx.worker.run_once() == 0
    assert await fx.candidates.list_for_review(tenant_id=tenant, agent_name="reporter") == []


@pytest.mark.asyncio
async def test_thread_with_failed_run_is_not_implicit() -> None:
    fx = _Fixture(with_run_store=True)
    tenant, thread = uuid4(), uuid4()
    await fx.seed_thread(tenant_id=tenant, thread_id=thread)
    await fx.seed_trajectory(tenant_id=tenant, thread_id=thread, outcome="success")
    await fx.seed_run(tenant_id=tenant, thread_id=thread, created_at=_BASE)
    await fx.seed_run(tenant_id=tenant, thread_id=thread, status=RunStatus.ERROR, created_at=_BASE)

    assert await fx.worker.run_once() == 0


@pytest.mark.asyncio
async def test_no_run_store_disables_implicit_detection() -> None:
    fx = _Fixture(with_run_store=False)
    tenant, thread = uuid4(), uuid4()
    await fx.seed_thread(tenant_id=tenant, thread_id=thread)
    await fx.seed_trajectory(tenant_id=tenant, thread_id=thread, outcome="success")

    assert await fx.worker.run_once() == 0


@pytest.mark.asyncio
async def test_explicit_up_still_outranks_implicit() -> None:
    fx = _Fixture(with_run_store=True)
    tenant, thread = uuid4(), uuid4()
    await fx.seed_thread(tenant_id=tenant, thread_id=thread)
    await fx.seed_trajectory(tenant_id=tenant, thread_id=thread, outcome="success")
    await fx.seed_run(tenant_id=tenant, thread_id=thread, created_at=_BASE)
    await fx.seed_feedback(tenant_id=tenant, thread_id=thread, rating="up")

    await fx.worker.run_once()
    rows = await fx.candidates.list_for_review(tenant_id=tenant, agent_name="reporter")
    assert [r.signal for r in rows] == ["positive_feedback"]


@pytest.mark.asyncio
async def test_rephrased_answer_is_not_implicit() -> None:
    """Live pilot finding #7 — a follow-up run landing within the rephrase
    window after this trajectory's own run reads as "no, I meant …": the
    user was not satisfied with THAT answer, so it must not enter the
    weak-label pool even though the thread later settled quietly."""
    from datetime import timedelta

    fx = _Fixture(with_run_store=True)
    tenant, thread, r1 = uuid4(), uuid4(), uuid4()
    await fx.seed_thread(tenant_id=tenant, thread_id=thread)
    await fx.seed_trajectory(
        tenant_id=tenant, thread_id=thread, outcome="success", run_id=r1, finished_at=_BASE
    )
    await fx.seed_run(tenant_id=tenant, thread_id=thread, run_id=r1, created_at=_BASE)
    # Rephrase lands 2 minutes after the first answer; thread then settles.
    await fx.seed_run(tenant_id=tenant, thread_id=thread, created_at=_BASE + timedelta(minutes=2))

    assert await fx.worker.run_once() == 0
    assert await fx.candidates.list_for_review(tenant_id=tenant, agent_name="reporter") == []


@pytest.mark.asyncio
async def test_later_followup_does_not_disqualify_implicit() -> None:
    """A follow-up well past the rephrase window is a new topic, not a
    correction — the earlier answer keeps its implicit signal."""
    from datetime import timedelta

    fx = _Fixture(with_run_store=True)
    tenant, thread, r1 = uuid4(), uuid4(), uuid4()
    await fx.seed_thread(tenant_id=tenant, thread_id=thread)
    await fx.seed_trajectory(
        tenant_id=tenant, thread_id=thread, outcome="success", run_id=r1, finished_at=_BASE
    )
    await fx.seed_run(tenant_id=tenant, thread_id=thread, run_id=r1, created_at=_BASE)
    await fx.seed_run(tenant_id=tenant, thread_id=thread, created_at=_BASE + timedelta(minutes=30))

    assert await fx.worker.run_once() == 1
    rows = await fx.candidates.list_for_review(tenant_id=tenant, agent_name="reporter")
    assert [r.signal for r in rows] == ["implicit_success"]


@pytest.mark.asyncio
async def test_negative_candidate_carries_run_and_comment() -> None:
    fx = _Fixture()
    tenant, thread, run = uuid4(), uuid4(), uuid4()
    await fx.seed_thread(tenant_id=tenant, thread_id=thread)
    await fx.seed_trajectory(tenant_id=tenant, thread_id=thread, outcome="success")
    await fx.seed_feedback(
        tenant_id=tenant, thread_id=thread, rating="down", run_id=run, comment="太慢"
    )
    assert await fx.worker.run_once() == 1
    rows = await fx.candidates.list_for_review(tenant_id=tenant)
    assert rows[0].feedback_run_id == run and rows[0].feedback_comment == "太慢"


@pytest.mark.asyncio
async def test_existing_failed_candidate_is_upgraded_when_a_down_arrives_later() -> None:
    fx = _Fixture()
    tenant, thread, run = uuid4(), uuid4(), uuid4()
    await fx.seed_thread(tenant_id=tenant, thread_id=thread)
    await fx.seed_trajectory(tenant_id=tenant, thread_id=thread, outcome="failed")
    assert await fx.worker.run_once() == 1  # failed_outcome
    await fx.seed_feedback(
        tenant_id=tenant, thread_id=thread, rating="down", run_id=run, comment="错了"
    )
    assert await fx.worker.run_once() == 1  # 升级计一次
    rows = await fx.candidates.list_for_review(tenant_id=tenant)
    assert len(rows) == 1
    assert rows[0].signal == "negative_feedback" and rows[0].feedback_run_id == run
    assert await fx.worker.run_once() == 0  # 已是 negative,第三次不再计
