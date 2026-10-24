"""Tests for J.5 structure-aware + semantic Markdown chunking."""

from __future__ import annotations

import random
import re
from collections.abc import Sequence
from itertools import pairwise
from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge.chunking import (
    _CUT_LINE,
    MIN_SEMANTIC_CHUNK_TOKENS,
    SHORT_SECTION_TOKENS,
    TextChunk,
    _cosine_distance,
    _encoding,
    _hard_split,
    _split_by_tokens,
    _tail_text,
    chunk_params_hash,
    configure_semantic_breaks,
    count_tokens,
)
from control_plane.knowledge.chunking import chunk_markdown as _chunk_markdown
from control_plane.knowledge.chunking import chunk_markdown_semantic as _chunk_markdown_semantic
from expert_work.protocol import chunk_retrieval_text


# The cases below assert on the retrieval text (breadcrumb + body) — the exact
# strings the chunker returned before the heading path was stored apart, so
# they double as the proof that embedding / keyword input did not change.
def chunk_markdown(markdown: str, *, max_tokens: int, overlap_tokens: int) -> list[str]:
    return [
        c.text
        for c in _chunk_markdown(markdown, max_tokens=max_tokens, overlap_tokens=overlap_tokens)
    ]


async def chunk_markdown_semantic(markdown: str, **kwargs: Any) -> list[str]:
    return [c.text for c in await _chunk_markdown_semantic(markdown, **kwargs)]


class _ScriptedEmbedder:
    """Embedder test double — vector chosen by a topic marker in the text."""

    def __init__(self, markers: dict[str, tuple[float, ...]]) -> None:
        self._markers = markers

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> tuple[float, ...]:
        for marker, vector in self._markers.items():
            if marker in text:
                return vector
        return (0.0, 0.0)


class _FailEmbedder:
    """Embedder that raises if used — proves a code path skips embedding."""

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        raise AssertionError("embedder must not be called")


def test_chunk_carries_clean_body_and_heading_path() -> None:
    src = "# 健康保险\n\n## 免赔额\n\n免赔额是每年五百元。\n\n# 其它\n\n无标题的段。"
    chunks = _chunk_markdown(src, max_tokens=512, overlap_tokens=0)
    assert chunks == [
        TextChunk(body="免赔额是每年五百元。", heading_path=("健康保险", "免赔额")),
        TextChunk(body="无标题的段。", heading_path=("其它",)),
    ]
    assert chunks[0].text == "[Section: 健康保险 > 免赔额]\n\n免赔额是每年五百元。"


def test_count_tokens() -> None:
    assert count_tokens("") == 0
    assert count_tokens("hello world") > 0


def test_empty_input_yields_no_chunks() -> None:
    assert chunk_markdown("", max_tokens=512, overlap_tokens=64) == []


def test_no_headings_has_no_section_prefix() -> None:
    chunks = chunk_markdown("just some plain text.", max_tokens=512, overlap_tokens=0)
    assert len(chunks) == 1
    assert not chunks[0].startswith("[Section:")
    assert "plain text" in chunks[0]


def test_different_sections_become_separate_chunks() -> None:
    # Sections long enough to stand alone (a short one joins its neighbour, K-3).
    src = f"# A\n\n{_long('alpha content.')}\n\n# B\n\n{_long('beta content.')}"
    chunks = chunk_markdown(src, max_tokens=512, overlap_tokens=0)
    assert len(chunks) == 2
    assert chunks[0].startswith("[Section: A]")
    assert "alpha content" in chunks[0]
    assert chunks[1].startswith("[Section: B]")
    assert "beta content" in chunks[1]


def test_nested_heading_breadcrumb_prefix() -> None:
    src = "# Guide\n\n## Setup\n\ninstall steps here."
    chunks = chunk_markdown(src, max_tokens=512, overlap_tokens=0)
    assert chunks[0].startswith("[Section: Guide > Setup]")
    assert "install steps" in chunks[0]


def test_chinese_content_chunks_with_breadcrumb() -> None:
    src = "# 健康保险\n\n## 免赔额\n\n免赔额是每年五百元。"
    chunks = chunk_markdown(src, max_tokens=512, overlap_tokens=0)
    assert len(chunks) == 1
    assert chunks[0].startswith("[Section: 健康保险 > 免赔额]")
    assert "五百元" in chunks[0]


def test_overlap_repeats_text_within_a_section() -> None:
    body = " ".join(f"word{i} sentence{i}." for i in range(40))
    src = f"# S\n\n{body}"
    without = chunk_markdown(src, max_tokens=30, overlap_tokens=0)
    overlapped = chunk_markdown(src, max_tokens=30, overlap_tokens=15)
    assert len(without) > 1
    assert len(overlapped) > 1
    # Overlap re-emits the previous chunk's tail → more total text.
    assert sum(len(c) for c in overlapped) > sum(len(c) for c in without)


def test_overlap_not_carried_across_a_heading_change() -> None:
    src = (
        f"# A\n\nuniquealphatoken {_long('alpha here.')}\n\n"
        f"# B\n\nuniquebetatoken {_long('beta here.')}"
    )
    chunks = chunk_markdown(src, max_tokens=512, overlap_tokens=64)
    assert len(chunks) == 2
    assert "uniquealphatoken" not in chunks[1]


def test_table_kept_whole_when_it_fits() -> None:
    src = "# T\n\n| a | b |\n| - | - |\n| 1 | 2 |\n| 3 | 4 |"
    chunks = chunk_markdown(src, max_tokens=512, overlap_tokens=0)
    assert len(chunks) == 1
    assert "| 1 | 2 |" in chunks[0]
    assert "| 3 | 4 |" in chunks[0]


