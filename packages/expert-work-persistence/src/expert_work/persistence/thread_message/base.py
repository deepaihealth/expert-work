"""Abstract ``ThreadMessageStore`` — the conversation transcript mirror (IA M4).

Message history lives in LangGraph's ``checkpoints`` blob (no SQL pushdown,
no tenant RLS). The ``TranscriptMirrorSweep`` copies user/assistant text
turns into ``thread_message`` so the conversation browser's content search
runs as an indexed, RLS-scoped query.

Implementations:
- :class:`expert_work.persistence.thread_message.memory.InMemoryThreadMessageStore`
- :class:`expert_work.persistence.thread_message.sql.SqlThreadMessageStore`
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class MessageTurn:
    """One user/assistant text turn extracted from a thread's checkpoint.

    ``seq`` is the message's index in the checkpoint's append-only
    ``messages`` channel — stable across reads (``add_messages`` reducer),
    so mirror writes are idempotent on ``(thread_id, seq)``. Non-text turns
    (tool/system) are skipped at extraction, leaving gaps in ``seq``.
    """

    seq: int
    role: str  # "user" | "assistant"
    content: str
    #: Structural output channel for assistant turns — "final" (last turn of
    #: its user-delimited segment AND no tool_calls) or "commentary"
    #: (everything else); always None for user turns. See
    #: docs/superpowers/specs/2026-07-30-conversation-output-channels-design.md.
    channel: str | None = None


class ThreadMessageStore(abc.ABC):
    """Transcript mirror repository + its sweep watermark."""

    @abc.abstractmethod
    async def sync_thread(
        self,
        *,
        thread_id: UUID,
        tenant_id: UUID,
        turns: Sequence[MessageTurn],
        synced_at: datetime,
    ) -> None:
        """Mirror a thread's turns and advance its watermark.

        Turn writes are ``ON CONFLICT (thread_id, seq) DO NOTHING`` — the
        checkpoint channel is append-only, so previously mirrored turns
        never change. The watermark row is upserted (``synced_at`` +
        ``message_count``) even when ``turns`` is empty, so an empty
        conversation leaves the backfill queue.
        """

    @abc.abstractmethod
    async def search_thread_ids(
        self,
        *,
        tenant_id: UUID | None,
        q: str,
        limit: int = 500,
    ) -> set[UUID]:
        """Distinct ``thread_id``s with ≥1 turn containing ``q`` (case-
        insensitive substring, LIKE wildcards escaped — same semantics as
        the title search). Feeds the browser's ``q`` filter through the
        same thread-id-set composition as ``thread_ids_with_runs``.
        ``tenant_id=None`` is the cross-tenant aggregate — the caller MUST
        wrap it in ``bypass_rls_session()`` (Stream N contract). ``limit``
        caps the set (surfaced via ``X-Limit-Capped``).
        """

    @abc.abstractmethod
    async def pending_thread_ids(self, *, limit: int) -> list[tuple[UUID, UUID]]:
        """``(thread_id, tenant_id)`` pairs whose mirror is stale — the
        sweep's work queue.

        Stale = no watermark row yet (backfill: converges as the sweep
        writes watermarks) OR ≥1 ``agent_run`` updated after ``synced_at``
        (new activity). Platform-level read across tenants — the caller
        MUST wrap it in ``bypass_rls_session()``. The in-memory backend
        has no thread/run tables to correlate against and returns ``[]``
        (same caveat as ``ThreadMetaStore``'s ``nonempty``); the sweep's
        selection logic is exercised against the SQL backend.
        """
