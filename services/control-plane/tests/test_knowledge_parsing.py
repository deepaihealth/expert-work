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


# B-181 — UTF-8 text is decoded as UTF-8, not by MarkItDown's charset guess on
# the first 4096 bytes.

_ASCII_HEAD = "Line of plain ascii text for padding.\n" * 120  # ~4.4 KB, past the guess window


@pytest.mark.parametrize("filename", ["prices.md", "prices.markdown", "prices.txt"])
def test_text_with_few_non_ascii_characters_keeps_them(filename: str) -> None:
    # The guess used to pick GBK / UTF-16 here: ``¥198 ¥298`` came back as mojibake.
    assert "¥198 ¥298" in parse_document(filename, "价目:¥198 ¥298\n".encode())


@pytest.mark.parametrize("filename", ["guide.md", "guide.txt", "export.csv"])
def test_text_whose_first_4kb_is_ascii_still_parses(filename: str) -> None:
    # An ASCII head made the guess ``ascii``; the later em dash then raised
    # UnicodeDecodeError and the whole document failed to ingest.
    if filename.endswith(".csv"):
        body, want = "col,val\n" + "x,1\n" * 1200 + "dash,—\n", "| dash | — |"
    elif filename.endswith(".md"):
        # Real Markdown: MarkItDown's file-type detector called the plain padding
        # text rather than Markdown, which happened to dodge the bug.
        body, want = (
            "# Title\n\n" + "Some *ascii* text, a [link](http://x.y).\n\n" * 100 + "Price — fine\n",
            "—",
        )
    else:
        body, want = _ASCII_HEAD + "Price — fine\n", "—"
    assert want in parse_document(filename, body.encode())


@pytest.mark.parametrize("filename", ["notes.md", "notes.txt"])
def test_text_keeps_trailing_spaces(filename: str) -> None:
    # Two trailing spaces are a Markdown hard break; MarkItDown strips them.
    assert "原价¥198  \n下一行" in parse_document(filename, "原价¥198  \n下一行\n".encode())
    # Windows line endings: the break is still kept.
    assert "原价¥198  \n下一行" in parse_document(filename, "原价¥198  \r\n下一行\r\n".encode())


@pytest.mark.parametrize("filename", ["notes.md", "notes.txt", "export.csv"])
def test_utf8_bom_is_dropped(filename: str) -> None:
    markdown = parse_document(filename, b"\xef\xbb\xbf" + "标题,内容\n第一行,¥198\n".encode())
    assert "﻿" not in markdown
    assert "标题" in markdown


def test_text_line_endings_are_normalised() -> None:
    assert parse_document("notes.txt", b"first\r\nsecond\r\r\n") == "first\nsecond\n"


def test_text_line_end_breaks_other_than_spaces_are_stripped() -> None:
    # As MarkItDown did. Kept, a trailing form feed / vertical tab / Unicode line
    # separator makes the chunker's line count drift from markdown-it's.
    markdown = parse_document("notes.txt", "a\x0c\nb\x0b\nc\u2028\nd\x85\ne\t\n".encode())
    assert markdown == "a\nb\nc\nd\ne\n"


def test_text_after_a_page_break_line_still_chunks() -> None:
    from control_plane.knowledge.chunking import chunk_markdown

    raw = b"# Chapter 1\n\nIntro paragraph.\n\x0c\n# Chapter 2\n\nRefund window is 14 days.\n"
    chunks = chunk_markdown(parse_document("manual.txt", raw), max_tokens=512, overlap_tokens=0)
    assert any("Refund window is 14 days." in chunk.body for chunk in chunks)


def test_text_blank_line_runs_collapse_as_before() -> None:
    # MarkItDown collapsed them; keeping that leaves existing chunk boundaries as they were.
    assert parse_document("notes.md", b"a\n\n\n\nb\n") == "a\n\nb\n"
    assert parse_document("notes.md", b"a\n  \n  \n  \nb\n") == "a\n\nb\n"


def test_stray_nul_bytes_are_dropped() -> None:
    # Postgres ``text`` refuses NUL: one stray byte used to fail the document
    # when its chunks were stored, after the embedding had been paid for.
    markdown = parse_document("notes.txt", b"Refund window\x00 is 14 days.\n")
    assert markdown == "Refund window is 14 days.\n"


def test_bomless_utf16_is_not_taken_for_utf8() -> None:
    # ASCII text in UTF-16 without a BOM is valid UTF-8 — with a NUL after every
    # character, which Postgres would refuse when the chunks are stored.
    markdown = parse_document("policy.txt", "Refund policy\n".encode("utf-16-le"))
    assert "Refund policy" in markdown
    assert "\x00" not in markdown


def test_csv_is_still_converted_to_a_table() -> None:
    markdown = parse_document("export.csv", "名称,价格\n益生菌,¥198\n".encode())
    assert "| 名称 | 价格 |" in markdown
    assert "| 益生菌 | ¥198 |" in markdown


def test_non_utf8_text_falls_back_to_detection() -> None:
    text = "公司简介:我们专注于营养健康产品的研发与销售,产品包括益生菌、膳食纤维和蛋白粉等。\n" * 5
    assert "膳食纤维" in parse_document("intro.txt", text.encode("gbk"))