def test_oversized_table_splits_by_rows_keeping_header() -> None:
    rows = "\n".join(f"| row{i} | value{i} |" for i in range(30))
    src = f"# T\n\n| name | value |\n| - | - |\n{rows}"
    chunks = chunk_markdown(src, max_tokens=40, overlap_tokens=0)
    assert len(chunks) > 1
    # Every part is a valid table — the header row is repeated.
    for chunk in chunks:
        assert "| name | value |" in chunk


def test_table_row_over_the_cap_is_cut_under_its_header() -> None:
    # A spreadsheet cell holding a whole exported chat record made one row —
    # and so one chunk — 30 times the cap (B-200).
    record = " ".join(f"msg{i}" for i in range(400))
    src = f"| id | content |\n| - | - |\n| 1 | short |\n| 2 | {record} |\n| 3 | tail |\n"
    chunks = chunk_markdown(src, max_tokens=64, overlap_tokens=0)
    assert all(count_tokens(chunk) <= 64 for chunk in chunks)
    assert all(chunk.startswith("| id | content |\n| - | - |") for chunk in chunks)
    words = set(re.findall(r"\w+", " ".join(chunks)))
    assert {f"msg{i}" for i in range(400)} | {"short", "tail"} <= words


def test_table_whose_header_fills_the_cap_is_cut_like_prose() -> None:
    header = "| " + " | ".join(f"col{i}" for i in range(80)) + " |"
    separator = "|" + " - |" * 80
    chunks = chunk_markdown(f"{header}\n{separator}\n| 1 | 2 |\n", max_tokens=64, overlap_tokens=0)
    assert len(chunks) > 1
    assert all(count_tokens(chunk) <= 64 for chunk in chunks)


def test_oversized_paragraph_is_hard_split() -> None:
    paragraph = " ".join(f"sentence number {i} of the paragraph." for i in range(60))
    src = f"# P\n\n{paragraph}"
    chunks = chunk_markdown(src, max_tokens=30, overlap_tokens=0)
    assert len(chunks) > 1
    assert all("sentence number" in chunk for chunk in chunks)


def test_consecutive_paragraphs_pack_into_one_chunk() -> None:
    src = "# S\n\nfirst short paragraph.\n\nsecond short paragraph."
    chunks = chunk_markdown(src, max_tokens=512, overlap_tokens=0)
    assert len(chunks) == 1
    assert "first short paragraph" in chunks[0]
    assert "second short paragraph" in chunks[0]


# ---------------------------------------------------------------------------
# 中文 / 多字节边界 —— 切片不许落在一个字符中间(B-175 L2-PR1)
# ---------------------------------------------------------------------------

_ZH = (
    "益生菌需要放冰箱冷藏吗？常温密封保存即可，开袋后尽快食用。"  # noqa: RUF001
    "每日一袋，温水冲服，建议餐后半小时内服用效果更佳。"  # noqa: RUF001
) * 6


@pytest.mark.parametrize("overlap", range(1, 40))
def test_tail_overlap_never_cuts_inside_a_character(overlap: int) -> None:
    # 10-10 实测:按 token 截尾,39 种重叠长度里 13 种带 U+FFFD。
    assert "\ufffd" not in _tail_text(_ZH, overlap)


def test_tail_overlap_starts_at_the_earliest_sentence_boundary() -> None:
    text = "第一句话在这里。第二句话在这里。第三句话在这里。"
    overlap = count_tokens("句话在这里。第三句话在这里。")  # 原始窗口从第二句中间开始
    assert _tail_text(text, overlap) == "第三句话在这里。"


def test_tail_overlap_treats_paragraph_break_as_a_boundary() -> None:
    text = "甲段第一句。甲段第二句。\n\n乙段第一句。乙段第二句。"
    overlap = count_tokens("第二句。\n\n乙段第一句。乙段第二句。")
    assert _tail_text(text, overlap) == "乙段第一句。乙段第二句。"


def test_tail_overlap_without_any_boundary_falls_back_to_character_boundary() -> None:
    text = "无标点无空格" * 20
    tail = _tail_text(text, 5)
    assert tail
    assert "\ufffd" not in tail
    assert text.endswith(tail)


def test_split_by_tokens_is_lossless_and_never_cuts_inside_a_character() -> None:
    text = "无标点" * 50
    parts = _split_by_tokens(text, 7)
    assert len(parts) > 1
    assert "".join(parts) == text
    assert all("\ufffd" not in part for part in parts)


def test_chinese_paragraph_hard_splits_on_sentence_marks_without_spaces() -> None:
    # 中文句号后面没有空格,老的边界正则一刀都切不下去,整段落到 token 窗口。
    parts = _hard_split(_ZH, 40)
    assert len(parts) > 1
    assert all("\ufffd" not in part for part in parts)
    assert all(part.endswith(("。", "？")) for part in parts)  # noqa: RUF001
    assert "".join(parts) == _ZH


def test_hard_split_keeps_english_words_and_spacing_intact() -> None:
    paragraph = " ".join(f"sentence number {i} of the paragraph." for i in range(60))
    parts = _hard_split(paragraph, 30)
    assert len(parts) > 1
    assert " ".join(parts).split() == paragraph.split()
    assert not any("  " in part for part in parts)


def test_chunk_markdown_chinese_section_has_no_replacement_characters() -> None:
    chunks = chunk_markdown("# 产品\n\n" + _ZH, max_tokens=40, overlap_tokens=10)
    assert len(chunks) > 1
    assert all("\ufffd" not in chunk for chunk in chunks)


# ---------------------------------------------------------------------------
# semantic refinement
# ---------------------------------------------------------------------------


def test_cosine_distance() -> None:
    assert _cosine_distance((1.0, 0.0), (1.0, 0.0)) == pytest.approx(0.0)
    assert _cosine_distance((1.0, 0.0), (0.0, 1.0)) == pytest.approx(1.0)
    assert _cosine_distance((0.0, 0.0), (1.0, 0.0)) == 1.0  # zero vector


