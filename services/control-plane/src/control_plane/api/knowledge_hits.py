"""The document a retrieval hit shows — B-175 P2-PR4.

A hit carries its document id (``RetrievedChunk.document_id``; a log row keeps
it in ``results``); the console shows the document (B-206 §6: short-id number,
title, upload time) as it is when read, so a document deleted since is simply
absent — the reference is ``null``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from expert_work.persistence import KnowledgeStore
from expert_work.protocol import KnowledgeDocument


def document_ref(document: KnowledgeDocument) -> dict[str, Any]:
    """The document as a hit shows it: ``D{doc_seq}``, title, file, upload time,
    and whether it is switched on — switching a document off flips its row
    only, so a record read later shows its chunks' text unmarked otherwise."""
    return {
        "id": str(document.id),
        "doc_seq": document.doc_seq,
        "title": document.title,
        "filename": document.filename,
        "created_at": document.created_at.isoformat() if document.created_at else None,
        "enabled": document.enabled,
    }


async def documents_by_id(
    store: KnowledgeStore, tenant_id: UUID, document_ids: Iterable[UUID]
) -> dict[UUID, dict[str, Any]]:
    """The tenant's documents among ``document_ids`` as :func:`document_ref`, by
    id — one read."""
    wanted = list(dict.fromkeys(document_ids))
    if not wanted:
        return {}
    documents = await store.get_documents(tenant_id=tenant_id, document_ids=wanted)
    return {doc_id: document_ref(doc) for doc_id, doc in documents.items()}
