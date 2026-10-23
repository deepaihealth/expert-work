"""B-209 PR-2 §5.4 —— 文档内图片的过滤(纯函数,所有格式同一套,按顺序)。

1. 任一边 < 80 px;
2. PDF 里占页面 < 1%;
3. 压缩后 < 10 KB;
4. 同一份文档里出现在 ≥ 3 页(docx / pptx:≥ 3 次引用)—— logo、页眉图标、背景纹样;
5. 同一租户 ≥ 3 份文档里出现(按像素哈希)—— 租户的信头、水印;
6. 每份最多 50 张,按面积从大到小保留。

前四道只看这一份文档,在解析进程里做(没过的图不带字节出进程);第 5 道要查库、第 6 道在
它之后,在入库协程里做(:func:`select_figures`)。一张图记第一道没过的原因(日志用)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

MIN_SIDE_PX = 80
MIN_PAGE_SHARE = 0.01
MIN_BYTES = 10 * 1024
REPEATED_MIN = 3
TENANT_REPEATED_MIN_DOCUMENTS = 3
MAX_FIGURES_PER_DOCUMENT = 50
#: The shortlist (largest first, chosen from what the image headers / PDF
#: objects say) that the parse process decodes, renders and hands over at
#: most — review H3: decoding every picture of an illustrated document before
#: the cap cost 5x the parse time. Half the cap again leaves the tenant-wide
#: filter room to drop logos; the rest count as capped.
MAX_CANDIDATES = MAX_FIGURES_PER_DOCUMENT + MAX_FIGURES_PER_DOCUMENT // 2
#: Short side below which a figure is described but its text never
#: transcribed (B-209 §4.1: every model invents text on small, low-res images).
TRANSCRIBE_MIN_SIDE_PX = 400

REASON_SMALL = "small"
REASON_PAGE_SHARE = "page_share"
REASON_BYTES = "bytes"
REASON_REPEATED = "repeated"


@dataclass(frozen=True)
class FigureCandidate:
    """One distinct figure of a document that passed the per-document filters.

    ``number`` names its placeholders in the text (every occurrence of the
    same image shares it); ``image`` is the normalized JPEG; ``width`` /
    ``height`` the source pixel size; ``pages`` the 1-based pages it is on
    (empty outside PDFs)."""

    number: int
    image: bytes
    width: int
    height: int
    pixel_hash: str
    pages: tuple[int, ...] = ()

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def short_side(self) -> int:
        return min(self.width, self.height)


def first_failed_filter(
    *,
    width: int,
    height: int,
    size_bytes: int,
    occurrences: int,
    page_share: float | None = None,
) -> str | None:
    """The first per-document filter (1-4) an image fails, ``None`` if it
    passes them all. ``occurrences`` = pages (PDF) / references (Word, slides)."""
    if min(width, height) < MIN_SIDE_PX:
        return REASON_SMALL
    if page_share is not None and page_share < MIN_PAGE_SHARE:
        return REASON_PAGE_SHARE
    if size_bytes < MIN_BYTES:
        return REASON_BYTES
    if occurrences >= REPEATED_MIN:
        return REASON_REPEATED
    return None


def largest_first(candidates: Sequence[FigureCandidate], limit: int) -> list[FigureCandidate]:
    """The ``limit`` largest candidates (by area), in document order."""
    keep = sorted(candidates, key=lambda c: (-c.area, c.number))[:limit]
    return sorted(keep, key=lambda c: c.number)


@dataclass(frozen=True)
class Selection:
    kept: tuple[FigureCandidate, ...]
    #: Dropped by the tenant-wide repeated-image filter (5).
    tenant_repeated: int
    #: Past the per-document cap (6).
    capped: int


def select_figures(
    candidates: Sequence[FigureCandidate],
    other_documents: Mapping[str, int],
    *,
    cap: int = MAX_FIGURES_PER_DOCUMENT,
) -> Selection:
    """Filters 5 and 6: drop images ``other_documents`` (pixel hash → how many
    of the tenant's other documents hold it) shows in ≥ 3 documents counting
    this one, then keep the ``cap`` largest."""
    survivors = [
        c
        for c in candidates
        if other_documents.get(c.pixel_hash, 0) + 1 < TENANT_REPEATED_MIN_DOCUMENTS
    ]
    kept = largest_first(survivors, cap)
    return Selection(
        kept=tuple(kept),
        tenant_repeated=len(candidates) - len(survivors),
        capped=len(survivors) - len(kept),
    )
