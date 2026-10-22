"""B-209 PR-2 §5.4 —— 扫描页上的插图(在解析进程池里跑)。

识别模型(版面解析接口)报出的图片区域(识别器已换算成相对这一页图片的 0~1 坐标),按框从
这一页的渲染图(与识别时同样的 200 DPI / 长边 2,000 px)裁出来。裁出来的图走同一套单文档过滤。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from control_plane.knowledge.recognition import figure_images
from control_plane.knowledge.recognition.figure_extract import FigureExtraction, capped
from control_plane.knowledge.recognition.figure_filters import FigureCandidate, first_failed_filter
from control_plane.knowledge.recognition.pages import open_pdf, render_page


@dataclass(frozen=True)
class ScannedFigures:
    """Figures cropped from scanned pages: the extraction and, per page
    (0-based) → region index → figure number, where each kept one goes."""

    figures: FigureExtraction
    numbers: Mapping[int, Mapping[int, int]]


def crop_scanned_figures(
    raw: bytes,
    regions: Mapping[int, Sequence[tuple[float, float, float, float]]],
    first_number: int,
) -> ScannedFigures:
    """Crop the image regions of recognized scanned pages (page index →
    boxes relative to the page, 0..1), filter them and number the survivors
    from ``first_number``."""
    doc = open_pdf(raw)
    crops: list[tuple[int, int, figure_images.NormalizedImage, float, int]] = []
    unreadable = 0  # a page that will not render, a box that will not crop
    try:
        for index, boxes in sorted(regions.items()):
            try:
                rendered = render_page(doc[index], None)
                width, height = figure_images.image_size(rendered) or (0, 0)
            except Exception:
                unreadable += len(boxes)
                continue
            for region, (x0, y0, x1, y1) in enumerate(boxes):
                try:
                    image = figure_images.crop(
                        rendered, (x0 * width, y0 * height, x1 * width, y1 * height)
                    )
                except Exception:
                    image = None
                if image is None:
                    unreadable += 1
                    continue
                share = image.width * image.height / (width * height)
                crops.append((index, region, image, share, len(image.jpeg)))
    finally:
        doc.close()
    pages_by_hash: dict[str, set[int]] = {}
    for index, _, image, _, _ in crops:
        pages_by_hash.setdefault(image.pixel_hash, set()).add(index)
    candidates: dict[str, FigureCandidate] = {}
    numbers: dict[int, dict[int, int]] = {}
    filtered: set[str] = set()
    for index, region, image, share, size_bytes in crops:
        digest = image.pixel_hash
        if digest in filtered:
            continue
        if digest not in candidates:
            if first_failed_filter(
                width=image.width,
                height=image.height,
                size_bytes=size_bytes,
                occurrences=len(pages_by_hash[digest]),
                page_share=share,
            ):
                filtered.add(digest)
                continue
            candidates[digest] = FigureCandidate(
                number=first_number + len(candidates),
                image=image.jpeg,
                width=image.width,
                height=image.height,
                pixel_hash=digest,
                pages=tuple(sorted(page + 1 for page in pages_by_hash[digest])),
            )
        numbers.setdefault(index, {})[region] = candidates[digest].number
    extraction = capped(
        list(candidates.values()),
        FigureExtraction(
            extracted=len(pages_by_hash) + unreadable,
            filtered=len(filtered),
            unsupported=unreadable,
        ),
    )
    kept = {c.number for c in extraction.candidates}
    return ScannedFigures(
        figures=extraction,
        numbers={
            page: {region: number for region, number in mapping.items() if number in kept}
            for page, mapping in numbers.items()
        },
    )
