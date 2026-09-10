"""👎 当场进策展待审池(P-2 §5)—— 端点与控制台共用的纯逻辑。

worker(``curation_worker.py``)每 300s 扫一遍 trajectory 才建候选;用户点 👎 是最有
价值的信号,不该等。这里在写反馈的同一请求里:trajectory 已落盘 → 直接建 / 升级候选;
还没落盘(run 刚结束的窗口)→ 什么都不做,worker 兜底。👍 永不建候选、永不降级;
👎→👍 只在候选上打改票标记。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from expert_work.persistence import CurationCandidateStore, ThreadMetaStore
from expert_work.protocol import CurationCandidateRecord
from orchestrator.trajectory import TrajectoryReader

CandidateSyncResult = Literal["inserted", "upgraded", "changed", "deferred", "noop"]


@dataclass(frozen=True)
class CandidateSyncDeps:
    threads: ThreadMetaStore
    candidates: CurationCandidateStore
    #: ``None`` = 没有 ObjectStore(注入 runtime 的测试装配)→ 一律 ``deferred``。
    reader: TrajectoryReader | None


async def sync_candidate_for_feedback(
    *,
    deps: CandidateSyncDeps,
    tenant_id: UUID,
    thread_id: UUID,
    run_id: UUID,
    rating: str,
    previous_rating: str | None,
    comment: str | None,
) -> CandidateSyncResult:
    if rating == "up":
        if previous_rating != "down":
            return "noop"
        changed = await deps.candidates.mark_feedback_changed(
            tenant_id=tenant_id, feedback_run_id=run_id, at=datetime.now(UTC)
        )
        return "changed" if changed else "noop"
    if rating != "down" or deps.reader is None:
        return "deferred" if rating == "down" else "noop"
    meta = await deps.threads.get(thread_id, tenant_id=tenant_id)
    if meta is None or meta.agent_name is None:
        return "deferred"
    stored = await deps.reader.find_by_thread(tenant_id=tenant_id, thread_id=thread_id)
    if stored is None:
        return "deferred"
    existing = await deps.candidates.get_by_trajectory_key(
        tenant_id=tenant_id, trajectory_key=stored.key
    )
    if existing is None:
        inserted = await deps.candidates.upsert(
            CurationCandidateRecord(
                id=uuid4(),
                tenant_id=tenant_id,
                agent_name=meta.agent_name,
                agent_version=meta.agent_version,
                thread_id=thread_id,
                user_id=stored.user_id or meta.user_id,
                trajectory_key=stored.key,
                outcome=stored.outcome,
                signal="negative_feedback",
                feedback_rating="down",
                feedback_run_id=run_id,
                feedback_comment=comment,
                detected_at=datetime.now(UTC),
            )
        )
        if inserted:
            return "inserted"
    # 已有候选(或刚被 worker / 另一副本抢先插入)→ 升级为 negative_feedback 并补两列。
    await deps.candidates.upgrade_to_negative(
        tenant_id=tenant_id,
        trajectory_key=stored.key,
        feedback_run_id=run_id,
        feedback_comment=comment,
    )
    return "upgraded"


__all__ = ["CandidateSyncDeps", "CandidateSyncResult", "sync_candidate_for_feedback"]
