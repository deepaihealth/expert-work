"""LOGO loading shared by both writers: trimmed to its visible (alpha) bounding box in memory."""

from __future__ import annotations

import io
from pathlib import Path


def trimmed_logo(path: Path) -> tuple[bytes, int, int] | None:
    """PNG bytes of the LOGO cropped to its alpha bbox, plus its pixel size; None when unreadable.
    The input file is never modified or copied on disk."""
    from PIL import Image as PILImage

    from hpr.images import IMAGE_ERRORS, strict_images

    try:
        with strict_images(), PILImage.open(path) as im:
            im.load()
            rgba = im.convert("RGBA")
    except IMAGE_ERRORS:
        return None
    bbox = rgba.getchannel("A").getbbox()
    if bbox is not None:
        rgba = rgba.crop(bbox)
    buf = io.BytesIO()
    rgba.save(buf, format="PNG")
    return buf.getvalue(), *rgba.size
