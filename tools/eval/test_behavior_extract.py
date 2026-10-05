"""B-140 behavior_extract 单元测试。"""

from __future__ import annotations

import io
import zipfile

import pytest
from behavior_extract import UnsupportedDocumentError, document_text

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


def _zip(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in members.items():
            zf.writestr(name, text)
    return buf.getvalue()


def test_docx_paragraphs_become_lines() -> None:
    xml = (
        f"<w:document {_W}><w:body>"
        "<w:p><w:r><w:t>王小雨</w:t></w:r><w:r><w:t>的方案</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>饮食建议</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    assert document_text("方案.docx", _zip({"word/document.xml": xml})) == "王小雨的方案\n饮食建议"


def test_pptx_slides_in_numeric_order() -> None:
    def slide(text: str) -> str:
        return f"<p:sld {_A} xmlns:p='x'><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:sld>"

    data = _zip(
        {"ppt/slides/slide10.xml": slide("第十页"), "ppt/slides/slide2.xml": slide("第二页")}
    )
    assert document_text("deck.PPTX", data) == "第二页\n第十页"


def test_plain_text_decoded() -> None:
    assert document_text("report.md", "第一行\n".encode()) == "第一行\n"


def test_pdf_is_unsupported() -> None:
    with pytest.raises(UnsupportedDocumentError):
        document_text("a.pdf", b"%PDF-1.7")
