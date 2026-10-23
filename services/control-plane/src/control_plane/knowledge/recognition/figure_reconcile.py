"""B-209 PR-2(复核 H5 / 安全复核 S-M2,二轮复核 H1,四轮复核)—— 原图对账:删掉文档行已经不在的原图。

删文档 / 删库时会按前缀删原图,入库收尾也会清掉自己那一代或更早的代;但这些都是尽力而为 ——
对象存储当时不可用、进程在中途退出,原图就会留下。原图可能是健康资料,文档删了它必须跟着删,
所以这里定期扫一遍 ``knowledge_figures/`` 前缀(只有原图,见 :mod:`~.figure_storage`),
按文档 id 去库里查(跨租户,绕过 RLS),文档行已经不在的原图删掉。

「库里查不到」只有在**证明查得到所有租户**时才可信,否则就是把所有租户的原图一次删光。所以:

* **默认只演练**:数出来、记日志和指标,不删;设置 ``knowledge_figure_reconcile_delete`` 打开才删。
* 只跟 SQL 库一起启动(app 里接线);库要先证明这次查询绕得过行级安全
  (:meth:`~expert_work.persistence.KnowledgeStore.sees_every_tenant`),而且每次按 id 查文档时在
  **同一个事务**里再核一遍,行级安全生效就报错(``RowSecurityActiveError``)—— 这两处任何一处
  不成立就放弃这一轮;按 id 查到一半出错也放弃(查不到不等于不存在)。
* 桶里有原图、库里一个文档都没查到 → 放弃(库不对、表空了,不是「全删了」)。
* 至少看到 :data:`MIN_DOCUMENTS_FOR_SHARE` 个文档时,孤儿文档占比超过 :data:`MAX_ORPHAN_SHARE`
  → 放弃。正常的孤儿只来自少数删除时对象存储恰好不可用的文档;过半就是查询坏了。少于这个数时
  占比太抖,只靠上一条兜底。运维确认确实是大批删除留下的孤儿时,可以打开
  ``knowledge_figure_reconcile_ignore_orphan_share`` 放过这一条(默认关)。
* 只动最后修改时间早于 :data:`MIN_AGE` 的原图(刚写的不碰);每轮最多删 :data:`MAX_DELETES_PER_PASS`
  个,剩下的下一轮。
* 启动后先等一个周期再跑第一轮;多个副本之间用 Postgres 事务级 advisory 锁单飞
  (``FIGURE_RECONCILE_LOCK_CLASSID``),没抢到锁的副本这一轮什么都不做。锁跟着一个一直开着的事务:
  持锁的节点要是整个死掉,锁要等 TCP keepalive 断开或空闲事务超时(这里设 6 小时)才放,这期间
  对账照样不跑 —— 不会出错,只是晚。
* 打开 ``knowledge_figure_reconcile_ignore_orphan_share`` 期间,每一轮都记警告,指标
  ``expert_work_knowledge_figure_reconcile_orphan_share_override`` 为 1。

**内存**:列桶按页流式读,每个文档只记它的租户 / 知识库和「有没有一天前写的原图」,不把所有键攒在
内存里;演练只报孤儿文档数,不重新列;真删时按文档前缀重新列,攒够本轮配额(每轮最多删的个数)
就停。

放弃时记警告和 ``expert_work_knowledge_figure_reconcile_aborted_total{reason}``。
键名对不上格式的对象不碰。删除是幂等的。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.advisory_locks import FIGURE_RECONCILE_LOCK_CLASSID
from control_plane.knowledge.recognition.figure_storage import FigureStorage, parse_key
from expert_work.common.observability import expert_work_counter, expert_work_gauge
from expert_work.persistence import KnowledgeStore
from expert_work.persistence.knowledge import SqlKnowledgeStore
from expert_work.persistence.knowledge.base import RowSecurityActiveError
from expert_work.persistence.rls import bypass_rls_var, current_tenant_id_var

logger = logging.getLogger(__name__)

#: Document ids looked up per query.
_LOOKUP_BATCH = 500
#: Only originals last written longer ago than this are touched.
MIN_AGE = timedelta(hours=24)
#: The orphan share a pass may act on (of the documents seen in the bucket) …
MAX_ORPHAN_SHARE = 0.5
#: … once at least this many documents are seen (below it the share is noise).
MIN_DOCUMENTS_FOR_SHARE = 20
#: Originals deleted per pass at most (the rest wait for the next pass).
MAX_DELETES_PER_PASS = 1000
#: The single-flight lock transaction stays open for a whole pass.
_LOCK_TXN_TIMEOUT_MS = 6 * 60 * 60 * 1000
_LOCK_KEY = "figure_reconcile"

_deleted = expert_work_counter(
    "expert_work_knowledge_figure_orphans_deleted_total",
    "Original figures deleted because their document no longer exists.",
)
_orphans = expert_work_gauge(
    "expert_work_knowledge_figure_orphans",
    "Documents the last reconcile pass found gone with originals old enough to delete.",
)
_override = expert_work_gauge(
    "expert_work_knowledge_figure_reconcile_orphan_share_override",
    "1 while the reconciler runs with its orphan-share guard switched off.",
)
_aborted = expert_work_counter(
    "expert_work_knowledge_figure_reconcile_aborted_total",
    "Reconcile passes given up because the document lookup could not be trusted.",
    ["reason"],
)


def build_figure_reconciler(
    *,
    store: KnowledgeStore,
    storage: FigureStorage | None,
    interval_s: float,
    delete: bool,
    ignore_orphan_share: bool = False,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> FigureReconciler | None:
    """The reconciler, for a durable object store and the SQL document store
    only — "no such document" from any other store proves nothing."""
    if storage is None or not isinstance(store, SqlKnowledgeStore):
        return None
    return FigureReconciler(
        store=store,
        storage=storage,
        interval_s=interval_s,
        delete=delete,
        ignore_orphan_share=ignore_orphan_share,
        session_factory=session_factory,
    )


@dataclass(frozen=True)
class ReconcileOutcome:
    """One pass: the orphans found (old enough to act on), how many were
    deleted, why the pass gave up (``None``: it did not), and whether another
    replica held the pass (``skipped``)."""

    orphans: int = 0
    deleted: int = 0
    aborted: str | None = None
    skipped: bool = False


@dataclass
class _Seen:
    """What the bucket listing keeps per document."""

    tenant_id: UUID
    kb_id: UUID
    old: bool  # holds an original older than MIN_AGE


class _AbortError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class FigureReconciler:
    """Periodically deletes original figures whose document is gone (see
    module doc). ``delete`` off = dry run; ``session_factory`` (the SQL
    stack) makes a pass single-flight across replicas."""

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        storage: FigureStorage,
        interval_s: float,
        delete: bool = False,
        ignore_orphan_share: bool = False,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        initial_delay_s: float | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if interval_s <= 0:
            msg = "interval_s must be positive"
            raise ValueError(msg)
        self._store = store
        self._storage = storage
        self._interval_s = interval_s
        self._delete = delete
        self._ignore_orphan_share = ignore_orphan_share
        self._session_factory = session_factory
        self._initial_delay_s = interval_s if initial_delay_s is None else initial_delay_s
        self._now = now
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        """Schedule the periodic loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="knowledge-figure-reconcile")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except (TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        finally:
            self._task = None

    async def run_once(self) -> ReconcileOutcome:
        """One pass (see module doc), single-flight across replicas."""
        if self._session_factory is None:
            return await self._pass()
        async with self._session_factory() as lock_session:
            bypass = bypass_rls_var.set(True)
            tenant = current_tenant_id_var.set(None)
            try:
                await lock_session.execute(
                    text(f"SET LOCAL idle_in_transaction_session_timeout = {_LOCK_TXN_TIMEOUT_MS}")
                )
                got = (
                    await lock_session.execute(
                        text("SELECT pg_try_advisory_xact_lock(:cid, hashtext(:k))"),
                        {"cid": FIGURE_RECONCILE_LOCK_CLASSID, "k": _LOCK_KEY},
                    )
                ).scalar_one()
            finally:
                current_tenant_id_var.reset(tenant)
                bypass_rls_var.reset(bypass)
            try:
                return await self._pass() if got else ReconcileOutcome(skipped=True)
            finally:
                await lock_session.rollback()  # ends the txn: releases the lock

    async def _pass(self) -> ReconcileOutcome:
        if not self._storage.can_list_times():
            return self._abort("no_modification_times")
        seen = await self._listing()
        if not seen:
            return ReconcileOutcome()
        try:
            existing = await self._existing(list(seen))
        except _AbortError as abort:
            return self._abort(abort.reason)
        if not existing:
            return self._abort("no_documents_found")
        orphaned = [document_id for document_id in seen if document_id not in existing]
        _override.set(1 if self._ignore_orphan_share else 0)
        if self._ignore_orphan_share:
            logger.warning(
                "knowledge.figure_reconcile_orphan_share_override documents=%d orphaned=%d",
                len(seen),
                len(orphaned),
            )
        share_counts = len(seen) >= MIN_DOCUMENTS_FOR_SHARE and not self._ignore_orphan_share
        if share_counts and len(orphaned) / len(seen) > MAX_ORPHAN_SHARE:
            return self._abort("orphan_share")
        documents = [document_id for document_id in orphaned if seen[document_id].old]
        _orphans.set(len(documents))
        if not documents:
            return ReconcileOutcome()
        if not self._delete:
            logger.info("knowledge.figure_orphans_found documents=%d dry_run=true", len(documents))
            return ReconcileOutcome(orphans=len(documents))
        keys = await self._old_keys(documents, seen)
        deleted = await self._storage.delete_keys(keys)
        _deleted.inc(deleted)
        logger.info(
            "knowledge.figure_orphans_deleted count=%d documents=%d", deleted, len(documents)
        )
        return ReconcileOutcome(orphans=len(documents), deleted=deleted)

    async def _listing(self) -> dict[UUID, _Seen]:
        """The bucket, page by page, folded into one small record per document."""
        cutoff = self._now() - MIN_AGE
        seen: dict[UUID, _Seen] = {}
        async for page in self._storage.key_pages():
            for key, modified in page:
                parsed = parse_key(key)
                if parsed is None:
                    continue
                record = seen.get(parsed.document_id)
                if record is None:
                    record = seen[parsed.document_id] = _Seen(parsed.tenant_id, parsed.kb_id, False)
                record.old = record.old or modified < cutoff
        return seen

    async def _old_keys(self, documents: list[UUID], seen: dict[UUID, _Seen]) -> list[str]:
        """Up to :data:`MAX_DELETES_PER_PASS` originals older than MIN_AGE of
        the orphaned documents, each document listed again now (a key written
        since the scan is not swept up); listing stops once the pass's quota
        is reached — the rest wait for the next pass."""
        cutoff = self._now() - MIN_AGE
        keys: list[str] = []
        for document_id in documents:
            if len(keys) >= MAX_DELETES_PER_PASS:
                break
            record = seen[document_id]
            listed = await self._storage.document_keys_modified(
                tenant_id=record.tenant_id, kb_id=record.kb_id, document_id=document_id
            )
            keys.extend(key for key, modified in listed if modified < cutoff)
        return keys[:MAX_DELETES_PER_PASS]

    def _abort(self, reason: str) -> ReconcileOutcome:
        _aborted.labels(reason=reason).inc()
        logger.warning("knowledge.figure_reconcile_aborted reason=%s", reason)
        return ReconcileOutcome(aborted=reason)

    async def _existing(self, document_ids: list[UUID]) -> set[UUID]:
        """Which documents exist, looked up across tenants — or :class:`_AbortError`
        when the lookup cannot be trusted (row security on, a batch failed)."""
        bypass = bypass_rls_var.set(True)
        tenant = current_tenant_id_var.set(None)
        try:
            if not await self._store.sees_every_tenant():
                raise _AbortError("rls_not_bypassed")
            found: set[UUID] = set()
            for start in range(0, len(document_ids), _LOOKUP_BATCH):
                found |= await self._store.existing_document_ids(
                    document_ids=document_ids[start : start + _LOOKUP_BATCH]
                )
            return found
        except RowSecurityActiveError as exc:
            raise _AbortError("rls_not_bypassed") from exc
        except _AbortError:
            raise
        except Exception as exc:
            logger.warning("knowledge.figure_reconcile_lookup_failed", exc_info=True)
            raise _AbortError("lookup_failed") from exc
        finally:
            current_tenant_id_var.reset(tenant)
            bypass_rls_var.reset(bypass)

    async def _loop(self) -> None:
        delay = self._initial_delay_s
        while not await self._stopped_within(delay):
            try:
                await self.run_once()
            except Exception:
                logger.exception("knowledge.figure_reconcile_failed")
            delay = self._interval_s

    async def _stopped_within(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            return False
        return True
