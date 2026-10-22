"""B-209 PR-2(复核 H5 / 安全复核 S-M2,二轮复核 H1)—— 原图对账:删掉文档行已经不在的原图。

删文档 / 删库时会按前缀删原图,入库收尾也会清掉自己那一代或更早的代;但这些都是尽力而为 ——
对象存储当时不可用、进程在中途退出,原图就会留下。原图可能是健康资料,文档删了它必须跟着删,
所以这里定期扫一遍 ``knowledge_figures/`` 前缀(只有原图,见 :mod:`~.figure_storage`),
按文档 id 去库里查(跨租户,绕过 RLS),文档行已经不在的原图删掉。

「库里查不到」只有在**证明查得到所有租户**时才可信,否则就是把所有租户的原图一次删光。所以:

* **默认只演练**:数出来、记日志和指标,不删;设置 ``knowledge_figure_reconcile_delete`` 打开才删。
* 只跟 SQL 库一起启动(app 里接线);库要先证明这次查询绕得过行级安全
  (:meth:`~expert_work.persistence.KnowledgeStore.sees_every_tenant`),证明不了就放弃这一轮。
* 桶里有原图、库里一个文档都没查到 → 放弃(库不对、表空了,不是「全删了」)。
* 至少看到 :data:`MIN_DOCUMENTS_FOR_SHARE` 个文档时,孤儿文档占比超过 :data:`MAX_ORPHAN_SHARE`
  → 放弃。正常的孤儿只来自少数删除时对象存储恰好不可用的文档;过半就是查询坏了。少于这个数时
  占比太抖,只靠上一条兜底。
* 只动最后修改时间早于 :data:`MIN_AGE` 的原图(刚写的不碰);每轮最多删 :data:`MAX_DELETES_PER_PASS`
  个,剩下的下一轮。
* 启动后先等一个周期再跑第一轮(滚动发布时不会每个新副本一起来一遍)。

放弃时记警告和 ``expert_work_knowledge_figure_reconcile_aborted_total{reason}``。
键名对不上格式的对象不碰。每个副本都可以跑:删除是幂等的。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from control_plane.knowledge.recognition.figure_storage import FigureStorage, parse_key
from expert_work.common.observability import expert_work_counter, expert_work_gauge
from expert_work.persistence import KnowledgeStore
from expert_work.persistence.knowledge import SqlKnowledgeStore
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

_deleted = expert_work_counter(
    "expert_work_knowledge_figure_orphans_deleted_total",
    "Original figures deleted because their document no longer exists.",
)
_orphans = expert_work_gauge(
    "expert_work_knowledge_figure_orphans",
    "Original figures the last reconcile pass found without a document (old enough to delete).",
)
_aborted = expert_work_counter(
    "expert_work_knowledge_figure_reconcile_aborted_total",
    "Reconcile passes given up because the document lookup could not be trusted.",
    ["reason"],
)


def build_figure_reconciler(
    *, store: KnowledgeStore, storage: FigureStorage | None, interval_s: float, delete: bool
) -> FigureReconciler | None:
    """The reconciler, for a durable object store and the SQL document store
    only — "no such document" from any other store proves nothing."""
    if storage is None or not isinstance(store, SqlKnowledgeStore):
        return None
    return FigureReconciler(store=store, storage=storage, interval_s=interval_s, delete=delete)


@dataclass(frozen=True)
class ReconcileOutcome:
    """One pass: the orphans found (old enough to act on), how many were
    deleted, and why the pass gave up (``None``: it did not)."""

    orphans: int = 0
    deleted: int = 0
    aborted: str | None = None


class FigureReconciler:
    """Periodically deletes original figures whose document is gone (see
    module doc). ``delete`` off = dry run."""

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        storage: FigureStorage,
        interval_s: float,
        delete: bool = False,
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
        """One pass (see module doc)."""
        listed = await self._storage.all_keys_modified()
        if listed is None:
            return self._abort("no_modification_times")
        keys_by_document: dict[UUID, list[tuple[str, datetime]]] = {}
        for key, modified in listed:
            parsed = parse_key(key)
            if parsed is not None:
                keys_by_document.setdefault(parsed.document_id, []).append((key, modified))
        if not keys_by_document:
            return ReconcileOutcome()
        bypass = bypass_rls_var.set(True)
        tenant = current_tenant_id_var.set(None)
        try:
            if not await self._store.sees_every_tenant():
                return self._abort("rls_not_bypassed")
            existing = await self._existing(list(keys_by_document))
        finally:
            current_tenant_id_var.reset(tenant)
            bypass_rls_var.reset(bypass)
        if not existing:
            return self._abort("no_documents_found")
        orphaned = [d for d in keys_by_document if d not in existing]
        seen = len(keys_by_document)
        if seen >= MIN_DOCUMENTS_FOR_SHARE and len(orphaned) / seen > MAX_ORPHAN_SHARE:
            return self._abort("orphan_share")
        cutoff = self._now() - MIN_AGE
        orphans = [
            key
            for document_id in orphaned
            for key, modified in keys_by_document[document_id]
            if modified < cutoff
        ]
        _orphans.set(len(orphans))
        if not orphans:
            return ReconcileOutcome()
        if not self._delete:
            logger.info("knowledge.figure_orphans_found count=%d dry_run=true", len(orphans))
            return ReconcileOutcome(orphans=len(orphans))
        deleted = await self._storage.delete_keys(orphans[:MAX_DELETES_PER_PASS])
        _deleted.inc(deleted)
        logger.info(
            "knowledge.figure_orphans_deleted count=%d remaining=%d",
            deleted,
            len(orphans) - deleted,
        )
        return ReconcileOutcome(orphans=len(orphans), deleted=deleted)

    def _abort(self, reason: str) -> ReconcileOutcome:
        _aborted.labels(reason=reason).inc()
        logger.warning("knowledge.figure_reconcile_aborted reason=%s", reason)
        return ReconcileOutcome(aborted=reason)

    async def _existing(self, document_ids: list[UUID]) -> set[UUID]:
        found: set[UUID] = set()
        for start in range(0, len(document_ids), _LOOKUP_BATCH):
            found |= await self._store.existing_document_ids(
                document_ids=document_ids[start : start + _LOOKUP_BATCH]
            )
        return found

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
