"""J.12 学习 / 反馈闭环 — DTOs.

Mini-ADR J-19 / J-27 / J-43 (STREAM-J-DESIGN § 17). J.12 is the
*curation* layer: a background worker turns L7 trajectories + G.6
feedback into :class:`CurationCandidateRow` rows; a human promotes
reviewed candidates into :class:`EvalDatasetRow` rows — the curated
dataset J.13 consumes.

These DTOs are the wire shape between the curation worker, the curation
stores, the curation / eval-dataset CRUD API, and the export CLI.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "CandidateStatus",
    "CurationCandidateRecord",
    "CurationSignal",
    "EvalDatasetRecord",
    "EvalDatasetSource",
    "FeedbackRating",
    "TrajectoryOutcome",
]

#: How an :class:`EvalDatasetRecord` was produced. ``golden`` — a
#: hand-authored case. ``trajectory`` — curated from a 👍 run (the run's
#: own output is the reference). ``regression`` — curated from a 👎 /
#: failed run with a human-written corrected ``expected``.
EvalDatasetSource = Literal["golden", "trajectory", "regression"]

#: Why the curation worker flagged a trajectory as a candidate.
#: ``negative_feedback`` — a 👎 on the thread. ``failed_outcome`` — the
#: run ended ``failed`` / ``max_steps``. ``positive_feedback`` — a 👍
#: (golden material).
CurationSignal = Literal[
    "negative_feedback",
    "failed_outcome",
    "positive_feedback",
    # SE-16 (SE-A38) — implicit positive: an unlabeled success whose thread
    # settled quietly (no 👎, no failed/pending runs, no follow-up activity).
    # Weak label — its distillates never auto-promote (SE-A44).
    "implicit_success",
]

#: A run's terminal outcome — mirrors the L7 trajectory ObjectStore
#: ``outcome`` key segment (``orchestrator.trajectory``).
TrajectoryOutcome = Literal["success", "failed", "max_steps", "cancelled"]

#: A G.6 user feedback rating — 👍 / 👎.
FeedbackRating = Literal["up", "down"]

#: PR4 — 一条反馈是谁打的:``console`` = 员工(控制台)、``external`` = 终端
#: 用户(对接方应用)。与 ``feedback.source`` 同词表。
FeedbackSource = Literal["console", "external"]


class CandidateStatus(StrEnum):
    """Review lifecycle of a :class:`CurationCandidateRecord`.

    ``PENDING`` — surfaced by the worker, awaiting human review.
    ``PROMOTED`` — accepted into an ``eval_dataset`` row
    (``eval_dataset_id`` backfilled). ``DISMISSED`` — rejected by the
    reviewer.
    """

    PENDING = "pending"
    PROMOTED = "promoted"
    DISMISSED = "dismissed"


class CurationCandidateRecord(BaseModel):
    """One row of ``curation_candidate`` — a trajectory worth reviewing.

    Scoped to ``(tenant_id, agent_name)`` — feedback / trajectories are
    collected per-instance but the curated dataset is agent-scoped
    (Mini-ADR J-43). ``agent_name`` / ``agent_version`` / ``user_id``
    are resolved from ``thread_meta`` by the worker; ``user_id`` /
    ``trajectory_key`` are provenance, not scope keys.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    agent_name: str = Field(min_length=1)
    agent_version: str | None = None
    thread_id: UUID
    user_id: UUID | None = None
    trajectory_key: str = Field(min_length=1)
    outcome: TrajectoryOutcome
    signal: CurationSignal
    feedback_rating: FeedbackRating | None = None
    status: CandidateStatus = CandidateStatus.PENDING
    eval_dataset_id: UUID | None = None
    detected_at: datetime
    reviewed_at: datetime | None = None
    #: 4.4 (#5) — SE-6 evolution marker, orthogonal to ``status`` (the J.12
    #: human-review verdict). Set once the evolution worker has distilled +
    #: replayed this candidate, so it is not re-processed every interval.
    evolved_at: datetime | None = None
    #: SE-16 (SE-A40) — transient-failure retries. A distillation attempt
    #: that died on a retryable fault (aux LLM timeout / rate limit /
    #: connection) bumps this instead of burning the candidate via
    #: ``evolved_at``; the worker gives up (marks evolved) at 3.
    retry_count: int = 0
    #: P-2 — 被踩的那一轮 / 用户原话:审阅员打开候选直接看到,不用翻整条 trajectory。
    feedback_run_id: UUID | None = None
    feedback_comment: str | None = None
    #: P-2 — 👎→👍 改票时间;None = 没改过(升级为 negative_feedback 时清空)。
    feedback_changed_at: datetime | None = None
    #: PR4 — 这一踩是谁打的:``console`` = 员工(控制台),``external`` = 终端
    #: 用户(对接方应用)。None = worker 兜底建的候选,归因不到具体某一条
    #: feedback。**展示面要区分来源,判断面不区分**(spec §6:四个下游消费者
    #: 一律不按来源过滤)。
    feedback_source: FeedbackSource | None = None

    @model_validator(mode="after")
    def _check_review_state(self) -> CurationCandidateRecord:
        """A reviewed candidate carries a ``reviewed_at``; a promoted one a dataset id."""
        if self.status is not CandidateStatus.PENDING and self.reviewed_at is None:
            msg = "a promoted / dismissed candidate must carry reviewed_at"
            raise ValueError(msg)
        if self.status is CandidateStatus.PROMOTED and self.eval_dataset_id is None:
            msg = "a promoted candidate must carry eval_dataset_id"
            raise ValueError(msg)
        return self


class EvalDatasetRecord(BaseModel):
    """One row of ``eval_dataset`` — a curated eval case (J.13 共用).

    A "dataset" is the set of rows sharing ``(tenant_id, agent_name,
    name)`` — ``name`` is not unique. ``input`` / ``expected`` are
    free-shaped JSON the J.13 eval module interprets;
    ``source_trajectory_key`` / ``source_user_id`` are provenance for
    trajectory-derived cases.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    agent_name: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=128)
    input: dict[str, object] = Field(default_factory=dict)
    expected: dict[str, object] | None = None
    source: EvalDatasetSource
    source_trajectory_key: str | None = None
    source_user_id: UUID | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _check_expected(self) -> EvalDatasetRecord:
        """``golden`` / ``regression`` cases need a human-authored ``expected``.

        A ``trajectory`` case may leave ``expected`` unset — the curated
        trajectory itself is the positive reference (Mini-ADR J-43).
        """
        if self.source in ("golden", "regression") and self.expected is None:
            msg = f"{self.source!r} eval-dataset case requires a non-null 'expected'"
            raise ValueError(msg)
        return self
