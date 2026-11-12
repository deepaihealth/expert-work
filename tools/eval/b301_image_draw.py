"""B-301 PR 2 体检图集的画图工具:字体、画布、「拍成照片」、七段码、无文字图。

字体只用随依赖自带的:PyMuPDF 内置的 Droid Sans Fallback(中文,Apache-2.0)与 Nimbus Sans /
Mono(拉丁),本机与 CI 都随 ``uv sync`` 装上;不读系统字体。图集见 :mod:`b301_images`。
"""

from __future__ import annotations

import functools
import hashlib
import io
import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pymupdf
from PIL import Image, ImageDraw, ImageFilter, ImageFont

#: 字体:名字 → PyMuPDF 内置字体名。
FONTS = {"cjk": "cjk", "sans": "helv", "sans-bold": "hebo", "mono": "cour"}
Box = tuple[int, int, int, int]
RGB = tuple[int, int, int]


@dataclass(frozen=True)
class BaseSpec:
    """一张底图:类、编号、关键字段、问题、字段种类、语言。"""

    image_class: str
    base_id: str
    key: str
    question: str
    kind: str  # sentence | name | date | number | code
    script: str  # zh | en


@dataclass(frozen=True)
class Flat:
    """一张底图正放、加照片效果之前的样子:图、画上去的全部文字(带字体)、关键字段的框。"""

    image: Image.Image
    texts: tuple[tuple[str, str], ...]
    key_box: Box


@functools.cache
def _font_bytes(name: str) -> bytes:
    return bytes(pymupdf.Font(FONTS[name]).buffer)


@functools.cache
def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(io.BytesIO(_font_bytes(name)), size)


def font_covers(name: str, text: str) -> bool:
    """``text`` 里每个非空白字符在这个字体里都有字形(没有就会画成方块)。"""
    probe = pymupdf.Font(FONTS[name])
    return all(probe.has_glyph(ord(ch)) for ch in text if not ch.isspace())


