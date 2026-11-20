"""B-322 第五轮(2026-11-20,用户收窄范围):看图工具(Path B / ``ask_image``)的裁图 ``crop_image``
与 origin/main 逐字节相同。

下面的哈希是用 origin/main ``630dcf9fd`` 的 ``orchestrator/tools/region_zoom.py``(原样另存一份、单独
import)对同样的输入算出来的(Pillow 13.0.0);本分支的 ``crop_image`` 必须给出同样的字节。图都是
确定性画出来的(渐变 + 直线,不用随机噪声)。也覆盖调色板(P)、1 位、16 位灰度(I;16)的 PNG —— 这几种
颜色模式在 main 上能裁(第四轮在裁图前加的 ``reduce`` 会在它们上抛错,已撤回)。
"""

from __future__ import annotations

import hashlib
import io

import pytest
from PIL import Image, ImageDraw

from orchestrator.tools.region_zoom import crop_image

_BOXES = [(0, 0, 1000, 1000), (100, 200, 600, 700)]


def _jpeg(width: int, height: int) -> bytes:
    image = Image.linear_gradient("L").resize((width, height)).convert("RGB")
    draw = ImageDraw.Draw(image)
    for x in range(0, width, 97):
        draw.line([(x, 0), (width - x, height)], fill=(200, 30, 60), width=3)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _png(width: int, height: int, mode: str) -> bytes:
    image = Image.linear_gradient("L").resize((width, height))
    draw = ImageDraw.Draw(image)
    for y in range(0, height, 53):
        draw.line([(0, y), (width, height - y)], fill=255, width=2)
    if mode == "RGB":
        flipped = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        image = Image.merge("RGB", (image, flipped, image))
    elif mode == "P":
        image = image.convert("RGB").convert("P", palette=Image.Palette.ADAPTIVE, colors=64)
    elif mode == "1":
        image = image.convert("1")
    elif mode == "I;16":
        image = image.convert("I;16")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


#: (输入, 角度) → origin/main 的 crop_image 给出的每一块的 sha256 前 16 位与尺寸。
_GOLDEN = {
    ("jpeg-9487x6000", 0): (["546fdab999e82bbd", "a686a703f4c1f44c"], [(1540, 973)] * 2),
    ("jpeg-9487x6000", 90): (["b03fe9a2f47d4fd3", "572c6d9fcdcb4c2e"], [(973, 1540)] * 2),
    ("png-rgb-3000x2000", 0): (["a0ea381bd0493669", "92c429a00765d139"], [(1500, 1000)] * 2),
    ("png-rgb-3000x2000", 90): (["1e78e9fc4476a7f3", "f47eb9c8ad4d2b60"], [(1000, 1500)] * 2),
    ("png-p-3000x2000", 0): (["bfd941fa4c41c077", "1a84f42348020afa"], [(1500, 1000)] * 2),
    ("png-p-3000x2000", 90): (["6b566591cca3aa01", "1d69b8e9b6ce1e75"], [(1000, 1500)] * 2),
    ("png-1-3000x2000", 0): (["badcfbc9951b95c8", "31bd3ba6e6c7090c"], [(1500, 1000)] * 2),
    ("png-1-3000x2000", 90): (["87eeb376fe42804c", "3d2ac84e2e0a946d"], [(1000, 1500)] * 2),
    ("png-i16-3000x2000", 0): (["831e2e7904fb51c3", "1b7fd9d032bfdb4d"], [(1500, 1000)] * 2),
    ("png-i16-3000x2000", 90): (["a6c63bf09937c9c3", "fc966acb10f1a4b7"], [(1000, 1500)] * 2),
}
_INPUTS = {
    "jpeg-9487x6000": lambda: _jpeg(9487, 6000),
    "png-rgb-3000x2000": lambda: _png(3000, 2000, "RGB"),
    "png-p-3000x2000": lambda: _png(3000, 2000, "P"),
    "png-1-3000x2000": lambda: _png(3000, 2000, "1"),
    "png-i16-3000x2000": lambda: _png(3000, 2000, "I;16"),
}


@pytest.mark.parametrize(("name", "degrees"), sorted(_GOLDEN))
def test_crop_image_is_byte_identical_to_origin_main(name: str, degrees: int) -> None:
    crops = crop_image(_INPUTS[name](), _BOXES, degrees)
    hashes, sizes = _GOLDEN[(name, degrees)]
    assert [hashlib.sha256(c.data).hexdigest()[:16] for c in crops] == hashes
    assert [(c.width, c.height) for c in crops] == sizes
