"""SE-16 (SE-A39) — 👎 trajectories join the contrastive failure side.

Exercises ``_TrajectoryEvidenceProvider`` over in-memory stores: a
thumbs-down candidate's trajectory (with the user's comment prefixed)
must appear in ``SkillEvidence.failures`` ahead of outcome-based sourcing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from control_plane.skill_evolution_wiring import _TrajectoryEvidenceProvider
from expert_work.persistence import InMemoryCurationCandidateStore
from expert_work.persistence.feedback_store import FeedbackRecord, InMemoryFeedbackStore
from expert_work.protocol import CurationCandidateRecord, CurationSignal, TrajectoryOutcome
from expert_work.runtime.storage import InMemoryObjectStore
from orchestrator.trajectory import TrajectoryReader, TrajectoryRecord, TrajectoryRecorder

_NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)


def _candidate(
    *, tenant: UUID, thread: UUID, key: str, signal: CurationSignal, outcome: TrajectoryOutcome
) -> CurationCandidateRecord:
    return CurationCandidateRecord(
        id=uuid4(),
        tenant_id=tenant,
        agent_name="assistant",
        thread_id=thread,
        trajectory_key=key,
        outcome=outcome,
        signal=signal,
        detected_at=_NOW,
    )


@pytest.mark.asyncio
async def test_downvoted_trajectory_joins_failures_with_comment() -> None:
    tenant = uuid4()
    object_store = InMemoryObjectStore()
    recorder = TrajectoryRecorder(object_store=object_store)
    reader = TrajectoryReader(object_store=object_store)
    candidates = InMemoryCurationCandidateStore()
    feedback = InMemoryFeedbackStore()

    # The 👍 candidate under distillation.
    up_thread = uuid4()
    await recorder.record(
        TrajectoryRecord(
            thread_id=up_thread,
            tenant_id=tenant,
            outcome="success",
            messages=[HumanMessage(content="do the report"), AIMessage(content="done right")],
            finished_at=_NOW,
        )
    )
    (up_key,) = await reader.list_keys(tenant_id=tenant, outcome="success")
    up_candidate = _candidate(
        tenant=tenant, thread=up_thread, key=up_key, signal="positive_feedback", outcome="success"
    )

    # A same-agent 👎 on a *successful* run — the "false success" contrast —
    # with the user's own comment.
    down_thread = uuid4()
    await recorder.record(
        TrajectoryRecord(
            thread_id=down_thread,
            tenant_id=tenant,
            outcome="success",
            messages=[HumanMessage(content="do the report"), AIMessage(content="did it wrong")],
            finished_at=_NOW,
        )
    )
    down_key = next(
        k for k in await reader.list_keys(tenant_id=tenant, outcome="success") if k != up_key
    )
    await candidates.upsert(
        _candidate(
            tenant=tenant,
            thread=down_thread,
            key=down_key,
            signal="negative_feedback",
            outcome="success",
        )
    )
    await feedback.insert(
        FeedbackRecord(
            tenant_id=tenant,
            thread_id=down_thread,
            rating="down",
            comment="数字全是编的",
            actor_id="user@example.com",
        )
    )

    provider = _TrajectoryEvidenceProvider(
        reader, candidate_store=candidates, feedback_store=feedback
    )
    evidence = await provider(up_candidate)

    assert evidence.successes  # the candidate's own trajectory
    assert len(evidence.failures) >= 1
    assert "数字全是编的" in evidence.failures[0]
    assert "did it wrong" in evidence.failures[0]


@pytest.mark.asyncio
async def test_without_stores_failures_fall_back_to_outcomes() -> None:
    tenant, thread = uuid4(), uuid4()
    object_store = InMemoryObjectStore()
    recorder = TrajectoryRecorder(object_store=object_store)
    await recorder.record(
        TrajectoryRecord(
            thread_id=thread,
            tenant_id=tenant,
            outcome="success",
            messages=[HumanMessage(content="hi"), AIMessage(content="ok")],
            finished_at=_NOW,
        )
    )
    reader = TrajectoryReader(object_store=object_store)
    (key,) = await reader.list_keys(tenant_id=tenant, outcome="success")
    provider = _TrajectoryEvidenceProvider(reader)
    evidence = await provider(
        _candidate(
            tenant=tenant, thread=thread, key=key, signal="positive_feedback", outcome="success"
        )
    )
    assert evidence.successes
    assert evidence.failures == ()


@pytest.mark.asyncio
async def test_trajectories_of_deleted_conversations_are_not_used() -> None:
    """B-285 —— listed trajectories (the success / failure top-up and the replay
    prompts) only come from conversations that still exist: a deleted
    conversation or user must not reach the distiller or the replay."""
    from control_plane.skill_evolution_wiring import _DualSourceHeldOutProvider
    from expert_work.persistence import InMemoryEvalDatasetStore, InMemoryThreadMetaStore

    tenant = uuid4()
    object_store = InMemoryObjectStore()
    recorder = TrajectoryRecorder(object_store=object_store)
    reader = TrajectoryReader(object_store=object_store)
    threads = InMemoryThreadMetaStore()
    live, gone = uuid4(), uuid4()
    await threads.create(
        thread_id=live,
        tenant_id=tenant,
        created_by="t",
        user_id=uuid4(),
        agent_name="assistant",
        agent_version="1",
    )
    outcomes: tuple[TrajectoryOutcome, ...] = ("success", "failed")
    for thread, text in ((live, "kept conversation"), (gone, "deleted conversation")):
        for outcome in outcomes:
            await recorder.record(
                TrajectoryRecord(
                    thread_id=thread,
                    tenant_id=tenant,
                    outcome=outcome,
                    messages=[HumanMessage(content=text), AIMessage(content="ok")],
                    finished_at=_NOW,
                )
            )
    candidate = _candidate(
        tenant=tenant, thread=live, key="not-listed", signal="failed_outcome", outcome="failed"
    )

    evidence = await _TrajectoryEvidenceProvider(reader, thread_store=threads)(candidate)
    rendered = " ".join(evidence.successes + evidence.failures)
    assert "kept conversation" in rendered
    assert "deleted conversation" not in rendered

    held_out = await _DualSourceHeldOutProvider(
        InMemoryEvalDatasetStore(), reader, thread_store=threads
    )(candidate)
    prompts = [task.prompt for task in held_out.tasks]
    assert prompts == ["kept conversation"]


def test_the_worker_assembly_hands_the_thread_store_to_both_readers() -> None:
    from unittest.mock import MagicMock

    from control_plane.skill_evolution_wiring import build_evolution_worker

    threads = MagicMock()
    worker = build_evolution_worker(
        aux_model=MagicMock(),
        aux_default_model="m",
        candidate_store=MagicMock(),
        skill_store=MagicMock(),
        eval_store=MagicMock(),
        agent_spec_store=MagicMock(),
        trajectory_reader=TrajectoryReader(object_store=InMemoryObjectStore()),
        agent_builder=MagicMock(),
        interval_s=60,
        thread_store=threads,
    )
    processor: Any = worker._processor
    assert processor.evidence_provider.thread_store is threads
    assert processor.held_out_provider.thread_store is threads


@pytest.mark.asyncio
async def test_replay_tasks_stop_looking_once_it_has_enough() -> None:
    """Medium-3 —— once ``limit`` tasks are in hand the key walk stops: no more
    conversation lookups (one per 100 keys) for keys it will never use."""
    from control_plane.skill_evolution_wiring import _DualSourceHeldOutProvider
    from expert_work.persistence import InMemoryEvalDatasetStore, InMemoryThreadMetaStore

    tenant = uuid4()
    object_store = InMemoryObjectStore()
    recorder = TrajectoryRecorder(object_store=object_store)
    reader = TrajectoryReader(object_store=object_store)
    threads = InMemoryThreadMetaStore()
    for _ in range(250):
        thread = uuid4()
        await threads.create(
            thread_id=thread,
            tenant_id=tenant,
            created_by="t",
            user_id=uuid4(),
            agent_name="assistant",
            agent_version="1",
        )
        await recorder.record(
            TrajectoryRecord(
                thread_id=thread,
                tenant_id=tenant,
                outcome="success",
                messages=[HumanMessage(content="a task"), AIMessage(content="ok")],
                finished_at=_NOW,
            )
        )
    lookups = 0
    real_get_many = threads.get_many

    async def _counting(thread_ids: Any, *, tenant_id: UUID) -> Any:
        nonlocal lookups
        lookups += 1
        return await real_get_many(thread_ids, tenant_id=tenant_id)

    threads.get_many = _counting  # type: ignore[method-assign]
    candidate = _candidate(
        tenant=tenant, thread=uuid4(), key="not-listed", signal="failed_outcome", outcome="failed"
    )

    tasks = await _DualSourceHeldOutProvider(
        InMemoryEvalDatasetStore(), reader, thread_store=threads
    )._trajectory_tasks(candidate, limit=2)

    assert len(tasks) == 2
    assert lookups == 1
