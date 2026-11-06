"""B-280 —— 删除单个对话(``purge_session``)时,挂在这条对话上、按 ``thread_id`` 存的子数据。

这些行都不经 ``thread_meta`` 找(各自带 ``thread_id``),所以对话行删了它们也不会跟着走:

* 反馈、记忆写回失败队列、上传登记、图片 —— 与 B-278 删智能体时按「对话集合」删的是同一批
  store 方法(``agent_purge_steps.purge_conversations``);
* 质量评分、评测候选样本 —— B-278 按智能体整批删,单个对话按对话删(B-280 新加的 store
  方法)。候选样本被采纳进评测集的,评测集本身(租户整理过的资产)不动,同 ``purge_user``。

规矩同 B-278:每类单独执行,一类失败不影响别的类;可重跑;失败只记异常**类型**(消息里可能
带连接串 / 路径,理由见 ``user_purge._step``)。任一类没删干净,调用方就**不删对话行**——
对话行是重跑时找到这条对话的唯一入口(先删子、子删成功才删父)。

图片比 B-278 / ``purge_user`` 严一点:对象存储里的字节没删掉的那几行**留着**(那两处照删行)。
没有任何作业按键扫对象存储里的孤儿图片(留存作业只按 ``image_upload`` 行删),行没了字节就
永远留在桶里;行留着,重跑或留存作业到期都还能删它。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID

from expert_work.persistence.curation import CurationCandidateStore
from expert_work.persistence.feedback_store import FeedbackStore
from expert_work.persistence.image_upload import ImageUploadStore
from expert_work.persistence.memory import MemoryWritebackDLQ
from expert_work.persistence.quality_score import QualityScoreStore
from expert_work.persistence.user_upload import UserUploadStore
from expert_work.runtime.storage import ObjectStore

logger = logging.getLogger("expert_work.control_plane.purge.session")

#: 删除顺序 = 响应 / 审计里的键名顺序。
KINDS: tuple[str, ...] = (
    "feedback",
    "quality_scores",
    "curation_candidates",
    "memory_writeback_dlq",
    "user_uploads",
    "image_uploads",
)


class ImageBytesNotDeletedError(Exception):
    """对象存储里有图片字节没删掉;那几行留着,等重跑。"""


@dataclass(frozen=True)
class SessionPurgeStores:
    feedback: FeedbackStore
    quality_scores: QualityScoreStore
    curation_candidates: CurationCandidateStore
    memory_dlq: MemoryWritebackDLQ
    user_uploads: UserUploadStore
    image_uploads: ImageUploadStore
    #: ``None`` = 没接对象存储:图片行照删(同 B-278),字节无从删起,记 ``image_blobs_skipped``。
    object_store: ObjectStore | None = None


@dataclass
class ThreadChildrenResult:
    deleted: dict[str, int] = field(default_factory=dict)
    #: 类名 → 异常类型名。
    failures: dict[str, str] = field(default_factory=dict)
    image_blobs_removed: int = 0
    image_blobs_failed: int = 0
    image_blobs_skipped: int = 0

    def as_details(self) -> dict[str, object]:
        """``purge_session`` 响应 / 审计里的字段:每类删了几行,失败的加 ``<类>_delete_failed``。"""
        details: dict[str, object] = {kind: self.deleted.get(kind, 0) for kind in KINDS}
        details["image_blobs_removed"] = self.image_blobs_removed
        details["image_blobs_failed"] = self.image_blobs_failed
        details["image_blobs_skipped"] = self.image_blobs_skipped
        for kind in self.failures:
            details[f"{kind}_delete_failed"] = True
        return details


async def _purge_images(
    stores: SessionPurgeStores, result: ThreadChildrenResult, *, tenant_id: UUID, thread_id: UUID
) -> int:
    """先删字节,只删字节已经没了的行;有字节没删掉 → 抛 :class:`ImageBytesNotDeletedError`
    (已删的行数先记下)。"""
    rows = await stores.image_uploads.list_for_threads(tenant_id=tenant_id, thread_ids=[thread_id])
    if stores.object_store is None:
        result.image_blobs_skipped += len(rows)
        return await stores.image_uploads.delete_for_threads(
            tenant_id=tenant_id, thread_ids=[thread_id]
        )
    gone: list[UUID] = []
    for row in rows:
        try:
            await stores.object_store.delete(row.object_key)
        except Exception as exc:
            result.image_blobs_failed += 1
            logger.warning("session_purge.image_blob_failed error=%s", type(exc).__name__)
            continue
        result.image_blobs_removed += 1
        gone.append(row.id)
    # ``list_for_threads`` 是按租户查的,这些 id 都属于本租户。
    removed = await stores.image_uploads.hard_delete(image_ids=gone)
    if len(gone) < len(rows):
        result.deleted["image_uploads"] = removed
        raise ImageBytesNotDeletedError
    return removed


async def purge_thread_children(
    stores: SessionPurgeStores, *, tenant_id: UUID, thread_id: UUID
) -> ThreadChildrenResult:
    """删 ``thread_id`` 名下的 :data:`KINDS`。不抛异常:失败记在 ``failures`` 里。"""
    result = ThreadChildrenResult()
    ids = [thread_id]
    # 可调用对象而不是现成的协程:调用本身同步抛错也落在下面的 ``try`` 里。
    steps: tuple[tuple[str, Callable[[], Awaitable[int]]], ...] = (
        (
            "feedback",
            lambda: stores.feedback.delete_for_threads(tenant_id=tenant_id, thread_ids=ids),
        ),
        (
            "quality_scores",
            lambda: stores.quality_scores.delete_for_threads(tenant_id=tenant_id, thread_ids=ids),
        ),
        (
            "curation_candidates",
            lambda: stores.curation_candidates.delete_for_threads(
                tenant_id=tenant_id, thread_ids=ids
            ),
        ),
        (
            "memory_writeback_dlq",
            lambda: stores.memory_dlq.delete_for_threads(tenant_id=tenant_id, thread_ids=ids),
        ),
        (
            "user_uploads",
            lambda: stores.user_uploads.delete_for_threads(tenant_id=tenant_id, thread_ids=ids),
        ),
        (
            "image_uploads",
            lambda: _purge_images(stores, result, tenant_id=tenant_id, thread_id=thread_id),
        ),
    )
    for kind, step in steps:
        try:
            result.deleted[kind] = await step()
        except Exception as exc:  # 每类独立:一类失败,别的类照删
            logger.warning("session_purge.step_failed step=%s error=%s", kind, type(exc).__name__)
            result.failures[kind] = type(exc).__name__
    return result
