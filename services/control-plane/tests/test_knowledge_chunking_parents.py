"""B-206 K-1: parent chunks — retrieval on children, the parent returned.

Parents are cut first, children inside them (Dify's order): a parent is a run
of whole blocks within :data:`PARENT_MAX_TOKENS`, closed early at a level-1 /
level-2 heading once at least half full, never right after a line that
introduces the next block; an oversized block is cut to the parent size first
(a table keeps its header, a long row its leading cells). Children are the K-3
chunks of each parent's blocks — they never cross a parent.
"""

from __future__ import annotations

import random
import re
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge.chunking import (
    _CELL_SPLIT,
    _LINE_BREAK,
    CHUNKER_VERSION,
    PARENT_MAX_TOKENS,
    ParentChunk,
    TextChunk,
    chunk_markdown,
    chunk_markdown_parented,
    chunk_markdown_semantic,
    chunk_markdown_semantic_parented,
    chunk_params_hash,
    count_tokens,
    structural_chunk_count,
)
from expert_work.protocol import chunk_retrieval_text

_TENANT = uuid4()


def _squashed(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _sized(text: str, tokens: int) -> str:
    """``text`` repeated to about ``tokens`` tokens."""
    repeated = text
    while count_tokens(repeated) < tokens:
        repeated = f"{repeated}{text}"
    return repeated


def _parent_tokens(parent: ParentChunk, doc_title: str | None = None) -> int:
    """A parent measured as a child is: title, breadcrumb and body."""
    return count_tokens(chunk_retrieval_text(parent.heading_path, parent.body, doc_title))


def _parent_text(parent: ParentChunk) -> str:
    return _squashed(" ".join(parent.heading_path) + parent.body)


def _uncovered(child: TextChunk, parent: ParentChunk) -> list[str]:
    """Lines of ``child``'s body whose text is not in ``parent`` — a table line
    cell by cell (a long row's continuation repeats its leading cells; an
    overlap may start inside a row), any other line whole. Whitespace is
    ignored: pieces are cut and re-joined there."""
    text = _parent_text(parent)
    missing: list[str] = []
    for line in _LINE_BREAK.split(child.body):
        if not line.strip():
            continue
        if "|" in line:
            cells = _CELL_SPLIT.split(line.strip().strip("|"))
            missing.extend(cell for cell in cells if _squashed(cell) not in text)
        elif _squashed(line) not in text:
            missing.append(line)
    return missing


def _paragraphs(body: str) -> list[str]:
    return [part for part in body.split("\n\n") if part.strip()]


# ---------------------------------------------------------------------------
# Documents of every shape the checks below run over
# ---------------------------------------------------------------------------


def _random_doc(rng: random.Random) -> str:
    words = [f"w{i}" for i in range(400)]

    def w() -> str:
        return " ".join(rng.sample(words, rng.randint(1, 4)))

    def paragraph() -> str:
        return " ".join(f"{w()}." for _ in range(rng.randint(1, 60)))

    makers = [
        lambda: f"# {w()}",
        lambda: f"## {w()}",
        lambda: f"### {w()}",
        lambda: f"**{w()}?**",
        lambda: f"{w()}:",
        lambda: (
            "| a | b |\n| --- | --- |\n"
            + "\n".join(f"| {w()} | {w()} |" for _ in range(rng.randint(1, 80)))
        ),
        lambda: "\n".join(f"- {w()}" for _ in range(rng.randint(1, 30))),
        paragraph,
        paragraph,
        paragraph,
    ]
    return "\n\n".join(rng.choice(makers)() for _ in range(rng.randint(1, 60)))


def _fixed_docs() -> list[str]:
    big_table = "| 编号 | 名称 | 说明 |\n| --- | --- | --- |\n" + "\n".join(
        f"| {i} | 产品{i} | 第 {i} 号产品的说明文字。 |" for i in range(300)
    )
    long_row = "".join(f"第 {i} 步按照说明书操作即可。" for i in range(400))
    faq = "\n\n".join(
        f"**问:第 {i} 种情况怎么处理?**\n\n" + _sized(f"第 {i} 种情况的处理办法。", 90 + 7 * i)
        for i in range(40)
    )
    chapters = "\n\n".join(
        f"# 第{c}章\n\n"
        + "\n\n".join(
            f"## {c}.{s} 小节\n\n" + _sized(f"第{c}章第{s}节的正文。", 60 + 40 * s)
            for s in range(6)
        )
        for c in range(5)
    )
    return [
        big_table,
        f"| 分类 | 问题 | 回答 |\n| --- | --- | --- |\n| 售后 | 运费谁承担? | {long_row} |",
        "".join(f"这是第 {i} 句完整的说明文字,内容比较长一些。" for i in range(400)),
        faq,
        chapters,
        f"# {'很长的标题' * 30}\n\n" + _sized("正文。", 3000),
    ]


def _docs() -> list[str]:
    rng = random.Random(2061)  # noqa: S311 — reproducible test data, not crypto
    return _fixed_docs() + [_random_doc(rng) for _ in range(60)]


_DOCS = _docs()
_SIZES = [(400, 0), (128, 16), (1024, 0)]


@pytest.mark.parametrize(("max_tokens", "overlap"), _SIZES)
def test_the_children_of_the_parents_are_the_chunks_in_order(max_tokens: int, overlap: int) -> None:
    for doc in _DOCS:
        parents = chunk_markdown_parented(
            doc, max_tokens=max_tokens, overlap_tokens=overlap, doc_title="t"
        )
        assert all(parent.children for parent in parents)
        flat = [child for parent in parents for child in parent.children]
        assert flat == chunk_markdown(
            doc, max_tokens=max_tokens, overlap_tokens=overlap, doc_title="t"
        )


@pytest.mark.parametrize(("max_tokens", "overlap"), _SIZES)
def test_a_parent_holds_all_the_text_of_its_children(max_tokens: int, overlap: int) -> None:
    for doc in _DOCS:
        for parent in chunk_markdown_parented(doc, max_tokens=max_tokens, overlap_tokens=overlap):
            for child in parent.children:
                assert not _uncovered(child, parent), doc[:80]


@pytest.mark.parametrize("doc_title", [None, "产品手册\uff1a益家人.docx"])
def test_no_parent_is_over_the_parent_cap(doc_title: str | None) -> None:
    for doc in _DOCS:
        parents = chunk_markdown_parented(
            doc, max_tokens=400, overlap_tokens=0, doc_title=doc_title
        )
        assert all(_parent_tokens(p, doc_title) <= PARENT_MAX_TOKENS for p in parents)


def test_the_parent_cap_is_a_parameter() -> None:
    doc = _sized("这是一段完整的说明。", 3000)
    small = chunk_markdown_parented(doc, max_tokens=128, overlap_tokens=0, parent_max_tokens=512)
    assert len(small) > len(chunk_markdown_parented(doc, max_tokens=128, overlap_tokens=0))
    assert all(_parent_tokens(p) <= 512 for p in small)


def test_no_source_word_is_lost_from_the_parents() -> None:
    # Every word of the source — heading text included — reaches some parent's
    # body or breadcrumb, as it reaches some child's (the K-3 check).
    for doc in _DOCS:
        parents = chunk_markdown_parented(doc, max_tokens=400, overlap_tokens=0)
        source = set(re.findall(r"w\d+", doc))
        in_parents = {
            w for p in parents for w in re.findall(r"w\d+", " ".join([*p.heading_path, p.body]))
        }
        in_children = {w for p in parents for c in p.children for w in re.findall(r"w\d+", c.text)}
        assert source <= in_parents
        assert source <= in_children


def test_every_heading_is_in_some_child_and_some_parent() -> None:
    long_heading = "年度产品线规划与渠道合作伙伴激励方案" * 9
    src = (
        f"# {long_heading}\n\n## 华东\n\n{_sized('华东渠道返点按季度结算。', 700)}\n\n"
        f"## 华南\n\n{_sized('华南渠道返点按月结算。', 700)}\n\n## 空的一节\n\n"
        f"# 附录\n\n### 术语\n\n{_sized('术语表。', 80)}"
    )
    parents = chunk_markdown_parented(src, max_tokens=400, overlap_tokens=0)
    assert len(parents) > 1
    for heading in (long_heading, "华东", "华南", "空的一节", "附录", "术语"):
        assert any(_squashed(heading) in _squashed(c.text) for p in parents for c in p.children)
        assert any(_squashed(heading) in _parent_text(p) for p in parents)


# ---------------------------------------------------------------------------
# What a parent repeats: nothing
# ---------------------------------------------------------------------------


def test_a_table_is_one_parent_with_its_header_once() -> None:
    header = "| 编号 | 名称 | 单价 |"
    table = f"{header}\n| --- | --- | --- |\n" + "\n".join(
        f"| {i} | 产品{i} | {i}.5 |" for i in range(60)
    )
    assert 400 < count_tokens(table) < PARENT_MAX_TOKENS - 50
    [parent] = chunk_markdown_parented(table, max_tokens=128, overlap_tokens=0)
    assert len(parent.children) > 1
    assert all(child.body.startswith(header) for child in parent.children)
    assert parent.body == table
    assert parent.body.count(header) == 1


def test_a_table_over_the_parent_cap_is_cut_by_rows_under_its_header() -> None:
    header = "| 编号 | 名称 | 说明 |"
    rows = [f"| {i} | 产品{i} | 第 {i} 号产品的说明文字。 |" for i in range(300)]
    table = "\n".join([header, "| --- | --- | --- |", *rows])
    assert count_tokens(table) > 3 * PARENT_MAX_TOKENS
    parents = chunk_markdown_parented(table, max_tokens=400, overlap_tokens=0)
    assert len(parents) > 3
    for parent in parents:
        lines = parent.body.splitlines()
        assert lines[:2] == [header, "| --- | --- | --- |"]
        assert parent.body.count(header) == 1
        assert _parent_tokens(parent) <= PARENT_MAX_TOKENS
    placed = [line for p in parents for line in p.body.splitlines()[2:]]
    assert placed == rows  # every row in exactly one parent, in order


def test_a_row_over_the_parent_cap_repeats_its_leading_cells_in_every_parent() -> None:
    answer = "".join(f"第 {i} 步按照说明书操作即可。" for i in range(400))
    src = f"| 分类 | 问题 | 回答 |\n| --- | --- | --- |\n| 售后 | 运费谁承担? | {answer} |"
    parents = chunk_markdown_parented(src, max_tokens=400, overlap_tokens=0)
    assert len(parents) > 1
    for parent in parents:
        rows = parent.body.splitlines()
        assert rows[0] == "| 分类 | 问题 | 回答 |"
        assert all(row.startswith("| 售后 | 运费谁承担? | ") for row in rows[2:])
        assert _parent_tokens(parent) <= PARENT_MAX_TOKENS
    steps = re.findall(r"第 \d+ 步[^。]*。", answer)
    assert all(any(_squashed(s) in _parent_text(p) for p in parents) for s in steps)


def test_a_paragraph_over_the_parent_cap_is_cut_to_parent_size() -> None:
    prose = "".join(f"这是第 {i} 句完整的说明文字,内容比较长一些。" for i in range(400))
    parents = chunk_markdown_parented(prose, max_tokens=400, overlap_tokens=0)
    assert len(parents) > 2
    assert all(_parent_tokens(p) <= PARENT_MAX_TOKENS for p in parents)
    assert all(_parent_tokens(p) > PARENT_MAX_TOKENS // 2 for p in parents[:-1])
    sentences = re.findall(r"这是第 \d+ 句[^。]*。", prose)
    assert all(any(s in p.body for p in parents) for s in sentences)


def test_a_lead_in_is_written_once_in_its_parent() -> None:
    question = "7. **退货的运费由谁承担\uff1f**"
    answer = "**答:**" + "".join(f"第 {i} 种情况下运费由卖家承担。" for i in range(40))
    [parent] = chunk_markdown_parented(f"{question}\n\n{answer}", max_tokens=128, overlap_tokens=0)
    assert len(parent.children) > 1
    assert all(child.body.startswith(question) for child in parent.children)
    assert parent.body == f"{question}\n\n{answer}"


# ---------------------------------------------------------------------------
# Where a parent closes
# ---------------------------------------------------------------------------


def _chapter(heading: str, marker: str, tokens: int) -> str:
    """A section of about ``tokens`` tokens in paragraphs of about 100."""
    paragraphs = [_sized(f"{marker}第{i}段的说明文字。", 100) for i in range(max(tokens // 100, 1))]
    return f"{heading}\n\n" + "\n\n".join(paragraphs)


@pytest.mark.parametrize("level", ["#", "##"])
def test_a_level_one_or_two_heading_closes_a_parent_at_least_half_full(level: str) -> None:
    src = "\n\n".join([_chapter(f"{level} 甲章", "甲", 600), _chapter(f"{level} 乙章", "乙", 600)])
    parents = chunk_markdown_parented(src, max_tokens=400, overlap_tokens=0)
    assert [("甲" in p.body, "乙" in p.body) for p in parents] == [(True, False), (False, True)]
    assert [p.heading_path for p in parents] == [("甲章",), ("乙章",)]


def test_a_heading_change_keeps_a_parent_open_while_under_half_full() -> None:
    src = "\n\n".join([_chapter("# 甲章", "甲", 300), _chapter("# 乙章", "乙", 1000)])
    parents = chunk_markdown_parented(src, max_tokens=400, overlap_tokens=0)
    assert "甲" in parents[0].body and "乙" in parents[0].body
    assert parents[0].body.index("乙章") < parents[0].body.index("乙第0段")


def test_a_level_three_heading_does_not_close_a_parent_early() -> None:
    src = "# 手册\n\n" + "\n\n".join(
        [_chapter("### 甲节", "甲", 600), _chapter("### 乙节", "乙", 600)]
    )
    parents = chunk_markdown_parented(src, max_tokens=400, overlap_tokens=0)
    assert "乙" in parents[0].body  # filled by size: the level-3 change is no boundary
    assert _parent_tokens(parents[0]) > PARENT_MAX_TOKENS - 120


def test_a_document_within_the_parent_cap_is_one_parent_whatever_its_chapters() -> None:
    src = "\n\n".join([_chapter("# 甲章", "甲", 600), _chapter("# 乙章", "乙", 200)])
    assert count_tokens(_chapter("# 甲章", "甲", 600)) > PARENT_MAX_TOKENS // 2
    assert count_tokens(src) < PARENT_MAX_TOKENS - 50
    [parent] = chunk_markdown_parented(src, max_tokens=400, overlap_tokens=0)
    assert parent.heading_path == ("甲章",)
    assert parent.body.startswith("甲第0段")
    assert "\n\n乙章\n\n乙第0段" in parent.body  # the heading opens its part of the body


@pytest.mark.parametrize("answer_tokens", [60, 95, 130, 170, 260])
def test_a_lead_in_never_ends_a_parent(answer_tokens: int) -> None:
    questions = [f"**问:第 {i} 种情况怎么处理?**" for i in range(60)]
    src = "\n\n".join(
        f"{q}\n\n" + _sized(f"第 {i} 种情况的处理办法写在这里。", answer_tokens)
        for i, q in enumerate(questions)
    )
    parents = chunk_markdown_parented(src, max_tokens=400, overlap_tokens=0)
    assert len(parents) > 2
    assert not any(_paragraphs(p.body)[-1] in questions for p in parents)
    for i, question in enumerate(questions):
        answer_start = f"第 {i} 种情况的处理办法"
        holding = [p for p in parents if answer_start in p.body]
        assert holding and all(question in p.body for p in holding)
    assert all(_parent_tokens(p) <= PARENT_MAX_TOKENS for p in parents)


def test_a_lead_in_opens_every_parent_of_an_answer_over_the_parent_cap() -> None:
    question = "**问:退货的运费由谁承担?**"
    answer = "".join(f"第 {i} 种情况下运费由卖家承担。" for i in range(300))
    src = f"{_sized('前言。', 200)}\n\n{question}\n\n{answer}"
    parents = chunk_markdown_parented(src, max_tokens=400, overlap_tokens=0)
    assert len(parents) > 2
    assert parents[0].body == _sized("前言。", 200)
    assert all(p.body.startswith(question) and p.body.count(question) == 1 for p in parents[1:])
    assert all(_parent_tokens(p) <= PARENT_MAX_TOKENS for p in parents)
    sentences = re.findall(r"第 \d+ 种[^。]*。", answer)
    assert all(any(_squashed(s) in _parent_text(p) for p in parents) for s in sentences)


def _heading_only(parents: Sequence[ParentChunk], headings: set[str]) -> list[TextChunk]:
    return [c for p in parents for c in p.children if set(_paragraphs(c.body)) <= headings]


def test_empty_headings_ending_a_document_join_the_last_parent() -> None:
    # A new chapter closed the parent before them: alone they would be a
    # parent — and a chunk — of nothing but headings.
    appendix = "## 附录\n\n### 附录 A 常用表单\n\n### 附录 B 修订记录"
    src = "\n\n".join([_chapter("## 甲章", "甲", 700), _chapter("## 乙章", "乙", 600), appendix])
    parents = chunk_markdown_parented(src, max_tokens=400, overlap_tokens=0)
    assert len(parents) == 2
    assert parents[-1].body.endswith("\n\n附录\n\n附录 A 常用表单\n\n附录 B 修订记录")
    assert not _heading_only(parents, {"附录", "附录 A 常用表单", "附录 B 修订记录"})


def test_an_empty_heading_at_a_parent_end_opens_the_next_parent_with_its_section() -> None:
    # "3.2" has nothing under it: as a chunk joins the section after it, the
    # empty heading opens the next parent with "3.3" rather than ending this one.
    src = (
        "# 手册\n\n## 第三章\n\n"
        + _chapter("### 3.1 甲节", "甲", 800)
        + "\n\n### 3.2 空节\n\n### 3.3 乙节\n\n"
        + _sized("乙节的说明文字。", 200)
    )
    parents = chunk_markdown_parented(src, max_tokens=400, overlap_tokens=0)
    assert len(parents) == 2
    assert parents[1].body.startswith("3.2 空节\n\n3.3 乙节\n\n乙节的说明文字。")
    [holding] = [c for p in parents for c in p.children if "3.2 空节" in c.body]
    assert "乙节的说明文字。" in holding.body
    assert not _heading_only(parents, {"手册", "第三章", "3.1 甲节", "3.2 空节", "3.3 乙节"})


def test_a_heading_too_long_for_the_breadcrumb_opens_one_chunk_and_every_parent() -> None:
    # The chunk rule — such a heading opens the body of the first chunk under
    # it, once in the document (B-200) — holds across parents; each parent
    # of the section writes it, its breadcrumb having no room for it either.
    long_heading = "年度产品线规划与渠道合作伙伴激励方案" * 9
    src = f"# {long_heading}\n\n" + "\n\n".join(
        _sized(f"第{i}段的说明文字。", 100) for i in range(30)
    )
    parents = chunk_markdown_parented(src, max_tokens=400, overlap_tokens=0)
    assert len(parents) > 2
    assert all(long_heading not in p.heading_path for p in parents)
    assert all(p.body.startswith(f"{long_heading}\n\n") for p in parents)
    assert sum(long_heading in c.body for p in parents for c in p.children) == 1


# ---------------------------------------------------------------------------
# The trivial parent — the caller stores no parent row for it
# ---------------------------------------------------------------------------


def test_a_short_document_is_one_trivial_parent() -> None:
    [parent] = chunk_markdown_parented(
        "# 放假通知\n\n国庆放假七天。", max_tokens=400, overlap_tokens=0
    )
    assert parent == ParentChunk(
        body="国庆放假七天。",
        heading_path=("放假通知",),
        children=(TextChunk(body="国庆放假七天。", heading_path=("放假通知",)),),
    )
    assert parent.is_trivial


def test_a_short_preamble_joined_to_the_first_section_is_still_trivial() -> None:
    [parent] = chunk_markdown_parented(
        "文件编号 2026-01\n\n# 放假通知\n\n国庆放假七天。", max_tokens=400, overlap_tokens=0
    )
    assert parent.heading_path == ("放假通知",)
    assert parent.is_trivial


def test_a_parent_is_trivial_only_with_one_child_equal_to_it() -> None:
    child = TextChunk(body="正文。", heading_path=("甲",))
    assert ParentChunk("正文。", ("甲",), (child,)).is_trivial
    assert not ParentChunk("正文。", ("乙",), (child,)).is_trivial
    assert not ParentChunk("前言。\n\n正文。", ("甲",), (child,)).is_trivial
    assert not ParentChunk("正文。\n\n正文。", ("甲",), (child, child)).is_trivial
    assert not ParentChunk("正文。", ("甲",), (child, child)).is_trivial


def test_a_parent_of_several_children_is_not_trivial() -> None:
    [parent] = chunk_markdown_parented(
        _sized("这是一段完整的说明。", 600), max_tokens=128, overlap_tokens=0
    )
    assert len(parent.children) > 1
    assert not parent.is_trivial


def test_empty_input_has_no_parents() -> None:
    assert chunk_markdown_parented("", max_tokens=400, overlap_tokens=0) == []


# ---------------------------------------------------------------------------
# Topic shifts split children inside a parent
# ---------------------------------------------------------------------------


class _MarkerEmbedder:
    """A vector by topic marker: blocks of different topics are a shift."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        self.texts.extend(texts)
        return [(1.0, 0.0) if "TOPICA" in text else (0.0, 1.0) for text in texts]


async def test_topic_shifts_split_children_inside_their_parent() -> None:
    # Two parents (the chapters); a topic shift inside the second one. Its
    # index in the second parent is not its index in the document.
    first = _chapter("# 甲章", "TOPICA甲", 900)
    second = "# 乙章\n\n" + "\n\n".join(
        [_sized("TOPICA 乙一。", 60), _sized("TOPICA 乙二。", 60), _sized("TOPICB 乙三。", 60)]
    )
    src = f"{first}\n\n{second}"
    kwargs: dict[str, Any] = {"max_tokens": 400, "overlap_tokens": 0}
    structural = chunk_markdown_parented(src, **kwargs)
    embedder = _MarkerEmbedder()
    semantic = await chunk_markdown_semantic_parented(
        src, embedder=embedder, tenant_id=_TENANT, **kwargs
    )
    assert [(p.body, p.heading_path) for p in semantic] == [
        (p.body, p.heading_path) for p in structural
    ]
    assert len(semantic) == 2
    assert [c.body for c in structural[1].children] == ["\n\n".join(_paragraphs(second)[1:])]
    assert [c.body for c in semantic[1].children] == [
        "\n\n".join(_paragraphs(second)[1:3]),
        _paragraphs(second)[3],
    ]
    flat = [c for p in semantic for c in p.children]
    again = await chunk_markdown_semantic(
        src, embedder=_MarkerEmbedder(), tenant_id=_TENANT, **kwargs
    )
    assert flat == again


async def test_no_topic_shift_leaves_the_structural_count() -> None:
    # What ingest stores — children, cut inside parents: a long section split
    # across parents makes more chunks than packed whole.
    src = "\n\n".join(_sized(f"TOPICA 第{i}段的说明文字。", 130) for i in range(30))
    kwargs: dict[str, Any] = {"max_tokens": 400, "overlap_tokens": 0}
    semantic = await chunk_markdown_semantic_parented(
        src, embedder=_MarkerEmbedder(), tenant_id=_TENANT, **kwargs
    )
    assert len(semantic) > 3
    assert structural_chunk_count(src, **kwargs) == sum(len(p.children) for p in semantic)


# ---------------------------------------------------------------------------
# The fingerprint
# ---------------------------------------------------------------------------


def test_the_chunker_version_names_the_parent_rules() -> None:
    assert CHUNKER_VERSION == "k3k1"


def test_the_fingerprint_follows_the_parent_cap() -> None:
    base = chunk_params_hash(max_tokens=400, overlap_tokens=0)
    assert base == chunk_params_hash(
        max_tokens=400, overlap_tokens=0, parent_max_tokens=PARENT_MAX_TOKENS
    )
    assert base != chunk_params_hash(max_tokens=400, overlap_tokens=0, parent_max_tokens=2048)
