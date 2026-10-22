"""B-209 PR-2 §5.4 —— 文档内图片的原图(规整后的 JPEG)存对象存储,与工作区同一套
(``expert_work.runtime.storage``)。

键 ``knowledge_figures/{tenant}/{kb}/{document}/g{generation}/{figure_id}.jpg``:

* 放在自己的顶层前缀下(同 ADR-0004 的 ``audit_worm/`` / ``archive/``),对账任务
  (:mod:`~.figure_reconcile`)列这一个前缀就能看到全部原图,不用去别处枚举租户;
* 按**入库代次**分目录:一次入库只往自己那一代写;这一代在 READY 落定(带代次栅栏)之后删掉
  别的代(重新上传换了图,旧图不会一直留着);一次没落定的入库(重传 / 被删 / 失败)删掉它这一代
  写的;删文档 / 删库按前缀批量删;都没删成的(对象存储当时不可用),对账任务按「文档行已经
  不在」清掉。
* 图片 id = 图片字节 sha256 的前 16 位十六进制 —— 同一份文档重新入库写到同一个 id。

读原图的接口先按租户 / 知识库 / 文档校验权限,再按这里的键取 —— 键里的租户、库、文档都来自
校验过的记录,不来自请求;id 只能是 16 位十六进制。
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from expert_work.runtime.storage import ObjectNotFoundError, ObjectStore

logger = logging.getLogger(__name__)

FIGURE_CONTENT_TYPE = "image/jpeg"
ROOT = "knowledge_figures/"
_FIGURE_ID = re.compile(r"[0-9a-f]{16}")
_KEY = re.compile(
    rf"{ROOT}(?P<tenant>[0-9a-f-]{{36}})/(?P<kb>[0-9a-f-]{{36}})/(?P<document>[0-9a-f-]{{36}})"
    r"/g(?P<generation>\d{1,12})/(?P<figure>[0-9a-f]{16})\.jpg"
)


def figure_id(image: bytes) -> str:
    """The figure's id: the first 16 hex digits of its bytes' sha256."""
    return hashlib.sha256(image).hexdigest()[:16]


def is_figure_id(value: str) -> bool:
    return _FIGURE_ID.fullmatch(value) is not None


@dataclass(frozen=True)
class FigureKey:
    """A stored figure's place, read back from its object key."""

    tenant_id: UUID
    kb_id: UUID
    document_id: UUID
    generation: int
    figure: str


def parse_key(key: str) -> FigureKey | None:
    match = _KEY.fullmatch(key)
    if match is None:
        return None
    return FigureKey(
        UUID(match.group("tenant")),
        UUID(match.group("kb")),
        UUID(match.group("document")),
        int(match.group("generation")),
        match.group("figure"),
    )


def _base_prefix(tenant_id: UUID, kb_id: UUID) -> str:
    return f"{ROOT}{tenant_id}/{kb_id}/"


def _document_prefix(tenant_id: UUID, kb_id: UUID, document_id: UUID) -> str:
    return f"{_base_prefix(tenant_id, kb_id)}{document_id}/"


def _generation_prefix(
    tenant_id: UUID, kb_id: UUID, document_id: UUID, generation: int | None
) -> str:
    return f"{_document_prefix(tenant_id, kb_id, document_id)}g{generation or 0}/"


