"""SQLAlchemy + pgvector ``MemoryStore`` (Postgres / asyncpg)."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.common.search.decay import (
    frequency_boost,
    importance_weight,
    temporal_decay_factor,
)
from expert_work.common.search.rrf import rrf_fuse_scored
from expert_work.persistence.bulk_delete import count_where, delete_where
from expert_work.persistence.knowledge.text_search import tokenize_for_search
from expert_work.persistence.memory.base import (
    FAIL_CLOSED_TIER,
    USER_SIDE_ORIGINS,
    ConsolidationSourcesChangedError,
    FlaggedSummary,
    MemberLoader,
    MemoryInjectionBlockedError,
    MemoryStore,
    MemoryWriteResult,
    RetireRefusal,
    RetireResult,
    SummaryRewriteOutcome,
    SupersedeResult,
    is_mixed_origin,
    may_retire,
    origin_standing,
    scan_memory_content,
    split_blocked,
    summary_fields,
    tier_of,
)
from expert_work.persistence.memory.hash import hash_content
from expert_work.persistence.models import (
    MemoryClusterRejectionRow,
    MemoryConsolidationCursorRow,
    MemoryItemRow,
)
from expert_work.protocol import MemoryItem
from expert_work.protocol.memory_item import EffectiveOrigin, MemoryOrigin, effective_origin

#: Postgres ``tsvector`` configuration — ``simple`` so app-side jieba
#: segmentation is what controls tokenization (mirrors J.5 knowledge).
_TS_CONFIG = "simple"

#: Per-side recall depth fetched before RRF fusion — mirrors J.5.
_HYBRID_RECALL_LIMIT = 20


def _cosine_distance_value(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(float(x) * float(y) for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(float(x) * float(x) for x in a))
    norm_b = math.sqrt(sum(float(y) * float(y) for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 1.0
    return 1.0 - dot / (norm_a * norm_b)


def _decay_for(
    last_used_at: datetime | None, created_at: datetime | None, *, now: datetime
) -> float:
    """Stream CM-6 (Mini-ADR CM-G2/G3) — recency weight for one row.

    Anchored on ``last_used_at`` (use keeps a memory fresh), falling back
    to ``created_at``; no timestamp at all decays nothing.
    """
    anchor = last_used_at or created_at
    if anchor is None:
        return 1.0
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=UTC)
    return temporal_decay_factor(age=now - anchor)


def _row_to_item(row: MemoryItemRow) -> MemoryItem:
    item = MemoryItem(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        kind=row.kind,  # type: ignore[arg-type]
        agent_name=row.agent_name,
        content=row.content,
        content_hash=row.content_hash,
        embedding=tuple(float(value) for value in row.embedding),
        # Stream Memory-Enhance (M-2) — write-filter / correction scores.
        importance=float(row.importance),
        confidence=float(row.confidence),
        source_thread_id=row.source_thread_id,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        access_count=row.access_count,
        deleted_at=row.deleted_at,
        # Capability Uplift Sprint #7 (Mini-ADR U-33) — lifecycle fields.
        status=row.status,  # type: ignore[arg-type]
        consolidated_into=row.consolidated_into,
        consolidated_from=tuple(UUID(str(uid)) for uid in row.consolidated_from),
        last_reviewed_at=row.last_reviewed_at,
        review_flagged_at=row.review_flagged_at,
        # Stream P5b — 溯源 + bi-temporal
        source_run_id=row.source_run_id,
        valid_at=row.valid_at,
        expired_at=row.expired_at,
        invalid_at=row.invalid_at,
        supersedes=row.supersedes,
        superseded_by=row.superseded_by,
        expected_valid_days=row.expected_valid_days,
        # B-300 PR 2 — an unknown stored origin reads as None (model validator).
        origin=row.origin,  # type: ignore[arg-type]
        embedding_model=row.embedding_model,
        # B-300 redesign PR M — evidence pointer, event date, last restated by the user.
        evidence_message_id=row.evidence_message_id,
        evidence_message_index=row.evidence_message_index,
        occurred_on=row.occurred_on,
        confirmed_at=row.confirmed_at,
    )
    # Capability Uplift Sprint #2 (Mini-ADR U-4) — drift detection.
    if row.content_hash and hash_content(row.content) != row.content_hash:
        return item.model_copy(update={"drift": True})
    return item


# Capability Uplift Sprint #7 (Mini-ADR U-33) — default retrieve filter
# applied to every code path that returns "what the agent should see".
# Skips ``archived`` outright and skips raw transient items that have
# been superseded by a consolidated parent (the parent is returned in
# their place when relevant).
def _retrieve_filter(as_of: datetime | None = None) -> list[ColumnElement[bool]]:
    base: list[ColumnElement[bool]] = [
        MemoryItemRow.status != "archived",
        or_(
            MemoryItemRow.status == "consolidated",
            MemoryItemRow.consolidated_into.is_(None),
        ),
    ]
    if as_of is None:
        # Current-time: hide superseded (invalid_at) + world-expired rows.
        base.append(MemoryItemRow.invalid_at.is_(None))
        base.append(or_(MemoryItemRow.expired_at.is_(None), MemoryItemRow.expired_at > func.now()))
    else:
        # Time-travel: what was valid AT ``as_of``.
        base.append(or_(MemoryItemRow.valid_at.is_(None), MemoryItemRow.valid_at <= as_of))
        base.append(or_(MemoryItemRow.invalid_at.is_(None), MemoryItemRow.invalid_at > as_of))
        base.append(or_(MemoryItemRow.expired_at.is_(None), MemoryItemRow.expired_at > as_of))
    return base


def _member_loader(session: AsyncSession, tenant_id: UUID, user_id: UUID) -> MemberLoader:
    """B-300 review MEDIUM-3 — read summary members by id in ``session``, whatever
    their current validity (folded, superseded, expired, deleted all count)."""

    async def load(ids: Sequence[UUID]) -> list[MemoryItem]:
        rows = (
            (
                await session.execute(
                    select(MemoryItemRow).where(
                        MemoryItemRow.id.in_(list(ids)),
                        MemoryItemRow.tenant_id == tenant_id,
                        MemoryItemRow.user_id == user_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return [_row_to_item(row) for row in rows]

    return load


def _owned(tenant_id: UUID, user_id: UUID) -> list[ColumnElement[bool]]:
    return [MemoryItemRow.tenant_id == tenant_id, MemoryItemRow.user_id == user_id]


async def _lock_rows(
    session: AsyncSession, tenant_id: UUID, user_id: UUID, ids: Sequence[UUID]
) -> dict[UUID, MemoryItemRow]:
    """Lock this user's rows in id order (the order ``write_consolidated`` and
    ``delete_all_for_user`` lock in, so the writers cannot deadlock each other)."""
    if not ids:
        return {}
    rows = (
        (
            await session.execute(
                select(MemoryItemRow)
                .where(MemoryItemRow.id.in_(list(ids)), *_owned(tenant_id, user_id))
                .order_by(MemoryItemRow.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    return {row.id: row for row in rows}


async def _current_ids(
    session: AsyncSession, tenant_id: UUID, user_id: UUID, ids: Sequence[UUID]
) -> set[UUID]:
    """Which of ``ids`` are currently valid — the same predicate recall uses."""
    if not ids:
        return set()
    found = await session.execute(
        select(MemoryItemRow.id).where(
            MemoryItemRow.id.in_(list(ids)),
            *_owned(tenant_id, user_id),
            MemoryItemRow.deleted_at.is_(None),
            *_retrieve_filter(),
        )
    )
    return set(found.scalars().all())


async def _judge_retirements(
    session: AsyncSession,
    locked: Mapping[UUID, MemoryItemRow],
    current: set[UUID],
    old_ids: Sequence[UUID],
    candidate: EffectiveOrigin,
    identical: frozenset[UUID],
) -> tuple[list[MemoryItemRow], dict[UUID, RetireRefusal]]:
    """B-300 redesign §7.8 — each target re-judged on its locked row (members are read,
    not locked): which may be retired, and why the others may not."""
    passed: list[MemoryItemRow] = []
    refused: dict[UUID, RetireRefusal] = {}
    for old_id in sorted(dict.fromkeys(old_ids)):
        row = locked.get(old_id)
        if row is None or old_id not in current:
            refused[old_id] = "gone"
            continue
        tier = await origin_standing(
            _row_to_item(row), _member_loader(session, row.tenant_id, row.user_id)
        )
        reason = may_retire(
            candidate,
            tier,
            target_is_summary=bool(row.consolidated_from),
            identical=old_id in identical,
        )
        if reason is None:
            passed.append(row)
        else:
            refused[old_id] = reason
    return passed, refused


def _dedup_slot(
    tenant_id: UUID, user_id: UUID, content_hash: str, agent_name: str | None
) -> list[ColumnElement[bool]]:
    """The predicate of ``memory_item_dedup_uniq`` (migration 0127): tenant + user +
    content hash + agent name among live rows; kind and origin are not part of it."""
    return [
        *_owned(tenant_id, user_id),
        MemoryItemRow.content_hash == content_hash,
        func.coalesce(MemoryItemRow.agent_name, "") == (agent_name or ""),
        MemoryItemRow.deleted_at.is_(None),
        MemoryItemRow.invalid_at.is_(None),
        MemoryItemRow.expired_at.is_(None),
    ]


async def _lock_dedup_clash(
    session: AsyncSession,
    tenant_id: UUID,
    user_id: UUID,
    content_hash: str,
    agent_name: str | None,
) -> MemoryItemRow | None:
    return (
        await session.execute(
            select(MemoryItemRow)
            .where(*_dedup_slot(tenant_id, user_id, content_hash, agent_name))
            .with_for_update()
        )
    ).scalar_one_or_none()


def _new_row(new_item: MemoryItem, tenant_id: UUID, user_id: UUID, now: datetime) -> MemoryItemRow:
    """The row :meth:`SqlMemoryStore.supersede` / ``write_history`` insert for a candidate."""
    content = new_item.content
    return MemoryItemRow(
        id=new_item.id,
        tenant_id=tenant_id,
        user_id=user_id,
        kind=new_item.kind,
        agent_name=new_item.agent_name,
        content=content,
        content_hash=new_item.content_hash or hash_content(content),
        embedding=list(new_item.embedding),
        importance=new_item.importance,
        confidence=new_item.confidence,
        source_thread_id=new_item.source_thread_id,
        source_run_id=new_item.source_run_id,
        content_tsv=func.to_tsvector(_TS_CONFIG, tokenize_for_search(content)),
        created_at=now,
        last_used_at=now,
        valid_at=now,
        expected_valid_days=new_item.expected_valid_days,
        origin=new_item.origin,
        embedding_model=new_item.embedding_model,
        # B-300 redesign §9.2 — the new row carries the candidate's evidence
        # pointer and event date (and, like the in-memory store, the rest of it).
        evidence_message_id=new_item.evidence_message_id,
        evidence_message_index=new_item.evidence_message_index,
        occurred_on=new_item.occurred_on,
        # Only user-side rows carry a confirmed date.
        confirmed_at=new_item.confirmed_at
        if effective_origin(new_item) in USER_SIDE_ORIGINS
        else None,
    )


#: B-300 redesign §9.2 — a row whose effective origin is on the user's side (the same
#: test as :func:`effective_origin` in ``USER_SIDE_ORIGINS``: 1.0 = a user correction).
_USER_SIDE_ROW = or_(
    MemoryItemRow.confidence >= 1.0,
    MemoryItemRow.origin.in_(sorted(USER_SIDE_ORIGINS - {"user_corrected"})),
)


async def _later_statements(session: AsyncSession, summary: MemoryItemRow) -> list[MemoryItem]:
    """B-300 redesign §7.12 — what the user said after a summary was written and that was
    linked to it: live user-side rows whose ``supersedes`` is the summary, oldest first."""
    rows = (
        (
            await session.execute(
                select(MemoryItemRow)
                .where(
                    *_owned(summary.tenant_id, summary.user_id),
                    MemoryItemRow.supersedes == summary.id,
                    MemoryItemRow.deleted_at.is_(None),
                    MemoryItemRow.created_at > summary.created_at,
                    _USER_SIDE_ROW,
                    *_retrieve_filter(),
                )
                .order_by(MemoryItemRow.created_at.asc(), MemoryItemRow.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return [_row_to_item(row) for row in rows]


async def _flag_summaries(
    session: AsyncSession,
    tenant_id: UUID,
    user_id: UUID,
    ids: Sequence[UUID],
    locked: Mapping[UUID, MemoryItemRow],
    current: set[UUID],
    candidate: EffectiveOrigin,
    now: datetime,
) -> list[UUID]:
    """B-300 redesign §7.12 step 1 — mark each (locked) summary "to rewrite"; one already
    rewritten is followed one step along ``superseded_by`` to the summary that replaced it."""
    flagged: list[UUID] = []
    for summary_id in dict.fromkeys(ids):
        row = locked.get(summary_id)
        if row is not None and summary_id not in current:
            successor = row.superseded_by
            row = None
            if successor is not None:
                step = await _lock_rows(session, tenant_id, user_id, [successor])
                if successor in await _current_ids(session, tenant_id, user_id, [successor]):
                    row = step.get(successor)
        if row is None or row.status != "consolidated" or not row.consolidated_from:
            continue
        tier = await origin_standing(_row_to_item(row), _member_loader(session, tenant_id, user_id))
        if may_retire(candidate, tier, target_is_summary=False) is not None:
            continue
        row.review_flagged_at = now
        flagged.append(row.id)
    return flagged


class SqlMemoryStore(MemoryStore):
    """Postgres-backed long-term memory repository (pgvector)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def write(self, items: Sequence[MemoryItem]) -> MemoryWriteResult:
        # B-300 D2 — strict scan per item: blocked ones are dropped on their own
        # and handed back; the rest of the batch is written.
        items, blocked = split_blocked(items)
        result = MemoryWriteResult(blocked=tuple(blocked))
        if not items:
            return result
        # Stream K.K7 — fill content_hash here so callers do not need
        # to import the hash helper, and use ON CONFLICT DO NOTHING
        # against the (tenant_id, user_id, content_hash) partial unique
        # index so a re-run that re-extracts the same memory is a no-op
        # instead of a duplicate row.
        payload = [
            {
                "id": item.id,
                "tenant_id": item.tenant_id,
                "user_id": item.user_id,
                "kind": item.kind,
                # Stream Agent-Templates (M1-5c) — episodic items carry their
                # owning agent; fact items leave it NULL (shared).
                "agent_name": item.agent_name,
                "content": item.content,
                "content_hash": item.content_hash or hash_content(item.content),
                "embedding": list(item.embedding),
                # Stream Memory-Enhance (M-2) — persist the write-filter scores.
                "importance": item.importance,
                "confidence": item.confidence,
                "source_thread_id": item.source_thread_id,
                # Capability Uplift Sprint #6 — populate the tsvector
                # column from jieba-segmented content. ``func.to_tsvector``
                # is evaluated server-side so the value lands as a real
                # tsvector, not a string cast.
                "content_tsv": func.to_tsvector(_TS_CONFIG, tokenize_for_search(item.content)),
                # Stream CM-N5 (Mini-ADR CM-K7) — honour caller-supplied
                # timestamps so ``write(items)`` matches its documented
                # "each item carries its own fields" semantics. ``None``
                # (every production path) falls back to ``now()`` exactly
                # like the server default; the eval harness sets both to
                # benchmark session dates so temporal decay (CM-6) is
                # exercised against real ages.
                "created_at": item.created_at if item.created_at is not None else func.now(),
                "last_used_at": item.last_used_at if item.last_used_at is not None else func.now(),
                # Stream P5b — provenance + world-validity anchor. valid_at
                # defaults to now() (== created_at at insert) so new rows are
                # "valid from creation"; supersede() overrides it explicitly.
                "source_run_id": item.source_run_id,
                "valid_at": item.valid_at if item.valid_at is not None else func.now(),
                "supersedes": item.supersedes,
                "expected_valid_days": item.expected_valid_days,
                # B-300 PR 2 — source + the model that computed the vector.
                "origin": item.origin,
                "embedding_model": item.embedding_model,
                # B-300 redesign PR M.
                "evidence_message_id": item.evidence_message_id,
                "evidence_message_index": item.evidence_message_index,
                "occurred_on": item.occurred_on,
                "confirmed_at": item.confirmed_at,
            }
            for item in items
        ]
        stmt = (
            pg_insert(MemoryItemRow)
            .values(payload)
            .on_conflict_do_nothing()
            # B-300 D12 — RETURNING only yields the rows that were inserted, so
            # a dedup no-op is not counted as written.
            .returning(MemoryItemRow.id)
        )
        async with self._sf() as session:
            landed = (await session.execute(stmt)).scalars().all()
            await session.commit()
        return MemoryWriteResult(blocked=result.blocked, written=len(landed))

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
        # Capability Uplift Sprint #6 (Mini-ADR U-5) — hybrid path.
        if query_text is not None and query_text.strip():
            return await self._hybrid_retrieve(
                tenant_id=tenant_id,
                user_id=user_id,
                query_embedding=query_embedding,
                query_text=query_text,
                kind=kind,
                agent_name=agent_name,
                as_of=as_of,
                limit=limit,
            )
        return await self._vector_retrieve(
            tenant_id=tenant_id,
            user_id=user_id,
            query_embedding=query_embedding,
            kind=kind,
            agent_name=agent_name,
            as_of=as_of,
            limit=limit,
        )

    async def _vector_retrieve(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        query_embedding: Sequence[float],
        kind: Literal["fact", "episodic"] | None,
        agent_name: str | None = None,
        as_of: datetime | None = None,
        limit: int,
    ) -> list[MemoryItem]:
        stmt = select(MemoryItemRow).where(
            MemoryItemRow.tenant_id == tenant_id,
            MemoryItemRow.user_id == user_id,
            MemoryItemRow.deleted_at.is_(None),  # Stream K.K6 — exclude soft-deleted
            *_retrieve_filter(as_of),  # Sprint #7 lifecycle filter (+ P5b as_of)
        )
        if kind is not None:
            stmt = stmt.where(MemoryItemRow.kind == kind)
        # Stream Agent-Templates (M1-5c) — per-agent episodic scope: shared facts
        # (agent_name NULL) + this agent's episodic rows; other agents excluded.
        if agent_name is not None:
            stmt = stmt.where(
                or_(MemoryItemRow.agent_name.is_(None), MemoryItemRow.agent_name == agent_name)
            )
        # pgvector cosine distance (``<=>``); HNSW index backs the sort.
        stmt = stmt.order_by(MemoryItemRow.embedding.cosine_distance(list(query_embedding))).limit(
            limit
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        # Stream CM-6 (Mini-ADR CM-G2) — temporal decay re-ranks inside the
        # recall window: similarity (1 - distance/2 ∈ [0,1]) weighted by
        # recency of use. The window itself is unchanged.
        now = datetime.now(UTC)
        weighted = sorted(
            rows,
            key=lambda row: (
                (1.0 - _cosine_distance_value(query_embedding, row.embedding) / 2.0)
                * _decay_for(row.last_used_at, row.created_at, now=now)
                * frequency_boost(row.access_count)
                * importance_weight(row.importance)
            ),
            reverse=True,
        )
        return [_row_to_item(row) for row in weighted]

    async def _hybrid_retrieve(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        query_embedding: Sequence[float],
        query_text: str,
        kind: Literal["fact", "episodic"] | None,
        agent_name: str | None = None,
        as_of: datetime | None = None,
        limit: int,
    ) -> list[MemoryItem]:
        tokenized = tokenize_for_search(query_text)
        if not tokenized:
            return await self._vector_retrieve(
                tenant_id=tenant_id,
                user_id=user_id,
                query_embedding=query_embedding,
                kind=kind,
                agent_name=agent_name,
                as_of=as_of,
                limit=limit,
            )
        # Two parallel selects under the same RLS-scoped session; fuse
        # in Python (cheaper than a SQL UNION + window function for the
        # small recall_limit window we work with).
        ts_query = func.plainto_tsquery(_TS_CONFIG, tokenized)
        base_where: list[ColumnElement[bool]] = [
            MemoryItemRow.tenant_id == tenant_id,
            MemoryItemRow.user_id == user_id,
            MemoryItemRow.deleted_at.is_(None),
            *_retrieve_filter(as_of),  # Sprint #7 lifecycle filter (+ P5b as_of)
        ]
        if kind is not None:
            base_where.append(MemoryItemRow.kind == kind)
        # Stream Agent-Templates (M1-5c) — per-agent episodic scope (see _vector).
        if agent_name is not None:
            base_where.append(
                or_(MemoryItemRow.agent_name.is_(None), MemoryItemRow.agent_name == agent_name)
            )

        vector_stmt = (
            select(MemoryItemRow)
            .where(*base_where)
            .order_by(MemoryItemRow.embedding.cosine_distance(list(query_embedding)))
            .limit(_HYBRID_RECALL_LIMIT)
        )
        keyword_stmt = (
            select(MemoryItemRow)
            .where(*base_where, MemoryItemRow.content_tsv.op("@@")(ts_query))
            .order_by(func.ts_rank(MemoryItemRow.content_tsv, ts_query).desc())
            .limit(_HYBRID_RECALL_LIMIT)
        )
        async with self._sf() as session:
            vector_rows = (await session.execute(vector_stmt)).scalars().all()
            keyword_rows = (await session.execute(keyword_stmt)).scalars().all()
        # RRF on the row IDs (hashable); resolve back to rows after fusion.
        # Stream CM-6 (Mini-ADR CM-G2) — temporal decay re-weights the
        # fused scores before the final cut so recently-used memories win
        # same-relevance ties inside the recall window.
        by_id = {row.id: row for row in list(vector_rows) + list(keyword_rows)}
        scored = rrf_fuse_scored([[r.id for r in vector_rows], [r.id for r in keyword_rows]])
        now = datetime.now(UTC)
        weighted = sorted(
            (
                (
                    mid,
                    score
                    * _decay_for(by_id[mid].last_used_at, by_id[mid].created_at, now=now)
                    * frequency_boost(by_id[mid].access_count)
                    * importance_weight(by_id[mid].importance),
                )
                for mid, score in scored
            ),
            key=lambda pair: pair[1],
            reverse=True,
        )
        return [_row_to_item(by_id[mid]) for mid, _score in weighted[:limit]]

    async def list_for_user(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        kind: Literal["fact", "episodic"] | None = None,
        as_of: datetime | None = None,
        limit: int = 50,
    ) -> list[MemoryItem]:
        conds: list[ColumnElement[bool]] = [
            MemoryItemRow.tenant_id == tenant_id,
            MemoryItemRow.user_id == user_id,
            MemoryItemRow.deleted_at.is_(None),
        ]
        if kind is not None:
            conds.append(MemoryItemRow.kind == kind)
        # Stream P5b-2c — as_of=None keeps the pre-existing behavior (no
        # bi-temporal predicate at all, so a superseded/not-yet-valid row
        # still surfaces here for direct inspection). Only when as_of is
        # given do we AND the same three time-travel predicates retrieve()'s
        # _retrieve_filter applies (sql.py:118-121).
        if as_of is not None:
            conds.append(or_(MemoryItemRow.valid_at.is_(None), MemoryItemRow.valid_at <= as_of))
            conds.append(or_(MemoryItemRow.invalid_at.is_(None), MemoryItemRow.invalid_at > as_of))
            conds.append(or_(MemoryItemRow.expired_at.is_(None), MemoryItemRow.expired_at > as_of))
        # newest first; ``memory_item_live_user_idx`` (migration 0024) is
        # a partial index on (user_id, created_at DESC) WHERE
        # deleted_at IS NULL — query shape matches.
        stmt = (
            select(MemoryItemRow)
            .where(*conds)
            .order_by(MemoryItemRow.created_at.desc())
            .limit(limit)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_item(row) for row in rows]

    async def list_all_tenants(
        self,
        *,
        kind: Literal["fact", "episodic"] | None = None,
        limit: int = 50,
    ) -> list[MemoryItem]:
        # Stream N — no tenant / user filter; caller must wrap in bypass_rls_session().
        stmt = select(MemoryItemRow).where(MemoryItemRow.deleted_at.is_(None))
        if kind is not None:
            stmt = stmt.where(MemoryItemRow.kind == kind)
        stmt = stmt.order_by(MemoryItemRow.created_at.desc()).limit(limit)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_item(row) for row in rows]

    async def get_live(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
    ) -> MemoryItem | None:
        stmt = select(MemoryItemRow).where(
            MemoryItemRow.id == memory_id,
            MemoryItemRow.tenant_id == tenant_id,
            MemoryItemRow.user_id == user_id,
            MemoryItemRow.deleted_at.is_(None),
        )
        async with self._sf() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return None if row is None else _row_to_item(row)

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
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(MemoryItemRow).where(
                        MemoryItemRow.id == memory_id,
                        MemoryItemRow.tenant_id == tenant_id,
                        MemoryItemRow.user_id == user_id,
                        MemoryItemRow.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            row.content = content
            row.content_hash = hash_content(content)  # K.K7 — keep dedup hash in sync
            # Capability Uplift Sprint #6 — keep keyword search vector in
            # sync with the new content.
            row.content_tsv = func.to_tsvector(_TS_CONFIG, tokenize_for_search(content))
            row.embedding = list(embedding)
            if kind is not None:
                row.kind = kind
            # Stream Memory-Enhance (M-4) — a user correction asserts the new
            # content as truth, so the caller bumps confidence to 1.0.
            if confidence is not None:
                row.confidence = confidence
            # B-300 review MEDIUM-2 — capped on the row as stored, not on a value
            # the caller read earlier.
            if max_confidence is not None:
                row.confidence = min(float(row.confidence), max_confidence)
            # B-300 PR 2 — the vector is new, so is its model name; the source
            # as the caller decides (see the base class).
            row.embedding_model = embedding_model
            if clear_origin:
                row.origin = None
                # B-300 redesign — off the user's side, no confirmed date.
                row.confirmed_at = None
            elif origin is not None:
                row.origin = origin
            await session.commit()
            await session.refresh(row)
            return _row_to_item(row)

    async def soft_delete(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
    ) -> bool:
        now = datetime.now(UTC)
        stmt = (
            update(MemoryItemRow)
            .where(
                MemoryItemRow.id == memory_id,
                MemoryItemRow.tenant_id == tenant_id,
                MemoryItemRow.user_id == user_id,
                MemoryItemRow.deleted_at.is_(None),
            )
            .values(deleted_at=now)
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        if int(getattr(result, "rowcount", 0) or 0) > 0:
            return True
        # Either truly missing or already deleted. Differentiate by a
        # cheap existence check so the caller gets idempotent semantics
        # on a second forget but a clean 404 on an unknown id.
        async with self._sf() as session:
            exists = (
                await session.execute(
                    select(MemoryItemRow.id).where(
                        MemoryItemRow.id == memory_id,
                        MemoryItemRow.tenant_id == tenant_id,
                        MemoryItemRow.user_id == user_id,
                    )
                )
            ).first()
        return exists is not None

    async def bump_access(self, *, tenant_id: UUID, user_id: UUID, ids: Sequence[UUID]) -> None:
        if not ids:
            return
        now = datetime.now(UTC)
        stmt = (
            update(MemoryItemRow)
            .where(
                MemoryItemRow.tenant_id == tenant_id,
                MemoryItemRow.user_id == user_id,
                MemoryItemRow.id.in_(list(ids)),
            )
            .values(last_used_at=now, access_count=MemoryItemRow.access_count + 1)
        )
        async with self._sf() as session:
            await session.execute(stmt)
            await session.commit()

    async def supersede(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        new_item: MemoryItem,
        old_ids: Sequence[UUID] = (),
        identical_ids: Sequence[UUID] = (),
        flag_summary_ids: Sequence[UUID] = (),
        link_id: UUID | None = None,
    ) -> SupersedeResult:
        hit = scan_memory_content(new_item.id, new_item.content)
        if hit is not None:
            raise MemoryInjectionBlockedError([hit])
        now = datetime.now(UTC)
        candidate = effective_origin(new_item)
        content_hash = new_item.content_hash or hash_content(new_item.content)
        async with self._sf() as session:
            # One id-ordered lock over everything this call may change: the targets,
            # the summaries to flag, and the row already holding this content (found
            # unlocked first, re-read under the lock below).
            slot = _dedup_slot(tenant_id, user_id, content_hash, new_item.agent_name)
            holder = (await session.execute(select(MemoryItemRow.id).where(*slot))).scalar()
            wanted = {*old_ids, *flag_summary_ids, *([holder] if holder else [])}
            locked = await _lock_rows(session, tenant_id, user_id, sorted(wanted))
            current = await _current_ids(session, tenant_id, user_id, sorted(wanted))
            passed, refused = await _judge_retirements(
                session, locked, current, old_ids, candidate, frozenset(identical_ids)
            )
            retired = [row.id for row in passed]
            clash = await _lock_dedup_clash(
                session, tenant_id, user_id, content_hash, new_item.agent_name
            )
            replaced: UUID | None = None
            if clash is not None and clash.id in retired:
                clash = None  # the row holding this content is one being retired
            if clash is not None:
                tier = await origin_standing(
                    _row_to_item(clash), _member_loader(session, tenant_id, user_id)
                )
                if tier >= tier_of(candidate):
                    # The sentence is already there, said by a source at least as strong:
                    # nothing inserted, the retired rows link to it.
                    for row in passed:
                        row.invalid_at = now
                        row.superseded_by = clash.id
                    kept = _row_to_item(clash)
                    await session.commit()
                    return SupersedeResult(
                        item=kept, inserted=False, retired=tuple(retired), refused=refused
                    )
                replaced = clash.id
                passed = [*passed, clash]
                retired = [*retired, clash.id]
            for row in passed:
                row.invalid_at = now
                row.superseded_by = new_item.id
            # The partial unique index is checked per statement: the retired rows must
            # leave it before the new row enters.
            await session.flush()
            flagged = await _flag_summaries(
                session, tenant_id, user_id, flag_summary_ids, locked, current, candidate, now
            )
            coexisting = next((i for i, r in refused.items() if r == "confirmed_vs_stated"), None)
            linked: UUID | None = None
            if link_id is not None:
                linked = (
                    await session.execute(
                        select(MemoryItemRow.id).where(
                            MemoryItemRow.id == link_id, *_owned(tenant_id, user_id)
                        )
                    )
                ).scalar()
            new_row = _new_row(new_item, tenant_id, user_id, now)
            new_row.supersedes = next(
                (i for i in (*retired[:1], *flagged[:1], coexisting, linked) if i), None
            )
            session.add(new_row)
            await session.commit()
            await session.refresh(new_row)
            return SupersedeResult(
                item=_row_to_item(new_row),
                retired=tuple(retired),
                refused=refused,
                flagged=tuple(flagged),
                collision_replaced=replaced,
            )

    async def retire(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        old_ids: Sequence[UUID],
        anchor_id: UUID,
        candidate_origin: EffectiveOrigin,
    ) -> RetireResult:
        now = datetime.now(UTC)
        targets = [i for i in old_ids if i != anchor_id]
        async with self._sf() as session:
            wanted = sorted({*targets, anchor_id})
            locked = await _lock_rows(session, tenant_id, user_id, wanted)
            current = await _current_ids(session, tenant_id, user_id, wanted)
            anchor = locked.get(anchor_id)
            if anchor is None or anchor_id not in current:
                return RetireResult(anchor_live=False)
            tier = await origin_standing(
                _row_to_item(anchor), _member_loader(session, tenant_id, user_id)
            )
            if tier < tier_of(candidate_origin):
                return RetireResult(anchor_live=False)
            passed, refused = await _judge_retirements(
                session, locked, current, targets, candidate_origin, frozenset()
            )
            for row in passed:
                row.invalid_at = now
                row.superseded_by = anchor_id
            await session.commit()
        return RetireResult(
            anchor_live=True, retired=tuple(row.id for row in passed), refused=refused
        )

    async def write_history(
        self, *, tenant_id: UUID, user_id: UUID, anchor_id: UUID, new_item: MemoryItem
    ) -> MemoryItem | None:
        hit = scan_memory_content(new_item.id, new_item.content)
        if hit is not None:
            raise MemoryInjectionBlockedError([hit])
        now = datetime.now(UTC)
        async with self._sf() as session:
            await _lock_rows(session, tenant_id, user_id, [anchor_id])
            if anchor_id not in await _current_ids(session, tenant_id, user_id, [anchor_id]):
                return None
            new_row = _new_row(new_item, tenant_id, user_id, now)
            # Already superseded when it lands: it never enters the dedup index.
            new_row.invalid_at = now
            new_row.superseded_by = anchor_id
            session.add(new_row)
            await session.commit()
            await session.refresh(new_row)
            return _row_to_item(new_row)

    async def expire(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
    ) -> bool:
        now = datetime.now(UTC)
        async with self._sf() as session:
            # B-300 review MEDIUM-1 — lock the one target row before changing it.
            row = (
                await session.execute(
                    select(MemoryItemRow)
                    .where(
                        MemoryItemRow.id == memory_id,
                        MemoryItemRow.tenant_id == tenant_id,
                        MemoryItemRow.user_id == user_id,
                        MemoryItemRow.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            row.expired_at = now
            await session.commit()
        return True

    async def origin_standing(
        self, *, tenant_id: UUID, user_id: UUID, memory_id: UUID
    ) -> int | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(MemoryItemRow).where(
                        MemoryItemRow.id == memory_id,
                        MemoryItemRow.tenant_id == tenant_id,
                        MemoryItemRow.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return await origin_standing(
                _row_to_item(row), _member_loader(session, tenant_id, user_id)
            )

    async def refresh_confirmed_at(
        self, *, tenant_id: UUID, user_id: UUID, memory_id: UUID, confirmed_at: datetime
    ) -> bool:
        stmt = (
            update(MemoryItemRow)
            .where(
                MemoryItemRow.id == memory_id,
                MemoryItemRow.tenant_id == tenant_id,
                MemoryItemRow.user_id == user_id,
                MemoryItemRow.deleted_at.is_(None),
                MemoryItemRow.invalid_at.is_(None),
                MemoryItemRow.expired_at.is_(None),
                _USER_SIDE_ROW,
            )
            # GREATEST skips NULL: only ever moves forward.
            .values(confirmed_at=func.greatest(MemoryItemRow.confirmed_at, confirmed_at))
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        now = datetime.now(UTC)
        live: list[ColumnElement[bool]] = [
            MemoryItemRow.tenant_id == tenant_id,
            MemoryItemRow.user_id == user_id,
            MemoryItemRow.deleted_at.is_(None),
        ]
        async with self._sf() as session:
            # B-300 —— 先按 id 顺序锁住这些行再改:一条不带顺序的批量 UPDATE 按扫描顺序
            # 上锁,会与按 id 顺序锁成员的 ``write_consolidated`` 互相死锁。
            ids = (
                (
                    await session.execute(
                        select(MemoryItemRow.id)
                        .where(*live)
                        .order_by(MemoryItemRow.id)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            if not ids:
                await session.rollback()
                return 0
            result = await session.execute(
                update(MemoryItemRow)
                .where(MemoryItemRow.id.in_(list(ids)), *live)
                .values(deleted_at=now)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    async def hard_delete_expired(self, *, before: datetime, limit: int = 1000) -> int:
        subq = (
            select(MemoryItemRow.id)
            .where(
                MemoryItemRow.deleted_at.is_not(None),
                MemoryItemRow.deleted_at < before,
            )
            .order_by(MemoryItemRow.deleted_at.asc())
            .limit(limit)
        )
        stmt = delete(MemoryItemRow).where(MemoryItemRow.id.in_(subq))
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    # ------------------------------------------------------------------
    # Capability Uplift Sprint #7 — MemoryConsolidator interface
    # ------------------------------------------------------------------

    async def consolidator_distinct_tenant_ids(self) -> list[UUID]:
        stmt = (
            select(MemoryItemRow.tenant_id)
            .where(
                MemoryItemRow.deleted_at.is_(None),
                MemoryItemRow.status == "transient",
            )
            .distinct()
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return list(rows)

    async def distinct_users(
        self, *, tenant_id: UUID, after: UUID | None = None, limit: int | None = None
    ) -> list[UUID]:
        conds: list[ColumnElement[bool]] = [
            MemoryItemRow.tenant_id == tenant_id,
            MemoryItemRow.deleted_at.is_(None),
            MemoryItemRow.status == "transient",
        ]
        if after is not None:
            conds.append(MemoryItemRow.user_id > after)
        # B-300 D4 — ordered by user id so the cursor walk is stable.
        stmt = (
            select(MemoryItemRow.user_id)
            .where(*conds)
            .distinct()
            .order_by(MemoryItemRow.user_id)
            .limit(limit)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return list(rows)

    async def get_consolidation_cursor(self, *, tenant_id: UUID) -> UUID | None:
        stmt = select(MemoryConsolidationCursorRow.last_user_id).where(
            MemoryConsolidationCursorRow.tenant_id == tenant_id
        )
        async with self._sf() as session:
            return (await session.execute(stmt)).scalar_one_or_none()

    async def set_consolidation_cursor(self, *, tenant_id: UUID, user_id: UUID) -> None:
        stmt = (
            pg_insert(MemoryConsolidationCursorRow)
            .values(tenant_id=tenant_id, last_user_id=user_id)
            .on_conflict_do_update(
                index_elements=[MemoryConsolidationCursorRow.tenant_id],
                set_={"last_user_id": user_id, "updated_at": func.now()},
            )
        )
        async with self._sf() as session:
            await session.execute(stmt)
            await session.commit()

    async def list_rejected_clusters(self, *, tenant_id: UUID, user_id: UUID) -> set[str]:
        stmt = select(MemoryClusterRejectionRow.member_hash).where(
            MemoryClusterRejectionRow.tenant_id == tenant_id,
            MemoryClusterRejectionRow.user_id == user_id,
        )
        async with self._sf() as session:
            return set((await session.execute(stmt)).scalars().all())

    async def record_rejected_cluster(
        self, *, tenant_id: UUID, user_id: UUID, member_hash: str, reason: str
    ) -> None:
        stmt = (
            pg_insert(MemoryClusterRejectionRow)
            .values(
                tenant_id=tenant_id, user_id=user_id, member_hash=member_hash, reject_reason=reason
            )
            .on_conflict_do_update(
                index_elements=[
                    MemoryClusterRejectionRow.tenant_id,
                    MemoryClusterRejectionRow.user_id,
                    MemoryClusterRejectionRow.member_hash,
                ],
                set_={"reject_reason": reason, "rejected_at": func.now()},
            )
        )
        async with self._sf() as session:
            await session.execute(stmt)
            await session.commit()

    async def prune_rejected_clusters(self, *, tenant_id: UUID, before: datetime) -> int:
        return await delete_where(
            self._sf,
            MemoryClusterRejectionRow,
            MemoryClusterRejectionRow.tenant_id == tenant_id,
            MemoryClusterRejectionRow.rejected_at < before,
        )

    async def delete_rejected_clusters_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        return await delete_where(
            self._sf,
            MemoryClusterRejectionRow,
            MemoryClusterRejectionRow.tenant_id == tenant_id,
            MemoryClusterRejectionRow.user_id == user_id,
        )

    async def list_transient(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        max_age_days: int,
        limit: int,
    ) -> list[MemoryItem]:
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        stmt = (
            select(MemoryItemRow)
            .where(
                MemoryItemRow.tenant_id == tenant_id,
                MemoryItemRow.user_id == user_id,
                MemoryItemRow.deleted_at.is_(None),
                MemoryItemRow.status == "transient",
                MemoryItemRow.consolidated_into.is_(None),
                *_retrieve_filter(),  # B-300 D1 — currently-valid rows only
                MemoryItemRow.created_at >= cutoff,
            )
            .order_by(MemoryItemRow.created_at.asc())
            .limit(limit)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_item(row) for row in rows]

    async def vector_neighbors(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        embedding: Sequence[float],
        cosine_max: float,
        limit: int,
    ) -> list[MemoryItem]:
        # pgvector cosine distance via the existing HNSW index. We sort
        # by distance ASC then filter to those within ``cosine_max``.
        stmt = (
            select(MemoryItemRow)
            .where(
                MemoryItemRow.tenant_id == tenant_id,
                MemoryItemRow.user_id == user_id,
                MemoryItemRow.deleted_at.is_(None),
                MemoryItemRow.status == "transient",
                MemoryItemRow.consolidated_into.is_(None),
                *_retrieve_filter(),  # B-300 D1 — currently-valid rows only
                MemoryItemRow.embedding.cosine_distance(list(embedding)) <= cosine_max,
            )
            .order_by(MemoryItemRow.embedding.cosine_distance(list(embedding)))
            .limit(limit)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_item(row) for row in rows]

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
        now = datetime.now(UTC)
        new_id = uuid4()
        # B-300 D2 — the summary goes through the same strict scan as write().
        hit = scan_memory_content(new_id, content)
        if hit is not None:
            raise MemoryInjectionBlockedError([hit])
        content_hash = hash_content(content)
        async with self._sf() as session:
            # B-300 D1 — re-read the members under a row lock in this write's
            # own transaction: a concurrent supersede / expire / delete / in-place
            # correction either commits first (and is seen here) or waits for this
            # commit. Locked in id order; ``delete_all_for_user`` (user purge)
            # locks the same way, so the two cannot deadlock. ``bump_access`` /
            # ``flag_for_review`` still update in scan order (not changed here).
            owned: list[ColumnElement[bool]] = [
                MemoryItemRow.id.in_(list(source_ids)),
                MemoryItemRow.tenant_id == tenant_id,
                MemoryItemRow.user_id == user_id,
            ]
            locked = (
                (
                    await session.execute(
                        select(MemoryItemRow)
                        .where(*owned)
                        .order_by(MemoryItemRow.id)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            # Idempotency guard — if any source has consolidated_into set
            # already, abort cleanly so the worker can skip on the next tick.
            already = next((row for row in locked if row.consolidated_into is not None), None)
            if already is not None:
                msg = f"memory item {already.id} already consolidated_into "
                msg += f"{already.consolidated_into}; skipping cluster"
                raise ConsolidationSourcesChangedError(msg)
            # Review MEDIUM-1 — a member corrected in place since it was read: the
            # summary was decided on the old text.
            changed = next(
                (row for row in locked if source_hashes.get(row.id) != row.content_hash), None
            )
            if changed is not None:
                msg = f"memory item {changed.id} changed since it was read; skipping cluster"
                raise ConsolidationSourcesChangedError(msg)
            members = [_row_to_item(row) for row in locked]
            # B-300 §6.2 — a mixed-source cluster is never summarized.
            if is_mixed_origin(members):
                msg = "cluster members mix assistant-derived and user-side sources; "
                raise ConsolidationSourcesChangedError(msg + "skipping cluster")
            # The same "currently valid" predicate recall uses.
            current = (
                await session.execute(
                    select(func.count()).where(
                        *owned, MemoryItemRow.deleted_at.is_(None), *_retrieve_filter()
                    )
                )
            ).scalar_one()
            if current != len(set(source_ids)):
                msg = "a cluster member is no longer current; skipping cluster"
                raise ConsolidationSourcesChangedError(msg)
            # B-300 D6 前半 — provenance and scores from the re-read members.
            inherited = summary_fields(members)
            # Insert the consolidated parent.
            new_row = MemoryItemRow(
                id=new_id,
                tenant_id=tenant_id,
                user_id=user_id,
                kind="fact",
                content=content,
                content_hash=content_hash,
                embedding=list(embedding),
                created_at=now,
                last_used_at=now,
                content_tsv=func.to_tsvector(_TS_CONFIG, tokenize_for_search(content)),
                status="consolidated",
                consolidated_from=[str(sid) for sid in source_ids],
                source_thread_id=inherited.source_thread_id,
                source_run_id=inherited.source_run_id,
                importance=inherited.importance,
                confidence=inherited.confidence,
                origin=inherited.origin,
                embedding_model=embedding_model,
            )
            session.add(new_row)
            # Atomically link sources back to the new parent.
            await session.execute(
                update(MemoryItemRow)
                .where(MemoryItemRow.id.in_(list(source_ids)))
                .values(consolidated_into=new_id)
            )
            await session.commit()
            await session.refresh(new_row)
            return _row_to_item(new_row)

    async def list_purge_candidates(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        min_age_days: int,
        limit: int,
    ) -> list[MemoryItem]:
        cutoff = datetime.now(UTC) - timedelta(days=min_age_days)
        # "Never retrieved" — exact via access_count (P5a T6; supersedes the
        # old ``last_used_at <= created_at + 1 minute`` approximation from
        # Mini-ADR U-37, which was vacuously true before access_count
        # existed and could misread an early-but-real hit as "never used").
        # access_count is bumped in lockstep with last_used_at on every
        # recall hit, so == 0 means retrieve() has never returned this row.
        never_used = MemoryItemRow.access_count == 0
        stmt = (
            select(MemoryItemRow)
            .where(
                MemoryItemRow.tenant_id == tenant_id,
                MemoryItemRow.user_id == user_id,
                MemoryItemRow.deleted_at.is_(None),
                MemoryItemRow.status == "transient",
                MemoryItemRow.consolidated_into.is_(None),
                MemoryItemRow.last_reviewed_at.is_(None),
                # B-300 redesign — a memory the user said again is not noise.
                MemoryItemRow.confirmed_at.is_(None),
                MemoryItemRow.created_at < cutoff,
                and_(never_used),
            )
            .order_by(MemoryItemRow.created_at.asc())
            .limit(limit)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_item(row) for row in rows]

    async def mark_reviewed(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
    ) -> bool:
        now = datetime.now(UTC)
        stmt = (
            update(MemoryItemRow)
            .where(
                MemoryItemRow.id == memory_id,
                MemoryItemRow.tenant_id == tenant_id,
                MemoryItemRow.user_id == user_id,
                MemoryItemRow.deleted_at.is_(None),
            )
            .values(last_reviewed_at=now, review_flagged_at=None)
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def flag_for_review(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        source_thread_id: str,
    ) -> int:
        now = datetime.now(UTC)
        stmt = (
            update(MemoryItemRow)
            .where(
                MemoryItemRow.tenant_id == tenant_id,
                MemoryItemRow.user_id == user_id,
                MemoryItemRow.source_thread_id == source_thread_id,
                MemoryItemRow.deleted_at.is_(None),
                MemoryItemRow.status == "transient",
            )
            .values(review_flagged_at=now)
            .returning(MemoryItemRow.id)
        )
        async with self._sf() as session:
            flagged = (await session.execute(stmt)).scalars().all()
            await session.commit()
        return len(flagged)

    async def list_review_flagged(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        limit: int,
    ) -> list[MemoryItem]:
        stmt = (
            select(MemoryItemRow)
            .where(
                MemoryItemRow.tenant_id == tenant_id,
                MemoryItemRow.user_id == user_id,
                MemoryItemRow.deleted_at.is_(None),
                MemoryItemRow.status == "transient",
                MemoryItemRow.review_flagged_at.is_not(None),
            )
            .order_by(MemoryItemRow.review_flagged_at.asc(), MemoryItemRow.id.asc())
            .limit(limit)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_item(r) for r in rows]

    async def list_flagged_summaries(
        self, *, tenant_id: UUID, user_id: UUID, limit: int
    ) -> list[FlaggedSummary]:
        stmt = (
            select(MemoryItemRow)
            .where(
                *_owned(tenant_id, user_id),
                MemoryItemRow.deleted_at.is_(None),
                MemoryItemRow.status == "consolidated",
                MemoryItemRow.review_flagged_at.is_not(None),
                *_retrieve_filter(),
            )
            .order_by(MemoryItemRow.review_flagged_at.asc(), MemoryItemRow.id.asc())
            .limit(limit)
        )
        out: list[FlaggedSummary] = []
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
            load = _member_loader(session, tenant_id, user_id)
            for row in rows:
                summary = _row_to_item(row)
                members = await load(list(summary.consolidated_from))
                statements = await _later_statements(session, row)
                out.append(FlaggedSummary(summary, tuple(members), tuple(statements)))
        return out

    async def rewrite_summary(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        summary_id: UUID,
        flagged_at: datetime,
        content: str | None,
        embedding: Sequence[float] = (),
        embedding_model: str | None = None,
        unchanged: bool = False,
    ) -> SummaryRewriteOutcome:
        new_id = uuid4()
        if content is not None and not unchanged:
            hit = scan_memory_content(new_id, content)
            if hit is not None:
                raise MemoryInjectionBlockedError([hit])
        now = datetime.now(UTC)
        async with self._sf() as session:
            locked = await _lock_rows(session, tenant_id, user_id, [summary_id])
            old = locked.get(summary_id)
            if (
                old is None
                or old.status != "consolidated"
                or old.review_flagged_at != flagged_at
                or summary_id not in await _current_ids(session, tenant_id, user_id, [summary_id])
            ):
                return "stale"
            load = _member_loader(session, tenant_id, user_id)
            old_item = _row_to_item(old)
            # 终审 L-2 —— a member corrected in the console since the flag was set.
            if await origin_standing(old_item, load) >= FAIL_CLOSED_TIER:
                old.review_flagged_at = None
                await session.commit()
                return "corrected_member"
            if unchanged:
                old.review_flagged_at = None
                await session.commit()
                return "unchanged"
            old.invalid_at = now
            old.review_flagged_at = None
            if content is None:
                statements = await _later_statements(session, old)
                old.superseded_by = statements[-1].id if statements else None
                await session.commit()
                return "emptied"
            content_hash = hash_content(content)
            clash = await _lock_dedup_clash(session, tenant_id, user_id, content_hash, None)
            if clash is not None and clash.id != old.id:
                old.superseded_by = clash.id
                await session.commit()
                return "collided"
            old.superseded_by = new_id
            # The old summary leaves the dedup index before the new one enters.
            await session.flush()
            members = await load(list(old_item.consolidated_from))
            inherited = summary_fields(members) if members else None
            session.add(
                MemoryItemRow(
                    id=new_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    kind="fact",
                    content=content,
                    content_hash=content_hash,
                    embedding=list(embedding),
                    created_at=now,
                    last_used_at=now,
                    valid_at=now,
                    content_tsv=func.to_tsvector(_TS_CONFIG, tokenize_for_search(content)),
                    status="consolidated",
                    consolidated_from=[str(sid) for sid in old_item.consolidated_from],
                    source_thread_id=inherited.source_thread_id
                    if inherited
                    else old.source_thread_id,
                    source_run_id=inherited.source_run_id if inherited else old.source_run_id,
                    importance=inherited.importance if inherited else old.importance,
                    confidence=inherited.confidence if inherited else old.confidence,
                    origin=inherited.origin if inherited else old.origin,
                    embedding_model=embedding_model,
                )
            )
            await session.execute(
                update(MemoryItemRow)
                .where(*_owned(tenant_id, user_id), MemoryItemRow.consolidated_into == summary_id)
                .values(consolidated_into=new_id)
            )
            await session.commit()
        return "rewritten"

    async def defer_summary_rewrite(
        self, *, tenant_id: UUID, user_id: UUID, summary_id: UUID
    ) -> bool:
        stmt = (
            update(MemoryItemRow)
            .where(
                MemoryItemRow.id == summary_id,
                *_owned(tenant_id, user_id),
                MemoryItemRow.deleted_at.is_(None),
            )
            .values(last_reviewed_at=datetime.now(UTC))
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def list_due_for_review(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        limit: int,
    ) -> list[MemoryItem]:
        now = datetime.now(UTC)
        # B-300 redesign §9.2 — the latest of created / last reviewed / last restated by
        # the user (GREATEST skips NULLs): a memory the user just said again is not due.
        base_ts = func.greatest(
            MemoryItemRow.created_at, MemoryItemRow.last_reviewed_at, MemoryItemRow.confirmed_at
        )
        due_at = base_ts + func.make_interval(0, 0, 0, MemoryItemRow.expected_valid_days)
        stmt = (
            select(MemoryItemRow)
            .where(
                MemoryItemRow.tenant_id == tenant_id,
                MemoryItemRow.user_id == user_id,
                MemoryItemRow.deleted_at.is_(None),
                MemoryItemRow.invalid_at.is_(None),
                MemoryItemRow.expired_at.is_(None),
                MemoryItemRow.expected_valid_days.isnot(None),
                due_at <= now,
            )
            .order_by(due_at.asc())
            .limit(limit)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_item(row) for row in rows]

    async def renew_review(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
        expected_valid_days: int | None,
    ) -> bool:
        now = datetime.now(UTC)
        stmt = (
            update(MemoryItemRow)
            .where(
                MemoryItemRow.id == memory_id,
                MemoryItemRow.tenant_id == tenant_id,
                MemoryItemRow.user_id == user_id,
                MemoryItemRow.deleted_at.is_(None),
            )
            .values(last_reviewed_at=now, expected_valid_days=expected_valid_days)
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def archive(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
    ) -> bool:
        # Capability Uplift Sprint #7 (Mini-ADR U-40) — reserved for
        # M2-C archive pipeline. Raised loud rather than no-op so the
        # M2-C implementer gets a clear "do me" signal.
        msg = (
            "MemoryStore.archive() is reserved for M2-C; Sprint #7 only "
            "lands the interface + status='archived' retrieve filter."
        )
        raise NotImplementedError(msg)

    async def count_for_agent(self, *, tenant_id: UUID, agent_name: str) -> int:
        return await count_where(
            self._sf,
            MemoryItemRow,
            MemoryItemRow.tenant_id == tenant_id,
            MemoryItemRow.agent_name == agent_name,
        )

    async def delete_for_agent(self, *, tenant_id: UUID, agent_name: str) -> int:
        return await delete_where(
            self._sf,
            MemoryItemRow,
            MemoryItemRow.tenant_id == tenant_id,
            MemoryItemRow.agent_name == agent_name,
        )
