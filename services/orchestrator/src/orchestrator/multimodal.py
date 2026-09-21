"""Image content blocks and resolution for multimodal input — Stream J.6.

A run message carries an uploaded image as an ``image_ref`` content
block — ``{"type": "image_ref", "ref": "expert_work://image/..."}`` — instead
of inline base64, which would bloat every checkpoint snapshot. The
provider adapters resolve the reference to bytes through an
:class:`ImageResolver` only at the moment they build the wire payload.

This module is a leaf — block helpers + the resolver interface, no
orchestrator-internal imports — so the ``llm`` adapter layer can import
it without an ``llm ↔ tools`` cycle.

See ``docs/streams/STREAM-J-DESIGN.md`` § 13.
"""

from __future__ import annotations

import base64
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from expert_work.protocol.multimodal import (
    IMAGE_REF_PREFIX,
    WORKSPACE_REF_PREFIX,
    parse_image_ref,
    parse_workspace_image_ref,
)
from expert_work.runtime.storage.base import ObjectStore

#: ``content`` block discriminator for an uploaded-image reference.
IMAGE_REF_BLOCK_TYPE: Final = "image_ref"

#: Image media type per file extension — the J.6 supported set. The
#: upload endpoint sets the extension from the validated content type,
#: so every reference resolves to exactly one of these.
_MEDIA_TYPE_BY_EXT: Final[dict[str, str]] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def image_ref_block(uri: str) -> dict[str, str]:
    """Build the ``image_ref`` content block for a ``expert_work://image/...`` URI."""
    return {"type": IMAGE_REF_BLOCK_TYPE, "ref": uri}


def split_human_content(content: str | Sequence[object]) -> tuple[str, list[str]]:
    """Split a ``HumanMessage.content`` into ``(text, image-ref URIs)``.

    ``content`` is either a plain string or a LangChain block list. Text
    blocks are concatenated; ``image_ref`` blocks contribute their URI.
    """
    if isinstance(content, str):
        return content, []
    text_parts: list[str] = []
    image_refs: list[str] = []
    for block in content:
        if isinstance(block, str):
            text_parts.append(block)
        elif isinstance(block, Mapping):
            if block.get("type") == IMAGE_REF_BLOCK_TYPE:
                ref = block.get("ref")
                if isinstance(ref, str):
                    image_refs.append(ref)
            else:
                text = block.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
    return "".join(text_parts), image_refs


@dataclass(frozen=True)
class ResolvedImage:
    """Image bytes + media type, resolved from an ``image_ref``."""

    media_type: str
    data: bytes = field(repr=False)

    @property
    def base64_data(self) -> str:
        """Base64-encoded image payload (ASCII)."""
        return base64.b64encode(self.data).decode("ascii")

    @property
    def data_uri(self) -> str:
        """``data:`` URI form — the OpenAI ``image_url`` wire shape."""
        return f"data:{self.media_type};base64,{self.base64_data}"


@runtime_checkable
class ImageResolver(Protocol):
    """Resolves a ``expert_work://image/...`` reference to image bytes."""

    async def resolve(self, ref: str) -> ResolvedImage:
        """Fetch the referenced image.

        Raises an error if the reference is malformed or the object is
        missing — the exact type is implementation-specific.
        """


@dataclass(frozen=True)
class InMemoryImageResolver:
    """:class:`ImageResolver` over a fixed ``ref -> ResolvedImage`` map — tests."""

    images: Mapping[str, ResolvedImage] = field(default_factory=dict)

    async def resolve(self, ref: str) -> ResolvedImage:
        try:
            return self.images[ref]
        except KeyError as exc:
            msg = f"no image for ref {ref!r}"
            raise KeyError(msg) from exc


@dataclass(frozen=True)
class ObjectStoreImageResolver:
    """:class:`ImageResolver` backed by an :class:`ObjectStore` — Stream J.6.

    The media type is derived from the reference's file extension:
    ``ObjectStore.get`` returns only bytes, and the upload endpoint sets
    the extension from the validated content type.
    """

    store: ObjectStore

    async def resolve(self, ref: str) -> ResolvedImage:
        image_ref = parse_image_ref(ref)
        media_type = _MEDIA_TYPE_BY_EXT.get(image_ref.ext.lower())
        if media_type is None:
            msg = f"unsupported image extension {image_ref.ext!r} in ref {ref!r}"
            raise ValueError(msg)
        data = await self.store.get(image_ref.storage_key)
        return ResolvedImage(media_type=media_type, data=data)


