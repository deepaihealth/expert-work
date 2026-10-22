"""B-209 PR-2 §5.4 —— 从 Word / 幻灯片里取出图片(在解析进程池里跑;只有可 pickle 的入参 /
返回值),以及各格式共用的 :class:`FigureExtraction`。

* **docx / pptx**:MarkItDown ``keep_data_uris=True``,把每个 ``![…](data:…)`` 抽成独立图片
  (WeKnora ``MarkdownImageBase64`` 同款),正文里换成图片占位符 —— 顺带修掉 Word / PPT 的
  图片占位符垃圾段。同一张图(字节相同)引用几次算几次「出现」。WMF / EMF / SVG 解不开:跳过、
  计数。页眉页脚、母版里的图 MarkItDown 本来就不输出。图片的替代文字不保留(多是形状名或自动
  生成的套话;图被描述了,描述比它好)。
* PDF 文字页上的插图见 :mod:`~.figure_pdf`,扫描页上的见 :mod:`~.figure_scan`。

单文档的过滤(§5.4 第 1~4 道)在这里做:没过的图不带字节出进程,只计数。先只看图片头里的尺寸
和字节数筛、按面积留一个短名单,只解码短名单里的图。

**内存上限**:见 :mod:`~.office_package` —— Word 的图片字节只在转换器读的那一刻读(按部件名读一次、
单张 / 合计有上限、正文里只放短标记,重复引用不再展开);幻灯片在交给 python-pptx 之前按「大小 x
引用次数」改写;两种都先查条目数、解压后总量、XML 部件大小。

Word / 幻灯片的图片抽取(MarkItDown + 解码规整)跑在**主解析池**里(它就是这份文档的解析本身),
不在插图专用的小进程池里:那个池只跑 PDF 插图收集和扫描页裁图。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from control_plane.knowledge.parsing import (
    MarkdownPicture,
    find_data_uri_pictures,
    find_token_pictures,
    office_markdown,
    scrub_data_uris,
    strip_lone_surrogates,
)
from control_plane.knowledge.recognition import figure_images, office_package
from control_plane.knowledge.recognition.figure_filters import (
    MAX_CANDIDATES,
    FigureCandidate,
    first_failed_filter,
    largest_first,
)
from control_plane.knowledge.recognition.figure_markup import inline_placeholder, placeholder
from control_plane.knowledge.recognition.office_package import (
    STAND_IN,
    DocxPictureReader,
    bound_office_package,
)


@dataclass(frozen=True)
class FigureExtraction:
    """A document's figures after the per-document filters: the candidates
    (with bytes) and how many images were found / dropped on the way."""

    candidates: tuple[FigureCandidate, ...] = ()
    extracted: int = 0
    filtered: int = 0
    unsupported: int = 0
    #: Past :data:`~.figure_filters.MAX_CANDIDATES` in the parse process.
    capped: int = 0

    def __add__(self, other: FigureExtraction) -> FigureExtraction:
        return FigureExtraction(
            candidates=(*self.candidates, *other.candidates),
            extracted=self.extracted + other.extracted,
            filtered=self.filtered + other.filtered,
            unsupported=self.unsupported + other.unsupported,
            capped=self.capped + other.capped,
        )

    @property
    def next_number(self) -> int:
        return max((c.number for c in self.candidates), default=0) + 1


NO_FIGURES = FigureExtraction()


@dataclass(frozen=True)
class OfficeParse:
    """A Word / slide file's Markdown with figure placeholders, and its figures."""

    markdown: str
    figures: FigureExtraction


def capped(candidates: Sequence[FigureCandidate], extraction: FigureExtraction) -> FigureExtraction:
    """``extraction`` holding at most :data:`MAX_CANDIDATES` of ``candidates``
    (largest first); the rest are counted as capped."""
    kept = largest_first(candidates, MAX_CANDIDATES)
    return replace(
        extraction,
        candidates=tuple(kept),
        capped=extraction.capped + len(candidates) - len(kept),
    )


def parse_office_figures(raw: bytes, ext: str) -> OfficeParse:
    """A ``.docx`` / ``.pptx`` parsed with its pictures pulled out (see module
    doc). The text is what :func:`~control_plane.knowledge.parsing.parse_document`
    gives, plus a placeholder where each kept picture was.

    :raises DocumentParseError: as ``parse_document`` (the empty check is the
        caller's: a picture-only file may still get its descriptions)."""
    bounded, skipped = bound_office_package(raw, ext)
    if ext == ".docx":
        reader = DocxPictureReader()
        markdown = office_markdown(bounded, ext, docx_pictures=reader)
        matches = find_token_pictures(markdown)
        blobs, references, in_text = _docx_pictures(matches, reader)
        skipped += reader.skipped
    else:
        markdown = office_markdown(bounded, ext)
        matches = find_data_uri_pictures(markdown)
        blobs, references, in_text = _inline_pictures(matches)
    extraction, numbers = _office_candidates(blobs, references)
    extraction = replace(
        extraction,
        extracted=extraction.extracted + skipped,
        unsupported=extraction.unsupported + skipped,
    )
    kept = {c.number for c in extraction.candidates}
    edits: list[tuple[int, int, str]] = []
    after_table: dict[int, list[int]] = {}
    for match, digest in zip(matches, in_text, strict=True):
        found = numbers.get(digest)
        number = found if found is not None and found in kept else None
        line_start = markdown.rfind("\n", 0, match.start) + 1
        if markdown[line_start : match.start].lstrip().startswith("|"):
            # A table row: an in-line reference in the cell, the block after the table.
            cell = inline_placeholder(number) if number is not None else ""
            edits.append((match.start, match.end, cell))
            if number is not None:
                spots = after_table.setdefault(_table_end(markdown, line_start), [])
                if number not in spots:
                    spots.append(number)
        else:
            block = placeholder(number) if number is not None else ""
            edits.append((match.start, match.end, block))
    for end, figures in after_table.items():
        edits.append((end, end, "".join(placeholder(n) for n in figures)))
    text = _apply(markdown, edits)
    return OfficeParse(markdown=strip_lone_surrogates(scrub_data_uris(text)), figures=extraction)


