"""B-209 PR-2 §5.4 —— 文档内图片:占位符与引用块、六道过滤、描述回答的解析、图片规整;
外加识别输出里版面模型留下的对齐标签(PR-1 清洗)。

全是纯函数,这里直接调(不起进程池、不联网)。图片现场合成。抽图(Word / 幻灯片 / PDF /
扫描页)见 ``test_knowledge_figure_extraction``。
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from control_plane.knowledge.recognition import figure_filters, figure_images
from control_plane.knowledge.recognition.cleaning import clean_reading
from control_plane.knowledge.recognition.figure_describe import (
    MAX_DESCRIPTION_CHARS,
    describe_prompt,
    description_failure,
    document_language,
    parse_description,
)
from control_plane.knowledge.recognition.figure_filters import (
    FigureCandidate,
    first_failed_filter,
    select_figures,
)
from control_plane.knowledge.recognition.figure_images import is_vector_format, normalize
from control_plane.knowledge.recognition.figure_markup import (
    FIGURE_ID_PATTERN,
    figure_numbers,
    inline_placeholder,
    placeholder,
    quote_block,
    region_placeholder,
    regions_to_figures,
    resolve_figures,
    resolve_in_order,
)
from tests.figure_test_images import noise_png

_WMF = b"\xd7\xcd\xc6\x9a" + b"\x00" * 64
_ID = "0123456789abcdef"
_BLOCK = quote_block(_ID, "A device photo.")


def _candidate(number: int, *, side: int = 500, digest: str | None = None) -> FigureCandidate:
    return FigureCandidate(
        number=number,
        image=b"jpeg",
        width=side,
        height=side,
        pixel_hash=digest or f"h{number}",
    )


# ---------------------------------------------------------------------------
# placeholders and quote blocks
# ---------------------------------------------------------------------------


def test_a_described_figure_becomes_its_own_paragraph() -> None:
    text = f"Before.{placeholder(1)}\n\nAfter."
    assert figure_numbers(text) == [1]
    assert resolve_figures(text, {1: _BLOCK}) == f"Before.\n\n{_BLOCK}\n\nAfter."


@pytest.mark.parametrize(
    "text",
    [
        "Before.\n\nAfter.",
        "Line one\nline two.\n\nAfter.",
        "| a | b |\n| --- | --- |\n| 1 | 2 |\nAfter.",
        "Before.\n\nMiddle.\n\n\n\nAfter.",  # blank lines away from it are left alone
    ],
)
def test_a_figure_not_described_leaves_the_text_exactly_as_it_was(text: str) -> None:
    # Review M3: removing a placeholder only collapses the line breaks right
    # around it — nothing anywhere else in the text changes.
    at_paragraph = text.replace("\n", f"{placeholder(1)}\n", 1)
    assert resolve_figures(at_paragraph, {}) == text
    assert resolve_figures(placeholder(2) + text, {}) == text
    assert resolve_figures(text + f"\n{placeholder(3)}", {}) == text + "\n"


def test_region_placeholders_become_their_figures_or_go() -> None:
    text = f"a\n\n{region_placeholder(0)}\n\nb{region_placeholder(1)}c"
    swapped = regions_to_figures(text, {0: 7})
    assert figure_numbers(swapped) == [7]
    assert resolve_figures(swapped, {7: _BLOCK}) == f"a\n\n{_BLOCK}\n\nbc"
    assert resolve_figures(swapped, {}) == "a\n\nbc"


def test_an_inline_reference_shows_the_figure_id_only_when_described() -> None:
    row = f"| Chair | {inline_placeholder(4)} | 100 |"
    assert resolve_figures(row, {4: _BLOCK}) == f"| Chair | \u3014图 {_ID}\u3015 | 100 |"
    assert resolve_figures(row, {}) == "| Chair |  | 100 |"


def test_a_figure_s_block_goes_only_where_it_first_appears() -> None:
    # Review L3: the same picture on two pages (or twice in a Word file) is
    # described once.
    pages = [f"Page one.{placeholder(1)}", f"Page two.{placeholder(1)}{placeholder(2)}"]
    other = quote_block("fedcba9876543210", "A chart.")
    first, second = resolve_in_order(pages, {1: _BLOCK, 2: other})
    assert first == f"Page one.\n\n{_BLOCK}\n\n"
    assert _BLOCK not in second and other in second
    assert resolve_figures(f"{placeholder(1)} x {placeholder(1)}", {1: _BLOCK}).count(_ID) == 1


def test_quote_block_carries_the_id_description_and_text() -> None:
    block = quote_block(_ID, "两条\n曲线对比成本", text="第一行\n第二行")
    lines = block.splitlines()
    assert lines[0] == f"> \u3014图 {_ID}\u3015两条 曲线对比成本"
    assert lines[1:] == [">", "> \u3014图中文字\u3015", "> 第一行", "> 第二行"]
    assert FIGURE_ID_PATTERN.search(block).group(1) == _ID  # type: ignore[union-attr]
    chart = quote_block(_ID, "柱状图", text="| a | 1 |", chart=True)
    assert "> \u3014图表数据\u3015" in chart.splitlines()
    assert quote_block(_ID, "照片").count("\n") == 0


# ---------------------------------------------------------------------------
# the six filters (§5.4), in order
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"width": 79, "height": 500}, figure_filters.REASON_SMALL),
        ({"width": 500, "height": 79}, figure_filters.REASON_SMALL),
        ({"page_share": 0.009}, figure_filters.REASON_PAGE_SHARE),
        ({"size_bytes": 10 * 1024 - 1}, figure_filters.REASON_BYTES),
        ({"occurrences": 3}, figure_filters.REASON_REPEATED),
        ({}, None),
        ({"width": 80, "height": 80, "page_share": 0.01, "size_bytes": 10 * 1024}, None),
        ({"occurrences": 2}, None),
    ],
)
def test_per_document_filters(kwargs: dict[str, object], reason: str | None) -> None:
    args: dict[str, object] = {
        "width": 500,
        "height": 400,
        "size_bytes": 50_000,
        "occurrences": 1,
        "page_share": 0.2,
    }
    args.update(kwargs)
    assert first_failed_filter(**args) == reason  # type: ignore[arg-type]


def test_filters_apply_in_order() -> None:
    # Small and repeated: the first filter in the list names it.
    assert (
        first_failed_filter(width=10, height=10, size_bytes=10, occurrences=9, page_share=0.0)
        == figure_filters.REASON_SMALL
    )


def test_tenant_repeated_images_are_dropped_at_three_documents() -> None:
    figures = [_candidate(1, digest="logo"), _candidate(2, digest="seal"), _candidate(3)]
    selection = select_figures(figures, {"logo": 2, "seal": 1})
    assert [c.number for c in selection.kept] == [2, 3]
    assert selection.tenant_repeated == 1


def test_at_most_fifty_figures_largest_kept_in_document_order() -> None:
    figures = [_candidate(n, side=100 + n) for n in range(1, 61)]
    selection = select_figures(figures, {})
    assert selection.capped == 10
    assert [c.number for c in selection.kept] == list(range(11, 61))


# ---------------------------------------------------------------------------
# describe answers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '{"text": "lots", "chart": false, "description": "一张保养说明的文字截图。"}',
        '```json\n{"text": "lots", "chart": false, "description": "一张保养说明的文字截图。"}\n```',
        '好的。{"text":"lots","chart":"否","description":"一张保养说明的文字截图。"}',
        "文字: 大段\n图表: 否\n描述: 一张保养说明的文字截图。",
        "文字\uff1a大段\uff0c\n图表\uff1a否\n描述\uff1a一张保养说明的文字截图。",
    ],
)
def test_description_answers_are_read_leniently(raw: str) -> None:
    parsed = parse_description(raw)
    assert parsed is not None
    assert (parsed.text_amount, parsed.chart) == ("lots", False)
    assert parsed.description == "一张保养说明的文字截图。"


def test_a_long_description_is_cut_at_a_full_width_sentence_end() -> None:
    sentence = "\u8fd9\u5f20\u56fe\u6709\u8868\u683c\uff01"  # seven characters ending in U+FF01
    parsed = parse_description('{"text": "none", "description": "' + sentence * 60 + '"}')
    assert parsed is not None
    assert parsed.description == sentence * (MAX_DESCRIPTION_CHARS // len(sentence))


def test_chart_flag_and_text_amounts() -> None:
    parsed = parse_description('{"text": "少", "chart": true, "description": "柱状图"}')
    assert parsed is not None and parsed.chart and parsed.text_amount == "little"
    parsed = parse_description('{"text": "none", "chart": "yes", "description": "风景照片"}')
    assert parsed is not None and parsed.chart and parsed.text_amount == "none"


@pytest.mark.parametrize(
    ("raw", "finish", "failure"),
    [
        ('{"text": "none", "chart": false, "description": "照片"}', "length", "truncated"),
        # Cut off mid-answer: truncated, not merely unreadable.
        ('{"text": "none", "chart": false, "descr', "length", "truncated"),
        ("看不清这张图。", "stop", "unparsed"),
        ('{"text": "none", "chart": false, "description": ""}', "stop", "unparsed"),
        ('{"text": "none", "chart": false, "description": "照片"}', "stop", None),
    ],
)
def test_description_failures(raw: str, finish: str, failure: str | None) -> None:
    assert description_failure(raw, finish_reason=finish) == failure


def test_a_repetitive_description_fails() -> None:
    loop = '{"text": "none", "chart": false, "description": "' + "重复的话。" * 80 + '"}'
    assert description_failure(loop, finish_reason="stop") == "repeated"


def test_a_long_description_is_cut_at_a_sentence() -> None:
    first = "第一句很长" * 40 + "。"  # 201 characters
    long = first + "第二句" * 60 + "。"  # 382 characters: over the 300 cap
    parsed = parse_description('{"text": "none", "chart": false, "description": "' + long + '"}')
    assert parsed is not None
    assert parsed.description == first


def test_the_describe_prompt_follows_the_document_language_and_small_rule() -> None:
    assert document_language("本手册说明公司的基本工作制度。") == "zh"
    assert document_language("Leave requests are approved by the manager. " * 3) == "en"
    assert document_language("") == "zh"
    assert "description 用中文写" in describe_prompt("zh", small=False)
    assert "Write the description in English." in describe_prompt("en", small=False)
    assert "不要写出任何具体的文字" in describe_prompt("zh", small=True)
    assert "不要写出任何具体的文字" not in describe_prompt("zh", small=False)


# ---------------------------------------------------------------------------
# image normalization
# ---------------------------------------------------------------------------


def test_images_are_normalized_to_jpeg_with_the_source_size() -> None:
    image = normalize(noise_png(2400, 300))
    assert image is not None
    assert image.jpeg.startswith(b"\xff\xd8")
    assert (image.width, image.height) == (2400, 300)  # the source's size
    with Image.open(io.BytesIO(image.jpeg)) as stored:
        assert max(stored.size) == 2000  # long edge capped


def test_same_pixels_in_another_container_hash_the_same() -> None:
    png = noise_png(200, 200)
    with Image.open(io.BytesIO(png)) as source:
        out = io.BytesIO()
        source.save(out, format="BMP")
    first, second = normalize(png), normalize(out.getvalue())
    assert first is not None and second is not None
    assert first.pixel_hash == second.pixel_hash


def test_vector_formats_are_unsupported() -> None:
    assert is_vector_format(_WMF)
    assert is_vector_format(b"x", "image/x-emf")
    assert normalize(_WMF) is None
    assert not is_vector_format(noise_png(10, 10), "image/png")


# ---------------------------------------------------------------------------
# PR-1 cleaning: layout wrappers from the vendor's layout model
# ---------------------------------------------------------------------------


def test_alignment_wrappers_are_stripped_and_their_text_kept() -> None:
    # Seen from glm-ocr on a centred chart title (B-209 PR-2 probe).
    raw = (
        '## 第一章\n\n<div align="center">\n\n季度销量(台)\n\n</div>\n\n'
        "<center>表 1</center>\n\n<p style='text-align:center'>注</p>\n\n正文"
    )
    cleaned = clean_reading(raw)
    assert "<" not in cleaned
    assert cleaned == "## 第一章\n\n季度销量(台)\n\n表 1\n\n注\n\n正文"


def test_a_picture_past_the_workers_memory_ceiling_is_skipped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Re-review H2: under the parse worker's RLIMIT_AS a big picture fails to
    # allocate — that picture is skipped (counted unsupported), the document
    # goes on.
    def out_of_memory(image: object) -> object:
        raise MemoryError

    monkeypatch.setattr(figure_images, "_flatten", out_of_memory)
    with caplog.at_level("WARNING"):
        assert figure_images.normalize(noise_png(64, 48)) is None
    assert "knowledge.figure_out_of_memory" in caplog.text
