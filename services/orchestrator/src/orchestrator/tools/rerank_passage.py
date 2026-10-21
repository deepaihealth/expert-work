"""The single-view rerank text of a knowledge chunk (B-206 K-14).

A scoring reranker judges each candidate once on one structured string, keys in
a fixed order — Cohere's advice for documents with fields (YAML-style
``title`` / ``date`` / ``content`` in one text); Onyx and Qwen3-Reranker put
title, metadata and body into one input the same way::

    title: <document title>
    section: <heading path>
    content: <chunk body>

A key with no value is left out; ``content`` is always there. There is no
``date``: the only dates a candidate carries are when the platform ingested it
(``KnowledgeChunk.created_at``), not when its document was written, and a wrong
date is worse than none.

:func:`clean_rerank_body` strips Markdown marks, URLs, table rules and HTML
from the body first (the ``single_clean`` view) — the same steps, in the same
order, as WeKnora's ``CleanPassage`` (``internal/reranking/passage.go``).
"""

from __future__ import annotations

import re

from expert_work.protocol import KnowledgeChunk

#: A link target with at most one level of balanced parentheses.
_TARGET = r"[^()\s]*(?:\([^)]*\)[^()\s]*)*"
_CODE_BLOCK = re.compile(r"```[^\r\n]*\r?\n(.*?)\r?\n?```", re.DOTALL)
_FORMULA_BLOCK = re.compile(r"\$\$(.*?)\$\$", re.DOTALL)
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_LINKED_IMAGE = re.compile(rf"\[!\[([^\]]*)\]\(({_TARGET})\)\]\({_TARGET}\)")
_IMAGE = re.compile(rf"!\[[^\]]*\]\({_TARGET}\)")
_LINK = re.compile(rf"\[([^\]]+)\]\({_TARGET}\)")
_RAW_URL = re.compile(r"https?://[^\s)\]>]+")
_TABLE_RULE = re.compile(r"^[ \t]*\|[ \t:|-]+\|[ \t]*$", re.MULTILINE)
_TABLE_ROW = re.compile(r"^[ \t]*\|(.+?)\|[ \t]*$", re.MULTILINE)
_HEADING_MARK = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_QUOTE_MARK = re.compile(r"^>\s?", re.MULTILINE)
_EMPHASIS = (
    re.compile(r"\*{3}(.+?)\*{3}"),
    re.compile(r"\*{2}(.+?)\*{2}"),
    re.compile(r"\*(.+?)\*"),
)
_LIST_MARK = re.compile(r"^[\t ]*(?:[-*+]|\d+\.)\s+", re.MULTILINE)
_BLANK_RUN = re.compile(r"\n{3,}")


def _table_row_text(match: re.Match[str]) -> str:
    return ", ".join(cell.strip() for cell in match.group(1).split("|") if cell.strip())


def clean_rerank_body(text: str) -> str:
    """``text`` without Markdown / HTML structure: code and formula blocks
    unwrapped, tags and images dropped, links reduced to their text, bare URLs
    and table rules dropped, table rows joined as ``a, b``, heading / quote /
    emphasis / list marks removed, blank-line runs folded to one."""
    text = _CODE_BLOCK.sub(r"\1", text)
    text = _FORMULA_BLOCK.sub(r"\1", text)
    text = _HTML_TAG.sub("", text)
    text = _LINKED_IMAGE.sub(r"![\1](\2)", text)
    text = _IMAGE.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _RAW_URL.sub("", text)
    text = _TABLE_RULE.sub("", text)
    text = _TABLE_ROW.sub(_table_row_text, text)
    text = _HEADING_MARK.sub("", text)
    text = _QUOTE_MARK.sub("", text)
    for emphasis in _EMPHASIS:
        text = emphasis.sub(r"\1", text)
    text = _LIST_MARK.sub("", text)
    text = _BLANK_RUN.sub("\n\n", text)
    return text.strip()


def rerank_passage(chunk: KnowledgeChunk, *, clean: bool = False) -> str:
    """The text a single-view rerank judges ``chunk`` on (see the module doc).
    ``clean`` runs the body — only the body — through :func:`clean_rerank_body`;
    a body that cleans to nothing (only an image or a link list) is kept as it
    is, so no candidate reaches the reranker without content."""
    body = chunk.content
    if clean:
        body = clean_rerank_body(body) or body
    lines = []
    if chunk.doc_title:
        lines.append(f"title: {chunk.doc_title}")
    if chunk.heading_path:
        lines.append(f"section: {' > '.join(chunk.heading_path)}")
    lines.append(f"content: {body}")
    return "\n".join(lines)
