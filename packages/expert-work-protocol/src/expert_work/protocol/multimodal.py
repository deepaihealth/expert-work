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

from expert_work.protocol.agent_key import require_safe_key

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
    """工作区里一张图的引用 —— 平台自己渲出来的文档页(B-64)。

    ``rel`` 恒相对**用户根**(``{root}/{tenant_id}/{user_id}/``),不是相对
    某次沙箱 exec 的视图 —— 这是 :class:`orchestrator.multimodal.NasWorkspaceImageResolver`
    的 openat 链实际走的那棵树。B-64 回修 C1 —— 绑了 agent 的 run 在这棵树里的
    真实位置是 ``agents/<agent_key>/...``,``rel`` 因此可能以这一段开头;
    ``agent_key`` 把这一层**结构性地**摘出来单独暴露成字段,而不是让每个消费方
    自己再从 ``rel`` 里正则一遍(「规矩只写一处就漏两个」在本仓有前科 ——
    见 :mod:`orchestrator.tools.vision` 与
    :func:`orchestrator.multimodal.is_cacheable_image_ref` 现在都要读这个字段)。
    没绑 agent 的 ref(``rel`` 直接在用户根下)是 ``None``。
    """

    tenant_id: UUID
    user_id: UUID
    rel: str
    ext: str
    #: B-64 回修 C1 —— 见类 docstring。``rel`` 以 ``agents/<agent_key>/`` 开头时
    #: 非 ``None``;那种情况下 ``rel`` 仍然是**完整**路径(含这一段前缀),不裁剪
    #: 掉它——``NasWorkspaceImageResolver`` 的 openat 链就是照用户根走的字符串
    #: 相对路径,裁一段出去它就要在两处分别拼回来,徒增出错面。
    agent_key: str | None = None


def parse_workspace_image_ref(uri: str) -> WorkspaceImageRef:
    """解析 ``expert_work://workspace/<tenant>/<user>/<rel>``;不合法抛 ``ValueError``。

    **这是 :func:`parse_image_ref` 的兄弟,不是它的分支。** 那一个是明标的系统
    边界(``image_ref`` 参数从 LLM 工具调用直达),往里加一条 scheme 分支等于把
    一个已加固的校验点重新打开 —— 每加一个形态,它要同时为两种形态负责,而两种
    形态的合法性规则并不相同。这里单独校验,规则参考
    ``orchestrator.tools.artifact._validate_path`` 但更严,不是同款:后者会把
    两种特定的绝对路径拼法(``/workspace/x``、``/workspace/agents/<own
    key>/x``)折叠成相对路径再收,这里**一律拒绝、不折叠**——``image_ref`` 是
    从模型的工具调用参数直达的字符串,没有 ``_validate_path`` 那种"来自哪个
    已知视图"的上下文可折,折叠只会凭空多认一种合法拼法,扩大攻击面而不带来
    实际好处。拒绝 ``..`` 段与 ``shared/`` 首段(只读共享区,永远不是渲染落点)
    这两条与 ``_validate_path`` 相同。

    B-64 回修 C1 —— ``agents/`` 首段**不再**一律拒绝。B-60 之后,绑了 agent 的
    run 在 NAS 用户根上的真实位置就是 ``agents/<agent_key>/...``——``read_page``
    渲染出来的文件本来就落在那里,一律拒等于把自己刚渲染的页也拒了(实测撞到:
    渲染"成功"但 ``NasWorkspaceImageResolver`` 必然 ``FileNotFoundError``,而
    模型被告知"图已在上下文里")。放行的前提是 ``agents/`` 后面必须跟着一个
    通过 :func:`~expert_work.protocol.agent_key.require_safe_key` 校验的
    key——那道闸与 ``sanitize_agent_key`` 产物的字符集同源,拒绝任何非
    ``[A-Za-z0-9._-]+`` 或退化成 ``.``/``..`` 的 key,防的是 ref 字符串里的
    key 段被塞进路径穿越字符。**"这个 key 是不是调用方自己的"不在这里判**——
    ref 字符串本身看不出"自己"是谁,那是下一层的活(``vision.AskImageTool``
    比对 ``ctx.agent_key``;见该模块)。裸 ``agents/`` 后面没有 key(或 key 是
    空串)仍然拒。

    B-64 回修第 2 轮 New-6(裁定:保持现状,只记账)—— 这道闸只校验"字符集
    安全",不校验"长得像 ``sanitize_agent_key`` 的产物"(``<清洗前缀>-<8 位
    hex>`` 那个形状)。所以 ``agents/x.jpg/...`` 这类第二段不是真实 agent_key
    的 ref 也会解析成功(``agent_key='x.jpg'``)。无危害——真实的 agent_key
    恒是 ``sanitize_agent_key`` 的产物,``vision.AskImageTool`` 拿它与
    ``ctx.agent_key`` 逐字比对时,一个凭空编的 ``'x.jpg'`` 必然对不上任何真实
    运行的 ``ctx.agent_key`` 而被拒。在这里加一条"key 必须长得像 sanitize
    产物"的格式校验,等于把一条本该只由 ``sanitize_agent_key`` 一处决定的
    格式约定钉进这道边界,以后改 ``sanitize_agent_key`` 的产物形状会在这个
    远处的地方炸——解析器只保证 key 是一个安全的路径段,不保证它是调用方
    自己的、也不保证它像谁的产物。
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
    if segments and segments[0] == "shared":
        msg = f"workspace image ref must not address the reserved {segments[0]}/ tree: {uri!r}"
        raise ValueError(msg)
    agent_key: str | None = None
    if segments and segments[0] == "agents":
        if len(segments) < 2 or not segments[1]:
            msg = f"workspace image ref 'agents/' must be followed by an agent key: {uri!r}"
            raise ValueError(msg)
        key = segments[1]
        try:
            require_safe_key(key)
        except ValueError as exc:
            msg = f"workspace image ref has an unsafe agent key {key!r}: {uri!r}"
            raise ValueError(msg) from exc
        agent_key = key
    ext = PurePosixPath(rel).suffix.lower()
    if ext not in _MEDIA_TYPE_BY_EXT:
        msg = f"unsupported image extension {ext!r} in workspace ref {uri!r}"
        raise ValueError(msg)
    return WorkspaceImageRef(
        tenant_id=tenant_id, user_id=user_id, rel=rel, ext=ext, agent_key=agent_key
    )
