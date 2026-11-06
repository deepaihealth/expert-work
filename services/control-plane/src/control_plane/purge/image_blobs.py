"""删图片:先删对象存储里的字节,只删字节已经没了的 ``image_upload`` 行。

三处彻底删除共用(删单个对话 ``session_purge``、删智能体 ``agent_purge_steps``、删用户
``user_purge``)。字节删不掉的那几行**留着**:没有任何作业按键扫对象存储里的孤儿图片 ——
留存作业只按 ``image_upload`` 行删(``RetentionCleanupJob._delete_expired_images``)—— 行一没,
字节就永远留在桶里;行留着,重跑或留存作业到期都还能删它。调用方把
:class:`ImageBytesNotDeletedError` 当作这一步失败(重跑接着删)。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from expert_work.persistence.image_upload import ImageUploadStore
from expert_work.protocol import ImageUpload
from expert_work.runtime.storage import ObjectStore

logger = logging.getLogger("expert_work.control_plane.purge.images")


class ImageBytesNotDeletedError(Exception):
    """对象存储里有图片字节没删掉;那几行留着,等重跑。"""


@dataclass
class ImageTally:
    rows_deleted: int = 0
    blobs_removed: int = 0
    blobs_failed: int = 0
    #: 没接对象存储:行照删(字节无从删起)。
    blobs_skipped: int = 0
    #: 字节没删掉、行留着的图片 id(只是 id,不含对象键)。
    kept_ids: list[UUID] = field(default_factory=list)


async def delete_images(
    images: ImageUploadStore,
    object_store: ObjectStore | None,
    rows: Sequence[ImageUpload],
    tally: ImageTally,
) -> None:
    """删 ``rows`` 的字节与行,计数记进 ``tally``;有字节没删掉 → 那几行留着,抛
    :class:`ImageBytesNotDeletedError`(已删的行先记进 ``tally``)。

    ``rows`` 必须是调用方按租户查出来的(``hard_delete`` 按 id 删,不再带租户条件)。"""
    gone: list[UUID] = []
    if object_store is None:
        tally.blobs_skipped += len(rows)
        gone = [row.id for row in rows]
    else:
        for row in rows:
            try:
                await object_store.delete(row.object_key)
            except Exception as exc:
                tally.blobs_failed += 1
                tally.kept_ids.append(row.id)
                logger.warning("purge.image_blob_failed error=%s", type(exc).__name__)
                continue
            tally.blobs_removed += 1
            gone.append(row.id)
    tally.rows_deleted += await images.hard_delete(image_ids=gone)
    if len(gone) < len(rows):
        raise ImageBytesNotDeletedError
