"""B-322 复审第五轮 M1 —— 现量峰值内存,对估算(设计 §4.1.5)。

审查的方法:新进程、读文件、调一次、看 ``ru_maxrss`` 涨了多少(先用一张小图热一遍编解码器,量的
只是这一次解码)。灰度 / RGB / CMYK 的 JPEG 各 12、20、48 MP:

* 问方向的候选 / 小副本(``crop_image`` 整张框、转 90 度):实测不超过估算(估算已乘 1.2);占 1 个
  解图名额的,两个同时在算也不超过每张的预算 110 MiB;
* 全分辨率转正图(按 qwen4-max 的单图预算):估得下的,实测不超过 110 MiB,也不超过估算。
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from orchestrator.image_views import FULL_COPY_PEAK_BYTES, full_copy_peak, slots_for
from orchestrator.tools.region_zoom import crop_peak_bytes

_QWEN_BUDGET = 12_595_200
_SIZES = {"12MP": (4032, 3024), "20MP": (5184, 3888), "48MP": (8064, 6048)}

_MEASURE = """
import gc, json, resource, sys
path, op = sys.argv[1], sys.argv[2]
data = open(path, "rb").read()
from orchestrator.image_views import upright_copy
from orchestrator.tools.region_zoom import crop_image
# 先用一张小图把编解码器、延迟导入走一遍:量的是这一次解码本身(生产进程早就热了)。
small = open(sys.argv[4], "rb").read()
crop_image(small, [(0, 0, 1000, 1000)], 90)
upright_copy(small, 90)
gc.collect()
base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
if op == "crop":
    crop_image(data, [(0, 0, 1000, 1000)], 90)
else:
    upright_copy(data, 90, int(sys.argv[3]))
grown = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - base
print(json.dumps(grown * (1 if sys.platform == "darwin" else 1024)))
"""


def _photo(size: tuple[int, int], mode: str) -> bytes:
    """像照片的合成图(渐变 + 噪点),存成 JPEG 92。"""
    gradient = Image.linear_gradient("L").resize(size)
    noise = Image.effect_noise(size, 6)
    image = Image.merge("RGB", (gradient, Image.blend(gradient, noise, 0.3), noise))
    buffer = io.BytesIO()
    image.convert(mode).save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def _grown(path: Path, op: str, warm: Path) -> int:
    """在新进程里(先用小图 ``warm`` 热一遍)调一次,返回 ``ru_maxrss`` 涨了多少字节。"""
    out = subprocess.run(  # noqa: S603 - 固定的解释器与脚本
        [sys.executable, "-c", _MEASURE, str(path), op, str(_QWEN_BUDGET), str(warm)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return int(json.loads(out.stdout.strip().splitlines()[-1]))


@pytest.mark.parametrize("mode", ["L", "RGB", "CMYK"])
@pytest.mark.parametrize("name", list(_SIZES))
def test_measured_peaks_stay_within_the_estimates(tmp_path: Path, name: str, mode: str) -> None:
    size = _SIZES[name]
    data = _photo(size, mode)
    path = tmp_path / f"{name}-{mode}.jpg"
    path.write_bytes(data)
    warm = tmp_path / f"warm-{mode}.jpg"
    warm.write_bytes(_photo((64, 48), mode))
    mib = 1024 * 1024

    crop_real, crop_estimate = _grown(path, "crop", warm), crop_peak_bytes(*size, mode)
    assert crop_real <= crop_estimate, (crop_real / mib, crop_estimate / mib)
    if slots_for(*size, mode) == 1:
        assert 2 * crop_real <= FULL_COPY_PEAK_BYTES

    full_estimate = full_copy_peak(data, _QWEN_BUDGET)
    assert full_estimate is not None
    if full_estimate <= FULL_COPY_PEAK_BYTES:
        full_real = _grown(path, "full", warm)
        assert full_real <= full_estimate, (full_real / mib, full_estimate / mib)
        assert full_real <= FULL_COPY_PEAK_BYTES
