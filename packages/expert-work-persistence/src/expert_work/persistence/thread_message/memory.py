"""In-memory ``ThreadMessageStore`` for unit tests."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from uuid import UUID

from expert_work.persistence.thread_message.base import MessageTurn, ThreadMessageStore


class InMemoryThreadMessageStore(ThreadMessageStore):
    def __init__(self) -> None:
        # (thread_id, seq) -> (tenant_id, turn)
        self._turns: dict[tuple[UUID, int], tuple[UUID, MessageTurn]] = {}
        # thread_id -> (tenant_id, synced_at, message_count)
        self._sync: dict[UUID, tuple[UUID, datetime, int]] = {}

    async def sync_thread(
        self,
        *,
        thread_id: UUID,
        tenant_id: UUID,
        turns: Sequence[MessageTurn],
        synced_at: datetime,
    ) -> None:
        for turn in turns:
            self._turns.setdefault((thread_id, turn.seq), (tenant_id, turn))
        self._sync[thread_id] = (tenant_id, synced_at, len(turns))

    async def mark_superseded(
        self,
        *,
        thread_id: UUID,
        tenant_id: UUID,
        seq_from: int,
        seq_to: int,
        superseded_by: UUID,
    ) -> int:
        updated = 0
        for (tid, seq), (row_tenant, turn) in list(self._turns.items()):
            if tid != thread_id or row_tenant != tenant_id or not (seq_from <= seq < seq_to):
                continue
            self._turns[(tid, seq)] = (row_tenant, replace(turn, superseded_by=superseded_by))
            updated += 1
        return updated

    async def search_thread_ids(
        self,
        *,
        tenant_id: UUID | None,
        q: str,
        limit: int = 500,
    ) -> set[UUID]:
        needle = q.lower()
        out: set[UUID] = set()
        for (thread_id, _seq), (row_tenant, turn) in self._turns.items():
            if tenant_id is not None and row_tenant != tenant_id:
                continue
            if needle in turn.content.lower():
                out.add(thread_id)
                if len(out) >= limit:
                    break
        return out

    async def pending_thread_ids(self, *, limit: int) -> list[tuple[UUID, UUID]]:
        # No thread_meta / agent_run tables to correlate against in-memory —
        # see the base docstring; the SQL backend does the real selection.
        del limit
        return []
