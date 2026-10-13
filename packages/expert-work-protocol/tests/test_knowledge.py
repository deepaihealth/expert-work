"""Schema tests for the J.5 knowledge DTOs."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from expert_work.protocol import (
    DocumentStatus,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    RetrievalMethod,
    ScoredChunk,
)


def test_document_status_values() -> None:
    assert {s.value for s in DocumentStatus} == {
        "pending",
        "processing",
        "ready",
        "failed",
        # Reserved for B-179 content safety; nothing writes them yet.
        "blocked",
        "disabled",
    }


def test_knowledge_base_constructs() -> None:
    kb = KnowledgeBase(id=uuid4(), tenant_id=uuid4(), name="hr-policies")
    assert kb.name == "hr-policies"
    assert kb.created_at is None


def test_knowledge_base_chunk_params_default() -> None:
    kb = KnowledgeBase(id=uuid4(), tenant_id=uuid4(), name="kb")
    assert kb.chunk_max_tokens == 512
    assert kb.chunk_overlap_tokens == 64


def test_knowledge_base_accepts_custom_chunk_params() -> None:
    kb = KnowledgeBase(
        id=uuid4(),
        tenant_id=uuid4(),
        name="kb",
        chunk_max_tokens=256,
        chunk_overlap_tokens=32,
    )
    assert kb.chunk_max_tokens == 256
    assert kb.chunk_overlap_tokens == 32


def test_knowledge_base_rejects_overlap_ge_max() -> None:
    with pytest.raises(ValidationError, match="chunk_overlap_tokens must be less than"):
        KnowledgeBase(
            id=uuid4(),
            tenant_id=uuid4(),
            name="kb",
            chunk_max_tokens=200,
            chunk_overlap_tokens=200,
        )


def test_knowledge_base_retrieval_defaults() -> None:
    kb = KnowledgeBase(id=uuid4(), tenant_id=uuid4(), name="kb")
    assert kb.retrieval_top_k == 5
    assert kb.retrieval_score_threshold is None
    assert kb.retrieval_method is RetrievalMethod.HYBRID
    assert kb.rerank_enabled is True
    assert kb.embedding_model is None
    assert kb.description is None


def test_knowledge_base_accepts_retrieval_config() -> None:
    kb = KnowledgeBase(
        id=uuid4(),
        tenant_id=uuid4(),
        name="kb",
        description="HR docs",
        created_by="alice@acme.com",
        retrieval_top_k=10,
        retrieval_score_threshold=0.4,
        retrieval_method=RetrievalMethod.VECTOR,
        rerank_enabled=False,
        embedding_provider="qwen",
        embedding_model="text-embedding-v4",
    )
    assert kb.retrieval_top_k == 10
    assert kb.retrieval_score_threshold == 0.4
    assert kb.retrieval_method is RetrievalMethod.VECTOR
    assert kb.rerank_enabled is False
    assert kb.embedding_model == "text-embedding-v4"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("retrieval_top_k", 0),
        ("retrieval_top_k", 51),
        ("retrieval_score_threshold", -0.1),
        ("retrieval_score_threshold", 1.1),
    ],
)
def test_knowledge_base_rejects_out_of_range_retrieval(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        # Dynamic kwargs for parametrised range checks — mypy can't infer the
        # per-field type from the spread dict, but the values are intentional.
        KnowledgeBase(id=uuid4(), tenant_id=uuid4(), name="kb", **{field: value})  # type: ignore[arg-type]


def test_scored_chunk_constructs() -> None:
    chunk = KnowledgeChunk(
        id=uuid4(),
        tenant_id=uuid4(),
        kb_id=uuid4(),
        document_id=uuid4(),
        chunk_index=0,
        content="x",
        embedding=(0.1,),
    )
    scored = ScoredChunk(chunk=chunk, score=0.92, source="vector")
    assert scored.score == 0.92
    assert scored.source == "vector"


def test_knowledge_document_constructs() -> None:
    doc = KnowledgeDocument(
        id=uuid4(),
        tenant_id=uuid4(),
        kb_id=uuid4(),
        filename="handbook.pdf",
        status=DocumentStatus.READY,
        chunk_count=12,
    )
    assert doc.status is DocumentStatus.READY
    assert doc.chunk_count == 12
    assert doc.error is None


def test_knowledge_document_rejects_negative_chunk_count() -> None:
    with pytest.raises(ValidationError):
        KnowledgeDocument(
            id=uuid4(),
            tenant_id=uuid4(),
            kb_id=uuid4(),
            filename="x.pdf",
            status=DocumentStatus.PENDING,
            chunk_count=-1,
        )


def test_knowledge_chunk_constructs() -> None:
    chunk = KnowledgeChunk(
        id=uuid4(),
        tenant_id=uuid4(),
        kb_id=uuid4(),
        document_id=uuid4(),
        chunk_index=0,
        content="some text",
        embedding=(0.1, 0.2, 0.3),
    )
    assert chunk.chunk_index == 0
    assert chunk.embedding == (0.1, 0.2, 0.3)


def test_knowledge_chunk_rejects_negative_index() -> None:
    with pytest.raises(ValidationError):
        KnowledgeChunk(
            id=uuid4(),
            tenant_id=uuid4(),
            kb_id=uuid4(),
            document_id=uuid4(),
            chunk_index=-1,
            content="x",
            embedding=(0.0,),
        )


def test_knowledge_dtos_are_frozen() -> None:
    kb = KnowledgeBase(id=uuid4(), tenant_id=uuid4(), name="kb")
    with pytest.raises(ValidationError):
        kb.name = "other"