@dataclass(frozen=True)
class NasWorkspaceImageResolver:
    """:class:`ImageResolver` over the NAS-mounted workspace volume —— B-64。

    control-plane 已经挂着 ``/mnt/workspaces``,所以渲染页的字节直接从盘上读,
    不用走沙箱 ``exec`` 的 stdout(一页 base64 约 83 KB,没必要塞进管道)。
    """

    root: Path

    async def resolve(self, ref: str) -> ResolvedImage:
        parsed = parse_workspace_image_ref(ref)
        path = self.root / str(parsed.tenant_id) / str(parsed.user_id) / parsed.rel
        media_type = _MEDIA_TYPE_BY_EXT[parsed.ext]
        return ResolvedImage(data=path.read_bytes(), media_type=media_type)


@dataclass(frozen=True)
class DispatchingImageResolver:
    """:class:`ImageResolver` that routes by ref scheme —— B-64。

    有且只有**一个** ``ImageResolver`` 实例贯穿全程:
    ``control_plane.runtime.make_image_resolver`` 造出它,两个 provider
    适配器(``openai.py`` / ``anthropic.py``)与 ``AskImageTool`` 共享同一个
    引用。给 B-64 加第二种 ref scheme(工作区渲出来的文档页)不能要求每个
    消费方都另外接一根线——那样漏接一处就是那一条路径永远拿不到字节;正确
    做法是把这**一个**实例本身改宽,让它认得两种 scheme。``ImageResolver`` 是
    Protocol,所以这是"加一个实现",不是"改接口"。

    ``workspace`` 为 ``None`` 时(部署没接 NAS,``settings.workspace_nas_root``
    未配)工作区 ref 报一个说明性的 ``ValueError``,而不是落进 ``uploads`` 那支
    然后被它自己的 scheme 校验拒掉——错误信息应该说「工作区没接」,不是「不是
    合法的上传 ref」。
    """

    uploads: ImageResolver
    workspace: ImageResolver | None = None

    async def resolve(self, ref: str) -> ResolvedImage:
        if ref.startswith(IMAGE_REF_PREFIX):
            return await self.uploads.resolve(ref)
        if ref.startswith(WORKSPACE_REF_PREFIX):
            if self.workspace is None:
                msg = f"workspace image refs are not available in this deployment: {ref!r}"
                raise ValueError(msg)
            return await self.workspace.resolve(ref)
        msg = f"unrecognized image ref scheme: {ref!r}"
        raise ValueError(msg)


@dataclass
class CachingImageResolver:
    """Wraps an :class:`ImageResolver` with a bounded LRU cache.

    Image refs are content-addressed + immutable, so a resolved image never
    goes stale; and a ref identifies exactly one image, so a ``ref ->
    ResolvedImage`` cache can only ever return that ref's own bytes (it never
    exposes anything a direct ``resolve(ref)`` would not). Without it, every LLM
    turn of a run re-fetches every image in the whole history from the object
    store — ``_human_content`` re-walks all messages on each ``complete`` call,
    and this resolver outlives individual turns, so the cache spans them.

    Failures are not cached (a raised lookup re-runs next time). ``max_size``
    bounds retained images so the cache cannot grow without limit.
    """

    inner: ImageResolver
    max_size: int = 32
    _cache: OrderedDict[str, ResolvedImage] = field(
        default_factory=OrderedDict, init=False, repr=False
    )

    async def resolve(self, ref: str) -> ResolvedImage:
        cached = self._cache.get(ref)
        if cached is not None:
            self._cache.move_to_end(ref)
            return cached
        resolved = await self.inner.resolve(ref)
        self._cache[ref] = resolved
        self._cache.move_to_end(ref)
        while len(self._cache) > self.max_size:
            self._cache.popitem(last=False)
        return resolved
