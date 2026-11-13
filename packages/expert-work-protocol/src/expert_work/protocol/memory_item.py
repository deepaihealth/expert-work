"""Long-term memory item — Stream J.3.

A :class:`MemoryItem` is one cross-session memory for a per-user
persistent agent — a stable ``fact`` or an ``episodic`` summary of a
past interaction — carrying an embedding for semantic retrieval.
Scoped to ``(tenant_id, user_id)``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Capability Uplift Sprint #7 — Mini-ADR U-33.
# Lifecycle of a memory item:
#   transient    — raw write from memory_writeback_node (default for new)
#   consolidated — created by MemoryConsolidator; consolidated_from holds
#                  the transient source UUIDs that this entry summarises
#   archived     — reserved for M2-C cold-storage pipeline; Sprint #7
#                  registers the state + retrieve() filter, M2-C wires
#                  the archive() implementation
MemoryStatus = Literal["transient", "consolidated", "archived"]

# B-300 ② —— 记忆的来源。提炼写入的只由「是哪一次提炼出来的」决定(模型不打标签,程序不看内容):
#   user_stated       —— 第一次提炼的产出:只看白名单里用户输入的文字就读得出来;
#                        另外,用户本人在控制台改写自己这一条也记它
#   assistant_derived —— 第二次提炼的产出:助手做的、得出的、提出的,以及用户只是回答 / 同意的
# 空 = 存量、失败重试重建的、整理汇总里不全是来自助手的、别人在控制台改写过内容的
# (那时置信度也压到 0.99 以内)。
MemoryOrigin = Literal["user_stated", "assistant_derived"]
MEMORY_ORIGINS: frozenset[str] = frozenset(get_args(MemoryOrigin))

#: 召回渲染与去重合并的拦截规则共用的「有效来源」(B-300 设计 §5.1)。
EffectiveOrigin = Literal["user_corrected", "user_stated", "assistant_derived", "unrecorded"]


class MemoryItem(BaseModel):
    """One row of ``memory_item`` — a cross-session memory."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    user_id: UUID
    kind: Literal["fact", "episodic"] = Field(
        description="fact = stable preference / truth; episodic = summary of an interaction"
    )
    # Stream Agent-Templates (M1-5c) — the agent this memory belongs to. Set only
    # for ``episodic`` items (per-agent isolation: a support agent's events do not
    # leak into a sales agent's recall); ``None`` for ``fact`` items, which are
    # agent-agnostic user profile shared across all of a user's agents.
    agent_name: str | None = Field(
        default=None,
        description="owning agent for episodic memory; None = shared fact (agent-agnostic)",
    )
    content: str
    embedding: tuple[float, ...] = Field(
        repr=False, description="semantic embedding vector of ``content``"
    )
    # Stream Memory-Enhance (M-2) — importance/confidence scoring.
    # ``importance`` = how reusable this memory is likely to be in future
    # sessions (rare stable user facts high; one-off chatter low). The
    # writeback path drops items below ``LongTermMemorySpec.write_min_importance``.
    # ``confidence`` = how sure the extraction is (explicit statements high;
    # inferred / hedged low). Both 0-1, default 0.5 (neutral) so legacy rows
    # and callers that omit them are unaffected. A user self-correction
    # (M-4) sets ``confidence`` to 1.0 (user-asserted truth).
    importance: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="future-reuse value of this memory, 0-1 (write-filter input)",
    )
    confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="extraction confidence, 0-1 (1.0 = user-asserted via correction)",
    )
    source_thread_id: str | None = Field(
        default=None, description="the thread this memory was extracted from"
    )
    content_hash: str | None = Field(
        default=None,
        description="Stream K.K7 — SHA-256 hex of ``lower(trim(content))``. "
        "Filled by the store at write time when ``None`` so callers do "
        "not need to import the hash helper; the DB column is NOT NULL.",
    )
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    access_count: int = Field(
        default=0,
        ge=0,
        description="Times this memory has been returned by retrieve (P5a access "
        "reinforcement). Bumped on recall; feeds the frequency boost in ranking "
        "and the consolidator's never-used purge protection.",
    )
    deleted_at: datetime | None = Field(
        default=None,
        description="Stream K.K6 — soft-delete timestamp (forget). "
        "When set the item is hidden from retrieve / list but kept "
        "until the retention sweep hard-deletes it.",
    )
    drift: bool = Field(
        default=False,
        description="Capability Uplift Sprint #2 (Mini-ADR U-4) — "
        "transient flag set by ``MemoryStore.retrieve()`` when "
        "``sha256(lower(trim(content)))`` does not match the stored "
        "``content_hash`` (DB-drift signal). Not persisted; defaults "
        "to ``False`` on all other paths so legacy callers are "
        "unaffected.",
    )
    # Capability Uplift Sprint #7 (Mini-ADR U-33) — lifecycle.
    status: MemoryStatus = Field(
        default="transient",
        description="Lifecycle stage. Defaults to ``transient`` so existing "
        "callers and the writeback path are unaffected. ``MemoryConsolidator`` "
        "creates ``consolidated`` entries; M2-C archive pipeline sets ``archived``.",
    )
    consolidated_into: UUID | None = Field(
        default=None,
        description="When non-NULL, this transient item has been superseded "
        "by a consolidated parent. ``MemoryStore.retrieve()`` default WHERE "
        "skips items where this is set (prevents double-counting raw + summary).",
    )
    consolidated_from: tuple[UUID, ...] = Field(
        default=(),
        description="Only populated on consolidated items. Reverse index of "
        "the transient source UUIDs that this consolidated fact summarises. "
        "Persisted as JSONB array in ``memory_item.consolidated_from``.",
    )
    last_reviewed_at: datetime | None = Field(
        default=None,
        description="Capability Uplift Sprint #7 (Mini-ADR U-37) — set by "
        "MemoryConsolidator's lone-item review sub-pass when the LLM "
        "classifies an aged lone transient as durable. NULL ↔ never "
        "reviewed; non-NULL ↔ skip re-review (prevents borderline-fact "
        "thrash where repeated LLM rolls could eventually flag-and-purge).",
    )
    review_flagged_at: datetime | None = Field(
        default=None,
        description="Stream HX-2 (Mini-ADR HX-B3) — set by the "
        "FeedbackConsumerWorker when a user 👎 lands on this item's "
        "source thread. The MemoryConsolidator's SUB-PASS 2 reviews "
        "flagged items regardless of age and clears the flag via "
        "mark_reviewed; soft-deleted noise clears implicitly.",
    )
    # ---- Stream P5b (溯源 + bi-temporal 时间模型) ----
    source_run_id: str | None = Field(
        default=None,
        description="P5b — the run (agent_run.id, stringified) that triggered this "
        "memory's extraction. Mirrors source_thread_id's str storage. NULL for "
        "legacy rows and DLQ-retried writes.",
    )
    valid_at: datetime | None = Field(
        default=None,
        description="P5b — when the fact became true in the world (backfilled = "
        "created_at). Anchors time-travel retrieval (as_of, P5b-2).",
    )
    expired_at: datetime | None = Field(
        default=None,
        description="P5b — when the fact stopped being true in the world with NO "
        "successor (a retraction). Default retrieve excludes rows where this is "
        "past. Orthogonal to invalid_at.",
    )
    invalid_at: datetime | None = Field(
        default=None,
        description="P5b — when this row was superseded by a newer version (db-layer "
        "supersession). Default retrieve excludes non-NULL. Orthogonal to expired_at.",
    )
    supersedes: UUID | None = Field(
        default=None,
        description="P5b — the older memory id this row replaces (forward link of the "
        "append-only version chain).",
    )
    superseded_by: UUID | None = Field(
        default=None,
        description="P5b — the newer memory id that replaced this row (reverse link; "
        "set together with invalid_at when superseded).",
    )
    expected_valid_days: int | None = Field(
        default=None,
        description="P5b — LLM-estimated validity window in days (per-fact predictive "
        "review input, read in P5b-2; column reserved here).",
    )
    # ---- B-300 PR 2 ----
    origin: MemoryOrigin | None = Field(
        default=None,
        description="B-300 — where the memory came from: ``user_stated`` (extracted "
        "from what the user typed) or ``assistant_derived`` (from the transcript). "
        "NULL = not recorded. An unknown stored value reads as NULL. Internal only.",
    )
    embedding_model: str | None = Field(
        default=None,
        description="B-300 — the embedding model (``provider/model``) that computed "
        "``embedding``; NULL for rows written before the column or by a path that "
        "cannot tell. Internal only — never on an external surface.",
    )

    @field_validator("origin", mode="before")
    @classmethod
    def _unknown_origin_is_unrecorded(cls, value: object) -> object:
        # 应用层校验(库里不加检查约束):不认识的值当作「未记录」,读时不报错。
        return value if isinstance(value, str) and value in MEMORY_ORIGINS else None


def effective_origin(item: MemoryItem) -> EffectiveOrigin:
    """The one source label every reader uses (B-300 design §5.1): the recall
    block, the reconcile blocking rule, consolidation and the console.

    ``confidence >= 1.0`` is written only by the user-correction endpoint, so it
    wins over the column — stored corrections need no backfill."""
    if item.confidence >= 1.0:
        return "user_corrected"
    if item.origin == "user_stated":
        return "user_stated"
    if item.origin == "assistant_derived":
        return "assistant_derived"
    return "unrecorded"
