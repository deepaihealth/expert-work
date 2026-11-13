"""Abstract ``MemoryStore`` repository — Stream J.3 long-term memory.

Implementations:
- :class:`expert_work.persistence.memory.memory.InMemoryMemoryStore`
- :class:`expert_work.persistence.memory.sql.SqlMemoryStore`

The store deals in *vectors* — embedding text into a vector is the
caller's job (Stream J.3 PR2 wires the embedder). This keeps the
persistence layer free of any embedding-model dependency.
"""

from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from expert_work.common.threat_patterns import ThreatFinding, scan_for_threats
from expert_work.protocol import MemoryItem
from expert_work.protocol.memory_item import MemoryOrigin, effective_origin

#: B-300 —— 不是用户更正的行(汇总行、别人改写过的行)置信度的上限:1.0 只留给用户更正
#: (更正接口独占的哨兵值)。
NON_CORRECTION_MAX_CONFIDENCE = 0.99
#: B-300 ② —— 来自助手的记忆置信度上限(与提炼第二次的封顶同一个数)。
ASSISTANT_DERIVED_MAX_CONFIDENCE = 0.6
#: 有效来源里算「用户一方」的两种(拦截规则、混合簇判断共用)。
USER_SIDE_ORIGINS = frozenset({"user_stated", "user_corrected"})


def is_mixed_origin(members: Sequence[MemoryItem]) -> bool:
    """B-300 §6.2 —— 一组记忆里既有「来自助手」,又有用户消息里说的 / 用户更正的。

    来源未记录的不算哪一边。整理时这样的簇不汇总。"""
    origins = {effective_origin(m) for m in members}
    return "assistant_derived" in origins and bool(origins & USER_SIDE_ORIGINS)


@dataclass(frozen=True)
class SummaryFields:
    """B-300 D6 前半 + §6.2 —— 一条汇总行从成员继承的出处、分数与来源。"""

    source_thread_id: str | None
    source_run_id: str | None
    importance: float
    confidence: float
    origin: MemoryOrigin | None


def summary_fields(members: Sequence[MemoryItem]) -> SummaryFields:
    """汇总行带的出处和分数(两个存储实现在写汇总的事务里、按重读到的成员算):

    * 来源会话 / 来源 run 取成员里最新(``created_at`` 最大)的那一条的;
    * 重要度取成员最大值;置信度取成员最小值,上限 0.99(1.0 只留给用户更正);
    * 成员全是「来自助手」→ 来源记「来自助手」、置信度再压到 0.6 以内;否则来源为空
      (汇总句是模型新写的文字,不给「用户消息里说的」)。
    """

    def _created(item: MemoryItem) -> float:
        return item.created_at.timestamp() if item.created_at is not None else float("-inf")

    newest = max(members, key=_created)
    all_assistant = all(effective_origin(m) == "assistant_derived" for m in members)
    confidence = min(min(m.confidence for m in members), NON_CORRECTION_MAX_CONFIDENCE)
    if all_assistant:
        confidence = min(confidence, ASSISTANT_DERIVED_MAX_CONFIDENCE)
    return SummaryFields(
        source_thread_id=newest.source_thread_id,
        source_run_id=newest.source_run_id,
        importance=max(m.importance for m in members),
        confidence=confidence,
        origin="assistant_derived" if all_assistant else None,
    )


#: 嵌套汇总向下查成员的层数上限;查到上限还有没查的成员,按「受保护」处理(保守方向)。
MAX_SUMMARY_DEPTH = 8

#: 按 id 取一批记忆(不看是否仍有效、是否已删):受保护判断向下查汇总成员用。
MemberLoader = Callable[[Sequence[UUID]], Awaitable[Sequence[MemoryItem]]]


