"""Image-block checks shared by both writers: every image decodes fully, with Pillow's
decompression-bomb guard treated as an error, before anything is drawn."""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from PIL import Image as PILImage

from hpr.blocks import RenderError, section_prims
from hpr.prims import Image

IMAGE_ERRORS = (
    OSError,
    ValueError,
    PILImage.DecompressionBombError,
    PILImage.DecompressionBombWarning,
)


@contextmanager
def strict_images() -> Iterator[None]:
    """Raise DecompressionBombWarning (images above Pillow's pixel limit) instead of warning."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", PILImage.DecompressionBombWarning)
        yield


def image_blocks(content: dict, style: dict, base_dir: Path) -> dict[Path, str]:
    """Resolved path → block path of every image block; RenderError when one does not decode."""
    files: dict[Path, str] = {}
    for _sec, items in section_prims(content, style):
        for p, path in items:
            if not isinstance(p, Image):
                continue
            f = Path(p.path)
            f = (f if f.is_absolute() else base_dir / f).resolve()
            try:
                with strict_images(), PILImage.open(f) as im:
                    im.load()
            except IMAGE_ERRORS as exc:
                raise RenderError(
                    path,
                    f"图片无法读取（文件损坏、过大或格式不支持）：{exc}",  # noqa: RUF001
                ) from exc
            files.setdefault(f, path)
    return files