def _para(marker: str, word: str) -> str:
    """A paragraph long enough to stand as a chunk of its own."""
    text = f"{marker} {word}." + f" {word}" * MIN_SEMANTIC_CHUNK_TOKENS
    assert count_tokens(text) >= MIN_SEMANTIC_CHUNK_TOKENS
    return text


@pytest.mark.asyncio
async def test_semantic_split_at_topic_shift() -> None:
    src = "\n\n".join(
        [
            _para("TOPICA", "first"),
            _para("TOPICA", "second"),
            _para("TOPICB", "third"),
            _para("TOPICB", "fourth"),
        ]
    )
    embedder = _ScriptedEmbedder({"TOPICA": (1.0, 0.0), "TOPICB": (0.0, 1.0)})
    chunks = await chunk_markdown_semantic(
        src, max_tokens=512, overlap_tokens=0, embedder=embedder, tenant_id=uuid4()
    )
    # The A→B topic shift splits a section structural chunking keeps whole.
    assert len(chunks) == 2
    assert "TOPICA first" in chunks[0]
    assert "TOPICA second" in chunks[0]
    assert "TOPICB third" in chunks[1]
    assert "TOPICB fourth" in chunks[1]
    assert len(chunk_markdown(src, max_tokens=512, overlap_tokens=0)) == 1


@pytest.mark.asyncio
async def test_semantic_no_split_when_section_is_coherent() -> None:
    src = "TOPICA one.\n\nTOPICA two.\n\nTOPICA three."
    embedder = _ScriptedEmbedder({"TOPICA": (1.0, 0.0)})
    chunks = await chunk_markdown_semantic(
        src, max_tokens=512, overlap_tokens=0, embedder=embedder, tenant_id=uuid4()
    )
    assert len(chunks) == 1


@pytest.mark.asyncio
async def test_semantic_still_flushes_on_heading_change() -> None:
    # Same topic marker → no semantic break, but the heading change flushes.
    src = f"# A\n\n{_long('TOPICA content.')}\n\n# B\n\n{_long('TOPICA other.')}"
    embedder = _ScriptedEmbedder({"TOPICA": (1.0, 0.0)})
    chunks = await chunk_markdown_semantic(
        src, max_tokens=512, overlap_tokens=0, embedder=embedder, tenant_id=uuid4()
    )
    assert len(chunks) == 2


@pytest.mark.asyncio
async def test_semantic_skips_embedding_for_single_block() -> None:
    # < 2 blocks → no topic shift possible → embedder is never called.
    chunks = await chunk_markdown_semantic(
        "just one paragraph.",
        max_tokens=512,
        overlap_tokens=0,
        embedder=_FailEmbedder(),
        tenant_id=uuid4(),
    )
    assert len(chunks) == 1


@pytest.mark.asyncio
async def test_semantic_empty_input() -> None:
    chunks = await chunk_markdown_semantic(
        "", max_tokens=512, overlap_tokens=64, embedder=_FailEmbedder(), tenant_id=uuid4()
    )
    assert chunks == []


@pytest.mark.asyncio
async def test_semantic_short_block_joins_the_next_chunk() -> None:
    """A title or question line on its own line reads as a topic of its own
    (its embedding is far from both neighbours), but a chunk of just that
    line answers nothing: below MIN_SEMANTIC_CHUNK_TOKENS the topic shift
    after it is not a boundary, so it opens the chunk it introduces."""
    body_a, body_c = _para("TOPICA", "alpha"), _para("TOPICC", "gamma")
    src = f"{body_a}\n\n**TOPICB 四、产品搭配**\n\n{body_c}"
    embedder = _ScriptedEmbedder(
        {"TOPICA": (1.0, 0.0), "TOPICB": (0.0, 1.0), "TOPICC": (-1.0, 0.0)}
    )
    chunks = await chunk_markdown_semantic(
        src, max_tokens=512, overlap_tokens=0, embedder=embedder, tenant_id=uuid4()
    )
    assert chunks == [body_a, f"**TOPICB 四、产品搭配**\n\n{body_c}"]


@pytest.mark.asyncio
async def test_semantic_short_section_joins_the_next_one() -> None:
    """A section too short to stand alone joins the next one, its heading
    written into the body (B-206 K-3) — with topic shifts on as well."""
    src = "# A\n\nTOPICA short.\n\n# B\n\n" + _para("TOPICB", "beta")
    embedder = _ScriptedEmbedder({"TOPICA": (1.0, 0.0), "TOPICB": (0.0, 1.0)})
    chunks = await chunk_markdown_semantic(
        src, max_tokens=512, overlap_tokens=0, embedder=embedder, tenant_id=uuid4()
    )
    assert chunks == [f"[Section: A]\n\nTOPICA short.\n\nB\n\n{_para('TOPICB', 'beta')}"]


# str.splitlines also breaks on these; markdown-it, whose token line numbers
# the chunker slices by, breaks only on \r\n, \r and \n.
_ODD_LINE_BREAKS = ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]


@pytest.mark.parametrize("brk", _ODD_LINE_BREAKS)
def test_odd_line_break_inside_a_line_loses_no_text(brk: str) -> None:
    text = "\n".join(
        chunk_markdown(f"para one\n{brk}para two\n\npara three\n", max_tokens=512, overlap_tokens=0)
    )
    assert "para two" in text
    assert "para three" in text


@pytest.mark.parametrize("brk", _ODD_LINE_BREAKS)
def test_odd_line_break_at_a_line_end_loses_no_later_section(brk: str) -> None:
    src = f"# Chapter 1\n\nIntro paragraph.{brk}\n\n# Chapter 2\n\nRefund window is 14 days.\n"
    text = "\n".join(chunk_markdown(src, max_tokens=512, overlap_tokens=0))
    assert "Refund window is 14 days." in text


