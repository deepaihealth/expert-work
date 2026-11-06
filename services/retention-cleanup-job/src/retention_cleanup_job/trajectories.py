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

一轮最多删 ``limit`` 个(剩下的明天接着删)。列不出来、删不掉的计数,调用方把这个 pass 记成
失败(进程非 0 退出)。认不出的键(不是这个布局的)不动。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
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


@dataclass
class TrajectorySweepTally:
    scanned: int = 0
    expired_removed: int = 0
    orphans_removed: int = 0
    failed: int = 0
    #: 认不出布局的键(不动)。
    unrecognised: int = 0
    #: 到了 ``limit``,剩下的明天删。
    capped: bool = False

    @property
    def needs_attention(self) -> bool:
        return self.failed > 0


async def _existing(
    threads: ThreadMetaStore, tenant_id: UUID, conversation_ids: set[UUID]
) -> set[UUID]:
    ids = sorted(conversation_ids, key=str)
    found: set[UUID] = set()
    for start in range(0, len(ids), _THREAD_CHUNK):
        found |= set(
            await threads.get_many(ids[start : start + _THREAD_CHUNK], tenant_id=tenant_id)
        )
    return found


async def sweep_trajectories(
    *,
    store: ObjectStore,
    threads: ThreadMetaStore,
    retention_days: Mapping[UUID, int | None],
    default_days: int,
    now: datetime,
    limit: int,
    prefix: str = TRAJECTORY_PREFIX,
) -> TrajectorySweepTally:
    """One pass. ``retention_days`` maps tenant → ``upload_retention_days`` (``None`` /
    absent = ``default_days``)."""
    tally = TrajectorySweepTally()
    keys = await store.list_prefix(f"{prefix}/")
    by_tenant: dict[UUID, list[tuple[str, ParsedTrajectoryKey]]] = {}
    for key in keys:
        tally.scanned += 1
        parsed = parse_trajectory_key(key, prefix=prefix)
        if parsed is None:
            tally.unrecognised += 1
            continue
        by_tenant.setdefault(parsed.tenant_id, []).append((key, parsed))

    budget = limit
    for tenant_id, entries in sorted(by_tenant.items(), key=lambda item: str(item[0])):
        days = retention_days.get(tenant_id) or default_days
        cutoff = (now - timedelta(days=days)).date()
        expired = [key for key, parsed in entries if parsed.day < cutoff]
        rest = [(key, parsed) for key, parsed in entries if parsed.day >= cutoff]
        alive = await _existing(threads, tenant_id, {p.conversation_id for _, p in rest})
        orphans = [key for key, parsed in rest if parsed.conversation_id not in alive]
        for bucket, kind in ((expired, "expired"), (orphans, "orphan")):
            for key in bucket:
                if budget <= 0:
                    tally.capped = True
                    return tally
                budget -= 1
                try:
                    await store.delete(key)
                except Exception as exc:
                    tally.failed += 1
                    logger.warning(
                        "retention.trajectory_delete_failed kind=%s error=%s",
                        kind,
                        type(exc).__name__,
                    )
                    continue
                if kind == "expired":
                    tally.expired_removed += 1
                else:
                    tally.orphans_removed += 1
    return tally