async def is_user_side_protected(item: MemoryItem, load: MemberLoader) -> bool:
    """B-300 §6.2 拦截规则保护的对象(审查 MEDIUM-3,用户 11-13 定「补上保护」):

    * 有效来源是用户消息里说的 / 用户更正的那一条;
    * 一条汇总行(来源为空),它的成员 —— 一层层往下,成员本身是汇总的接着查它的成员 ——
      里有任何一条是用户一方的。成员早已被汇总、取代或过期,照样算(汇总句里还有它的内容)。

    ``visited`` 防环;``MAX_SUMMARY_DEPTH`` 层之后还有没查的成员 → 受保护。"""
    if effective_origin(item) in USER_SIDE_ORIGINS:
        return True
    visited = {item.id}
    frontier = [sid for sid in item.consolidated_from if sid not in visited]
    for _depth in range(MAX_SUMMARY_DEPTH):
        if not frontier:
            return False
        visited.update(frontier)
        members = await load(frontier)
        if any(effective_origin(m) in USER_SIDE_ORIGINS for m in members):
            return True
        frontier = list(
            dict.fromkeys(sid for m in members for sid in m.consolidated_from if sid not in visited)
        )
    return bool(frontier)


class UserSideTargetError(RuntimeError):
    """B-300 审查 MEDIUM-1 —— 带保护的 ``supersede`` / ``expire``(``refuse_user_side``)
    在锁里重读目标,发现它(或它汇总的成员)是用户一方的:什么都没改。

    去重合并读邻居与真正改库之间隔着一次模型调用,用户可能恰好在这几秒里「更正」了这一条;
    调用方按拦截规则处理(被拦的更新带更正 → 按新增,否则丢弃;被拦的删除 → 不动)。"""


class UserSideHigherConfidenceError(UserSideTargetError):
    """B-300 用户 11-14 —— ``refuse_user_side_above``:锁里重读的目标是用户一方的、且它此刻的
    置信度高于调用方给的值(候选的置信度):什么都没改。是 :class:`UserSideTargetError` 的子类,
    同一条处理路径接得住;调用方据类型分开计数。"""


async def refuse_if_user_side(
    row: MemoryItem, load: MemberLoader, *, refuse: bool, above: float | None
) -> None:
    """两个存储实现在行锁里共用的重判(行已由调用方锁住 / 重读,成员只读):

    * ``refuse``(来自助手的候选):目标是用户一方的 → :class:`UserSideTargetError`;
    * ``above``(用户消息里说的候选的置信度):目标是用户一方的、且置信度高于它 →
      :class:`UserSideHigherConfidenceError`。汇总行按它自己这一行的置信度比。"""
    stricter = above is not None and row.confidence > above
    if not (refuse or stricter) or not await is_user_side_protected(row, load):
        return
    if refuse:
        msg = f"memory item {row.id} is on the user's side; not changing it"
        raise UserSideTargetError(msg)
    msg = f"memory item {row.id} is on the user's side and more certain; not changing it"
    raise UserSideHigherConfidenceError(msg)


@dataclass(frozen=True)
class BlockedMemory:
    """B-300 D2 — one memory text the strict write scan refused.

    Carries what an audit row may hold — the item id, the text length and the
    matched rule categories — and the findings themselves; never the text.
    """

    item_id: UUID
    content_length: int
    findings: tuple[ThreatFinding, ...]

    @property
    def categories(self) -> tuple[str, ...]:
        return tuple(sorted({str(f.category) for f in self.findings}))


@dataclass(frozen=True)
class MemoryWriteResult:
    """B-300 D2 — what :meth:`MemoryStore.write` did not store, item by item.

    B-300 D12 — ``written`` is how many rows actually landed: an item whose
    ``(tenant, user, content_hash)`` already has a live row is skipped (the
    dedup no-op), and a blocked item is not written either."""

    blocked: tuple[BlockedMemory, ...] = ()
    written: int = 0


def scan_memory_content(item_id: UUID, content: str) -> BlockedMemory | None:
    """The one strict scan every automatic memory write goes through (B-300 D2):
    ``write`` / ``supersede`` / ``write_consolidated``. ``None`` = clean."""
    findings = scan_for_threats(content, scope="strict")
    if not findings:
        return None
    return BlockedMemory(item_id=item_id, content_length=len(content), findings=tuple(findings))


def split_blocked(items: Sequence[MemoryItem]) -> tuple[list[MemoryItem], list[BlockedMemory]]:
    """Scan a batch: the items that pass, and one :class:`BlockedMemory` per refused item."""
    clean: list[MemoryItem] = []
    blocked: list[BlockedMemory] = []
    for item in items:
        hit = scan_memory_content(item.id, item.content)
        if hit is None:
            clean.append(item)
        else:
            blocked.append(hit)
    return clean, blocked


