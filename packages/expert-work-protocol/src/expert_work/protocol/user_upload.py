"""对外附件登记记录 + 对外 upload_id 的渲染 / 解析(spec 2026-08-17)。

第三方对接方上传文档或图片后拿到的一个 `upl_<uuid>` id,原样回传用于发起
对话 / 下载,不需要也不应该解析出内部格式。``UserUpload`` 是该 id 背后
登记行的 DTO(表 ``user_upload``,迁移 0146);``render_upload_id`` /
``parse_upload_id`` 是这枚 id 与 UUID 互转的唯一实现,供上传端点渲染、run
请求解析、下载端点解析三处共用。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

__all__ = [
    "UPLOAD_ID_PREFIX",
    "UserUpload",
    "UserUploadKind",
    "is_session_deleted_tombstone",
    "parse_upload_id",
    "render_upload_id",
]

UserUploadKind = Literal["image", "document"]

UPLOAD_ID_PREFIX: Final = "upl_"

# Lowercase-only, hyphenated UUID body — matches what render_upload_id emits.
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class UserUpload(BaseModel):
    """One row of ``user_upload`` — one landed document or image, uniform id.

    ``ref`` carries the underlying storage location, whose shape differs by
    ``kind`` (image: ``expert_work://image/…`` URI; document: workspace-
    relative ``uploads/<name>``) — third parties never see it, only the
    opaque ``upl_<uuid>`` id.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    user_id: UUID
    thread_id: UUID
    kind: UserUploadKind
    ref: str
    mime_type: str
    size_bytes: int
    filename: str
    created_at: datetime
    deleted_at: datetime | None = None
    #: B-271 —— 落地字节的 sha256(十六进制),上传时写入;列加上之前的老行为 ``None``。
    sha256: str | None = None
    #: B-63 —— 最后一次被重新附加到 run、或经对外接口下载的时间(每行每天最多记一次);
    #: 列加上之前没用过的行为 ``None``。保留期从 ``created_at`` / 这个时间 / 会话最后一次
    #: run 三者中最晚的那个算起。
    last_used_at: datetime | None = None


def render_upload_id(upload_id: UUID) -> str:
    """Render the internal id as the external ``upl_<uuid>`` string."""
    return f"{UPLOAD_ID_PREFIX}{upload_id}"


def parse_upload_id(raw: str) -> UUID | None:
    """Parse an external ``upl_<uuid>`` string; ``None`` on any other shape.

    Strict on purpose: exact prefix, exact lowercase-hyphenated 36-char UUID
    body, nothing before or after (no surrounding whitespace, no trailing
    NUL). Any other input — including the pre-unification formats
    (``uploads/report.pdf``, ``expert_work://image/…``) — returns ``None``
    rather than raising, so callers can treat it as a uniform 404/422 signal.
    """
    if not raw.startswith(UPLOAD_ID_PREFIX):
        return None
    body = raw[len(UPLOAD_ID_PREFIX) :]
    if not _UUID_RE.fullmatch(body):
        return None
    return UUID(body)


#: B-280 —— 会话被彻底删除后,它的 document 登记行留成「墓碑」:只留 id / 租户 / 用户 / 会话 /
#: ``kind`` / ``created_at``,``ref`` / ``filename`` / ``mime_type`` 清成空串(列是 NOT NULL)、
#: ``size_bytes`` 0、``sha256`` / ``last_used_at`` 置空、``deleted_at`` 有值。留着它只为一件事:
#: 别的会话再引用这个 ``upload_id`` 时仍回 404 ``UPLOAD_SESSION_DELETED``(对外已公布的码)。
#: 空 ``ref`` 只会出现在墓碑上 —— 上传总是写 ``uploads/<名字>``。
SESSION_DELETED_TOMBSTONE_REF: Final = ""


def is_session_deleted_tombstone(upload: UserUpload) -> bool:
    """这一行是会话被彻底删除后留下的墓碑(见 :data:`SESSION_DELETED_TOMBSTONE_REF`)。"""
    return (
        upload.deleted_at is not None
        and upload.kind == "document"
        and upload.ref == SESSION_DELETED_TOMBSTONE_REF
    )
