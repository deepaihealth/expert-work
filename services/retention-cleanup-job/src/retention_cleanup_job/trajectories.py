"""B-285 —— 轨迹对象(L7 trajectory)的留存规则。

每个结束的 run 在对象存储里写一个对象,内容是整段对话(键见
:mod:`expert_work.protocol.trajectory_keys`)。此前没有任何东西删它们:三处彻底删除不删,
留存不删,仓库里也没有桶生命周期。B-285 起彻底删除按 run 推出日期当场删;这里每晚补两条:

* **到期**:对象日期(键里的 ``YYYY/MM/DD``,写对象那天)早于今天减去租户的
  ``tenant_config.upload_retention_days``(没设用平台默认,90)。用这个保留期的理由:轨迹是
  用户发来的内容(连同回复)的逐字副本,与上传文件同属「用户交给我们的对话内容」;租户为隐私
  把上传保留期调短时,这份副本应当一起变短。它可在 7 到 365 天之间按租户调,默认与图片 / 产物
  的 90 天一致。
* **孤儿**:对象所属的对话(对话本身的 run 是 ``thread``,子智能体的 run 是键里的
  ``conversation`` 段)在 ``thread_meta`` 里已经没有行 —— 本修复之前删掉的对话、删除后才收尾
  的 run 写的对象、B-285 之前平铺写的子智能体对象(它们的 ``thread`` 从来没有行,找不到所属
  对话)。

**依赖 B-286**:部署环境里留存作业现在连不上真对象存储(``memory``,解析不了控制面的 vault
引用),作业层面这一条会记 ``skipped``(不是「没东西可删」)。

按页列(对象存储的分页列举,一页一批):每页里认得出的键按租户分组,到期的直接删、其余的
一页一次批量查 ``thread_meta``(按租户、每次最多 :data:`_THREAD_CHUNK` 个)。删除预算
``limit``(只算删成功的)用完就停止往下列;删失败不占预算,另有 :data:`MAX_FAILURES` 上限 ——
到了就停、记失败,不会因为对象存储整片出错把整晚耗在重试上。认不出的键不动。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.protocol.trajectory_keys import (
    TRAJECTORY_PREFIX,
    ParsedTrajectoryKey,
    parse_trajectory_key,
)
from expert_work.runtime.storage import ObjectStore

logger = logging.getLogger(__name__)

#: 一次 ``get_many`` 查多少条对话。
_THREAD_CHUNK = 500
#: 一轮里删失败到这么多个就停(记失败);删失败不占删除预算。
MAX_FAILURES = 50


@dataclass
class TrajectorySweepTally:
    #: 列出来看过的键数。
    scanned: int = 0
    expired_removed: int = 0
    orphans_removed: int = 0
    failed: int = 0
    #: 认不出布局的键(不动)。
    unrecognised: int = 0
    #: 删除预算用完,剩下的明天接着删。
    capped: bool = False
    #: 删失败到了 :data:`MAX_FAILURES`,这一轮提前停了。
    aborted: bool = False
    #: 这一轮没跑(作业没接对象存储或 ``ThreadMetaStore``)—— 与「跑了、没东西可删」区分。
    skipped: bool = False

    @property
    def needs_attention(self) -> bool:
        return self.failed > 0 or self.aborted


async def _pages(store: ObjectStore, prefix: str) -> AsyncGenerator[list[str], None]:
    """Keys under ``prefix`` one listing page at a time (``iter_prefix_modified``
    on the S3 and in-memory stores); a store without it is listed in one go."""
    pager: Any = getattr(store, "iter_prefix_modified", None)
    if pager is None:
        yield await store.list_prefix(prefix)
        return
    async for page in pager(prefix):
        yield [key for key, _modified in page]


class _Sweep:
    def __init__(
        self,
        *,
        store: ObjectStore,
        threads: ThreadMetaStore,
        retention_days: Mapping[UUID, int | None],
        default_days: int,
        now: datetime,
        limit: int,
        max_failures: int,
        prefix: str,
    ) -> None:
        self.store = store
        self.threads = threads
        self.retention_days = retention_days
        self.default_days = default_days
        self.now = now
        self.budget = limit
        self.max_failures = max_failures
        self.prefix = prefix
        self.tally = TrajectorySweepTally()

    @property
    def done(self) -> bool:
        return self.tally.capped or self.tally.aborted

    async def _alive(self, tenant_id: UUID, ids: set[UUID]) -> set[UUID]:
        ordered = sorted(ids, key=str)
        found: set[UUID] = set()
        for start in range(0, len(ordered), _THREAD_CHUNK):
            chunk = ordered[start : start + _THREAD_CHUNK]
            found |= set(await self.threads.get_many(chunk, tenant_id=tenant_id))
        return found

    async def _delete(self, key: str, kind: str) -> None:
        if self.budget <= 0:
            self.tally.capped = True
            return
        try:
            await self.store.delete(key)
        except Exception as exc:
            self.tally.failed += 1
            logger.warning(
                "retention.trajectory_delete_failed kind=%s error=%s", kind, type(exc).__name__
            )
            if self.tally.failed >= self.max_failures:
                self.tally.aborted = True
            return
        self.budget -= 1
        if self.budget <= 0:
            self.tally.capped = True  # budget spent: stop listing, the rest is tomorrow's
        if kind == "expired":
            self.tally.expired_removed += 1
        else:
            self.tally.orphans_removed += 1

    async def page(self, keys: list[str]) -> None:
        by_tenant: dict[UUID, list[tuple[str, ParsedTrajectoryKey]]] = {}
        for key in keys:
            self.tally.scanned += 1
            parsed = parse_trajectory_key(key, prefix=self.prefix)
            if parsed is None:
                self.tally.unrecognised += 1
                continue
            by_tenant.setdefault(parsed.tenant_id, []).append((key, parsed))
        for tenant_id, entries in by_tenant.items():
            days = self.retention_days.get(tenant_id) or self.default_days
            cutoff = (self.now - timedelta(days=days)).date()
            rest = []
            for key, parsed in entries:
                if parsed.day < cutoff:
                    await self._delete(key, "expired")
                    if self.done:
                        return
                else:
                    rest.append((key, parsed))
            if not rest:
                continue
            alive = await self._alive(tenant_id, {p.conversation_id for _, p in rest})
            for key, parsed in rest:
                if parsed.conversation_id in alive:
                    continue
                await self._delete(key, "orphan")
                if self.done:
                    return


async def sweep_trajectories(
    *,
    store: ObjectStore,
    threads: ThreadMetaStore,
    retention_days: Mapping[UUID, int | None],
    default_days: int,
    now: datetime,
    limit: int,
    max_failures: int = MAX_FAILURES,
    prefix: str = TRAJECTORY_PREFIX,
) -> TrajectorySweepTally:
    """One pass. ``retention_days`` maps tenant → ``upload_retention_days`` (``None`` /
    absent = ``default_days``). Stops listing once ``limit`` objects were deleted or
    ``max_failures`` deletes failed."""
    sweep = _Sweep(
        store=store,
        threads=threads,
        retention_days=retention_days,
        default_days=default_days,
        now=now,
        limit=limit,
        max_failures=max_failures,
        prefix=prefix,
    )
    pages = _pages(store, f"{prefix}/")
    try:
        async for keys in pages:
            await sweep.page(keys)
            if sweep.done:
                break
    finally:
        await pages.aclose()
    if sweep.tally.capped:
        logger.info("retention.trajectories_capped limit=%d scanned=%d", limit, sweep.tally.scanned)
    return sweep.tally