class MemoryInjectionBlockedError(Exception):
    """Capability Uplift Sprint #2 (Mini-ADR U-3) — write rejected.

    B-300 D2: raised by the single-item writes — :meth:`MemoryStore.supersede`
    and :meth:`MemoryStore.write_consolidated` — when the new text matches the
    ``strict`` threat-pattern set, before anything is changed (the old row stays
    current, no summary is written). :meth:`MemoryStore.write` no longer raises:
    it stores the clean items and returns the blocked ones.

    Carries the blocked items so the caller can emit one
    ``MEMORY_INJECTION_BLOCKED`` audit row per item with its own context.
    """

    def __init__(self, blocked: Sequence[BlockedMemory]) -> None:
        super().__init__(f"{len(blocked)} memory item(s) blocked by strict threat scan")
        self.blocked = tuple(blocked)


class SupersedeTargetChangedError(RuntimeError):
    """B-300 D1 (the other direction) — the memory an UPDATE meant to replace
    was folded into a summary, expired or archived after the write-back picked
    it. Raised by :meth:`MemoryStore.supersede` under the target's row lock,
    before anything changes; the caller stores the new fact as a plain write
    instead, so it is not lost."""


class ConsolidationSourcesChangedError(RuntimeError):
    """B-300 D1 — a summary's members changed after the cluster was picked.

    Raised by :meth:`MemoryStore.write_consolidated` when, re-read under a row
    lock in the write's own transaction, a member is no longer current
    (deleted, superseded, expired, archived) or was already folded into another
    summary. Nothing is written; the next sweep re-reads the members.
    """