def test_odd_line_break_in_a_table_cell_keeps_the_row_whole() -> None:
    rows = "".join(f"| item {i} | {i} |\n" for i in range(40))
    table = "| name | qty |\n| --- | --- |\n" + rows + "| two\u2028lines | 99 |\n"
    parts = chunk_markdown(table, max_tokens=60, overlap_tokens=0)
    assert len(parts) > 1
    assert any("| two\u2028lines | 99 |" in part for part in parts)
    assert all(part.startswith("| name | qty |\n| --- | --- |") for part in parts)


@pytest.mark.parametrize("brk", ["\r", "\r\n"])
def test_cr_and_crlf_still_break_lines(brk: str) -> None:
    src = brk.join(["# A", "", "intro", "", "# B", "", "Refund window is 14 days.", ""])
    chunks = _chunk_markdown(src, max_tokens=512, overlap_tokens=0)
    # Both headings read as headings: A on the breadcrumb, B opening the part
    # of the body its short section joined (B-206 K-3).
    assert [(c.heading_path, c.body) for c in chunks] == [
        (("A",), "intro\n\nB\n\nRefund window is 14 days."),
    ]


# A document about LLMs quotes the tokenizer's special tokens as plain text;
# tiktoken refuses to encode them unless told they are ordinary text.
_SPECIAL_TOKENS = sorted(_encoding.special_tokens_set)


@pytest.mark.parametrize("special", _SPECIAL_TOKENS)
def test_special_token_text_is_counted_as_ordinary_text(special: str) -> None:
    assert count_tokens(f"the model stops at {special} here") > 1


@pytest.mark.parametrize("special", _SPECIAL_TOKENS)
def test_special_token_text_survives_chunking(special: str) -> None:
    src = f"# Tokenizer\n\nEach sample ends with {special} before padding.\n\n# Next\n\nOther.\n"
    text = "\n".join(chunk_markdown(src, max_tokens=512, overlap_tokens=0))
    assert f"Each sample ends with {special} before padding." in text


def test_special_token_text_survives_token_window_split_and_overlap() -> None:
    text = "".join(f"x{i}{special}" for i in range(20) for special in _SPECIAL_TOKENS)
    parts = _split_by_tokens(text, 7)
    assert len(parts) > 1
    assert "".join(parts) == text
    assert text.endswith(_tail_text(text, 5))


# markdown-it reads any paragraph right above a `---` / `===` line as a setext
# heading, however many lines it has: a meeting transcript became one 17 KB
# "heading", its text in no chunk body and on every chunk's breadcrumb (B-198).
def test_multi_line_setext_heading_is_kept_as_content() -> None:
    src = (
        "Header:\nDate: 2026-09-18\nAttendees: Maya, Priya\n---\n"
        "[00:00] Maya: hi everyone\n[00:16] Priya: hello\n----\n"
    )
    chunks = _chunk_markdown(src, max_tokens=512, overlap_tokens=0)
    assert all(c.heading_path == () for c in chunks)
    text = "\n".join(c.body for c in chunks)
    assert "Attendees: Maya, Priya" in text
    assert "[00:16] Priya: hello" in text


def test_overlong_heading_line_is_kept_as_content() -> None:
    line = " ".join(f"word{i}" for i in range(60))
    chunks = _chunk_markdown(f"# {line}\n\nBody text.\n", max_tokens=512, overlap_tokens=0)
    assert all(c.heading_path == () for c in chunks)
    assert line in "\n".join(c.body for c in chunks)


def test_setext_underline_is_not_a_heading() -> None:
    # Only ATX ``#`` headings name a section (the Dify / FastGPT rule, B-199):
    # a line above ``---`` is a paragraph and the ``---`` a rule.
    chunks = _chunk_markdown(
        "Refunds\n-------\n\nRefund window is 14 days.\n", max_tokens=512, overlap_tokens=0
    )
    assert all(c.heading_path == () for c in chunks)
    text = "\n".join(c.body for c in chunks)
    assert "Refunds" in text
    assert "Refund window is 14 days." in text


def test_heading_with_no_content_keeps_its_text() -> None:
    src = "# Overview\n# Details\n\nBody text.\n## Empty tail\n"
    joined = " ".join(c.text for c in _chunk_markdown(src, max_tokens=512, overlap_tokens=0))
    assert "Overview" in joined
    assert "Empty tail" in joined


def test_reference_definition_line_keeps_its_text() -> None:
    src = "See the policy.\n\n[policy]: https://example.com/refunds\n\nMore text.\n"
    joined = " ".join(c.text for c in _chunk_markdown(src, max_tokens=512, overlap_tokens=0))
    assert "https://example.com/refunds" in joined


# Plain text (.txt) is not Markdown (B-199, as Dify / RAGFlow / Onyx read it):
# paragraphs split on blank lines, and nothing in a line is structure.
def test_plain_text_mode_reads_no_markdown_structure() -> None:
    src = (
        "# not a heading\nSummary line\n---\n    indented line\n"
        "[1]: http://example.com/ref\n\nNext paragraph.\n"
    )
    chunks = _chunk_markdown(src, max_tokens=512, overlap_tokens=0, plain_text=True)
    assert all(c.heading_path == () for c in chunks)
    text = "\n".join(c.body for c in chunks)
    for piece in (
        "# not a heading",
        "Summary line\n---",
        "    indented line",
        "[1]: http://example.com/ref",
        "Next paragraph.",
    ):
        assert piece in text


