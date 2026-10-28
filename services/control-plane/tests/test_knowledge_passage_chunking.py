"""B-175 P3-PR2 —— an edited passage is cut into chunks again.

:func:`chunk_passage_body` cuts a passage's text (an edit, a passage added by
hand, a re-ingest putting an edit back) by the base's chunk rules, inside the
passage: every chunk carries the passage's breadcrumb, together they hold all
of its text, the same text always gives the same chunks, and a text within
the cap is one chunk. :func:`normalize_passage_body` is the text such a
passage is stored as (paragraphs between single blank lines).
"""

from __future__ import annotations

from control_plane.knowledge.chunking import (
    TextChunk,
    chunk_markdown_parented,
    chunk_passage_body,
    count_tokens,
    normalize_passage_body,
)
from expert_work.protocol import chunk_retrieval_text

_PATH = ("保险条款", "免赔额")
_TITLE = "handbook"


def _paragraph(tag: str, words: int) -> str:
    return " ".join(f"{tag}{i}" for i in range(words)) + "."


def _cut(body: str, max_tokens: int = 128, overlap: int = 0) -> list[TextChunk]:
    return chunk_passage_body(
        body, _PATH, max_tokens=max_tokens, overlap_tokens=overlap, doc_title=_TITLE
    )


def test_a_passage_within_the_cap_is_one_chunk() -> None:
    body = "免赔额是 500 元。\n\n超出部分按比例赔付。"
    assert _cut(body) == [TextChunk(body=body, heading_path=_PATH)]


def test_a_long_passage_is_cut_and_every_chunk_carries_its_breadcrumb() -> None:
    paragraphs = [_paragraph(tag, 30) for tag in ("alpha", "beta", "gamma", "delta")]
    body = "\n\n".join(paragraphs)
    chunks = _cut(body)
    assert len(chunks) > 1
    assert all(chunk.heading_path == _PATH for chunk in chunks)
    for paragraph in paragraphs:  # the chunks cover the whole text
        assert any(paragraph in chunk.body for chunk in chunks), paragraph
    for chunk in chunks:  # each within the cap, title and breadcrumb counted
        assert count_tokens(chunk_retrieval_text(chunk.heading_path, chunk.body, _TITLE)) <= 128


def test_an_oversized_paragraph_and_a_table_are_cut_like_a_document() -> None:
    sentence = "This sentence fills the paragraph. "
    long = (sentence * 40).strip()
    rows = "\n".join(f"| item {i} | {'value ' * 12}|" for i in range(30))
    table = f"| name | value |\n| --- | --- |\n{rows}"
    chunks = _cut(f"{long}\n\n{table}")
    assert len(chunks) > 3
    joined = " ".join(chunk.body for chunk in chunks)
    assert all(f"| item {i} |" in joined for i in range(30))
    table_parts = [chunk.body for chunk in chunks if "| item" in chunk.body]
    assert all(part.startswith("| name | value |\n| --- | --- |") for part in table_parts)


def test_a_long_breadcrumb_is_never_written_into_the_text() -> None:
    """A breadcrumb over a third of the cap loses its outer headings on a
    document's chunks, which then open the body instead; a passage's text is
    the user's, so nothing is put into it."""
    path = (_paragraph("chapter", 30), "免赔额")
    body = "免赔额是 500 元。"
    chunks = chunk_passage_body(body, path, max_tokens=128, overlap_tokens=0, doc_title=None)
    assert chunks == [TextChunk(body=body, heading_path=path)]


def test_the_same_text_always_gives_the_same_chunks() -> None:
    body = "\n\n".join(_paragraph(tag, 70) for tag in ("one", "two", "three"))
    assert _cut(body, overlap=16) == _cut(body, overlap=16)


def test_an_unchanged_passage_cuts_like_the_ingest_did() -> None:
    """A one-section passage: the edit path cuts it into the chunks the
    ingest made, so only what an edit changes needs embedding again."""
    markdown = "# Guide\n\n" + "\n\n".join(_paragraph(tag, 50) for tag in "abcdef")
    (parent,) = chunk_markdown_parented(
        markdown, max_tokens=128, overlap_tokens=0, doc_title=_TITLE
    )
    assert len(parent.children) > 1
    again = chunk_passage_body(
        parent.body, parent.heading_path, max_tokens=128, overlap_tokens=0, doc_title=_TITLE
    )
    assert again == list(parent.children)


def test_normalize_passage_body() -> None:
    assert (
        normalize_passage_body("  第一段\r\n第二行\n\n\n\n  第二段  \n")
        == "第一段\n第二行\n\n第二段"
    )
    assert normalize_passage_body(" \n\n ") == ""
    assert _cut(" \n ") == []