class MemoryStore(abc.ABC):
    """Cross-session memory repository, scoped to ``(tenant_id, user_id)``."""

    @abc.abstractmethod
    async def write(self, items: Sequence[MemoryItem]) -> MemoryWriteResult:
        """Persist long-term memories. Each item carries its own ``id``
        and ``embedding``.

        B-300 D2 — every item goes through :func:`scan_memory_content`; a
        blocked item is dropped on its own (the rest of the batch is written)
        and returned in :attr:`MemoryWriteResult.blocked` for the caller to
        audit. Nothing is raised for a blocked item."""

    @abc.abstractmethod
    async def retrieve(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        query_embedding: Sequence[float],
        query_text: str | None = None,
        kind: Literal["fact", "episodic"] | None = None,
        agent_name: str | None = None,
        as_of: datetime | None = None,
        limit: int = 5,
    ) -> list[MemoryItem]:
        """Return the user's ``limit`` memories nearest ``query_embedding``,
        closest first. Soft-deleted rows are excluded (Stream K.K6).

        Stream Agent-Templates (M1-5c): when ``agent_name`` is given, episodic
        memories are scoped to that agent — the result includes shared ``fact``
        rows (``agent_name IS NULL``) plus ``episodic`` rows owned by this agent,
        so one agent's events never leak into another's recall. ``None`` (the
        admin / legacy path) applies no agent scoping.

        Capability Uplift Sprint #6 (Mini-ADR U-5): when ``query_text``
        is ``None`` the retrieval is pure-vector (cosine distance against
        the pgvector column) — the pre-Sprint-#6 behavior, kept for
        backward compatibility. When ``query_text`` is a non-empty string
        the retrieval is **hybrid**: vector recall is fused with Postgres
        full-text recall via Reciprocal Rank Fusion (``k=60``). Empty /
        whitespace-only ``query_text`` degrades to the vector path.

        Stream P5b — ``as_of`` (when given) time-travels retrieval: instead of
        "currently valid" it returns rows that were valid AT that instant
        (``valid_at <= as_of`` and neither superseded nor expired as of then).
        ``None`` (default) is the current-time behaviour. ``deleted_at`` and
        the archived/consolidated filters still apply — as_of only time-travels
        the bi-temporal (valid/invalid/expired) dimension."""

    @abc.abstractmethod
    async def list_for_user(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        kind: Literal["fact", "episodic"] | None = None,
        as_of: datetime | None = None,
        limit: int = 50,
    ) -> list[MemoryItem]:
        """Stream K.K6 — list a user's live memories, newest first.

        Soft-deleted rows are filtered out so the UI / API caller never
        sees a forgotten memory. ``kind`` optionally narrows. The
        ``embedding`` field is still populated (callers may project it
        away at the API boundary).

        Stream P5b-2c — ``as_of`` (when given) time-travels the bi-temporal
        (valid/invalid/expired) dimension the same way :meth:`retrieve` does:
        instead of "currently live" it returns rows that were valid AT that
        instant. ``None`` (default) applies NO bi-temporal filter — today's
        behavior (only ``deleted_at`` + optional ``kind``) is unchanged, so
        e.g. a superseded or not-yet-valid row still surfaces for direct
        inspection (mirrors how ``supersede()``'s closed old row is reached
        via this method)."""

    @abc.abstractmethod
    async def list_all_tenants(
        self,
        *,
        kind: Literal["fact", "episodic"] | None = None,
        limit: int = 50,
    ) -> list[MemoryItem]:
        """Cross-tenant memory list — Stream N (Mini-ADR N-4).

        Caller MUST be inside ``bypass_rls_session()``. No ``user_id``
        filter — the platform admin view aggregates every user's
        memories across every tenant. Soft-deleted rows are excluded,
        newest first.
        """

    @abc.abstractmethod
    async def get_live(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
    ) -> MemoryItem | None:
        """B-300 D8 — one live memory by id, scoped like :meth:`update_content`.

        Returns ``None`` for unknown id / wrong tenant / wrong user / already
        soft-deleted — exactly the rows :meth:`update_content` would refuse, so
        the console's write endpoints can 404 before they scan or re-embed."""

    @abc.abstractmethod
    async def update_content(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
        content: str,
        embedding: Sequence[float],
        kind: Literal["fact", "episodic"] | None = None,
        confidence: float | None = None,
        embedding_model: str | None = None,
        clear_origin: bool = False,
        origin: MemoryOrigin | None = None,
        max_confidence: float | None = None,
    ) -> MemoryItem | None:
        """Stream K.K6 — rewrite a live memory's content / kind.

        B-300 PR 2 — ``embedding_model`` (the model that computed ``embedding``;
        ``None`` = not known) always replaces the row's value, since the vector
        is replaced. The source column: ``clear_origin`` sets it to NULL,
        otherwise a given ``origin`` replaces it, otherwise it is kept (a user
        correction keeps it — its effective source is the 1.0 confidence).
        ``max_confidence`` caps the row's confidence as stored (review
        MEDIUM-2: someone else's edit takes the row out of the 1.0 sentinel).

        The caller must re-embed before calling — the store does not
        own an embedder. Soft-deleted rows are not updatable; returns
        ``None`` for unknown id / wrong tenant / wrong user / already
        soft-deleted.

        Stream Memory-Enhance (M-4) — ``confidence`` (when given) overwrites the
        row's confidence; the user-correction endpoint sets it to 1.0.
        CONTRACT (RT-2 PR-2): 1.0 is that endpoint's EXCLUSIVE sentinel — the
        injection budget's correction guarantee keys on ``confidence >= 1.0``
        and the extraction write path caps at 0.99 (orchestrator
        ``memory._MAX_EXTRACTED_CONFIDENCE``); no other caller may write 1.0."""

    @abc.abstractmethod
    async def soft_delete(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
    ) -> bool:
        """Stream K.K6 — stamp ``deleted_at`` (the forget action).

        Idempotent: returns ``True`` even when already deleted. Returns
        ``False`` for unknown id / wrong tenant / wrong user."""

    @abc.abstractmethod
    async def bump_access(self, *, tenant_id: UUID, user_id: UUID, ids: Sequence[UUID]) -> None:
        """P5a — mark memories as retrieved: stamp ``last_used_at=now`` and
        increment ``access_count`` for each id. Best-effort at the call site
        (recall must not fail on a bump error). Empty ``ids`` is a no-op.
        Tenant- AND user-scoped as a defensive predicate."""

    @abc.abstractmethod
    async def supersede(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        old_id: UUID,
        new_item: MemoryItem,
        refuse_user_side: bool = False,
        refuse_user_side_above: float | None = None,
    ) -> MemoryItem | None:
        """P5b — append-only UPDATE. Atomically close ``old_id`` (set
        ``invalid_at=now``, ``superseded_by=new_item.id``) and insert
        ``new_item`` with ``supersedes=old_id`` + ``valid_at=now``. The old
        row is KEPT (history/audit) but excluded from default retrieve.

        Returns the newly written item, or ``None`` when ``old_id`` is
        unknown / wrong tenant-user / already superseded (``invalid_at`` set) —
        in which case NO new row is written (the caller falls back to a plain
        write). Tenant- AND user-scoped.

        B-300 D2 — ``new_item`` goes through the same strict scan as
        :meth:`write`; when blocked, :class:`MemoryInjectionBlockedError` is
        raised before anything changes: the old row stays current.

        B-300 D1 (the other direction) — the target is re-read under a row lock;
        when it was folded into a summary, expired or archived since the caller
        picked it, :class:`SupersedeTargetChangedError` is raised and nothing
        changes (``None`` keeps meaning unknown / deleted / already superseded).

        B-300 review MEDIUM-1 — ``refuse_user_side`` (the write-back passes it for
        an assistant-derived candidate): under the same row lock, a target that
        is :func:`is_user_side_protected` raises :class:`UserSideTargetError` and
        nothing changes.

        B-300 user 11-14 — ``refuse_user_side_above`` (the write-back passes the
        candidate's confidence for a user-stated candidate): under the same row
        lock, a user-side target whose confidence is above it raises
        :class:`UserSideHigherConfidenceError` and nothing changes."""

    @abc.abstractmethod
    async def expire(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
        refuse_user_side: bool = False,
        refuse_user_side_above: float | None = None,
    ) -> bool:
        """P5b — retraction: stamp ``expired_at=now`` on a fact that stopped
        being true in the world with NO successor (orthogonal to ``supersede``'s
        ``invalid_at``). The row is KEPT (history/audit) but excluded from
        default retrieve. Returns ``False`` for unknown id / wrong tenant-user.
        Tenant- AND user-scoped.

        B-300 review MEDIUM-1 — the target row is locked (``FOR UPDATE``) before
        it is changed; with ``refuse_user_side`` a :func:`is_user_side_protected`
        target raises :class:`UserSideTargetError` and nothing changes;
        ``refuse_user_side_above`` as in :meth:`supersede`."""

    @abc.abstractmethod
    async def user_side_protected(self, *, tenant_id: UUID, user_id: UUID, memory_id: UUID) -> bool:
        """B-300 review MEDIUM-3 — :func:`is_user_side_protected` for one row, its
        summary members read whatever their current validity. Unknown id → False."""

    @abc.abstractmethod
    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        """Phase 3a (purge_user) — soft-delete EVERY live memory for a user.

        Bulk ``soft_delete``: stamps ``deleted_at`` on all of the user's
        not-yet-deleted rows and returns the count newly deleted (a re-purge
        returns 0 — already-deleted rows are skipped). Tenant- AND user-scoped:
        the ``(tenant_id, user_id)`` predicate never touches another tenant's
        or user's memories. The rows are KEPT (recoverable within the retention
        window); a later retention sweep hard-deletes them."""

    @abc.abstractmethod
    async def hard_delete_expired(self, *, before: datetime, limit: int = 1000) -> int:
        """PR1 Task 1 — retention job: physically DELETE soft-deleted rows
        whose ``deleted_at`` is older than ``before``.

        Cross-tenant: NO ``(tenant_id, user_id)`` predicate — unlike every
        other method on this interface, this is the platform-wide retention
        sweep's own method (consumed by the ``retention-cleanup-job``), not
        a per-tenant repository call. Deletes at most ``limit`` rows, oldest
        ``deleted_at`` first, and returns the number of rows actually
        removed. Live rows (``deleted_at IS NULL``) are never touched."""

    # ------------------------------------------------------------------
    # Capability Uplift Sprint #7 — MemoryConsolidator interface.
    # Mini-ADRs U-33 / U-34 / U-37 / U-40. The control-plane
    # ``MemoryConsolidator`` worker is the only caller of these methods
    # in M0; M1-K Admin UI may extend.
    # ------------------------------------------------------------------

    @abc.abstractmethod
    async def consolidator_distinct_tenant_ids(self) -> list[UUID]:
        """Return all tenant ids with at least one live transient memory row.

        Used by the ``MemoryConsolidator`` worker to enumerate tenants
        worth scanning per tick (skipping tenants with no transient data).
        Mirrors :meth:`SkillStore.curator_distinct_tenant_ids` (Sprint #4
        Mini-ADR U-26 pattern). Soft-deleted rows are excluded."""

    @abc.abstractmethod
    async def distinct_users(
        self,
        *,
        tenant_id: UUID,
        after: UUID | None = None,
        limit: int | None = None,
    ) -> list[UUID]:
        """Return distinct ``user_id`` with at least one transient row.

        Used by the consolidator worker to enumerate users worth
        scanning per tick (skipping users with no fresh writes).
        Soft-deleted rows are excluded.

        B-300 D4 — ordered by user id; ``after`` keeps only ids greater than it
        (the consolidation cursor), ``limit`` caps the count."""

    @abc.abstractmethod
    async def get_consolidation_cursor(self, *, tenant_id: UUID) -> UUID | None:
        """B-300 D4 — the last user the consolidator swept in this tenant, or
        ``None`` before the first one."""

    @abc.abstractmethod
    async def set_consolidation_cursor(self, *, tenant_id: UUID, user_id: UUID) -> None:
        """B-300 D4 — move the tenant's consolidation cursor to ``user_id``."""

    @abc.abstractmethod
    async def list_rejected_clusters(self, *, tenant_id: UUID, user_id: UUID) -> set[str]:
        """Member-set hashes of the clusters the consolidation model rejected
        for this user (B-300 PR A rejected-cluster markers)."""

    @abc.abstractmethod
    async def record_rejected_cluster(
        self, *, tenant_id: UUID, user_id: UUID, member_hash: str, reason: str
    ) -> None:
        """Mark a rejected cluster (by its member-set hash); re-marking the
        same set refreshes its reason and time."""

    @abc.abstractmethod
    async def prune_rejected_clusters(self, *, tenant_id: UUID, before: datetime) -> int:
        """Drop the tenant's markers (every user) recorded before ``before``;
        returns how many."""

    @abc.abstractmethod
    async def delete_rejected_clusters_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        """Drop every marker of a user (user purge); returns how many."""

    @abc.abstractmethod
    async def list_transient(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        max_age_days: int,
        limit: int,
    ) -> list[MemoryItem]:
        """Sprint #7 SUB-PASS 1 candidate fetch — return live transient
        items written within ``max_age_days``, oldest first, capped at
        ``limit``. Skips items already consolidated_into something.

        B-300 D1 — only currently-valid rows: the same predicate as the
        current-time :meth:`retrieve` (no superseded / expired / archived row)."""

    @abc.abstractmethod
    async def vector_neighbors(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        embedding: Sequence[float],
        cosine_max: float,
        limit: int,
    ) -> list[MemoryItem]:
        """Sprint #7 SUB-PASS 1 cluster builder — return live transient
        items within ``cosine_max`` cosine distance of ``embedding``,
        closest first, capped at ``limit``. Skips items already
        ``consolidated_into`` something; B-300 D1 — currently-valid rows only,
        as :meth:`list_transient`.

        Implementations should use the existing pgvector HNSW index
        (no new index for Sprint #7)."""

    @abc.abstractmethod
    async def write_consolidated(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        content: str,
        embedding: Sequence[float],
        source_ids: Sequence[UUID],
        source_hashes: Mapping[UUID, str],
        embedding_model: str | None = None,
    ) -> MemoryItem:
        """Sprint #7 SUB-PASS 1 commit — write a new consolidated item
        with ``status='consolidated'`` + ``consolidated_from=source_ids``
        and atomically mark every source's ``consolidated_into`` to the
        new id. The new item's ``kind`` is always ``"fact"`` (episodic
        consolidation is out-of-scope for Sprint #7).

        Idempotent under retry: if any source already has
        ``consolidated_into`` set the entire operation aborts and the
        method raises so the worker can skip the cluster on the next tick.

        B-300 — the members are re-read (row-locked) in the write's own
        transaction; when any is missing or no longer current
        :class:`ConsolidationSourcesChangedError` is raised and nothing is
        written (D1). ``source_hashes`` carries each member's ``content_hash``
        as it was read when the cluster was picked; a member corrected in place
        meanwhile (still current, new text) counts as changed too — the summary
        was decided on the old text. ``content`` goes through the strict write scan first;
        blocked → :class:`MemoryInjectionBlockedError`, nothing written (D2).

        B-300 PR 2 — the summary row takes its provenance, scores and source
        from the re-read members (:func:`summary_fields`) and records
        ``embedding_model``. Members that turned out mixed-source (assistant and
        user side, :func:`is_mixed_origin`) raise
        :class:`ConsolidationSourcesChangedError` — such clusters are never
        summarized; the consolidator filters them when it picks clusters, this
        re-check covers a member corrected meanwhile."""

    @abc.abstractmethod
    async def list_purge_candidates(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        min_age_days: int,
        limit: int,
    ) -> list[MemoryItem]:
        """Sprint #7 SUB-PASS 2 candidate fetch — return live transient
        items older than ``min_age_days`` that have never been retrieved
        (``access_count == 0``) and have never been
        reviewed (``last_reviewed_at IS NULL``).

        Oldest first, capped at ``limit``. The 3 filters together are
        Mini-ADR U-37's purge protections."""

    @abc.abstractmethod
    async def mark_reviewed(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
    ) -> bool:
        """Sprint #7 SUB-PASS 2 commit — stamp ``last_reviewed_at`` so
        future ticks skip this item, and clear ``review_flagged_at``
        (Stream HX-2 — a reviewed flag is a consumed flag). Returns
        ``False`` for unknown id / wrong tenant / wrong user."""

    @abc.abstractmethod
    async def flag_for_review(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        source_thread_id: str,
    ) -> int:
        """Stream HX-2 (Mini-ADR HX-B3) — stamp ``review_flagged_at`` on
        every live transient item extracted from ``source_thread_id``.

        Returns the number of items flagged. Idempotent: re-flagging
        refreshes the timestamp. Consolidated parents are not touched
        (their transient sources are; cascade re-review is out of scope).
        """

    @abc.abstractmethod
    async def list_review_flagged(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        limit: int,
    ) -> list[MemoryItem]:
        """Stream HX-2 — live transient items with ``review_flagged_at``
        set, oldest flag first, capped at ``limit``. Unlike
        :meth:`list_purge_candidates` there is no age / retrieval / prior
        review filter — a user 👎 re-opens even a previously-reviewed
        item.
        """

    @abc.abstractmethod
    async def list_due_for_review(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        limit: int,
    ) -> list[MemoryItem]:
        """P5b-2b ⑦ — live facts whose predicted validity window is due for
        re-review: ``expected_valid_days IS NOT NULL`` and
        ``COALESCE(last_reviewed_at, created_at) + expected_valid_days days
        <= now``, excluding deleted / invalidated (superseded) / expired rows.
        Oldest-due first, capped at ``limit``. Unlike
        :meth:`list_purge_candidates` there is NO access_count / status
        filter — a retrieved, in-use fact is exactly what predictive review
        must revisit."""

    @abc.abstractmethod
    async def renew_review(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
        expected_valid_days: int | None,
    ) -> bool:
        """P5b-2b ⑦ still-true verdict — stamp ``last_reviewed_at=now`` (re-arms
        the due-scan window from now) and set ``expected_valid_days`` to the
        given value (None clears it → the row drops out of future due-scans).
        Returns False for unknown id / wrong tenant / wrong user / deleted."""

    @abc.abstractmethod
    async def archive(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
    ) -> bool:
        """Reserved for M2-C archive pipeline (Stream K.K6 vNext).

        Sprint #7 (Mini-ADR U-40) reserves the interface +
        ``status='archived'`` semantics so M2-C can land without a
        schema migration. Sprint #7 implementations raise
        ``NotImplementedError`` to make the gap loud — better than a
        silent no-op."""

    # --- B-278 彻底删除智能体 ---------------------------------------------

    @abc.abstractmethod
    async def count_for_agent(self, *, tenant_id: UUID, agent_name: str) -> int:
        """B-278 —— rows ``delete_for_agent`` would remove (purge preview)."""

    @abc.abstractmethod
    async def delete_for_agent(self, *, tenant_id: UUID, agent_name: str) -> int:
        """B-278 —— hard-delete every row whose ``agent_name`` is exactly this
        name (agent purge). Tenant-scoped; returns the rows removed.

        Shared memories (``agent_name IS NULL``) are never touched, even when
        their source conversation belonged to this agent — they are the
        user's, not the agent's. Soft-deleted rows go too: the purge is
        irreversible, it does not wait for the retention sweep."""
