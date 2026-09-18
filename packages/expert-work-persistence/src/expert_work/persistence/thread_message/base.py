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
    #: P2 —— 这条消息产生的时刻,来自写入侧盖的 ``expert_work_created_at``
    #: (ISO8601)。``None`` = 盖戳上线之前写入的消息(不回填,见 P2 spec §二)。
    created_at: datetime | None = None
    #: P2 —— 产生这条消息的 run。来自写入侧盖的 ``expert_work_run_id``。
    #: ``None`` 同上。
    run_id: UUID | None = None
    #: P-1 —— 取代这条消息的新 run;``None`` = 未被取代。
    superseded_by: UUID | None = None
    #: P-1 —— 墓碑(正文已清理),此时 ``content == ""``。
    tombstone: bool = False
    #: B-73 ① —— 编排层自己写进检查点的脚手架(B-67「本轮输入」段、CM-1
    #: ``<recovery-advisory>``)。镜像照抄(审计要看得见),**内容搜索**过滤掉。
    hidden: bool = False


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
    async def mark_superseded(
        self,
        *,
        thread_id: UUID,
        tenant_id: UUID,
        seq_from: int,
        seq_to: int,
        superseded_by: UUID,
    ) -> int:
        """把镜像里 ``seq ∈ [seq_from, seq_to)`` 的行标成被 ``superseded_by`` 取代,返回更新行数。

        镜像可能落后于检查点(sweep 还没跑到),更新 0 行不是错误 —— sweep
        之后写入的行也拿不到标记,所以镜像的这一列只是尽力而为的搜索/审计
        辅助,真相永远在检查点。
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

        B-73 ① —— ``hidden`` 的行不参与匹配。镜像本身是忠实的(审计要看得见平台
        脚手架),但搜索是另一回事:B-67 给每个 jinja 线程都贴了一段平台文案,拿它
        当可搜内容的话,搜「清单」「输入」或任意一个变量名会命中所有这类线程 ——
        假阳性,不泄任何租户文本,但把搜索淹掉。
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
