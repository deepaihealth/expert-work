"""Image references for multimodal input — Stream J.6.

A user uploads an image with a run; the bytes land in object storage and
the run message carries an opaque ``expert_work://image/...`` reference instead
of the bytes (base64 never enters the checkpointer). ``ImageRef`` is the
parsed form of that URI — the bridge between the protocol-level reference
string and the object-storage key.

B-64 —— ``WorkspaceImageRef`` / ``parse_workspace_image_ref`` 是一套**兄弟**
引用形态,指向平台自己在用户工作区里渲出来的文档页,不落对象存储。两套
scheme 各自独立校验,细节见 ``parse_workspace_image_ref`` 的 docstring。

See ``docs/streams/STREAM-J-DESIGN.md`` § 13.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final
from uuid import UUID

#: URI scheme prefix for an uploaded image reference.
IMAGE_REF_PREFIX: Final = "expert_work://image/"

#: URI scheme prefix for a workspace image reference (B-64) —— 平台渲出来的
#: 文档页,活在用户工作区而不是对象存储。
WORKSPACE_REF_PREFIX: Final = "expert_work://workspace/"

#: A file extension is optional; when present it must be a short,
#: lowercase, dotted token. The upload endpoint derives it from the
#: content-type allowlist, never from an untrusted filename.
_EXT_RE = re.compile(r"\.[a-z0-9]{1,12}")

#: Canonical (dashed) UUID string length.
_UUID_LEN: Final = 36

#: 工作区图片 ref 认的扩展名表 —— 与 ``orchestrator.multimodal._MEDIA_TYPE_BY_EXT``
#: 同一份取值(两处刻意重复:``packages/`` 不能反向依赖 ``services/orchestrator``,
#: 这里是校验用,orchestrator 那份是解析用);改一处务必同步另一处。
_MEDIA_TYPE_BY_EXT: Final[dict[str, str]] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


@dataclass(frozen=True)
class ImageRef:
    """A parsed ``expert_work://image/{tenant_id}/{thread_id}/{image_id}{ext}`` URI.

    The reference is self-describing: ``tenant_id`` lets a tool reject a
    cross-tenant image without a store lookup, and ``storage_key`` derives
    the object-storage key deterministically (ADR-0004 key convention) so
    the upload endpoint and the resolver share one source of truth.
    """

    tenant_id: UUID
    thread_id: UUID
    image_id: UUID
    #: File extension including the leading dot (e.g. ``".png"``); empty
    #: when the upload had no recognised extension.
    ext: str = ""

    def to_uri(self) -> str:
        """Render the canonical ``expert_work://image/...`` reference string."""
        return f"{IMAGE_REF_PREFIX}{self.tenant_id}/{self.thread_id}/{self.image_id}{self.ext}"

    @property
    def storage_key(self) -> str:
        """Object-storage key for the image bytes (ADR-0004 § 2.3)."""
        return f"{self.tenant_id}/uploads/{self.thread_id}/{self.image_id}{self.ext}"


def parse_image_ref(uri: str) -> ImageRef:
    """Parse a ``expert_work://image/...`` reference; raise ``ValueError`` if malformed.

    This is a system boundary — the ``image_ref`` argument reaches it
    straight from an LLM tool call — so every component is validated.
    """
    if not uri.startswith(IMAGE_REF_PREFIX):
        msg = f"image ref must start with {IMAGE_REF_PREFIX!r}: {uri!r}"
        raise ValueError(msg)
    parts = uri[len(IMAGE_REF_PREFIX) :].split("/")
    if len(parts) != 3:
        msg = f"image ref must be {IMAGE_REF_PREFIX}<tenant>/<thread>/<image>: {uri!r}"
        raise ValueError(msg)
    tenant_raw, thread_raw, last = parts
    try:
        tenant_id = UUID(tenant_raw)
        thread_id = UUID(thread_raw)
    except ValueError as exc:
        msg = f"image ref tenant / thread segment is not a UUID: {uri!r}"
        raise ValueError(msg) from exc
    id_raw, ext = last[:_UUID_LEN], last[_UUID_LEN:]
    try:
        image_id = UUID(id_raw)
    except ValueError as exc:
        msg = f"image ref image-id segment is not a UUID: {uri!r}"
        raise ValueError(msg) from exc
    if ext and not _EXT_RE.fullmatch(ext):
        msg = f"image ref has a malformed extension {ext!r}: {uri!r}"
        raise ValueError(msg)
    return ImageRef(tenant_id=tenant_id, thread_id=thread_id, image_id=image_id, ext=ext)


@dataclass(frozen=True)
class WorkspaceImageRef:
    """工作区里一张图的引用 —— 平台自己渲出来的文档页(B-64)。"""

    tenant_id: UUID
    user_id: UUID
    rel: str
    ext: str


def parse_workspace_image_ref(uri: str) -> WorkspaceImageRef:
    """解析 ``expert_work://workspace/<tenant>/<user>/<rel>``;不合法抛 ``ValueError``。

    **这是 :func:`parse_image_ref` 的兄弟,不是它的分支。** 那一个是明标的系统
    边界(``image_ref`` 参数从 LLM 工具调用直达),往里加一条 scheme 分支等于把
    一个已加固的校验点重新打开 —— 每加一个形态,它要同时为两种形态负责,而两种
    形态的合法性规则并不相同。这里单独校验,规则与
    ``orchestrator.tools.artifact._validate_path`` 同款:拒绝绝对路径、拒绝
    ``..`` 段、拒绝 ``agents/`` 与 ``shared/`` 首段(别人的子树 / 只读共享区)。
    """
    if not uri.startswith(WORKSPACE_REF_PREFIX):
        msg = f"workspace image ref must start with {WORKSPACE_REF_PREFIX!r}: {uri!r}"
        raise ValueError(msg)
    parts = uri[len(WORKSPACE_REF_PREFIX) :].split("/")
    if len(parts) < 3:
        msg = f"workspace image ref must carry tenant/user/path: {uri!r}"
        raise ValueError(msg)
    try:
        tenant_id, user_id = UUID(parts[0]), UUID(parts[1])
    except ValueError as exc:
        msg = f"workspace image ref has a malformed tenant/user id: {uri!r}"
        raise ValueError(msg) from exc
    rel = "/".join(parts[2:])
    segments = PurePosixPath(rel).parts
    if not rel or rel.startswith("/") or ".." in segments:
        msg = f"workspace image ref path must be relative and free of '..': {uri!r}"
        raise ValueError(msg)
    if segments and segments[0] in ("agents", "shared"):
        msg = f"workspace image ref must not address the reserved {segments[0]}/ tree: {uri!r}"
        raise ValueError(msg)
    ext = PurePosixPath(rel).suffix.lower()
    if ext not in _MEDIA_TYPE_BY_EXT:
        msg = f"unsupported image extension {ext!r} in workspace ref {uri!r}"
        raise ValueError(msg)
    return WorkspaceImageRef(tenant_id=tenant_id, user_id=user_id, rel=rel, ext=ext)
