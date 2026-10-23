"""Synthetic pictures for the B-209 PR-2 figure tests (no tenant content)."""

from __future__ import annotations

import hashlib
import io

from PIL import Image


def noise_png(width: int, height: int, seed: int = 1) -> bytes:
    """A noisy picture — never compresses below the 10 KB floor at a few
    hundred pixels a side (a photo, as far as the filters can tell)."""
    noise = hashlib.shake_256(f"noise-{seed}".encode()).digest(width * height * 3)
    image = Image.frombytes("RGB", (width, height), noise)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def flat_png(width: int, height: int) -> bytes:
    """A flat grey picture — compresses to a few hundred bytes."""
    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 200, 200)).save(out, format="PNG")
    return out.getvalue()


def jpeg(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (255, 255, 255)).save(out, format="JPEG")
    return out.getvalue()
