"""``GET /v1/knowledge/bases/{name}/documents/{document_id}/figures/{figure_id}`` ——
B-209 PR-2 §5.9:分段预览里图片描述旁的原图缩略图。

权限与同一份文档的分段 / 逐页接口完全一样:控制台平面(``console_only``)、``manifest:read``、
读范围只认具体租户、知识库按请求的租户查、文档必须属于这个库。对象存储的键里的租户 / 库 /
文档都来自查到的记录,不来自请求;图片 id 只能是 16 位十六进制 —— 别的租户的图片 URL 一律 404。

缓存:``Cache-Control: private, no-cache`` + ``ETag``(图片 id 就是内容哈希):浏览器每次都
回来问(权限每次都重新校验,删掉的文档的图不会从缓存里继续显示),内容没变时回 304、不再传图。
不用 ``no-store``:分段预览翻页会反复取同一批缩略图。浏览器带来的 ``If-None-Match`` 对得上就直接
回 304,不去对象存储取图;按弱比较 —— 前面的负载均衡压缩图片时把 ETag 改成 ``W/"…"``,
浏览器回传的就是它。

本次入库那一代没有这张图时,只有文档当前的分段里提到这张图,才去别的代找(重传入库中、或待处理的
重传被放弃后,分段还是旧版本的);被放弃的入库留下的、分段里已经不提的图不再能按 id 取到。
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from control_plane.api._authz import console_only, require
from control_plane.api.knowledge import _get_audit, _get_knowledge_store, _require_base
from control_plane.knowledge.recognition.figure_storage import (
    FIGURE_CONTENT_TYPE,
    FigureStorage,
    chunks_name_figure,
    is_figure_id,
)
from control_plane.tenant_scope import (
    applied_scope,
    cross_tenant_query_enabled,
    ensure_single_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence import KnowledgeStore
from expert_work.runtime.audit.logger import AuditLogger

_NOT_FOUND = "figure not found"


def _none_match(if_none_match: str | None, etag: str) -> bool:
    """RFC 9110 §13.1.2: any listed tag equals ``etag`` once a ``W/`` is dropped."""
    if not if_none_match:
        return False
    return any(tag.strip().removeprefix("W/") == etag for tag in if_none_match.split(","))


def build_knowledge_figures_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/knowledge", tags=["knowledge"], dependencies=[Depends(console_only())]
    )

    @router.get(
        "/bases/{name}/documents/{document_id}/figures/{figure_id}",
        response_model=None,
        dependencies=[Depends(require("manifest", "read"))],
    )
    async def get_figure(
        name: str,
        document_id: UUID,
        figure_id: str,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W3 read scope — subordinate detail read: concrete tenant only.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> Response:
        """One figure of the document as stored when it was described (JPEG)."""
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/knowledge/bases/{name}/documents/{document_id}/figures/{figure_id}",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            base = await _require_base(store, scope.tenant_id, name)
            document = await store.get_document(tenant_id=scope.tenant_id, document_id=document_id)
            if document is None or document.kb_id != base.id:
                raise HTTPException(status_code=404, detail="document not found")
        object_store = getattr(request.app.state, "object_store", None)
        if not is_figure_id(figure_id) or object_store is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        headers = {
            "Cache-Control": "private, no-cache",
            "ETag": f'"{figure_id}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
        }
        if _none_match(request.headers.get("if-none-match"), headers["ETag"]):
            return Response(status_code=304, headers=headers)
        storage = FigureStorage(object_store)
        image = await storage.get(
            tenant_id=scope.tenant_id,
            kb_id=base.id,
            document_id=document.id,
            figure=figure_id,
            generation=document.ingest_generation,
        )
        if image is None:
            # Only when another generation holds it (one listing of this
            # document's prefix) are the chunks asked whether they still name
            # it — a miss on a figure no generation holds costs no query.
            other = await storage.other_generation_key(
                tenant_id=scope.tenant_id,
                kb_id=base.id,
                document_id=document.id,
                figure=figure_id,
                generation=document.ingest_generation,
            )
            if other is not None:
                async with applied_scope(scope):
                    named = await chunks_name_figure(
                        store, tenant_id=scope.tenant_id, document_id=document.id, figure=figure_id
                    )
                image = await storage.read(other) if named else None
        if image is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        return Response(content=image, media_type=FIGURE_CONTENT_TYPE, headers=headers)

    return router
