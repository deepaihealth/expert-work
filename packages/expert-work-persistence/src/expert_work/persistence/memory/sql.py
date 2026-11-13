"""SQLAlchemy + pgvector ``MemoryStore`` (Postgres / asyncpg)."""

from __future__ import annotations

import math
from collections.abc import Sequence
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
    ConsolidationSourcesChangedError,
    MemoryInjectionBlockedError,
    MemoryStore,
    MemoryWriteResult,
    scan_memory_content,
    split_blocked,
)
from expert_work.persistence.memory.hash import hash_content
from expert_work.persistence.models import (
    MemoryClusterRejectionRow,
    MemoryConsolidationCursorRow,
    MemoryItemRow,
)
from expert_work.protocol import MemoryItem

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
            }
            for item in items
        ]
        stmt = pg_insert(MemoryItemRow).values(payload).on_conflict_do_nothing()
        async with self._sf() as session:
            await session.execute(stmt)
            await session.commit()
        return result

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
        old_id: UUID,
        new_item: MemoryItem,
    ) -> MemoryItem | None:
        hit = scan_memory_content(new_item.id, new_item.content)
        if hit is not None:
            raise MemoryInjectionBlockedError([hit])
        now = datetime.now(UTC)
        async with self._sf() as session:
            old = (
                await session.execute(
                    select(MemoryItemRow).where(
                        MemoryItemRow.id == old_id,
                        MemoryItemRow.tenant_id == tenant_id,
                        MemoryItemRow.user_id == user_id,
                        MemoryItemRow.deleted_at.is_(None),
                        MemoryItemRow.invalid_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if old is None:
                return None
            old.invalid_at = now
            old.superseded_by = new_item.id
            content = new_item.content
            new_row = MemoryItemRow(
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
                supersedes=old_id,
                expected_valid_days=new_item.expected_valid_days,
            )
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
        stmt = (
            update(MemoryItemRow)
            .where(
                MemoryItemRow.id == memory_id,
                MemoryItemRow.tenant_id == tenant_id,
                MemoryItemRow.user_id == user_id,
                MemoryItemRow.deleted_at.is_(None),
            )
            .values(expired_at=now)
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        now = datetime.now(UTC)
        stmt = (
            update(MemoryItemRow)
            .where(
                MemoryItemRow.tenant_id == tenant_id,
                MemoryItemRow.user_id == user_id,
                MemoryItemRow.deleted_at.is_(None),
            )
            .values(deleted_at=now)
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
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

    async def prune_rejected_clusters(
        self, *, tenant_id: UUID, user_id: UUID, before: datetime
    ) -> int:
        return await delete_where(
            self._sf,
            MemoryClusterRejectionRow,
            MemoryClusterRejectionRow.tenant_id == tenant_id,
            MemoryClusterRejectionRow.user_id == user_id,
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
            # own transaction: a concurrent supersede / expire / delete either
            # commits first (and is seen here) or waits for this commit.
            owned: list[ColumnElement[bool]] = [
                MemoryItemRow.id.in_(list(source_ids)),
                MemoryItemRow.tenant_id == tenant_id,
                MemoryItemRow.user_id == user_id,
            ]
            locked = (
                await session.execute(
                    select(MemoryItemRow.id, MemoryItemRow.consolidated_into)
                    .where(*owned)
                    .with_for_update()
                )
            ).all()
            # Idempotency guard — if any source has consolidated_into set
            # already, abort cleanly so the worker can skip on the next tick.
            already = next((row for row in locked if row[1] is not None), None)
            if already is not None:
                msg = f"memory item {already[0]} already consolidated_into {already[1]}; "
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

    async def list_due_for_review(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        limit: int,
    ) -> list[MemoryItem]:
        now = datetime.now(UTC)
        base_ts = func.coalesce(MemoryItemRow.last_reviewed_at, MemoryItemRow.created_at)
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
