"""``DocumentFileFacts`` — what a store records about an uploaded file.

The mime comes from the extension through a fixed table, so a laptop, CI and
the production image agree. Python's built-in table has no Office types: before
B-175's acceptance run, every ``.docx`` / ``.pptx`` / ``.xlsx`` got ``None``.
"""

from __future__ import annotations

import pytest

from expert_work.persistence.knowledge.base import DocumentFileFacts

_OOXML = "application/vnd.openxmlformats-officedocument"


@pytest.mark.parametrize(
    ("filename", "mime"),
    [
        ("report.pdf", "application/pdf"),
        ("guide.docx", f"{_OOXML}.wordprocessingml.document"),
        ("deck.pptx", f"{_OOXML}.presentationml.presentation"),
        ("sheet.xlsx", f"{_OOXML}.spreadsheetml.sheet"),
        ("notes.md", "text/markdown"),
        ("notes.markdown", "text/markdown"),
        ("plain.txt", "text/plain"),
        ("page.html", "text/html"),
        ("page.htm", "text/html"),
        ("table.csv", "text/csv"),
        ("UPPER.DOCX", f"{_OOXML}.wordprocessingml.document"),
        ("no-extension", None),
    ],
)
def test_mime_from_extension(filename: str, mime: str | None) -> None:
    assert DocumentFileFacts.of(filename, b"x").mime == mime


def test_title_and_size() -> None:
    facts = DocumentFileFacts.of("季度报告.v2.docx", b"abc")
    assert facts.title == "季度报告.v2"
    assert facts.size_bytes == 3
    assert DocumentFileFacts.of("a.docx", None).size_bytes is None