def _random_line(rng: random.Random, words: list[str]) -> str:
    def w() -> str:
        return " ".join(rng.sample(words, rng.randint(1, 4)))

    makers = [
        lambda: f"# {w()}",
        lambda: f"## {w()}",
        lambda: "---",
        lambda: "===",
        lambda: "***",
        lambda: f"[{rng.choice(words)}]: https://example.com/{rng.choice(words)}",
        lambda: f"- {w()}",
        lambda: f"1. {w()}",
        lambda: f"> {w()}",
        lambda: f"    {w()}",
        lambda: "```",
        lambda: f"| {rng.choice(words)} | {rng.choice(words)} |",
        lambda: "|---|---|",
        lambda: f"<div>{rng.choice(words)}</div>",
        lambda: "",
        w,
        w,
    ]
    return rng.choice(makers)()


@pytest.mark.parametrize("plain_text", [False, True])
def test_no_source_word_is_lost(plain_text: bool) -> None:
    # Every word of the source reaches some chunk's retrieval text (body or
    # breadcrumb), whatever mix of Markdown-looking lines it is made of.
    rng = random.Random(1234)  # noqa: S311 — reproducible test data, not crypto
    words = [f"w{i}" for i in range(400)]
    for _ in range(200):
        src = "\n".join(_random_line(rng, words) for _ in range(rng.randint(1, 25)))
        chunks = _chunk_markdown(src, max_tokens=512, overlap_tokens=0, plain_text=plain_text)
        joined = " ".join(c.text for c in chunks)
        missing = set(re.findall(r"w\d+", src)) - set(re.findall(r"w\d+", joined))
        assert not missing, (src, missing)


def test_token_window_split_cuts_between_words() -> None:
    # A run with no sentence break is cut on token windows; a cut inside a
    # word left "T" and "riage" in the chunk, so "Triage" matched nothing.
    text = " ".join(f"Triage{i} affected routes" for i in range(80))
    parts = _split_by_tokens(text, 7)
    assert len(parts) > 1
    assert "".join(parts) == text
    found = set(re.findall(r"\w+", " ".join(parts)))
    assert set(re.findall(r"\w+", text)) <= found


def test_long_unbroken_line_keeps_every_word_whole() -> None:
    line = "\\\\n- ".join(f"Triage{i}: identify affected routes" for i in range(200))
    joined = " ".join(c.text for c in _chunk_markdown(line, max_tokens=64, overlap_tokens=8))
    assert set(re.findall(r"\w+", line)) <= set(re.findall(r"\w+", joined))


# The chunk cap bounds the whole embedded text — document title + breadcrumb +
# body, as Onyx sizes chunks (B-200) — not the body alone.
def test_chunk_cap_bounds_title_breadcrumb_and_body_together() -> None:
    title = " ".join(f"title{i}" for i in range(12))
    headings = "".join(
        f"{'#' * (i + 1)} " + " ".join(f"h{i}w{j}" for j in range(8)) + "\n\n" for i in range(5)
    )
    body = " ".join(f"word{i}." for i in range(400))
    chunks = _chunk_markdown(headings + body, max_tokens=64, overlap_tokens=8, doc_title=title)
    assert len(chunks) > 1
    for c in chunks:
        assert count_tokens(chunk_retrieval_text(c.heading_path, c.body, title)) <= 64 + 4


