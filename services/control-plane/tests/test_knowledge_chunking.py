"""Tests for J.5 structure-aware + semantic Markdown chunking."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge.chunking import (
    MIN_SEMANTIC_CHUNK_TOKENS,
    TextChunk,
    _cosine_distance,
    _encoding,
    _hard_split,
    _split_by_tokens,
    _tail_text,
    count_tokens,
)
from control_plane.knowledge.chunking import chunk_markdown as _chunk_markdown
from control_plane.knowledge.chunking import chunk_markdown_semantic as _chunk_markdown_semantic


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
    src = "# A\n\nalpha content.\n\n# B\n\nbeta content."
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
    src = "# A\n\nuniquealphatoken here.\n\n# B\n\nuniquebetatoken here."
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
    src = "# A\n\nTOPICA content.\n\n# B\n\nTOPICA other."
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
async def test_semantic_short_block_still_stops_at_a_heading() -> None:
    """Only a topic shift is skipped; a section boundary still flushes."""
    src = "# A\n\nTOPICA short.\n\n# B\n\n" + _para("TOPICB", "beta")
    embedder = _ScriptedEmbedder({"TOPICA": (1.0, 0.0), "TOPICB": (0.0, 1.0)})
    chunks = await chunk_markdown_semantic(
        src, max_tokens=512, overlap_tokens=0, embedder=embedder, tenant_id=uuid4()
    )
    assert chunks == ["[Section: A]\n\nTOPICA short.", f"[Section: B]\n\n{_para('TOPICB', 'beta')}"]


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
    assert [(c.heading_path, c.body) for c in chunks] == [
        (("A",), "intro"),
        (("B",), "Refund window is 14 days."),
    ]


# A document about LLMs quotes the tokenizer's special tokens as plain text;
# tiktoken refuses to encode them unless told they are ordinary text.
_SPECIAL_TOKENS = sorted(_encoding.special_tokens_set)


@pytest.mark.parametrize("special", _SPECIAL_TOKENS)
def test_special_token_text_is_counted_as_ordinary_text(special: str) -> None:
    assert count_tokens(f"the model stops at {special} here") > 1


@pytest.mark.parametrize("special", _SPECIAL_TOKENS)
def test_special_token_text_survives_chunking(special: str) -> None:
    src = f"# Tokenizer\n\nEach sample ends with {special} before padding.\n\n# Next\n\nOther text.\n"
    text = "\n".join(chunk_markdown(src, max_tokens=512, overlap_tokens=0))
    assert f"Each sample ends with {special} before padding." in text


def test_special_token_text_survives_token_window_split_and_overlap() -> None:
    text = "".join(f"x{i}{special}" for i in range(20) for special in _SPECIAL_TOKENS)
    parts = _split_by_tokens(text, 7)
    assert len(parts) > 1
    assert "".join(parts) == text
    assert text.endswith(_tail_text(text, 5))