class Canvas:
    """记下画过的每一段文字(给测试查字形)和关键字段的框。"""

    def __init__(self, size: tuple[int, int], background: RGB = (255, 255, 255)) -> None:
        self.image = Image.new("RGB", size, background)
        self.draw = ImageDraw.Draw(self.image)
        self.texts: list[tuple[str, str]] = []
        self.key_box: Box | None = None

    def text(
        self,
        xy: tuple[float, float],
        text: str,
        *,
        face: str = "cjk",
        size: int = 32,
        fill: RGB = (20, 20, 20),
        key: bool = False,
        anchor: str = "la",
    ) -> Box:
        fnt = font(face, size)
        self.draw.text(xy, text, font=fnt, fill=fill, anchor=anchor)
        left, top, right, bottom = self.draw.textbbox(xy, text, font=fnt, anchor=anchor)
        box = (int(left), int(top), int(right), int(bottom))
        self.texts.append((text, face))
        if key:
            self.key_box = box
        return box

    def vertical(
        self, x: int, y: int, text: str, *, size: int, fill: RGB = (20, 20, 20), key: bool = False
    ) -> Box:
        """竖排一列(每个字正着、从上往下)。"""
        fnt = font("cjk", size)
        step = int(size * 1.15)
        for index, ch in enumerate(text):
            self.draw.text((x, y + index * step), ch, font=fnt, fill=fill, anchor="ma")
        box = (x - size // 2 - 2, y, x + size // 2 + 2, y + len(text) * step)
        self.texts.append((text, "cjk"))
        if key:
            self.key_box = box
        return box

    def flat(self) -> Flat:
        if self.key_box is None:
            msg = "no key field was drawn"
            raise ValueError(msg)
        return Flat(self.image, tuple(self.texts), self.key_box)


def seed_of(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")


def _perspective_coeffs(
    dst: Sequence[tuple[float, float]], src: Sequence[tuple[float, float]]
) -> list[float]:
    """PIL ``PERSPECTIVE`` 的 8 个系数:把输出图上的 ``dst`` 四点映射回输入图上的 ``src``。"""
    rows: list[list[float]] = []
    rhs: list[float] = []
    for (x, y), (u, v) in zip(dst, src, strict=True):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        rhs.extend([u, v])
    solved = np.linalg.solve(np.array(rows, dtype=np.float64), np.array(rhs, dtype=np.float64))
    return [float(c) for c in solved]


def photo(
    flat: Image.Image,
    *,
    seed: int,
    view_deg: float = 6.0,
    tilt_deg: float = 2.0,
    margin: float = 0.12,
    table: RGB = (96, 84, 72),
    blur: float = 1.0,
    noise: float = 4.0,
) -> Image.Image:
    """把一张平面图「拍」成照片:放在桌面上、相机绕竖轴偏 ``view_deg`` 度(远端缩短)、轻微
    倾斜 ``tilt_deg`` 度、一侧亮一侧暗、模糊、噪声。"""
    rng = np.random.default_rng(seed)
    width, height = flat.size
    pad_w, pad_h = int(width * margin), int(height * margin)
    out_w, out_h = width + 2 * pad_w, height + 2 * pad_h
    shrink = 1.0 - 0.55 * math.sin(math.radians(view_deg))
    squeeze = math.cos(math.radians(view_deg))
    left, top = pad_w + width * (1 - squeeze) / 2, pad_h
    right = left + width * squeeze
    inset = height * (1 - shrink) / 2
    quad = [(left, top), (right, top + inset), (right, top + height - inset), (left, top + height)]
    angle = math.radians(tilt_deg)
    cx, cy = out_w / 2, out_h / 2
    quad = [
        (
            cx + (x - cx) * math.cos(angle) - (y - cy) * math.sin(angle),
            cy + (x - cx) * math.sin(angle) + (y - cy) * math.cos(angle),
        )
        for x, y in quad
    ]
    src = [(0.0, 0.0), (float(width), 0.0), (float(width), float(height)), (0.0, float(height))]
    coeffs = _perspective_coeffs(quad, src)
    mask = Image.new("L", flat.size, 255).transform(
        (out_w, out_h), Image.Transform.PERSPECTIVE, coeffs, Image.Resampling.BILINEAR
    )
    warped = flat.transform(
        (out_w, out_h), Image.Transform.PERSPECTIVE, coeffs, Image.Resampling.BICUBIC
    )
    yy, xx = np.mgrid[0:out_h, 0:out_w].astype(np.float32)
    grain = ((np.sin(yy / 7.0 + np.sin(xx / 53.0) * 3.0) + 1.0) * 6.0)[..., None]
    base = np.array(table, dtype=np.float32)[None, None, :] + grain
    scene = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")
    scene.paste(warped, (0, 0), mask)
    light_x = float(rng.uniform(0.15, 0.85))
    gradient = 0.72 + 0.4 * np.exp(-(((xx / out_w - light_x) / 0.55) ** 2))
    arr = np.asarray(scene).astype(np.float32) * gradient[..., None]
    arr += rng.normal(0.0, noise, (*arr.shape[:2], 1)).astype(np.float32)
    result = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")
    return result.filter(ImageFilter.GaussianBlur(blur))


_SEGMENTS = {
    "0": "abcdef",
    "1": "bc",
    "2": "abged",
    "3": "abgcd",
    "4": "fgbc",
    "5": "afgcd",
    "6": "afgedc",
    "7": "abc",
    "8": "abcdefg",
    "9": "abcdfg",
}


def seven_segment(
    canvas: Canvas, x: int, y: int, text: str, *, height: int, on: RGB, off: RGB | None
) -> Box:
    """在 (x, y) 画一串七段码数字(0-9、``.``、``:``),记成关键字段;``off`` = 没点亮的淡笔段。"""
    width, thick = int(height * 0.52), max(4, height // 9)
    draw = canvas.draw
    cursor = x
    half = height // 2

    def segment(name: str, ox: int, colour: RGB) -> None:
        spans = {
            "a": (ox + thick, y, ox + width - thick, y + thick),
            "g": (ox + thick, y + half - thick // 2, ox + width - thick, y + half + thick // 2),
            "d": (ox + thick, y + height - thick, ox + width - thick, y + height),
            "f": (ox, y + thick, ox + thick, y + half - thick // 2),
            "b": (ox + width - thick, y + thick, ox + width, y + half - thick // 2),
            "e": (ox, y + half + thick // 2, ox + thick, y + height - thick),
            "c": (ox + width - thick, y + half + thick // 2, ox + width, y + height - thick),
        }
        draw.rectangle(spans[name], fill=colour)

    for ch in text:
        if ch == ".":
            draw.ellipse((cursor, y + height - thick, cursor + thick, y + height), fill=on)
            cursor += thick * 2
            continue
        if ch == ":":
            for fy in (0.3, 0.7):
                top = y + int(height * fy) - thick // 2
                draw.rectangle((cursor, top, cursor + thick, top + thick), fill=on)
            cursor += thick * 2 + 4
            continue
        for name in "abcdefg":
            if name in _SEGMENTS[ch]:
                segment(name, cursor, on)
            elif off is not None:
                segment(name, cursor, off)
        cursor += width + thick
    box = (x, y, cursor, y + height)
    canvas.texts.append((text, "seven-segment"))
    canvas.key_box = box
    return box


def no_text(index: int) -> Image.Image:
    """无文字但有明确上下的图(0..3):山丘、地面上的几何体、木地板上的杯子、树。"""
    rng = np.random.default_rng(seed_of(f"no-text-{index}"))
    w, h = 1600, 1200
    yy = np.mgrid[0:h, 0:w][0].astype(np.float32)
    sky = np.stack([120 + yy / h * 80, 170 + yy / h * 50, 235 - yy / h * 20], axis=-1)
    image = Image.fromarray(np.clip(sky, 0, 255).astype(np.uint8), "RGB")
    draw = ImageDraw.Draw(image)
    if index == 0:
        for shift, colour in ((0, (70, 120, 70)), (180, (50, 95, 55))):
            points = [
                (x, int(700 + shift + 120 * math.sin(x / 260 + shift)))
                for x in range(0, w + 40, 40)
            ]
            draw.polygon([*points, (w, h), (0, h)], fill=colour)
        draw.ellipse((1250, 120, 1400, 270), fill=(255, 236, 160))
    elif index == 1:
        draw.rectangle((0, 850, w, h), fill=(180, 170, 150))
        for _ in range(6):
            x, size = int(rng.uniform(100, 1400)), int(rng.uniform(80, 200))
            red, green, blue = (int(v) for v in rng.integers(40, 220, 3))
            draw.ellipse((x, 850 - size, x + size, 850), fill=(red, green, blue))
    elif index == 2:
        for y in range(0, h, 90):
            tone = int(rng.uniform(130, 170))
            draw.rectangle((0, y, w, y + 86), fill=(tone, int(tone * 0.75), int(tone * 0.5)))
        draw.rectangle((650, 450, 950, 900), fill=(240, 240, 235))
        draw.arc((900, 520, 1080, 760), -90, 90, fill=(240, 240, 235), width=30)
        draw.ellipse((650, 870, 950, 930), fill=(220, 220, 215))
    else:
        draw.rectangle((0, 900, w, h), fill=(90, 150, 60))
        draw.rectangle((770, 520, 830, 920), fill=(100, 70, 40))
        draw.ellipse((560, 180, 1040, 640), fill=(40, 110, 50))
    noisy = np.asarray(image).astype(np.float32) + rng.normal(0, 3.0, (h, w, 1)).astype(np.float32)
    return Image.fromarray(np.clip(noisy, 0, 255).astype(np.uint8), "RGB")