Pictures = tuple[dict[str, tuple[bytes, str]], dict[str, int], list[str]]


def _docx_pictures(matches: Sequence[MarkdownPicture], reader: DocxPictureReader) -> Pictures:
    """The pictures the reader read, by digest; each reference's digest in
    text order (``""``: skipped)."""
    by_index = {
        str(index): (hashlib.sha256(data).hexdigest(), data, mime)
        for index, (data, mime) in enumerate(reader.pictures)
    }
    blobs: dict[str, tuple[bytes, str]] = {}
    references: dict[str, int] = {}
    in_text: list[str] = []
    for match in matches:
        found = by_index.get(match.data)  # "skipped" is no index
        if found is None:
            in_text.append("")
            continue
        digest, data, mime = found
        blobs.setdefault(digest, (data, mime))
        references[digest] = references.get(digest, 0) + 1
        in_text.append(digest)
    return blobs, references, in_text


def _inline_pictures(matches: Sequence[MarkdownPicture]) -> Pictures:
    """Slide pictures inlined as data URIs, by digest (a stand-in the bound
    put there is counted by the bound already)."""
    blobs: dict[str, tuple[bytes, str]] = {}
    references: dict[str, int] = {}
    in_text: list[str] = []
    for match in matches:
        decoded = _decode(match.data)
        digest = hashlib.sha256(decoded).hexdigest() if decoded is not None else ""
        if decoded != STAND_IN:
            blobs.setdefault(digest, (decoded or b"", match.mime))
            references[digest] = references.get(digest, 0) + 1
        in_text.append(digest)
    return blobs, references, in_text


def _office_candidates(
    blobs: Mapping[str, tuple[bytes, str]], references: Mapping[str, int]
) -> tuple[FigureExtraction, dict[str, int]]:
    """Filters 1-4 on what the image headers say, a shortlist of the largest
    (:data:`MAX_CANDIDATES`), and only then the decoding of the shortlist;
    candidates numbered in document order."""
    passing: dict[str, int] = {}  # digest → pixel area
    filtered = unsupported = 0
    for digest, (blob, mime) in blobs.items():
        size = (
            None
            if not blob or figure_images.is_vector_format(blob, mime)
            else (figure_images.image_size(blob))
        )
        if size is None:
            unsupported += 1
        elif first_failed_filter(
            width=size[0], height=size[1], size_bytes=len(blob), occurrences=references[digest]
        ):
            filtered += 1
        else:
            passing[digest] = size[0] * size[1]
    shortlist = set(sorted(passing, key=lambda d: -passing[d])[:MAX_CANDIDATES])
    candidates: list[FigureCandidate] = []
    numbers: dict[str, int] = {}
    for digest in blobs:  # insertion order = document order
        if digest not in shortlist:
            continue
        image = figure_images.normalize(blobs[digest][0])
        if image is None:
            unsupported += 1
            continue
        numbers[digest] = len(candidates) + 1
        candidates.append(
            FigureCandidate(
                number=len(candidates) + 1,
                image=image.jpeg,
                width=image.width,
                height=image.height,
                pixel_hash=image.pixel_hash,
            )
        )
    extraction = FigureExtraction(
        candidates=tuple(candidates),
        extracted=len(blobs),
        filtered=filtered,
        unsupported=unsupported,
        capped=len(passing) - len(shortlist),
    )
    return extraction, numbers


def _table_end(markdown: str, line_start: int) -> int:
    """The end (before its line break) of the last row of the table holding
    the line that starts at ``line_start``."""
    end = markdown.find("\n", line_start)
    while end != -1:
        following = markdown.find("\n", end + 1)
        row = markdown[end + 1 : following if following != -1 else len(markdown)]
        if not row.lstrip().startswith("|"):
            return end
        end = following
    return len(markdown)


def _apply(text: str, edits: list[tuple[int, int, str]]) -> str:
    """``text`` with non-overlapping ``(start, end, replacement)`` edits."""
    parts: list[str] = []
    position = 0
    for start, end, replacement in sorted(edits, key=lambda edit: (edit[0], edit[1])):
        parts.append(text[position:start])
        parts.append(replacement)
        position = end
    parts.append(text[position:])
    return "".join(parts)


def _decode(data: str) -> bytes | None:
    if len(data) > office_package.MAX_IMAGE_BYTES * 4 // 3 + 16:
        return None
    try:
        return base64.b64decode("".join(data.split()), validate=True)
    except (binascii.Error, ValueError):
        return None
