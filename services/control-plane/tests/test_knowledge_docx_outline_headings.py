"""B-206 K-19 —— a Word paragraph with an outline level is a heading.

Word marks a heading two ways: a heading style (``heading 1`` …), or an
outline level on the paragraph (or on a style of another name) — the
navigation pane shows both. mammoth reads only the first; on the dev corpus
37 of 63 Word files used only the second and every one of their 2,310
chunks lost its heading path. Here:

* a paragraph's own outline level ``n`` (0-based) makes it a level ``n + 1``
  heading (levels past 6 read as 6); a style's outline level does the same,
  also through ``basedOn``;
* a heading style keeps mammoth's own mapping; outline level 9 is body text;
* not inside a table, and not past 100 characters (a stray level on a body
  paragraph);
* the chunks then carry the heading path.

Made-up text only.
"""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

from control_plane.knowledge.chunking import chunk_markdown_parented
from control_plane.knowledge.office_docx import docx_markdown
from control_plane.knowledge.parsing import parse_document
from tests.knowledge_form_fixtures import _W_NS, _docx

_STYLES = (
    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles xmlns:w="{_W_NS}">'
    '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Topic"><w:name w:val="Topic"/>'
    '<w:pPr><w:outlineLvl w:val="1"/></w:pPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="SubTopic"><w:name w:val="Sub topic"/>'
    '<w:basedOn w:val="Topic"/></w:style>'
    "</w:styles>"
)


def _para(text: str, *, level: int | None = None, style: str | None = None) -> str:
    props = ""
    if style is not None:
        props += f'<w:pStyle w:val="{style}"/>'
    if level is not None:
        props += f'<w:outlineLvl w:val="{level}"/>'
    ppr = f"<w:pPr>{props}</w:pPr>" if props else ""
    return f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def _with_styles(raw: bytes, styles: str = _STYLES) -> bytes:
    """``raw`` with its ``word/styles.xml`` replaced."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(out, "w") as target:
        for entry in source.infolist():
            data = styles.encode() if entry.filename == "word/styles.xml" else source.read(entry)
            target.writestr(entry, data)
    return out.getvalue()


def _lines(markdown: str) -> list[str]:
    return [line for line in markdown.splitlines() if line.strip()]


def test_a_paragraphs_outline_level_makes_it_a_heading_of_that_level() -> None:
    raw = _docx(
        _para("Service guide", level=0)
        + _para("Opening hours", level=1)
        + _para("Weekdays nine to five.")
        + _para("Holidays", level=2)
        + _para("Closed on public holidays.")
    )
    assert _lines(docx_markdown(raw)) == [
        "# Service guide",
        "## Opening hours",
        "Weekdays nine to five.",
        "### Holidays",
        "Closed on public holidays.",
    ]


def test_levels_past_six_read_as_six() -> None:
    raw = _docx(_para("Deep", level=7) + _para("Body."))
    assert _lines(docx_markdown(raw))[0] == "###### Deep"


def test_a_styles_outline_level_counts_also_through_based_on() -> None:
    raw = _with_styles(
        _docx(
            _para("Billing", style="Topic")
            + _para("Paid monthly.")
            + _para("Refunds", style="SubTopic")
            + _para("Within thirty days.")
        )
    )
    assert _lines(docx_markdown(raw)) == [
        "## Billing",
        "Paid monthly.",
        "## Refunds",
        "Within thirty days.",
    ]


def test_a_heading_style_keeps_its_own_level() -> None:
    raw = _with_styles(_docx(_para("Contacts", style="Heading2", level=0) + _para("Call us.")))
    assert _lines(docx_markdown(raw))[0] == "## Contacts"


def test_outline_level_nine_long_paragraphs_and_table_cells_stay_body_text() -> None:
    long = "A paragraph that carries a stray outline level but is plainly body text " * 2
    cell = f"<w:tbl><w:tr><w:tc>{_para('In a cell', level=0)}</w:tc></w:tr></w:tbl>"
    raw = _docx(_para("Body level", level=9) + _para(long.strip(), level=0) + cell)
    markdown = docx_markdown(raw)
    assert not any(line.startswith("#") for line in markdown.splitlines())
    assert "Body level" in markdown and "In a cell" in markdown


def test_the_chunks_carry_the_heading_path() -> None:
    raw = _docx(
        _para("Service guide", level=0)
        + _para("Opening hours", level=1)
        + _para("Weekdays nine to five, Saturdays nine to noon.")
    )
    markdown = parse_document("guide.docx", raw)
    parents = chunk_markdown_parented(markdown, max_tokens=400, overlap_tokens=0)
    paths = [chunk.heading_path for parent in parents for chunk in parent.children]
    assert ("Service guide", "Opening hours") in paths
