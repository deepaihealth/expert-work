"""``/v1/knowledge`` —— a document's passages (B-175 P3-PR2).

A passage is what a search returns and what the console edits as one unit:
a parent row with the chunks under it (B-206 K-1), or a chunk standing for
itself. All under ``/bases/{name}/documents/{document_id}``:

* ``GET …/passages`` — the passages in document order, each with its chunks
  (and ``figure_ids``: the pictures they describe whose originals are stored);
* ``POST …/passages`` — add one at the end of the document;
* ``PATCH …/passages/{passage_id}`` — new text (``content`` / ``heading_path``;
  needs ``revision``, the one it was read at) and / or ``enabled``;
* ``GET …/passages/{passage_id}/revisions`` / ``POST …/passages/{passage_id}/restore``
  — the history, and a revision's text written back as a new revision;
* ``GET …/unapplied-edits`` — edits (and switch-offs) a re-ingest could not
  put back, and the passages it switched off by inheritance; ``DELETE``
  discards the former (``manifest:delete``: it deletes history).

A revision carries ``edited_by`` (the editor's subject id) and
``edited_by_name`` (B-241: their display name in the base's tenant — never a
coworker's email, the reader may lack ``user:read`` — or ``null``).

New text is cut by the base's chunk rules and only the chunks whose text
changed are embedded (billed to the tenant like an ingest). Refusals: 409
``revision_conflict`` (with the passage as it stands), 409 ``document_busy``
(being ingested, or a new version pending), 422 ``passage_too_long`` (more than
twice the passage cap). Console plane, ``manifest`` permissions; reads take
the W3 tenant scope; writes are audited on the document.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from control_plane.api._authz import console_only, require
from control_plane.api._user_scope import get_user_repo
from control_plane.api.knowledge import (
    _audit_write,
    _get_audit,
    _get_ingestion_runner,
    _get_knowledge_store,
    _iso,
    _require_base,
)
from control_plane.knowledge.chunking import (
    PARENT_MAX_TOKENS,
    count_tokens,
    normalize_passage_body,
)
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner
from control_plane.knowledge.recognition.figure_storage import confirmed_figure_ids
from control_plane.tenant_scope import (
    applied_scope,
    cross_tenant_query_enabled,
    ensure_single_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence import KnowledgeStore
from expert_work.persistence.knowledge.base import (
    DisabledReason,
    Passage,
    PassageRevision,
)
from expert_work.persistence.knowledge.passages import PassageConflictError
from expert_work.persistence.tenant_member import TenantMemberStore
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.protocol import (
    AuditAction,
    ChunkType,
    DocumentStatus,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    Principal,
    chunk_retrieval_text,
    knowledge_document_title,
)
from expert_work.runtime.audit.logger import AuditLogger

#: A passage's new text may hold up to twice the passage cap (a passage is cut
#: to the cap on ingest; an edit may grow it, not without bound).
PASSAGE_MAX_TOKENS = 2 * PARENT_MAX_TOKENS

#: Passages per ``GET …/passages`` response (default / ceiling).
PASSAGES_PAGE_DEFAULT = 50
PASSAGES_PAGE_MAX = 200

#: Characters of a request's text, before any token is counted.
_CONTENT_MAX_CHARS = 50_000
_HEADING_MAX_CHARS = 200
_HEADING_MAX_DEPTH = 10

_NO_RUNNER = "knowledge ingestion unavailable: no embedding model configured"

HeadingPath = Annotated[
    list[Annotated[str, Field(max_length=_HEADING_MAX_CHARS)]],
    Field(max_length=_HEADING_MAX_DEPTH),
]


class _NewPassage(BaseModel):
    """Body of ``POST …/passages``."""

    content: str = Field(min_length=1, max_length=_CONTENT_MAX_CHARS)
    heading_path: HeadingPath = Field(default_factory=list)


class _PassagePatch(BaseModel):
    """Body of ``PATCH …/passages/{passage_id}``: new text (with the revision
    it was read at) and / or the switch."""

    content: str | None = Field(default=None, min_length=1, max_length=_CONTENT_MAX_CHARS)
    heading_path: HeadingPath | None = None
    revision: int | None = Field(default=None, ge=1)
    enabled: bool | None = None


class _Restore(BaseModel):
    """Body of ``POST …/passages/{passage_id}/restore``."""

    revision: int = Field(ge=1, description="the revision whose text to write back")
    expected_revision: int = Field(ge=1, description="the passage's revision as read")


def _child_dict(chunk: KnowledgeChunk) -> dict[str, Any]:
    return {
        "id": str(chunk.id),
        "chunk_index": chunk.chunk_index,
        "content": chunk.content,
        "heading_path": list(chunk.heading_path),
        "enabled": chunk.enabled,
        "disabled_reason": chunk.disabled_reason,
    }


def _passage_dict(passage: Passage) -> dict[str, Any]:
    """One passage: ``kind`` ``parent`` (a parent row; ``chunk_index`` in the
    parents' own numbering) or ``chunk`` (a chunk standing for itself; its one
    child is itself); ``disabled_reason`` ``manual`` / ``inherited`` (switched
    off by a re-ingest — to review) when off. The shape the admin UI reads —
    keep ``apps/admin-ui/src/api/__fixtures__/knowledge_passages.json`` in step."""
    row = passage.row
    return {
        "id": str(row.id),
        "kind": "parent" if row.chunk_type is ChunkType.PARENT else "chunk",
        "chunk_index": row.chunk_index,
        "content": row.content,
        "heading_path": list(row.heading_path),
        "enabled": row.enabled,
        "disabled_reason": row.disabled_reason,
        "edited": row.edited,
        "revision": row.revision,
        "children": [_child_dict(child) for child in passage.children],
    }


def _get_member_repo(request: Request) -> TenantMemberStore | None:
    return getattr(request.app.state, "tenant_member_repo", None)


async def _account_name(
    users: TenantUserStore, members: TenantMemberStore | None, tenant_id: UUID, subject_id: str
) -> str | None:
    """B-241 —— 一个作者的显示名:成员的 display_name → ``tenant_user.display_name``
    (成员行几乎都有名字,tenant_user 几乎都没有)。不回落到成员邮箱:只有
    ``manifest:read`` 的 viewer / operator 不该借此读到同事邮箱。"""
    user = await users.get_by_subject(
        tenant_id=tenant_id, subject_type="user", subject_id=subject_id
    )
    if user is None:
        return None
    member = (
        await members.get_by_subject_id(tenant_id=tenant_id, subject_id=user.id)
        if members is not None
        else None
    )
    if member is not None and member.display_name:
        return member.display_name
    return user.display_name


async def _editor_names(
    users: TenantUserStore,
    members: TenantMemberStore | None,
    tenant_id: UUID,
    records: Sequence[PassageRevision],
    caller: Principal,
) -> dict[str, str]:
    """B-241 —— 修订作者的账号名,每个作者查一次(不按修订逐条查)。查不到的不在
    结果里(界面退回显示编号)。"""
    names: dict[str, str] = {}
    for subject_id in dict.fromkeys(r.edited_by for r in records if r.edited_by):
        name = await _account_name(users, members, tenant_id, subject_id)
        if not name and subject_id == caller.subject_id:
            # 没有显示名(如系统管理员:没有成员行、tenant_user 也没名字)时,看自己的
            # 修改用自己的登录邮箱。已知限制:别的系统管理员的修改仍显示编号(平台没存
            # 他们的名字)。
            name = caller.email
        if name:
            names[subject_id] = name
    return names


def _revision_dict(record: PassageRevision, names: Mapping[str, str]) -> dict[str, Any]:
    return {
        "passage_id": str(record.passage_id),
        "revision": record.revision,
        "content": record.content,
        "heading_path": list(record.heading_path),
        "edited_by": record.edited_by,
        # B-241 —— 账号名(显示用);查不到为 null。
        "edited_by_name": names.get(record.edited_by) if record.edited_by else None,
        "created_at": _iso(record.created_at),
        # When a re-ingest left the passage out; deleted 30 days on.
        "orphaned_at": _iso(record.orphaned_at),
        # It was switched off then, and nothing was switched off in its place.
        "switch_off_lost": record.switch_off_lost,
    }


def _conflict(error: PassageConflictError) -> HTTPException:
    detail: dict[str, Any] = {"error": error.reason}
    if error.current is not None:
        detail["passage"] = _passage_dict(error.current)
    return HTTPException(status_code=409, detail=detail)


async def _require_document(
    store: KnowledgeStore, tenant_id: UUID, base: KnowledgeBase, document_id: UUID
) -> KnowledgeDocument:
    document = await store.get_document(tenant_id=tenant_id, document_id=document_id)
    if document is None or document.kb_id != base.id:
        raise HTTPException(status_code=404, detail="document not found")
    return document


def _require_settled(document: KnowledgeDocument) -> None:
    """Refuse before anything is embedded; the store checks again, atomically."""
    if document.status is not DocumentStatus.READY or document.has_pending:
        raise _conflict(PassageConflictError("document_busy"))


def _new_text(
    content: str, heading_path: list[str] | tuple[str, ...], document: KnowledgeDocument
) -> str:
    """``content`` as a passage stores it; 422 when nothing is left or it is
    over :data:`PASSAGE_MAX_TOKENS` (measured as a passage is: title,
    breadcrumb and body)."""
    text = normalize_passage_body(content)
    if not text:
        raise HTTPException(status_code=422, detail={"error": "passage_empty"})
    title = knowledge_document_title(document.filename)
    if count_tokens(chunk_retrieval_text(heading_path, text, title)) > PASSAGE_MAX_TOKENS:
        raise HTTPException(
            status_code=422,
            detail={"error": "passage_too_long", "limit_tokens": PASSAGE_MAX_TOKENS},
        )
    return text


def _runner_or_503(runner: KnowledgeIngestionRunner | None) -> KnowledgeIngestionRunner:
    if runner is None:
        raise HTTPException(status_code=503, detail=_NO_RUNNER)
    return runner


async def _write_text(
    store: KnowledgeStore,
    runner: KnowledgeIngestionRunner,
    request: Request,
    *,
    base: KnowledgeBase,
    document: KnowledgeDocument,
    passage: Passage,
    expected_revision: int,
    content: str,
    heading_path: list[str] | tuple[str, ...],
) -> Passage:
    """New text for ``passage``: refused before anything is embedded when the
    revision is stale, then cut, embedded (only what changed) and written."""
    if passage.row.revision != expected_revision:
        raise _conflict(PassageConflictError("revision_conflict", current=passage))
    children = await runner.passage_children(
        document=document,
        content=content,
        heading_path=heading_path,
        chunk_max_tokens=base.chunk_max_tokens,
        chunk_overlap_tokens=base.chunk_overlap_tokens,
    )
    try:
        written = await store.replace_passage(
            tenant_id=document.tenant_id,
            document_id=document.id,
            passage_id=passage.id,
            expected_revision=expected_revision,
            content=content,
            heading_path=heading_path,
            children=children,
            edited_by=request.state.principal.subject_id,
        )
    except PassageConflictError as exc:
        raise _conflict(exc) from exc
    if written is None:
        raise HTTPException(status_code=404, detail="passage not found")
    return written


def build_knowledge_passages_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/knowledge", tags=["knowledge"], dependencies=[Depends(console_only())]
    )
    root = "/bases/{name}/documents/{document_id}"

    @router.get(
        f"{root}/passages",
        response_model=None,
        dependencies=[Depends(require("manifest", "read"))],
    )
    async def list_passages(
        name: str,
        document_id: UUID,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        offset: int = 0,
        limit: int = PASSAGES_PAGE_DEFAULT,
        # W3 read scope — subordinate detail read: concrete tenant only.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
        # Only the passages switched off for this reason (``inherited``: to review).
        disabled_reason: DisabledReason | None = None,
    ) -> JSONResponse:
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint=f"GET /v1/knowledge{root}/passages",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        safe_offset = max(0, offset)
        safe_limit = max(1, min(limit, PASSAGES_PAGE_MAX))
        async with applied_scope(scope):
            base = await _require_base(store, scope.tenant_id, name)
            await _require_document(store, scope.tenant_id, base, document_id)
            if disabled_reason is None:
                passages = await store.list_passages(
                    tenant_id=scope.tenant_id,
                    document_id=document_id,
                    offset=safe_offset,
                    limit=safe_limit,
                )
                total = await store.count_passages(
                    tenant_id=scope.tenant_id, document_id=document_id
                )
            else:
                matching = await store.list_passages(
                    tenant_id=scope.tenant_id,
                    document_id=document_id,
                    disabled_reason=disabled_reason,
                )
                passages = matching[safe_offset : safe_offset + safe_limit]
                total = len(matching)
        # B-209 PR-2 —— figures whose originals this document really has (as the chunk list).
        figure_ids = await confirmed_figure_ids(
            request.app.state.object_store,
            tenant_id=scope.tenant_id,
            kb_id=base.id,
            document_id=document_id,
            contents=[c.content for p in passages for c in (p.row, *p.children)],
        )
        return JSONResponse(
            content={
                "passages": [_passage_dict(p) for p in passages],
                "figure_ids": figure_ids,
                "total": total,
                "offset": safe_offset,
                "limit": safe_limit,
            }
        )

    @router.post(
        f"{root}/passages",
        response_model=None,
        dependencies=[Depends(require("manifest", "write"))],
    )
    async def add_passage(
        name: str,
        document_id: UUID,
        body: _NewPassage,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        runner: Annotated[KnowledgeIngestionRunner | None, Depends(_get_ingestion_runner)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        base = await _require_base(store, tenant_id, name)
        document = await _require_document(store, tenant_id, base, document_id)
        content = _new_text(body.content, body.heading_path, document)
        _require_settled(document)
        children = await _runner_or_503(runner).passage_children(
            document=document,
            content=content,
            heading_path=body.heading_path,
            chunk_max_tokens=base.chunk_max_tokens,
            chunk_overlap_tokens=base.chunk_overlap_tokens,
        )
        try:
            added = await store.add_passage(
                tenant_id=tenant_id,
                document_id=document_id,
                content=content,
                heading_path=body.heading_path,
                children=children,
                edited_by=request.state.principal.subject_id,
            )
        except PassageConflictError as exc:
            raise _conflict(exc) from exc
        if added is None:  # pragma: no cover - deleted while embedding
            raise HTTPException(status_code=404, detail="document not found")
        await _audit_write(
            audit,
            request,
            action=AuditAction.KNOWLEDGE_PASSAGE_ADD,
            resource_type="knowledge_document",
            resource_id=str(document_id),
            details={"kb": base.name, "passage_id": str(added.id), "revision": 1},
        )
        return JSONResponse(status_code=201, content=_passage_dict(added))

    @router.patch(
        f"{root}/passages/{{passage_id}}",
        response_model=None,
        dependencies=[Depends(require("manifest", "write"))],
    )
    async def patch_passage(
        name: str,
        document_id: UUID,
        passage_id: UUID,
        body: _PassagePatch,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        runner: Annotated[KnowledgeIngestionRunner | None, Depends(_get_ingestion_runner)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        changes_text = body.content is not None or body.heading_path is not None
        if not changes_text and body.enabled is None:
            raise HTTPException(status_code=422, detail="nothing to change")
        if changes_text and body.revision is None:
            raise HTTPException(
                status_code=422, detail="a text change needs the revision it was read at"
            )
        base = await _require_base(store, tenant_id, name)
        document = await _require_document(store, tenant_id, base, document_id)
        passage = await store.get_passage(
            tenant_id=tenant_id, document_id=document_id, passage_id=passage_id
        )
        if passage is None:
            raise HTTPException(status_code=404, detail="passage not found")
        if changes_text:
            assert body.revision is not None  # noqa: S101 — checked above
            heading = (
                body.heading_path if body.heading_path is not None else passage.row.heading_path
            )
            content = _new_text(
                body.content if body.content is not None else passage.row.content, heading, document
            )
            _require_settled(document)
            passage = await _write_text(
                store,
                _runner_or_503(runner),
                request,
                base=base,
                document=document,
                passage=passage,
                expected_revision=body.revision,
                content=content,
                heading_path=heading,
            )
            await _audit_write(
                audit,
                request,
                action=AuditAction.KNOWLEDGE_PASSAGE_EDIT,
                resource_type="knowledge_document",
                resource_id=str(document_id),
                details={
                    "kb": base.name,
                    "passage_id": str(passage_id),
                    "revision": passage.row.revision,
                },
            )
        if body.enabled is not None:
            try:
                switched = await store.set_passage_enabled(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    passage_id=passage_id,
                    enabled=body.enabled,
                )
            except PassageConflictError as exc:
                raise _conflict(exc) from exc
            if switched is None:  # pragma: no cover - replaced by a re-ingest meanwhile
                raise HTTPException(status_code=404, detail="passage not found")
            passage = switched
            await _audit_write(
                audit,
                request,
                action=AuditAction.KNOWLEDGE_PASSAGE_TOGGLE,
                resource_type="knowledge_document",
                resource_id=str(document_id),
                details={"kb": base.name, "passage_id": str(passage_id), "enabled": body.enabled},
            )
        return JSONResponse(content=_passage_dict(passage))

    @router.get(
        f"{root}/passages/{{passage_id}}/revisions",
        response_model=None,
        dependencies=[Depends(require("manifest", "read"))],
    )
    async def list_revisions(
        name: str,
        document_id: UUID,
        passage_id: UUID,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        members: Annotated[TenantMemberStore | None, Depends(_get_member_repo)],
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        """The passage's history, oldest first — also for a passage a
        re-ingest could not find again (its edits are unapplied)."""
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint=f"GET /v1/knowledge{root}/passages/{{passage_id}}/revisions",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            base = await _require_base(store, scope.tenant_id, name)
            await _require_document(store, scope.tenant_id, base, document_id)
            history = await store.list_passage_revisions(
                tenant_id=scope.tenant_id, document_id=document_id, passage_id=passage_id
            )
            names = await _editor_names(
                users, members, scope.tenant_id, history, request.state.principal
            )
        return JSONResponse(content={"revisions": [_revision_dict(r, names) for r in history]})

    @router.post(
        f"{root}/passages/{{passage_id}}/restore",
        response_model=None,
        dependencies=[Depends(require("manifest", "write"))],
    )
    async def restore_passage(
        name: str,
        document_id: UUID,
        passage_id: UUID,
        body: _Restore,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        runner: Annotated[KnowledgeIngestionRunner | None, Depends(_get_ingestion_runner)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        """Write a revision's text back — as a new revision (restoring the
        first, machine-made one clears ``edited``)."""
        tenant_id: UUID = request.state.tenant_id
        base = await _require_base(store, tenant_id, name)
        document = await _require_document(store, tenant_id, base, document_id)
        passage = await store.get_passage(
            tenant_id=tenant_id, document_id=document_id, passage_id=passage_id
        )
        history = await store.list_passage_revisions(
            tenant_id=tenant_id, document_id=document_id, passage_id=passage_id
        )
        target = next((r for r in history if r.revision == body.revision), None)
        if passage is None or target is None:
            raise HTTPException(status_code=404, detail="passage revision not found")
        _require_settled(document)
        restored = await _write_text(
            store,
            _runner_or_503(runner),
            request,
            base=base,
            document=document,
            passage=passage,
            expected_revision=body.expected_revision,
            content=target.content,
            heading_path=target.heading_path,
        )
        await _audit_write(
            audit,
            request,
            action=AuditAction.KNOWLEDGE_PASSAGE_RESTORE,
            resource_type="knowledge_document",
            resource_id=str(document_id),
            details={
                "kb": base.name,
                "passage_id": str(passage_id),
                "from_revision": body.revision,
                "revision": restored.row.revision,
            },
        )
        return JSONResponse(content=_passage_dict(restored))

    @router.get(
        f"{root}/unapplied-edits",
        response_model=None,
        dependencies=[Depends(require("manifest", "read"))],
    )
    async def unapplied_edits(
        name: str,
        document_id: UUID,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        members: Annotated[TenantMemberStore | None, Depends(_get_member_repo)],
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        """What a re-ingest left for a person to review: ``unapplied`` — manual
        edits the chunks no longer carry (their passage could not be found
        again), each one's latest revision with ``orphaned_at``, and lost
        switch-offs no passage holds the text of any more (``switch_off_lost``,
        revision 0); ``inherited`` — passages switched off because they hold
        text a person switched off on a passage that is gone. Switching one of
        those on or off (``PATCH``) settles it for good."""
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint=f"GET /v1/knowledge{root}/unapplied-edits",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            base = await _require_base(store, scope.tenant_id, name)
            await _require_document(store, scope.tenant_id, base, document_id)
            unapplied = await store.list_unapplied_revisions(
                tenant_id=scope.tenant_id, document_id=document_id
            )
            inherited = await store.list_passages(
                tenant_id=scope.tenant_id, document_id=document_id, disabled_reason="inherited"
            )
            names = await _editor_names(
                users, members, scope.tenant_id, unapplied, request.state.principal
            )
        return JSONResponse(
            content={
                "unapplied": [_revision_dict(r, names) for r in unapplied],
                "inherited": [_passage_dict(p) for p in inherited],
            }
        )

    @router.delete(
        f"{root}/unapplied-edits",
        response_model=None,
        dependencies=[Depends(require("manifest", "delete"))],
    )
    async def discard_unapplied_edits(
        name: str,
        document_id: UUID,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        """Discard the unapplied edits and lost switch-offs — their revision
        history is deleted, so ``manifest:delete`` (otherwise the retention
        job does it 30 days after a re-ingest left them behind). Answers
        ``discarded`` — the entries ``GET`` listed (what the console counts) —
        and ``revisions_deleted``, the revision rows that went (B-241)."""
        tenant_id: UUID = request.state.tenant_id
        base = await _require_base(store, tenant_id, name)
        await _require_document(store, tenant_id, base, document_id)
        discarded = await store.discard_unapplied_revisions(
            tenant_id=tenant_id, document_id=document_id
        )
        await _audit_write(
            audit,
            request,
            action=AuditAction.KNOWLEDGE_PASSAGE_DISCARD_UNAPPLIED,
            resource_type="knowledge_document",
            resource_id=str(document_id),
            details={
                "kb": base.name,
                "discarded": discarded.entries,
                "revisions_deleted": discarded.revisions,
            },
        )
        return JSONResponse(
            content={"discarded": discarded.entries, "revisions_deleted": discarded.revisions}
        )

    return router
