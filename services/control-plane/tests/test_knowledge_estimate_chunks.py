"""B-222 PR-2 —— an estimate chunks a text exactly as its ingest will, embedding nothing.

For every document form (``knowledge_form_fixtures``, Ⓐ ~ Ⓔ): with topic
shifts off, the estimate's chunks are the ingest's; with them on, the blocks
the estimate counts for topic detection are exactly the ones the chunker
embeds — none at all once every pair is in the document's break memo (B-215),
and then the chunks are the ingest's too.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge.chunking import (
    BreakMemo,
    chunk_markdown_parented,
    chunk_markdown_semantic_parented,
    estimate_chunks,
)
from control_plane.knowledge.parsing import is_plain_text_file, parse_document
from expert_work.protocol import (
    DEFAULT_CHUNK_MAX_TOKENS,
    DEFAULT_CHUNK_OVERLAP_TOKENS,
    knowledge_document_title,
)
from tests.knowledge_form_fixtures import TEXT_FORM_DOCS, FormDoc

_TENANT = uuid4()


class _ShiftingEmbedder:
    """A topic per text (from its hash: shifts land all over the document);
    records what it embeds."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        self.texts.extend(texts)
        return [
            (1.0, 0.0) if hashlib.sha256(t.encode()).digest()[0] % 3 else (0.0, 1.0) for t in texts
        ]


class _NoEmbedder:
    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        raise AssertionError(f"embedded {len(texts)} texts")


def _markdown(doc: FormDoc) -> str:
    return parse_document(doc.filename, doc.raw)


def _sizes(doc: FormDoc) -> dict[str, object]:
    return {
        "max_tokens": DEFAULT_CHUNK_MAX_TOKENS,
        "overlap_tokens": DEFAULT_CHUNK_OVERLAP_TOKENS,
        "plain_text": is_plain_text_file(doc.filename),
        "doc_title": knowledge_document_title(doc.filename),
    }


@pytest.mark.parametrize("doc", TEXT_FORM_DOCS, ids=lambda d: d.id)
def test_without_topic_shifts_the_estimate_cuts_what_the_ingest_cuts(doc: FormDoc) -> None:
    markdown = _markdown(doc)
    estimate = estimate_chunks(markdown, semantic=False, **_sizes(doc))  # type: ignore[arg-type]
    assert estimate.parents == chunk_markdown_parented(markdown, **_sizes(doc))  # type: ignore[arg-type]
    assert estimate.break_texts == []


@pytest.mark.parametrize("doc", TEXT_FORM_DOCS, ids=lambda d: d.id)
async def test_topic_detection_is_counted_block_for_block(doc: FormDoc) -> None:
    markdown = _markdown(doc)
    embedder = _ShiftingEmbedder()
    memo = BreakMemo(namespace="fake/model")
    await chunk_markdown_semantic_parented(
        markdown,
        embedder=embedder,
        tenant_id=_TENANT,
        memo=memo,
        **_sizes(doc),  # type: ignore[arg-type]
    )
    fresh = estimate_chunks(
        markdown,
        semantic=True,
        memo=BreakMemo(namespace="fake/model"),
        **_sizes(doc),  # type: ignore[arg-type]
    )
    assert fresh.break_texts == embedder.texts
    without_memo = _ShiftingEmbedder()
    await chunk_markdown_semantic_parented(
        markdown,
        embedder=without_memo,
        tenant_id=_TENANT,
        memo=None,
        **_sizes(doc),  # type: ignore[arg-type]
    )
    assert (
        estimate_chunks(markdown, semantic=True, memo=None, **_sizes(doc)).break_texts  # type: ignore[arg-type]
        == without_memo.texts
    )

    # Every pair decided before (a re-ingest): nothing to embed, the same chunks.
    known = BreakMemo(namespace="fake/model", known=dict(memo.decided))
    again = estimate_chunks(markdown, semantic=True, memo=known, **_sizes(doc))  # type: ignore[arg-type]
    assert again.break_texts == []
    replayed = await chunk_markdown_semantic_parented(
        markdown,
        embedder=_NoEmbedder(),
        tenant_id=_TENANT,
        memo=BreakMemo(namespace="fake/model", known=dict(memo.decided)),
        **_sizes(doc),  # type: ignore[arg-type]
    )
    assert again.parents == replayed


async def test_a_half_known_text_counts_only_the_blocks_next_to_unknown_pairs() -> None:
    paragraphs = [f"paragraph {i}: " + "word " * 60 for i in range(8)]
    markdown = "\n\n".join(paragraphs)
    embedder = _ShiftingEmbedder()
    memo = BreakMemo(namespace="fake/model")
    await chunk_markdown_semantic_parented(
        markdown,
        max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
        overlap_tokens=0,
        embedder=embedder,
        tenant_id=_TENANT,
        memo=memo,
    )
    decided = list(memo.decided.items())
    half = BreakMemo(namespace="fake/model", known=dict(decided[: len(decided) // 2]))
    second = _ShiftingEmbedder()
    await chunk_markdown_semantic_parented(
        markdown,
        max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
        overlap_tokens=0,
        embedder=second,
        tenant_id=_TENANT,
        memo=BreakMemo(namespace="fake/model", known=dict(decided[: len(decided) // 2])),
    )
    estimate = estimate_chunks(
        markdown, max_tokens=DEFAULT_CHUNK_MAX_TOKENS, overlap_tokens=0, semantic=True, memo=half
    )
    assert 0 < len(estimate.break_texts) < len(embedder.texts)
    assert estimate.break_texts == second.texts
