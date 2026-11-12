"""B-301 PR 2 —— 上生产前体检的合成图集(设计 §9.2「图集」)。

``docs/superpowers/specs/2026-11-11-b301-image-orientation-design.md``。确定性生成,不读任何文件、
不联网、不用真实照片:

* 11 类内容 x 每类 3 张底图(:mod:`b301_image_bases`);每张底图定一个可以逐字比对的
  **关键字段**(一句话 / 名称 / 日期 / 数 / 编号,不只数字;约三分之一是英文)。
* 每张底图存 0 / 90 / 180 / 270 四个方向 = 132 张,另加 4 张无文字图(只存 0 度)= 136 张。
* ``rotation`` = 这张图要**顺时针**转多少度才正(与 ``ask_image`` meta 里 ``orientation.degrees``
  同一口径):存的是正图逆时针转 ``rotation`` 度(``Image.transpose`` 精确转,不重采样)。
* 「像照片」的几类(单据、包装、文档照片、标签、显示屏、手写、竖排、斜拍)加透视、单侧光、
  模糊、噪声;截图、图表、表格保持干净。斜拍类的拍摄偏角是 20 / 30 / 45 度。
* 字体只用 PyMuPDF 内置的(:mod:`b301_image_draw`),本机与 CI 一样;手写是逐字抖动的模拟,
  显示屏数字是自己画的七段码。

编码:JPEG(质量 88,不带方向标记),所以「正放且无标记」的图走 ``ask_image`` 读原图的那条路。

    python tools/eval/b301_images.py OUT_DIR     # 写成文件,人工抽看
"""

from __future__ import annotations

import functools
import hashlib
import io
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from b301_image_bases import REGISTRY  # noqa: E402
from b301_image_draw import BaseSpec, Flat, font_covers, no_text, photo, seed_of  # noqa: E402
from PIL import Image  # noqa: E402

__all__ = ["BaseSpec", "Flat", "SetImage", "build_set", "font_covers", "render_flat"]

#: 四个方向(要顺时针转多少度才正)。
ROTATIONS = (0, 90, 180, 270)
#: 存图时用的转法:正图**逆时针**转 r 度(PIL 的 ROTATE_* 是逆时针)。
_TRANSPOSE = {
    90: Image.Transpose.ROTATE_90,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_270,
}
JPEG_QUALITY = 88
NO_TEXT_CLASS = "no_text"
NO_TEXT_COUNT = 4
NO_TEXT_QUESTION = "Describe this image in one sentence."


@dataclass(frozen=True)
class SetImage:
    """图集里的一张图。``data`` 是 JPEG 字节。无文字图的 ``key`` 是 ``None``。"""

    image_id: str
    image_class: str
    base_id: str
    rotation: int
    key: str | None
    question: str
    data: bytes = field(repr=False)
    media_type: str = "image/jpeg"
    #: 关键字段的种类(sentence / name / date / number / code);无文字图是空串。判分时只有
    #: ``number`` 的数统一写法(``36.80`` = ``36.8``),编号、日期按原样比。
    kind: str = ""

    @property
    def upright(self) -> bool:
        return self.rotation == 0


def base_specs() -> tuple[BaseSpec, ...]:
    return tuple(spec for spec, _render, _effects in REGISTRY)


def render_flat(base_id: str) -> Flat:
    """一张底图正放、加照片效果之前的样子(测试用:查关键字段画上去了、字形都有)。"""
    for spec, render, _effects in REGISTRY:
        if spec.base_id == base_id:
            return render()
    msg = f"unknown base image {base_id!r}"
    raise KeyError(msg)


def render_upright(base_id: str) -> Image.Image:
    for spec, render, effects in REGISTRY:
        if spec.base_id == base_id:
            flat = render().image
            if effects is None:
                return flat
            return photo(
                flat,
                seed=seed_of(base_id),
                view_deg=effects.get("view_deg", 6.0),
                tilt_deg=effects.get("tilt_deg", 2.0),
                blur=effects.get("blur", 1.0),
                noise=effects.get("noise", 4.0),
            )
    msg = f"unknown base image {base_id!r}"
    raise KeyError(msg)


def encode(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()


def stored(upright: Image.Image, rotation: int) -> Image.Image:
    """存下来的样子:正图逆时针转 ``rotation`` 度,要顺时针转 ``rotation`` 度才正。"""
    return upright if rotation == 0 else upright.transpose(_TRANSPOSE[rotation])


@functools.cache
def build_set() -> tuple[SetImage, ...]:
    """136 张:33 张底图 x 4 个方向,加 4 张无文字图。顺序固定。"""
    images: list[SetImage] = []
    for spec in base_specs():
        upright = render_upright(spec.base_id)
        for rotation in ROTATIONS:
            images.append(
                SetImage(
                    image_id=f"{spec.base_id}@{rotation}",
                    image_class=spec.image_class,
                    base_id=spec.base_id,
                    rotation=rotation,
                    key=spec.key,
                    question=spec.question,
                    data=encode(stored(upright, rotation)),
                    kind=spec.kind,
                )
            )
    for index in range(NO_TEXT_COUNT):
        base_id = f"nt-{index + 1}"
        images.append(
            SetImage(
                image_id=f"{base_id}@0",
                image_class=NO_TEXT_CLASS,
                base_id=base_id,
                rotation=0,
                key=None,
                question=NO_TEXT_QUESTION,
                data=encode(no_text(index)),
            )
        )
    return tuple(images)


def set_digest(images: Sequence[SetImage]) -> str:
    """图集内容的哈希(字节 + 标注):报价与真跑在不同机器上生成的图不一样时,编号对不上。"""
    digest = hashlib.sha256()
    for image in images:
        digest.update(f"{image.image_id}|{image.rotation}|{image.key}|{image.question}|".encode())
        digest.update(hashlib.sha256(image.data).digest())
    return digest.hexdigest()


def write_set(out_dir: Path) -> int:
    """把图集写成文件(人工抽看用);返回张数。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    images = build_set()
    for image in images:
        (out_dir / f"{image.image_id.replace('@', '_r')}.jpg").write_bytes(image.data)
    return len(images)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write("usage: python tools/eval/b301_images.py OUT_DIR\n")
        raise SystemExit(2)
    sys.stdout.write(f"wrote {write_set(Path(sys.argv[1]))} images\n")
