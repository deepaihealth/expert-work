"""``GET /v1/knowledge/bases/{name}/documents/{document_id}/figures/{figure_id}`` ——
B-209 PR-2 §5.9:分段预览里图片描述旁的原图缩略图。

权限与同一份文档的分段 / 逐页接口完全一样:控制台平面(``console_only``)、``manifest:read``、
读范围只认具体租户、知识库按请求的租户查、文档必须属于这个库。对象存储的键里的租户 / 库 /
文档都来自查到的记录,不来自请求;图片 id 只能是 16 位十六进制 —— 别的租户的图片 URL 一律 404。

缓存:``Cache-Control: private, no-cache`` + ``ETag``(图片 id 就是内容哈希):浏览器每次都
回来问(权限每次都重新校验,删掉的文档的图不会从缓存里继续显示),内容没变时回 304、不再传图。
不用 ``no-store``:分段预览翻页会反复取同一批缩略图。
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
        image = await FigureStorage(object_store).get(
            tenant_id=scope.tenant_id,
            kb_id=base.id,
            document_id=document.id,
            figure=figure_id,
            generation=document.ingest_generation,
        )
        if image is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        headers = {
            "Cache-Control": "private, no-cache",
            "ETag": f'"{figure_id}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
        }
        if request.headers.get("if-none-match") == headers["ETag"]:
            return Response(status_code=304, headers=headers)
        return Response(content=image, media_type=FIGURE_CONTENT_TYPE, headers=headers)

    return router