def test_long_breadcrumb_drops_outer_headings_first_but_keeps_their_text() -> None:
    headings = "".join(f"{'#' * (i + 1)} level{i} " + "pad " * 10 + "\n\n" for i in range(5))
    chunks = _chunk_markdown(headings + "Body text.\n", max_tokens=64, overlap_tokens=0)
    assert all(c.heading_path and c.heading_path[-1].startswith("level4") for c in chunks)
    assert not any(h.startswith("level0") for c in chunks for h in c.heading_path)
    assert all(count_tokens(chunk_retrieval_text(c.heading_path, "")) <= 64 // 3 for c in chunks)
    # The headings cut from the breadcrumb open the body instead (B-200 regression).
    bodies = " ".join(c.body for c in chunks)
    assert all(f"level{i}" in bodies for i in range(4))
    assert all(count_tokens(c.text) <= 64 + 4 for c in chunks)


def test_a_breadcrumb_within_a_third_of_the_cap_keeps_every_level() -> None:
    # Continuation chunks of a deep section kept fewer levels than they did at
    # a 512 cap once the budget was a quarter of 400 (B-206 K-3 dev set).
    names = tuple(f"第{i}级标题关于渠道合作伙伴关于渠道合作伙伴" for i in range(4))
    headings = "".join(f"{'#' * (i + 1)} {name}\n\n" for i, name in enumerate(names))
    prefix = count_tokens(chunk_retrieval_text(names, ""))
    assert 400 // 4 < prefix <= 400 // 3
    chunks = _chunk_markdown(headings + _long("正文内容。"), max_tokens=400, overlap_tokens=0)
    assert all(len(c.heading_path) == 4 for c in chunks)


def test_overlap_is_dropped_when_it_would_push_a_chunk_past_the_cap() -> None:
    # Two paragraphs each just under the cap: carrying the first one's tail
    # in front of the second would overrun it, so that chunk opens without.
    para_a = " ".join(f"alpha{i}" for i in range(26))
    para_b = " ".join(f"beta{i}" for i in range(26))
    chunks = _chunk_markdown(f"{para_a}\n\n{para_b}\n", max_tokens=64, overlap_tokens=16)
    assert len(chunks) == 2
    assert all(count_tokens(c.text) <= 64 for c in chunks)


# ---------------------------------------------------------------------------
# B-206 K-3: heading text never lost, short sections joined, oversized blocks
# cut where it does least harm (chapter / page / line), tables run together.
# ---------------------------------------------------------------------------


def _long(text: str) -> str:
    """``text`` repeated past SHORT_SECTION_TOKENS: a section that stands as
    a chunk of its own."""
    repeated = text
    while count_tokens(repeated) <= SHORT_SECTION_TOKENS + 10:
        repeated = f"{repeated} {text}"
    return repeated


def _squashed(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _all_text(chunks: Sequence[TextChunk]) -> str:
    return _squashed("".join(c.text for c in chunks))


# A slide title read as a heading: under 200 characters, over a quarter of
# the cap in tokens — the breadcrumb has no room for it (B-200 regression).
_LONG_HEADING = "年度产品线规划与渠道合作伙伴激励方案" * 9


def test_a_heading_the_breadcrumb_has_no_room_for_keeps_its_text() -> None:
    assert count_tokens(_LONG_HEADING) > 400 // 4
    src = f"# {_LONG_HEADING}\n\n{_long('渠道返点按季度结算。')}"
    chunks = _chunk_markdown(src, max_tokens=400, overlap_tokens=0)
    assert all(_LONG_HEADING not in c.heading_path for c in chunks)
    assert _squashed(_LONG_HEADING) in _squashed("".join(c.body for c in chunks))
    assert all(count_tokens(c.text) <= 400 + 4 for c in chunks)


def test_an_inner_heading_too_long_for_the_breadcrumb_leaves_the_outer_ones_on_it() -> None:
    body = _long("渠道返点按季度结算。")
    chunks = _chunk_markdown(
        f"# 渠道大会\n\n## {_LONG_HEADING}\n\n{body}", max_tokens=400, overlap_tokens=0
    )
    assert [c.heading_path for c in chunks] == [("渠道大会",)]
    assert chunks[0].body.startswith(_LONG_HEADING)


def test_a_dropped_heading_is_written_into_one_body_only() -> None:
    body = _long("渠道返点按季度结算。")
    src = f"# {_LONG_HEADING}\n\n## 华东\n\n{body}\n\n## 华南\n\n{body}"
    chunks = _chunk_markdown(src, max_tokens=400, overlap_tokens=0)
    holding = [c for c in chunks if _squashed(_LONG_HEADING) in _squashed(c.body)]
    assert len(holding) == 1


def test_short_sibling_sections_join_the_next_chunk() -> None:
    after_sales = _long("七天内无理由退货。")
    src = (
        "# 产品\n\n## 规格\n\n尺寸 10 厘米。\n\n## 价格\n\n每件 500 元。\n\n"
        f"## 售后\n\n{after_sales}"
    )
    chunks = _chunk_markdown(src, max_tokens=400, overlap_tokens=0)
    assert chunks == [
        TextChunk(
            body=f"尺寸 10 厘米。\n\n价格\n\n每件 500 元。\n\n售后\n\n{after_sales}",
            heading_path=("产品", "规格"),
        )
    ]


def test_a_heading_with_nothing_under_it_joins_the_next_section() -> None:
    body = _long("员工入职当天领取工牌。")
    chunks = _chunk_markdown(
        f"# 手册\n\n## 第一章\n\n## 第二章\n\n{body}", max_tokens=400, overlap_tokens=0
    )
    assert chunks == [TextChunk(body=f"第一章\n\n第二章\n\n{body}", heading_path=("手册",))]


def test_a_short_section_does_not_join_another_parent() -> None:
    other = _long("beta content.")
    src = f"# A\n\n## A1\n\nshort a1.\n\n# B\n\n{other}"
    chunks = _chunk_markdown(src, max_tokens=400, overlap_tokens=0)
    assert chunks == [
        TextChunk(body="short a1.", heading_path=("A", "A1")),
        TextChunk(body=other, heading_path=("B",)),
    ]


def test_a_trailing_short_section_joins_the_chunk_before() -> None:
    first = _long("alpha content.")
    chunks = _chunk_markdown(
        f"# A\n\n{first}\n\n# B\n\nlast words.", max_tokens=400, overlap_tokens=0
    )
    assert chunks == [TextChunk(body=f"{first}\n\nB\n\nlast words.", heading_path=("A",))]


def test_a_join_never_passes_the_cap() -> None:
    intro = " ".join(f"intro{i}." for i in range(8))
    words = 0
    while count_tokens(" ".join(f"word{i}." for i in range(words))) < 385:
        words += 1
    full = " ".join(f"word{i}." for i in range(words))
    assert count_tokens(intro) < SHORT_SECTION_TOKENS
    assert count_tokens(f"{intro}\n\nB\n\n{full}") > 400  # joined, it would not fit
    src = f"# A\n\n{intro}\n\n# B\n\n{full}"
    chunks = _chunk_markdown(src, max_tokens=400, overlap_tokens=0)
    assert [c.heading_path for c in chunks] == [("A",), ("B",)]
    assert all(count_tokens(c.text) <= 400 + 4 for c in chunks)


def test_short_entries_join_until_they_are_no_longer_short() -> None:
    entries = [f"第 {i} 号订单怎样改地址?" for i in range(24)]
    src = "# 常见问题\n\n" + "\n\n".join(
        f"## 条目 {i}\n\n{entry}" for i, entry in enumerate(entries)
    )
    chunks = _chunk_markdown(src, max_tokens=400, overlap_tokens=0)
    assert 1 < len(chunks) < len(entries) // 2
    assert all(
        SHORT_SECTION_TOKENS <= count_tokens(c.body) < 2 * SHORT_SECTION_TOKENS + 20
        for c in chunks[:-1]
    )
    joined = _all_text(chunks)
    assert all(_squashed(entry) in joined for entry in entries)


def _sized(text: str, tokens: int) -> str:
    """``text`` repeated to about ``tokens`` tokens."""
    repeated = text
    while count_tokens(repeated) < tokens:
        repeated = f"{repeated}{text}"
    return repeated


def test_a_focused_section_over_the_join_threshold_keeps_its_own_chunk() -> None:
    # An 80-token section joined into a bigger neighbour was outranked by
    # chunks about only its subject (B-206 K-3 dev set: 3 questions lost).
    warranty = _sized("整机保修一年,电池保修半年。", 80)
    returns = _long("七天内无理由退货。")
    assert SHORT_SECTION_TOKENS < count_tokens(warranty) < 100
    src = f"# 售后\n\n## 保修\n\n{warranty}\n\n## 退货\n\n{returns}"
    chunks = _chunk_markdown(src, max_tokens=400, overlap_tokens=0)
    assert chunks == [
        TextChunk(body=warranty, heading_path=("售后", "保修")),
        TextChunk(body=returns, heading_path=("售后", "退货")),
    ]


def test_a_short_section_joins_one_neighbour_and_stops() -> None:
    note = "以下为保修条款。"
    warranty = _sized("整机保修一年,电池保修半年。", 80)
    returns = _sized("七天内无理由退货。", 80)
    assert count_tokens(note) < SHORT_SECTION_TOKENS
    src = f"# 售后\n\n## 说明\n\n{note}\n\n## 保修\n\n{warranty}\n\n## 退货\n\n{returns}"
    chunks = _chunk_markdown(src, max_tokens=400, overlap_tokens=0)
    assert [c.body for c in chunks] == [f"{note}\n\n保修\n\n{warranty}", returns]


_CHAT = [
    f"客户:第 {i} 号订单还没发货。能不能加急。地址也要改一下。改成公司的地址。"
    if i % 2 == 0
    else f"客服:您好。第 {i} 号订单明天发出。加急要另付十元。地址已经改好了。请问还有别的问题吗。"
    for i in range(60)
]


@pytest.mark.parametrize("plain_text", [False, True])
def test_a_conversation_is_cut_between_lines(plain_text: bool) -> None:
    src = "\n".join(_CHAT)  # one paragraph: no blank line between utterances
    # Cut at sentence marks, some utterance would be cut in two.
    by_sentence = _hard_split(src, 128)
    assert any(line not in _CHAT for piece in by_sentence for line in piece.splitlines() if line)
    chunks = _chunk_markdown(src, max_tokens=128, overlap_tokens=0, plain_text=plain_text)
    assert len(chunks) > 1
    lines = [line for c in chunks for line in c.body.splitlines() if line.strip()]
    assert lines == _CHAT


def test_a_timestamped_chat_export_is_cut_between_lines() -> None:
    # "[time] name name<full-width colon> text": a speaker label after a time stamp.
    lines = [
        f"[2026-04-02 10:{i:02d}] {'销售 小林' if i % 2 else '客户'}\uff1a"
        "第一句话说明情况。第二句话补充细节。第三句话提出问题。第四句话表示感谢。"
        for i in range(40)
    ]
    chunks = _chunk_markdown("\n".join(lines), max_tokens=128, overlap_tokens=0, plain_text=True)
    assert len(chunks) > 1
    assert [line for c in chunks for line in c.body.splitlines()] == lines


def test_a_list_of_short_lines_is_cut_between_lines() -> None:
    rows = [f"第 {i} 项。型号 A{i}。尺寸 {i} 厘米。" for i in range(80)]
    chunks = _chunk_markdown("\n".join(rows), max_tokens=128, overlap_tokens=0, plain_text=True)
    assert len(chunks) > 1
    assert [line for c in chunks for line in c.body.splitlines()] == rows


def test_prose_is_still_cut_at_sentence_ends() -> None:
    prose = "".join(f"这是第 {i} 句完整的说明文字,内容比较长一些。" for i in range(80))
    chunks = _chunk_markdown(prose, max_tokens=128, overlap_tokens=0, plain_text=True)
    assert len(chunks) > 1
    assert all(c.body.endswith("。") for c in chunks)


def test_an_oversized_text_is_cut_in_front_of_a_chapter_line() -> None:
    part = "".join(f"第 {i} 条说明的具体内容写在这里。" for i in range(16))
    src = f"第一章 总则\n{part}\n第二章 细则\n{part}"
    assert count_tokens(src) > 400 > count_tokens(f"第一章 总则\n{part}")
    chunks = _chunk_markdown(src, max_tokens=400, overlap_tokens=0, plain_text=True)
    assert [c.body.split("\n", 1)[0] for c in chunks] == ["第一章 总则", "第二章 细则"]


def test_a_page_break_is_a_cut_point() -> None:
    page = " ".join(f"line{i} of the page." for i in range(60))
    chunks = _chunk_markdown(f"{page}\f{page}", max_tokens=400, overlap_tokens=0, plain_text=True)
    assert [c.body for c in chunks] == [page, page]


@pytest.mark.parametrize(
    ("line", "cut"),
    [
        ("1. 总则", True),
        ("1.总则", True),
        ("2.3\u3001适用范围", True),
        ("\u4e00\u3001目的", True),
        ("\uff08\u4e8c\uff09职责", True),
        ("第三章 细则", True),
        ("第 2 条 适用", True),
        ("---", True),
        ("3.5 元的价格说明", False),
        ("12.5% 的折扣", False),
        ("2026.10.24 会议", False),
        ("普通的一行文字", False),
    ],
)
def test_cut_lines(line: str, cut: bool) -> None:
    assert bool(_CUT_LINE.match(line)) is cut


def _faq(count: int, answer_tokens: int, question: str) -> list[tuple[str, str]]:
    return [
        (
            question.format(i=i),
            "**答:**" + _sized(f"第 {i} 条的处理办法写在这里。", answer_tokens),
        )
        for i in range(count)
    ]


@pytest.mark.parametrize(
    "question",
    [
        "{i}. **第 {i} 种情况怎么处理\uff1f**",
        "**问:第 {i} 种情况怎么处理?**",
        "Q{i}: how is case {i} handled?",
        "第 {i} 种情况的处理步骤如下\uff1a",
        "**第 {i} 种情况的注意事项**",
        "{i}. 第 {i} 种情况",
    ],
)
def test_a_lead_in_line_never_ends_a_chunk(question: str) -> None:
    # FAQ-style Word lists: a question paragraph, then its answer. A cut right
    # after the question left the answer's chunk without it (B-206 K-3 dev
    # set: 4 questions lost) — the question opens the next chunk instead.
    entries = _faq(12, 110, question)
    src = "\n\n".join(f"{q}\n\n{a}" for q, a in entries)
    chunks = _chunk_markdown(src, max_tokens=400, overlap_tokens=0)
    for q, a in entries:
        holding = [c for c in chunks if a[:20] in c.body]
        assert holding and all(q in c.body for c in holding)
    assert all(count_tokens(c.text) <= 400 + 4 for c in chunks)


def test_a_question_line_in_a_conversation_opens_the_next_piece() -> None:
    lines: list[str] = []
    for i in range(30):
        lines.append(f"客户:第 {i} 号订单什么时候发货\uff1f")
        lines.append(f"客服:第 {i} 号订单" + "明天上午从仓库发出,物流单号稍后短信通知。" * 2)
    chunks = _chunk_markdown("\n".join(lines), max_tokens=128, overlap_tokens=0, plain_text=True)
    assert len(chunks) > 2
    for question, answer in zip(lines[::2], lines[1::2], strict=True):
        holding = [c for c in chunks if answer in c.body.splitlines()]
        assert holding and all(question in c.body.splitlines() for c in holding)


def test_an_answer_cut_in_pieces_repeats_its_question_on_every_piece() -> None:
    question = "7. **退货的运费由谁承担\uff1f**"
    answer = "**答:**" + "".join(f"第 {i} 种情况下运费由卖家承担。" for i in range(80))
    chunks = _chunk_markdown(f"{question}\n\n{answer}", max_tokens=128, overlap_tokens=0)
    assert len(chunks) > 1
    assert all(c.body.startswith(question) for c in chunks)
    assert all(count_tokens(c.text) <= 128 + 4 for c in chunks)
    for sentence in re.findall(r"第 \d+ 种[^。]*。", answer):
        assert any(_squashed(sentence) in _squashed(c.body) for c in chunks)


def test_a_sentence_ending_paragraph_is_not_held_back() -> None:
    first = _sized("这是一段完整的说明。", 300)
    second = _sized("这是另一段完整的说明。", 300)
    chunks = _chunk_markdown(f"{first}\n\n{second}", max_tokens=400, overlap_tokens=0)
    assert [c.body for c in chunks] == [first, second]


def test_a_table_row_over_the_cap_repeats_its_leading_cells_on_every_piece() -> None:
    # An FAQ sheet row whose answer cell outruns the cap: every piece is a
    # whole row naming its category and question, under the header (B-206 K-3
    # dev set: 5 questions lost when the pieces carried the header only).
    answer = "".join(f"第 {i} 步按照说明书操作即可。" for i in range(90))
    header = "| 分类 | 问题 | 回答 | 标签 |"
    src = f"{header}\n| --- | --- | --- | --- |\n| 售后 | 退货运费谁承担? | {answer} | 运费 |\n"
    chunks = _chunk_markdown(src, max_tokens=128, overlap_tokens=0)
    assert len(chunks) > 1
    for chunk in chunks:
        rows = chunk.body.splitlines()
        assert rows[0] == header
        assert all(row.startswith("| 售后 | 退货运费谁承担? | ") for row in rows[2:])
        assert all(row.count("|") == 5 for row in rows[2:])
    assert all(count_tokens(c.text) <= 128 for c in chunks)
    steps = re.findall(r"第 \d+ 步[^。]*。", answer)
    assert len(steps) == 90
    assert all(_squashed(step) in _all_text(chunks) for step in steps)
    assert "运费" in chunks[0].body.splitlines()[2].rsplit("|", 2)[1]


def test_prose_cut_inside_a_paragraph_carries_the_sentence_before_the_cut() -> None:
    # Overlap only where a paragraph has to be cut (FastGPT / Unstructured):
    # each piece after the first opens with the end of the piece before it.
    prose = "".join(f"这是第 {i} 句完整的说明文字,内容比较长一些。" for i in range(160))
    chunks = _chunk_markdown(prose, max_tokens=400, overlap_tokens=0, plain_text=True)
    assert len(chunks) > 2
    for before, after in pairwise(chunks):
        last = re.findall(r"[^。]+。", before.body)[-1]
        assert after.body.startswith("这是第")
        assert last in after.body[: 3 * len(last)]
    assert all(count_tokens(c.text) <= 400 for c in chunks)


def test_tables_run_together_keep_their_own_headers() -> None:
    first = "| item | qty |\n| --- | --- |\n" + "\n".join(f"| a{i} | {i} |" for i in range(40))
    second = "| code | name | price |\n| --- | --- | --- |\n" + "\n".join(
        f"| c{i} | n{i} | {i}.0 |" for i in range(40)
    )
    chunks = _chunk_markdown(f"# T\n\n{first}\n{second}", max_tokens=128, overlap_tokens=0)
    for chunk in chunks:
        rows = chunk.body.splitlines()
        three = [row for row in rows if row.count("|") == 4]
        two = [row for row in rows if row.count("|") == 3]
        if three:
            assert rows[0] == "| code | name | price |"
        if two:
            assert rows[0] == "| item | qty |"


def test_chunk_params_hash_follows_the_parameters_and_the_switch() -> None:
    base = chunk_params_hash(max_tokens=400, overlap_tokens=0)
    assert base == chunk_params_hash(max_tokens=400, overlap_tokens=0)
    assert base != chunk_params_hash(max_tokens=512, overlap_tokens=0)
    assert base != chunk_params_hash(max_tokens=400, overlap_tokens=64)
    configure_semantic_breaks(enabled=True)
    try:
        assert base != chunk_params_hash(max_tokens=400, overlap_tokens=0)
    finally:
        configure_semantic_breaks(enabled=False)
