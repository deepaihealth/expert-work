# ruff: noqa: RUF001 —— 文件名里的中文全角标点是被测输入
"""Schema tests for the J.5 knowledge DTOs."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from expert_work.protocol import (
    KB_DOCUMENT_TITLE_MAX_CHARS,
    DocumentPage,
    DocumentStatus,
    IngestStage,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    PageDecision,
    PageStats,
    PageStatus,
    RetrievalMethod,
    ScoredChunk,
    chunk_retrieval_text,
    knowledge_document_title,
    rerank_model_key,
    rerank_threshold_paused,
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
    assert kb.chunk_max_tokens == 400
    assert kb.chunk_overlap_tokens == 0


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


# -- document title in the retrieval text (B-175 wave 2 PR2b) -----------------


@pytest.mark.parametrize(
    ("filename", "title"),
    [
        ("产品说明书：益休.docx", "产品说明书：益休"),
        ("folder/sub/guide.md", "guide"),
        ("a_b__c.PDF", "a b c"),
        ("  spaced   name  .txt", "spaced name"),
        ("v1.2 release notes.md", "v1.2 release notes"),
        ("README", "README"),
        (".bashrc", ".bashrc"),
        ("v1.2 report", "v1.2 report"),
        ("ZL202410048761.8", "ZL202410048761.8"),
        ("专利 202211480228.6.pdf", "专利 202211480228.6"),
        ("C:\\docs\\guide.pdf", "guide"),
        ("notes.tar.gz", "notes.tar"),
    ],
)
def test_document_title_is_the_cleaned_file_name(filename: str, title: str) -> None:
    assert knowledge_document_title(filename) == title


@pytest.mark.parametrize("filename", ["", "   ", "dir/", "__.md"])
def test_document_title_of_an_empty_name_is_none(filename: str) -> None:
    """Nothing left after cleaning: no title, so no ``[Document: ]`` line."""
    assert knowledge_document_title(filename) is None


def test_document_title_is_capped() -> None:
    """Long, noisy titles hurt retrieval (DAPR): cut to a fixed length."""
    title = knowledge_document_title("x" * 200 + ".pdf")
    assert title == "x" * KB_DOCUMENT_TITLE_MAX_CHARS


def test_retrieval_text_puts_the_document_before_the_section() -> None:
    assert (
        chunk_retrieval_text(("A", "B"), "body", "Guide")
        == "[Document: Guide]\n[Section: A > B]\n\nbody"
    )
    assert chunk_retrieval_text((), "body", "Guide") == "[Document: Guide]\n\nbody"


@pytest.mark.parametrize("doc_title", [None, ""])
def test_retrieval_text_without_a_title_is_unchanged(doc_title: str | None) -> None:
    """Chunks ingested before titles were stored read exactly as before."""
    assert chunk_retrieval_text(("A",), "body", doc_title) == "[Section: A]\n\nbody"
    assert chunk_retrieval_text((), "body", doc_title) == "body"


def test_knowledge_chunk_retrieval_text_carries_its_document_title() -> None:
    def chunk(**extra: object) -> KnowledgeChunk:
        return KnowledgeChunk(
            id=uuid4(),
            tenant_id=uuid4(),
            kb_id=uuid4(),
            document_id=uuid4(),
            chunk_index=0,
            content="body",
            embedding=(0.0,),
            heading_path=("A",),
            **extra,  # type: ignore[arg-type]
        )

    assert chunk().doc_title is None
    assert chunk().retrieval_text == "[Section: A]\n\nbody"
    assert chunk(doc_title="Guide").retrieval_text == "[Document: Guide]\n[Section: A]\n\nbody"


# ---------------------------------------------------------------------------
# B-209 — per-page records
# ---------------------------------------------------------------------------


def test_recognizing_stage_sits_between_parsing_and_chunking() -> None:
    stages = list(IngestStage)
    assert stages.index(IngestStage.PARSING) + 1 == stages.index(IngestStage.RECOGNIZING)
    assert stages.index(IngestStage.RECOGNIZING) + 1 == stages.index(IngestStage.CHUNKING)


def _page(page: int, status: PageStatus | None) -> DocumentPage:
    decision = PageDecision.TEXT if status is None else PageDecision.OCR
    return DocumentPage(page=page, decision=decision, status=status)


def test_page_stats_count_each_status() -> None:
    pages = [
        _page(1, None),
        _page(2, PageStatus.RECOGNIZED),
        _page(3, PageStatus.RECOGNIZED),
        _page(4, PageStatus.FAILED),
        _page(5, PageStatus.PENDING),
        _page(6, PageStatus.OVER_LIMIT),
    ]
    assert PageStats.of(pages) == PageStats(
        pages_total=6, pages_ocr=2, pages_failed=1, pages_pending=1, pages_over_limit=1
    )


def test_page_stats_needs_recognition_for_failed_pending_or_over_limit() -> None:
    assert not PageStats.of([_page(1, PageStatus.RECOGNIZED)]).needs_recognition
    assert not PageStats.of([_page(1, None)]).needs_recognition
    # Past the page limit: read once the platform limit is raised.
    assert PageStats.of([_page(1, PageStatus.OVER_LIMIT)]).needs_recognition
    assert PageStats.of([_page(1, PageStatus.FAILED)]).needs_recognition
    assert PageStats.of([_page(1, PageStatus.PENDING)]).needs_recognition


# --- B-206 K-7b: a rerank-score threshold is bound to the model it was set under ---


def _base(threshold: float | None, model: str | None) -> KnowledgeBase:
    return KnowledgeBase(
        id=uuid4(),
        tenant_id=uuid4(),
        name="kb",
        rerank_score_threshold=threshold,
        rerank_threshold_model=model,
    )


def test_rerank_model_key_names_provider_and_model() -> None:
    assert rerank_model_key("qwen", "qwen3-rerank") == "qwen/qwen3-rerank"


def test_a_threshold_under_the_scoring_model_is_in_force() -> None:
    base = _base(0.7, "qwen/qwen3-vl-rerank")
    assert rerank_threshold_paused(base, "qwen/qwen3-vl-rerank") is False


def test_a_threshold_set_under_another_model_is_paused() -> None:
    base = _base(0.7, "qwen/qwen3-vl-rerank")
    assert rerank_threshold_paused(base, "qwen/qwen3-rerank") is True


def test_a_threshold_with_no_recorded_model_is_paused_once_a_model_scores() -> None:
    """Calibrated under an unknown model: it cannot be told apart from a stale one."""
    assert rerank_threshold_paused(_base(0.7, None), "qwen/qwen3-rerank") is True


def test_nothing_is_paused_without_a_threshold_or_a_scoring_model() -> None:
    assert rerank_threshold_paused(_base(None, None), "qwen/qwen3-rerank") is False
    # A reranker that does not say its model (test doubles, chat-model rerank,
    # which gives no scores) keeps the threshold as before.
    assert rerank_threshold_paused(_base(0.7, "qwen/qwen3-vl-rerank"), None) is False