class FigureStorage:
    """Original figures of knowledge documents in the object store."""

    def __init__(self, store: ObjectStore) -> None:
        self._store = store

    @staticmethod
    def key(
        tenant_id: UUID, kb_id: UUID, document_id: UUID, generation: int | None, figure: str
    ) -> str:
        if not is_figure_id(figure):
            msg = f"not a figure id: {figure!r}"
            raise ValueError(msg)
        return f"{_generation_prefix(tenant_id, kb_id, document_id, generation)}{figure}.jpg"

    async def put(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        document_id: UUID,
        generation: int | None,
        figure: str,
        image: bytes,
    ) -> None:
        await self._store.put(
            self.key(tenant_id, kb_id, document_id, generation, figure),
            image,
            content_type=FIGURE_CONTENT_TYPE,
        )

    async def get(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        document_id: UUID,
        figure: str,
        generation: int | None = None,
    ) -> bytes | None:
        """The figure's bytes — from ``generation`` first, else from whichever
        generation still holds it (chunks of an earlier version keep serving
        while a re-upload ingests); ``None`` when there is none."""
        key = self.key(tenant_id, kb_id, document_id, generation, figure)
        try:
            return await self._store.get(key)
        except ObjectNotFoundError:
            pass
        suffix = f"/{figure}.jpg"
        for other in await self._store.list_prefix(_document_prefix(tenant_id, kb_id, document_id)):
            if other.endswith(suffix) and other != key:
                try:
                    return await self._store.get(other)
                except ObjectNotFoundError:
                    continue
        return None

    async def figure_ids(self, *, tenant_id: UUID, kb_id: UUID, document_id: UUID) -> set[str]:
        """Ids of the figures stored for the document (any generation)."""
        keys = await self._store.list_prefix(_document_prefix(tenant_id, kb_id, document_id))
        return {parsed.figure for parsed in map(parse_key, keys) if parsed is not None}

    async def delete_document(self, *, tenant_id: UUID, kb_id: UUID, document_id: UUID) -> int:
        """Delete every figure of the document; the number deleted."""
        return await self._delete_prefix(_document_prefix(tenant_id, kb_id, document_id))

    async def delete_base(self, *, tenant_id: UUID, kb_id: UUID) -> int:
        """Delete every figure of every document of the base."""
        return await self._delete_prefix(_base_prefix(tenant_id, kb_id))

    async def delete_generation(
        self, *, tenant_id: UUID, kb_id: UUID, document_id: UUID, generation: int | None
    ) -> int:
        """Delete what one ingest generation of the document wrote."""
        return await self._delete_prefix(
            _generation_prefix(tenant_id, kb_id, document_id, generation)
        )

    async def delete_older_generations(
        self, *, tenant_id: UUID, kb_id: UUID, document_id: UUID, generation: int | None
    ) -> int:
        """Delete the document's figures of every generation before
        ``generation`` (a newer one may be ingesting already: left alone)."""
        keys = await self._store.list_prefix(_document_prefix(tenant_id, kb_id, document_id))
        older = [
            key
            for key in keys
            if (parsed := parse_key(key)) is not None and parsed.generation < (generation or 0)
        ]
        return await self.delete_keys(older)

    async def all_keys(self) -> list[str]:
        """Every stored figure key (the reconciler's scan)."""
        return await self._store.list_prefix(ROOT)

    async def delete_keys(self, keys: Sequence[str]) -> int:
        """Delete ``keys`` — in batches when the store can (S3 ``DeleteObjects``)."""
        if not keys:
            return 0
        delete_many = getattr(self._store, "delete_many", None)
        if callable(delete_many):
            await delete_many(list(keys))
        else:
            for key in keys:
                await self._store.delete(key)
        return len(keys)

    async def _delete_prefix(self, prefix: str) -> int:
        return await self.delete_keys(await self._store.list_prefix(prefix))


#: The figure id on a quote block's first line (the bracket, the word for
#: "figure", the id, the closing bracket).
_FIGURE_ID_IN_TEXT = re.compile("\u3014\u56fe ([0-9a-f]{16})\u3015")


async def confirmed_figure_ids(
    store: ObjectStore | None,
    *,
    tenant_id: UUID,
    kb_id: UUID,
    document_id: UUID,
    contents: Sequence[str],
) -> list[str]:
    """The figure ids named in ``contents`` that are stored originals of this
    document (security review S-L1: a line a document's author typed to look
    like a figure block gets no thumbnail). ``[]`` without an object store."""
    named = {match for content in contents for match in _FIGURE_ID_IN_TEXT.findall(content)}
    if not named or store is None:
        return []
    stored = await FigureStorage(store).figure_ids(
        tenant_id=tenant_id, kb_id=kb_id, document_id=document_id
    )
    return sorted(named & stored)


async def delete_figures_quietly(
    store: ObjectStore | None, *, tenant_id: UUID, kb_id: UUID, document_id: UUID | None = None
) -> None:
    """Best-effort removal after a document / base was deleted: the rows are
    gone already, so a storage error is logged (the reconciler removes what
    is left once it runs), not raised."""
    if store is None:
        return
    storage = FigureStorage(store)
    try:
        if document_id is None:
            await storage.delete_base(tenant_id=tenant_id, kb_id=kb_id)
        else:
            await storage.delete_document(tenant_id=tenant_id, kb_id=kb_id, document_id=document_id)
    except Exception:
        logger.warning(
            "knowledge.figure_delete_failed tenant=%s kb=%s document=%s",
            tenant_id,
            kb_id,
            document_id,
            exc_info=True,
        )
