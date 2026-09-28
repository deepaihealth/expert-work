"""Guards around weasyprint: a deny-by-default resource fetcher and post-layout checks that the
cover and the page footer fit (weasyprint clips or drops overflowing content silently)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname

from hpr.blocks import RenderError
from hpr.ppt_layout import LayoutError

FOOT_EDGE_PX = 5 / 25.4 * 96  # footer text must end at least 5 mm above the paper edge


def file_path(url: str) -> Path | None:
    """The resolved local path a file: URL opens, decoded exactly once — the way urllib's
    FileHandler (weasyprint's fetcher) decodes it; None for other URLs."""
    parts = urlsplit(url)
    if parts.scheme.lower() != "file" or parts.netloc not in ("", "localhost"):
        return None
    return Path(url2pathname(parts.path)).resolve()


def raise_for_failed_images(failed_urls: list[str], images: dict[Path, str]) -> None:
    """An image block that weasyprint refused or failed to load must not silently vanish."""
    for url in failed_urls:
        path = file_path(url)
        if path is not None and path in images:
            raise RenderError(images[path], f"图片未能嵌入 PDF：{url}")  # noqa: RUF001


def check_url(url: str, base_dir: Path, allowed_files: frozenset[Path] = frozenset()) -> None:
    """Allow only data: URIs and file: URIs inside base_dir or explicitly referenced; raise
    ValueError for anything else (weasyprint then logs the resource as failed and skips it)."""
    scheme = urlsplit(url).scheme.lower()
    if scheme == "data":
        return
    path = file_path(url)
    if path is not None and (path in allowed_files or path.is_relative_to(base_dir.resolve())):
        return
    raise ValueError(f"不允许加载外部资源：{url}")  # noqa: RUF001


def make_fetcher(base_dir: Path, allowed_files: frozenset[Path]) -> Any:
    from weasyprint.urls import URLFetcher

    class _Fetcher(URLFetcher):  # type: ignore[misc]
        def fetch(self, url: str, headers: Any = None) -> Any:
            check_url(url, base_dir, allowed_files)
            return super().fetch(url, headers)

    return _Fetcher(allowed_protocols=("data", "file"))


_COVER_PARTS = (
    ("h1", "title"),
    ("sub", "subtitle"),
    ("who", "client.name"),
    ("metaline", "generated_at"),
    ("brand", "brand"),
)


_COVER_HINT = "请缩短方案名称或副标题"
_COVER_FULL = f"封面内容过多，一页放不下，{_COVER_HINT}"  # noqa: RUF001


def _cover_part(el: Any) -> str | None:
    if el.tag == "h1":
        return "title"
    classes = (el.get("class") or "").split()
    return next((path for cls, path in _COVER_PARTS if cls in classes), None)


def _is_cover(box: Any) -> bool:
    el = getattr(box, "element", None)
    return el is not None and "cover" in (el.get("class") or "").split()


def check_cover(document: Any) -> None:
    """Raise LayoutError when laid-out cover text overlaps or does not fit on the cover page."""
    pages = document.pages
    if len(pages) > 1 and any(_is_cover(b) for b in pages[1]._page_box.descendants()):
        raise LayoutError("title", _COVER_FULL)
    page = pages[0]._page_box
    seen: set[int] = set()
    rects: list[tuple[str, float, float, float, float]] = []
    for box in page.descendants():
        el = getattr(box, "element", None)
        if el is None or id(el) in seen or not hasattr(box, "border_box_x"):
            continue
        seen.add(id(el))
        part = _cover_part(el)
        if part:
            x, y = box.border_box_x(), box.border_box_y()
            rects.append((part, x, y, x + box.border_width(), y + box.border_height()))
    if not {"title", "client.name"} <= {r[0] for r in rects}:  # pushed off the fixed-height cover
        raise LayoutError("title", _COVER_FULL)
    bottom = page.margin_height()
    for i, (part, x0, y0, x1, y1) in enumerate(rects):
        if y1 > bottom + 0.5:
            raise LayoutError(part, _COVER_FULL)
        for other, a0, b0, a1, b1 in rects[i + 1 :]:
            if x0 < a1 - 0.5 and a0 < x1 - 0.5 and y0 < b1 - 0.5 and b0 < y1 - 0.5:
                raise LayoutError(part, f"封面上与 {other} 重叠，{_COVER_HINT}")  # noqa: RUF001


_FOOT_LONG = "页脚（署名与免责声明）过长，超出页面底边，请缩短免责声明（页脚最多约 4 行）"  # noqa: RUF001


def check_footer(document: Any) -> None:
    """Raise LayoutError when the running footer runs into the paper edge on any page."""
    for page in document.pages[1:]:
        box = page._page_box
        limit = box.margin_height() - FOOT_EDGE_PX
        for b in box.descendants():
            el = getattr(b, "element", None)
            if el is None or "pfoot" not in (el.get("class") or "").split():
                continue
            if not hasattr(b, "border_box_y"):
                continue
            if b.border_box_y() + b.border_height() > limit + 0.5:
                raise LayoutError("brand.disclaimer", _FOOT_LONG)
