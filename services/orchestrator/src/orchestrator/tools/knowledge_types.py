"""The passage type ``knowledge_search`` retrieves and shows.

Owned here, apart from :mod:`orchestrator.tools.knowledge` (the retriever and
the tool) and :mod:`orchestrator.tools.knowledge_result` (what one result
shows): both import it, and neither has to import the other back.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from expert_work.protocol import KnowledgeDocument, chunk_retrieval_text


@dataclass(frozen=True)
class RetrievedChunk:
    """A retrieved chunk with its source document attribution.

    ``score`` is the vector cosine similarity in [0, 1] when the chunk was
    surfaced by vector recall (``None`` for keyword-only hits, whose
    ``ts_rank`` is not a comparable similarity). ``recall_source`` records
    which recall path(s) found it. Both default to ``None`` so the tool's
    formatted output is unaffected — they exist for the retrieval-test
    endpoint's transparency.

    The per-path sub-scores (B-175 wave 2) are kept for diagnostics and are
    ``None`` when a path did not surface the chunk: ``vector_score`` (same
    value as ``score``), ``keyword_score`` (the keyword path's rank value)
    and ``fused_score`` (the weighted RRF score that ordered the fusion).
    ``rerank_score`` is the score the reranker ranked it by — the two-view
    weighted score when both views ran — and ``None`` when rerank was off,
    failed, or ranked without scores.

    ``content`` is the chunk body; ``heading_path`` its section breadcrumb.
    The agent sees (and the eval matches against) :attr:`retrieval_text`, the
    two joined, under its document's ``[doc N]`` header (B-206 K-4). The
    reranker ranks the stored chunk's own retrieval text, which also carries
    the document title — here it would only repeat the header. ``document`` is
    the source document that header is drawn from (``None`` when it is gone).
    """

    content: str
    filename: str
    chunk_index: int
    score: float | None = None
    recall_source: str | None = None
    heading_path: tuple[str, ...] = ()
    chunk_id: UUID | None = None
    vector_score: float | None = None
    keyword_score: float | None = None
    fused_score: float | None = None
    rerank_score: float | None = None
    document: KnowledgeDocument | None = None

    @property
    def retrieval_text(self) -> str:
        return chunk_retrieval_text(self.heading_path, self.content)
