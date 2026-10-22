"""B-209 PR-2(复核 H5 / 安全复核 S-M2)—— 原图对账:删掉文档行已经不在的原图。

删文档 / 删库时会按前缀删原图,入库收尾也会清掉自己那一代或更早的代;但这些都是尽力而为 ——
对象存储当时不可用、进程在中途退出,原图就会留下。原图可能是健康资料,文档删了它必须跟着删,
所以这里定期扫一遍 ``knowledge_figures/`` 前缀(只有原图,见 :mod:`~.figure_storage`),
按文档 id 去库里查(跨租户,绕过 RLS —— 同恢复任务),文档行已经不在的,它的原图全部删掉。

文档行在任何原图写入之前就已存在(上传先建行,再排入库),所以「行不在」只可能是删了;不需要
按对象的年龄等一等。键名对不上格式的对象不碰。每个副本都可以跑:删除是幂等的。
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from control_plane.knowledge.recognition.figure_storage import FigureStorage, parse_key
from expert_work.common.observability import expert_work_counter
from expert_work.persistence import KnowledgeStore
from expert_work.persistence.rls import bypass_rls_var, current_tenant_id_var

logger = logging.getLogger(__name__)

#: Document ids looked up per query.
_LOOKUP_BATCH = 500

_deleted = expert_work_counter(
    "expert_work_knowledge_figure_orphans_deleted_total",
    "Original figures deleted because their document no longer exists.",
)


class FigureReconciler:
    """Periodically deletes original figures whose document is gone."""

    def __init__(self, *, store: KnowledgeStore, storage: FigureStorage, interval_s: float) -> None:
        if interval_s <= 0:
            msg = "interval_s must be positive"
            raise ValueError(msg)
        self._store = store
        self._storage = storage
        self._interval_s = interval_s
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

    async def run_once(self) -> int:
        """One pass; the number of originals deleted."""
        keys_by_document: dict[UUID, list[str]] = {}
        for key in await self._storage.all_keys():
            parsed = parse_key(key)
            if parsed is not None:
                keys_by_document.setdefault(parsed.document_id, []).append(key)
        if not keys_by_document:
            return 0
        existing = await self._existing(list(keys_by_document))
        orphans = [
            key
            for document_id, keys in keys_by_document.items()
            if document_id not in existing
            for key in keys
        ]
        deleted = await self._storage.delete_keys(orphans)
        if deleted:
            _deleted.inc(deleted)
            logger.info("knowledge.figure_orphans_deleted count=%d", deleted)
        return deleted

    async def _existing(self, document_ids: list[UUID]) -> set[UUID]:
        # Cross-tenant by design — bypass RLS (mirrors the recovery worker).
        bypass = bypass_rls_var.set(True)
        tenant = current_tenant_id_var.set(None)
        try:
            found: set[UUID] = set()
            for start in range(0, len(document_ids), _LOOKUP_BATCH):
                found |= await self._store.existing_document_ids(
                    document_ids=document_ids[start : start + _LOOKUP_BATCH]
                )
            return found
        finally:
            current_tenant_id_var.reset(tenant)
            bypass_rls_var.reset(bypass)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("knowledge.figure_reconcile_failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
            except TimeoutError:
                pass  # normal periodic wake-up
