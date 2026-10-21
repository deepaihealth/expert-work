"""B-206 K-14 — the single-view text a scoring reranker judges each candidate on."""

from __future__ import annotations

from uuid import uuid4

from expert_work.protocol import KnowledgeChunk
from orchestrator.tools.rerank_passage import clean_rerank_body, rerank_passage


def _chunk(
    content: str, *, title: str | None = None, heading_path: tuple[str, ...] = ()
) -> KnowledgeChunk:
    return KnowledgeChunk(
        id=uuid4(),
        tenant_id=uuid4(),
        kb_id=uuid4(),
        document_id=uuid4(),
        chunk_index=0,
        content=content,
        embedding=(1.0,),
        heading_path=heading_path,
        doc_title=title,
    )


def test_passage_keys_come_in_a_fixed_order() -> None:
    chunk = _chunk("the deductible is 500", title="guide", heading_path=("Costs", "Fees"))
    assert rerank_passage(chunk) == (
        "title: guide\nsection: Costs > Fees\ncontent: the deductible is 500"
    )


def test_a_missing_field_leaves_its_key_out_and_content_is_always_there() -> None:
    assert rerank_passage(_chunk("body")) == "content: body"
    assert rerank_passage(_chunk("body", title="guide")) == "title: guide\ncontent: body"
    assert rerank_passage(_chunk("body", heading_path=("Costs",))) == (
        "section: Costs\ncontent: body"
    )


def test_cleaning_strips_markup_and_keeps_the_words() -> None:
    body = (
        "## Storage\n"
        "Keep **refrigerated** below *4 °C*; see [the manual](https://example.com/m.pdf) "
        "or https://example.com/faq.\n"
        "![label](https://example.com/a.png)\n"
        "<div class='note'>Shelf life<br>24 months</div>\n"
        "| Item | Value |\n"
        "|---|:---:|\n"
        "| dose | 2 g |\n"
        "- first step\n"
        "1. second step\n"
        "> quoted line\n\n\n\n"
        "end"
    )
    assert clean_rerank_body(body) == (
        "Storage\n"
        "Keep refrigerated below 4 °C; see the manual or \n"
        "\n"
        "Shelf life24 months\n"
        "Item, Value\n"
        "\n"
        "dose, 2 g\n"
        "first step\n"
        "second step\n"
        "quoted line\n\n"
        "end"
    )


def test_cleaning_unwraps_code_and_formula_blocks() -> None:
    assert clean_rerank_body("```python\nprint(1)\n```") == "print(1)"
    assert clean_rerank_body("$$E = mc^2$$") == "E = mc^2"


def test_the_clean_passage_cleans_only_the_body() -> None:
    chunk = _chunk("**dose** 2 g", title="guide **v2**", heading_path=("## Use",))
    assert rerank_passage(chunk, clean=True) == (
        "title: guide **v2**\nsection: ## Use\ncontent: dose 2 g"
    )


def test_a_body_that_cleans_to_nothing_is_sent_as_it_is() -> None:
    # A chunk that is only an image reference or a link list would otherwise
    # reach the reranker with no content at all.
    raw = "![](https://example.com/a.png)"
    assert rerank_passage(_chunk(raw), clean=True) == f"content: {raw}"
