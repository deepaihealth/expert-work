"""B-140 —— 从产物字节里取文本给判据用。

docx / pptx 是 zip 包,直接用标准库读里面的 XML(本机与 CI 都没有 python-docx,
不为评测加依赖)。pdf / xlsx 本版不支持,判据会判不过并写明原因。
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_TEXT_SUFFIXES = (".md", ".txt", ".csv", ".json", ".py", ".html")
_SLIDE_RE = re.compile(r"ppt/slides/slide(\d+)\.xml")


class UnsupportedDocumentError(ValueError):
    """判据要读的产物类型本版取不出文本。"""


def _paragraphs(xml: bytes, para: str, text: str) -> list[str]:
    # 输入是我们自己的评测智能体在测试环境生成的产物,不是外部上传。
    root = ET.fromstring(xml)  # noqa: S314
    return ["".join(t.text or "" for t in p.iter(text)) for p in root.iter(para)]


def document_text(name: str, data: bytes) -> str:
    lower = name.lower()
    if lower.endswith(".docx"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return "\n".join(_paragraphs(zf.read("word/document.xml"), f"{_W}p", f"{_W}t"))
    if lower.endswith(".pptx"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            numbered = [
                (int(match.group(1)), member)
                for member in zf.namelist()
                if (match := _SLIDE_RE.fullmatch(member))
            ]
            lines: list[str] = []
            for _, member in sorted(numbered):
                lines.extend(_paragraphs(zf.read(member), f"{_A}p", f"{_A}t"))
            return "\n".join(lines)
    if lower.endswith(_TEXT_SUFFIXES):
        return data.decode("utf-8", errors="replace")
    raise UnsupportedDocumentError(f"no text extractor for {name!r}")
