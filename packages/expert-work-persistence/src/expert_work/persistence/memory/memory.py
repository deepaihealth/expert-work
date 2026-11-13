"""In-memory ``MemoryStore`` for unit tests."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from expert_work.common.search.decay import (
    frequency_boost,
    importance_weight,
    temporal_decay_factor,
)
from expert_work.common.search.rrf import rrf_fuse_scored
from expert_work.common.threat_patterns import ThreatFinding, scan_for_threats
from expert_work.persistence.knowledge.text_search import tokenize_for_search
from expert_work.persistence.memory.base import MemoryInjectionBlockedError, MemoryStore
from expert_work.persistence.memory.hash import hash_content
from expert_work.protocol import MemoryItem

#: Per-side recall depth fetched before RRF fusion — mirrors the J.5
#: knowledge subsystem so behavior is comparable across hybrid paths.
_HYBRID_RECALL_LIMIT = 20


def _keyword_rank(rows: Sequence[MemoryItem], *, query_text: str) -> list[MemoryItem]:
    """Capability Uplift Sprint #6 — InMemory keyword ranking.

    Approximates Postgres ``ts_rank`` with a token-overlap count on
    jieba-segmented content. The contract is "items whose content
    shares more search tokens with the query rank higher"; the absolute
    score is not part of the contract — the SQL implementation computes
    it differently (``ts_rank``) and that's fine, RRF normalizes by
    rank, not by raw score (Mini-ADR U-5 § 7.3.4).
    """
    query_tokens = set(tokenize_for_search(query_text).split())
    if not query_tokens:
        return []

    def score(row: MemoryItem) -> int:
        return len(query_tokens & set(tokenize_for_search(row.content).split()))

    scored = [(row, score(row)) for row in rows]
    scored = [(row, s) for row, s in scored if s > 0]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [row for row, _ in scored]


def _with_drift_flag(item: MemoryItem) -> MemoryItem:
    """Capability Uplift Sprint #2 (Mini-ADR U-4) — recompute the
    K.K7 content hash and stamp ``drift=True`` when it diverges from
    the stored ``content_hash``. The original content is preserved;
    the recall node redacts based on the flag."""
    stored = item.content_hash
    if stored is None:
        return item
    if hash_content(item.content) == stored:
        return item
    return item.model_copy(update={"drift": True})


def _decay_for(item: MemoryItem, *, now: datetime) -> float:
    """Stream CM-6 (Mini-ADR CM-G2/G3) — recency weight, mirrors the SQL store.

    Anchored on ``last_used_at`` (use keeps a memory fresh), falling back
    to ``created_at``; no timestamp at all decays nothing.
    """
    anchor = item.last_used_at or item.created_at
    if anchor is None:
        return 1.0
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=UTC)
    return temporal_decay_factor(age=now - anchor)


def _cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine distance (0 = identical) — mirrors pgvector's ``<=>``."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 1.0
    return 1.0 - dot / (norm_a * norm_b)


class InMemoryMemoryStore(MemoryStore):
    def __init__(self) -> None:
        self._rows: list[MemoryItem] = []

    async def write(self, items: Sequence[MemoryItem]) -> None:
        # Capability Uplift Sprint #2 (Mini-ADR U-3) — atomic strict scan.
        blocked: list[tuple[object, list[ThreatFinding]]] = []
        for item in items:
            findings = scan_for_threats(item.content, scope="strict")
            if findings:
                blocked.append((item.id, findings))
        if blocked:
            raise MemoryInjectionBlockedError(blocked)  # type: ignore[arg-type]
        # Stream P5b-2c T1 — mirrors SqlMemoryStore.write()'s func.now()
        # fallback (sql.py:175/181) so an in-memory item written without
        # explicit timestamps doesn't diverge from SQL (never "due" for
        # predictive review, different as_of time-travel behavior against a
        # null valid_at). Computed once, outside the loop, so a multi-item
        # batch backfills to the same instant rather than drifting per row.
        now = datetime.now(UTC)
        for item in items:
            # Stream K.K7 — fill content_hash if the caller didn't, and
            # skip the row when an identical live entry already exists
            # for the same ``(tenant, user, content_hash)`` (mirrors the
            # SQL ON CONFLICT DO NOTHING path).
            content_hash = item.content_hash or hash_content(item.content)
            if any(
                r.tenant_id == item.tenant_id
                and r.user_id == item.user_id
                and r.deleted_at is None
                # Stream P5b fix (I-1) — mirrors migration 0127's rebuilt
                # partial unique index: a superseded/expired-but-not-deleted
                # row no longer occupies the dedup slot.
                and r.invalid_at is None
                and r.expired_at is None
                and (r.content_hash or hash_content(r.content)) == content_hash
                for r in self._rows
            ):
                continue
            self._rows.append(
                item.model_copy(
                    update={
                        "content_hash": content_hash,
                        "created_at": item.created_at if item.created_at is not None else now,
                        "valid_at": item.valid_at if item.valid_at is not None else now,
                    }
                )
            )

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
        now = datetime.now(UTC)

        def _temporally_visible(row: MemoryItem) -> bool:
            if as_of is None:
                return row.invalid_at is None and (row.expired_at is None or row.expired_at > now)
            return (
                (row.valid_at is None or row.valid_at <= as_of)
                and (row.invalid_at is None or row.invalid_at > as_of)
                and (row.expired_at is None or row.expired_at > as_of)
            )

        candidates = [
            row
            for row in self._rows
            if row.tenant_id == tenant_id
            and row.user_id == user_id
            and row.deleted_at is None  # Stream K.K6 — hide soft-deleted
            # Stream P5b — bi-temporal: hide superseded (invalid_at) + world-expired
            # (or, when as_of is given, time-travel to that instant).
            and _temporally_visible(row)
            # Capability Uplift Sprint #7 (Mini-ADR U-33) — skip archived
            # + skip raw transient that has been superseded by a
            # consolidated parent (prevents double-counting raw + summary).
            and row.status != "archived"
            and (row.status == "consolidated" or row.consolidated_into is None)
            and (kind is None or row.kind == kind)
            # Stream Agent-Templates (M1-5c) — per-agent episodic scope: shared
            # facts (agent_name None) + this agent's episodic rows.
            and (agent_name is None or row.agent_name is None or row.agent_name == agent_name)
        ]
        # Vector recall — closest cosine distance first.
        vector_hits = sorted(
            candidates, key=lambda row: _cosine_distance(query_embedding, row.embedding)
        )

        # Capability Uplift Sprint #6 (Mini-ADR U-5) — hybrid path.
        # Stream CM-6 (Mini-ADR CM-G2) — temporal decay re-ranks inside the
        # recall window on both paths, mirroring the SQL store.
        if query_text is not None and query_text.strip():
            keyword_hits = _keyword_rank(candidates, query_text=query_text)
            scored = rrf_fuse_scored(
                [vector_hits[:_HYBRID_RECALL_LIMIT], keyword_hits[:_HYBRID_RECALL_LIMIT]]
            )
            weighted = sorted(
                (
                    (
                        row,
                        score
                        * _decay_for(row, now=now)
                        * frequency_boost(row.access_count)
                        * importance_weight(row.importance),
                    )
                    for row, score in scored
                ),
                key=lambda pair: pair[1],
                reverse=True,
            )
            return [_with_drift_flag(row) for row, _score in weighted[:limit]]

        window = vector_hits[:limit]
        reweighted = sorted(
            window,
            key=lambda row: (
                (1.0 - _cosine_distance(query_embedding, row.embedding) / 2.0)
                * _decay_for(row, now=now)
                * frequency_boost(row.access_count)
                * importance_weight(row.importance)
            ),
            reverse=True,
        )
        return [_with_drift_flag(row) for row in reweighted]

    async def list_for_user(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        kind: Literal["fact", "episodic"] | None = None,
        as_of: datetime | None = None,
        limit: int = 50,
    ) -> list[MemoryItem]:
        # Stream P5b-2c — as_of=None keeps the pre-existing behavior (no
        # bi-temporal predicate at all); mirrors retrieve()'s
        # _temporally_visible as_of branch only when as_of is given.
        def _temporally_visible(row: MemoryItem) -> bool:
            if as_of is None:
                return True
            return (
                (row.valid_at is None or row.valid_at <= as_of)
                and (row.invalid_at is None or row.invalid_at > as_of)
                and (row.expired_at is None or row.expired_at > as_of)
            )

        candidates = [
            row
            for row in self._rows
            if row.tenant_id == tenant_id
            and row.user_id == user_id
            and row.deleted_at is None
            and (kind is None or row.kind == kind)
            and _temporally_visible(row)
        ]
        # newest-first matches the partial index ordering in migration 0024.
        candidates.sort(
            key=lambda row: row.created_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return candidates[:limit]

    async def list_all_tenants(
        self,
        *,
        kind: Literal["fact", "episodic"] | None = None,
        limit: int = 50,
    ) -> list[MemoryItem]:
        candidates = [
            row
            for row in self._rows
            if row.deleted_at is None and (kind is None or row.kind == kind)
        ]
        candidates.sort(
            key=lambda row: row.created_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return candidates[:limit]

    async def get_live(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
    ) -> MemoryItem | None:
        return next(
            (
                row
                for row in self._rows
                if row.id == memory_id
                and row.tenant_id == tenant_id
                and row.user_id == user_id
                and row.deleted_at is None
            ),
            None,
        )

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
        for idx, row in enumerate(self._rows):
            if (
                row.id == memory_id
                and row.tenant_id == tenant_id
                and row.user_id == user_id
                and row.deleted_at is None
            ):
                updated = row.model_copy(
                    update={
                        "content": content,
                        # K.K7 — keep dedup hash in sync with content
                        "content_hash": hash_content(content),
                        "embedding": tuple(float(v) for v in embedding),
                        "kind": kind if kind is not None else row.kind,
                        # Stream Memory-Enhance (M-4) — user correction asserts
                        # the rewrite as truth (confidence 1.0).
                        "confidence": confidence if confidence is not None else row.confidence,
                    }
                )
                self._rows[idx] = updated
                return updated
        return None

    async def soft_delete(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
    ) -> bool:
        for idx, row in enumerate(self._rows):
            if row.id == memory_id and row.tenant_id == tenant_id and row.user_id == user_id:
                if row.deleted_at is not None:
                    return True  # idempotent
                self._rows[idx] = row.model_copy(update={"deleted_at": datetime.now(UTC)})
                return True
        return False

    async def bump_access(self, *, tenant_id: UUID, user_id: UUID, ids: Sequence[UUID]) -> None:
        if not ids:
            return
        now = datetime.now(UTC)
        id_set = set(ids)
        self._rows = [
            row.model_copy(update={"last_used_at": now, "access_count": row.access_count + 1})
            if (row.tenant_id == tenant_id and row.user_id == user_id and row.id in id_set)
            else row
            for row in self._rows
        ]

    async def supersede(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        old_id: UUID,
        new_item: MemoryItem,
    ) -> MemoryItem | None:
        now = datetime.now(UTC)
        idx = next(
            (
                i
                for i, r in enumerate(self._rows)
                if r.id == old_id
                and r.tenant_id == tenant_id
                and r.user_id == user_id
                and r.deleted_at is None
                and r.invalid_at is None
            ),
            None,
        )
        if idx is None:
            return None
        self._rows[idx] = self._rows[idx].model_copy(
            update={"invalid_at": now, "superseded_by": new_item.id}
        )
        content_hash = new_item.content_hash or hash_content(new_item.content)
        written = new_item.model_copy(
            update={
                "content_hash": content_hash,
                "supersedes": old_id,
                "valid_at": now,
                "created_at": now,
                "last_used_at": now,
            }
        )
        self._rows.append(written)
        return written

    async def expire(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
    ) -> bool:
        for idx, row in enumerate(self._rows):
            if (
                row.id == memory_id
                and row.tenant_id == tenant_id
                and row.user_id == user_id
                and row.deleted_at is None
            ):
                self._rows[idx] = row.model_copy(update={"expired_at": datetime.now(UTC)})
                return True
        return False

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        now = datetime.now(UTC)
        removed = 0
        for idx, row in enumerate(self._rows):
            if row.tenant_id == tenant_id and row.user_id == user_id and row.deleted_at is None:
                self._rows[idx] = row.model_copy(update={"deleted_at": now})
                removed += 1
        return removed

    async def hard_delete_expired(self, *, before: datetime, limit: int = 1000) -> int:
        expired = sorted(
            (row for row in self._rows if row.deleted_at is not None and row.deleted_at < before),
            key=lambda row: row.deleted_at or datetime.min.replace(tzinfo=UTC),
        )[:limit]
        if not expired:
            return 0
        expired_ids = {row.id for row in expired}
        self._rows = [row for row in self._rows if row.id not in expired_ids]
        return len(expired_ids)

    # ------------------------------------------------------------------
    # Capability Uplift Sprint #7 — MemoryConsolidator interface
    # ------------------------------------------------------------------

    async def consolidator_distinct_tenant_ids(self) -> list[UUID]:
        seen: set[UUID] = set()
        for row in self._rows:
            if row.deleted_at is None and row.status == "transient":
                seen.add(row.tenant_id)
        return sorted(seen)

    async def distinct_users(self, *, tenant_id: UUID) -> list[UUID]:
        seen: set[UUID] = set()
        for row in self._rows:
            if row.tenant_id == tenant_id and row.deleted_at is None and row.status == "transient":
                seen.add(row.user_id)
        return sorted(seen)

    async def list_transient(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        max_age_days: int,
        limit: int,
    ) -> list[MemoryItem]:
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        candidates = [
            row
            for row in self._rows
            if row.tenant_id == tenant_id
            and row.user_id == user_id
            and row.deleted_at is None
            and row.status == "transient"
            and row.consolidated_into is None
            and (row.created_at or datetime.now(UTC)) >= cutoff
        ]
        candidates.sort(key=lambda r: r.created_at or datetime.min.replace(tzinfo=UTC))
        return candidates[:limit]

    async def vector_neighbors(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        embedding: Sequence[float],
        cosine_max: float,
        limit: int,
    ) -> list[MemoryItem]:
        candidates = [
            row
            for row in self._rows
            if row.tenant_id == tenant_id
            and row.user_id == user_id
            and row.deleted_at is None
            and row.status == "transient"
            and row.consolidated_into is None
        ]
        scored = [(row, _cosine_distance(embedding, row.embedding)) for row in candidates]
        scored = [(row, d) for row, d in scored if d <= cosine_max]
        scored.sort(key=lambda pair: pair[1])
        return [row for row, _ in scored[:limit]]

    async def write_consolidated(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        content: str,
        embedding: Sequence[float],
        source_ids: Sequence[UUID],
    ) -> MemoryItem:
        # Idempotency guard — fail fast if any source already consolidated
        for sid in source_ids:
            for row in self._rows:
                if row.id == sid and row.consolidated_into is not None:
                    msg = f"memory item {sid} already consolidated_into {row.consolidated_into}"
                    raise RuntimeError(msg)
        now = datetime.now(UTC)
        new_id = uuid4()
        new_item = MemoryItem(
            id=new_id,
            tenant_id=tenant_id,
            user_id=user_id,
            kind="fact",
            content=content,
            embedding=tuple(float(v) for v in embedding),
            content_hash=hash_content(content),
            created_at=now,
            last_used_at=now,
            status="consolidated",
            consolidated_from=tuple(source_ids),
        )
        self._rows.append(new_item)
        # Atomically link sources back to the new consolidated parent.
        for idx, row in enumerate(self._rows):
            if row.id in set(source_ids):
                self._rows[idx] = row.model_copy(update={"consolidated_into": new_id})
        return new_item

    async def list_purge_candidates(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        min_age_days: int,
        limit: int,
    ) -> list[MemoryItem]:
        cutoff = datetime.now(UTC) - timedelta(days=min_age_days)
        candidates = []
        for row in self._rows:
            if not (
                row.tenant_id == tenant_id
                and row.user_id == user_id
                and row.deleted_at is None
                and row.status == "transient"
                and row.consolidated_into is None
                and row.last_reviewed_at is None
                and row.created_at is not None
                and row.created_at < cutoff
            ):
                continue
            # never retrieved: access_count == 0 (P5a T6 — exact, replaces
            # the old last_used_at <= created_at + 1 minute approximation)
            if row.access_count == 0:
                candidates.append(row)
        candidates.sort(key=lambda r: r.created_at or datetime.min.replace(tzinfo=UTC))
        return candidates[:limit]

    async def mark_reviewed(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
    ) -> bool:
        for idx, row in enumerate(self._rows):
            if (
                row.id == memory_id
                and row.tenant_id == tenant_id
                and row.user_id == user_id
                and row.deleted_at is None
            ):
                self._rows[idx] = row.model_copy(
                    update={"last_reviewed_at": datetime.now(UTC), "review_flagged_at": None}
                )
                return True
        return False

    async def flag_for_review(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        source_thread_id: str,
    ) -> int:
        now = datetime.now(UTC)
        flagged = 0
        for idx, row in enumerate(self._rows):
            if (
                row.tenant_id == tenant_id
                and row.user_id == user_id
                and row.source_thread_id == source_thread_id
                and row.deleted_at is None
                and row.status == "transient"
            ):
                self._rows[idx] = row.model_copy(update={"review_flagged_at": now})
                flagged += 1
        return flagged

    async def list_review_flagged(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        limit: int,
    ) -> list[MemoryItem]:
        rows = [
            r
            for r in self._rows
            if r.tenant_id == tenant_id
            and r.user_id == user_id
            and r.deleted_at is None
            and r.status == "transient"
            and r.review_flagged_at is not None
        ]
        rows.sort(key=lambda r: (r.review_flagged_at, r.id))
        return rows[:limit]

    async def list_due_for_review(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        limit: int,
    ) -> list[MemoryItem]:
        now = datetime.now(UTC)

        def _due_at(row: MemoryItem) -> datetime | None:
            if row.expected_valid_days is None or row.created_at is None:
                return None
            base = row.last_reviewed_at or row.created_at
            return base + timedelta(days=row.expected_valid_days)

        candidates = []
        for row in self._rows:
            if not (
                row.tenant_id == tenant_id
                and row.user_id == user_id
                and row.deleted_at is None
                and row.invalid_at is None
                and row.expired_at is None
                and row.expected_valid_days is not None
            ):
                continue
            due = _due_at(row)
            if due is not None and due <= now:
                candidates.append(row)
        candidates.sort(key=lambda r: _due_at(r) or now)
        return candidates[:limit]

    async def renew_review(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
        expected_valid_days: int | None,
    ) -> bool:
        for idx, row in enumerate(self._rows):
            if (
                row.id == memory_id
                and row.tenant_id == tenant_id
                and row.user_id == user_id
                and row.deleted_at is None
            ):
                self._rows[idx] = row.model_copy(
                    update={
                        "last_reviewed_at": datetime.now(UTC),
                        "expected_valid_days": expected_valid_days,
                    }
                )
                return True
        return False

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
        return sum(1 for r in self._rows if r.tenant_id == tenant_id and r.agent_name == agent_name)

    async def delete_for_agent(self, *, tenant_id: UUID, agent_name: str) -> int:
        kept = [
            r for r in self._rows if not (r.tenant_id == tenant_id and r.agent_name == agent_name)
        ]
        removed = len(self._rows) - len(kept)
        self._rows = kept
        return removed
