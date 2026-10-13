"""Tests for J.5 document parsing — ``parse_document``."""

from __future__ import annotations

import pymupdf
import pytest

from control_plane.knowledge.parsing import DocumentParseError, _is_sparse, parse_document


def _pdf(text: str, *, pages: int = 1) -> bytes:
    """A minimal PDF carrying ``text`` on each of ``pages`` pages."""
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
    return bytes(doc.tobytes())


def test_parse_pdf_returns_markdown() -> None:
    markdown = parse_document("handbook.pdf", _pdf("Hello knowledge base"))
    assert "Hello knowledge base" in markdown


def test_parse_markdown_file() -> None:
    markdown = parse_document("notes.md", b"# Heading\n\nthe body text")
    assert "Heading" in markdown
    assert "the body text" in markdown


def test_parse_txt_file() -> None:
    markdown = parse_document("notes.txt", b"plain text content here")
    assert "plain text content here" in markdown


def test_unsupported_extension_rejected() -> None:
    with pytest.raises(DocumentParseError, match="unsupported document type"):
        parse_document("data.xyz", b"some bytes")


def test_no_extension_rejected() -> None:
    with pytest.raises(DocumentParseError, match="unsupported document type"):
        parse_document("noextension", b"some bytes")


def test_empty_pdf_rejected() -> None:
    # A text-less PDF parses sparse → MarkItDown fallback also empty →
    # rejected rather than ingested as a blank document.
    with pytest.raises(DocumentParseError, match="empty content"):
        parse_document("blank.pdf", _pdf(""))


@pytest.mark.parametrize(
    ("markdown", "page_count", "expected"),
    [
        ("x" * 10, 1, True),  # 10 chars over 1 page — sparse
        ("x" * 200, 1, False),  # dense enough
        ("x" * 60, 2, True),  # 60 chars over 2 pages — below 50/page
        ("", 0, True),  # no pages, no text
        ("recovered text", 0, False),  # no pages but text present
    ],
)
def test_is_sparse(markdown: str, page_count: int, expected: bool) -> None:
    assert _is_sparse(markdown, page_count) is expected


def test_parse_document_drops_lone_surrogates(monkeypatch: pytest.MonkeyPatch) -> None:
    """docx 里半个 emoji 解析成孤立代理项(``\\ud83d``);之后任何 UTF-8 编码都会炸.

    清洗掉它,分段正文保证可以 ``encode("utf-8")``。"""
    from control_plane.knowledge import parsing

    monkeypatch.setattr(parsing, "_parse_with_markitdown", lambda raw, ext: "好\ud83d评\udc4d!")
    markdown = parse_document("review.docx", b"irrelevant")
    assert markdown == "好评!"
    markdown.encode("utf-8")  # must not raise
