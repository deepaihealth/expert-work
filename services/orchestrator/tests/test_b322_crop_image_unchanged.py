"""B-322 第五轮(2026-11-20,用户收窄范围):看图工具(Path B / ``ask_image``)的裁图与 origin/main 一致。

``region_zoom.py`` 里 origin/main 上原有的每个顶层定义(函数、类、常量),源码必须逐字不变;B-322 只能
新增定义。下面的哈希是对 origin/main ``aa1e8a890`` 的该文件逐项取 ``ast.get_source_segment`` 后算的
sha256 前 16 位。

为什么比源码不比裁出来的字节:JPEG 编码在不同平台(macOS arm64 / Linux x86_64 的 SIMD 路径)上字节不同,
同一份代码在 CI 上裁出的哈希与本机不同 —— 比字节的测试在 CI 必红。源码相同即行为相同。

以后有意改动看图工具的裁图时,改这里的哈希即可(这条只防 B-322 顺手改到 Path B)。
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from orchestrator.tools import region_zoom

#: origin/main 上 ``region_zoom.py`` 每个顶层定义的源码哈希。
_MAIN_SOURCE = {
    "AUTO_BAND_OVERLAP": "ac01b8ebbe066a97",
    "AUTO_BAND_PIXELS": "6a521bab1cfcf3a6",
    "AUTO_GATE_PIXELS": "5db1047816fc983e",
    "AUTO_MAX_BANDS": "8359900d252f3a99",
    "AUTO_MIN_BANDS": "7d9203186e9253cb",
    "AUTO_REGION": "3519b16cf0eab4b2",
    "BandAxis": "8ccefb1a3208509c",
    "Box": "acc8b84ea674e281",
    "CROP_JPEG_QUALITY": "32c274203e73dc1b",
    "CroppedImage": "f95d74752231c556",
    "DOCUMENT_ZOOM_DPI": "ac2b883e2bccde81",
    "HOST_CROP_SLOTS": "6b8ccb9a3ea9f63a",
    "LARGE_IMAGE_PIXELS": "8bc95413d0dd6c7b",
    "MAX_BAND_ASPECT": "1547d2737c1a63f8",
    "MAX_BAND_PIXELS": "3d5e3a3cf3c0a3ac",
    "MAX_DECODE_PIXELS": "b65e1af6b322a16b",
    "MAX_REGION_DPI": "698df9ecd3f8e899",
    "MAX_SIDE_PIXELS": "ef207b105422e9ee",
    "MIN_REGION_AREA": "0140e54ccc02830a",
    "OPEN_FORMATS": "e9d2b458b924d287",
    "REGION_HINT": "500e8a7b7fc97e0e",
    "REGION_SCALE": "60fd08cbd9d190f0",
    "REGION_TARGET_PIXELS": "a4607f6931c41f7d",
    "ROTATIONS": "c75319dc5906e22c",
    "Region": "7af4d449c6b0747b",
    "RegionImageError": "ae6982cc58340f78",
    "Tile": "64cc2e6b538a3da5",
    "VERTICAL_BANDS_ASPECT": "5440fc4c07e29acb",
    "_BOX_SHAPE": "c6dab152bb74b277",
    "_JPEG_DRAFT_SCALES": "cac97788e44549af",
    "_SWAPPED_ORIENTATIONS": "c5ce2dea50611bd9",
    "_TURNED": "716115f7326b3bd7",
    "_UPRIGHT_METHODS": "ce82aef0165b4623",
    "_band_aspect": "4a3c61df47354646",
    "_band_spans": "8fa87b25c715c245",
    "_box_size_at": "0b71bfd9023f82e9",
    "_capped": "533a8434fcab82e3",
    "_capped_dpi": "1dcdb2fd0f5f2626",
    "_draft_scale": "8ec9d84183001ac3",
    "_flatten": "3403d05dd82cab95",
    "_orientation": "0e34a7b79925ebfb",
    "_raw_point": "c5a2df5a27318fb7",
    "auto_bands": "ce43ea52c80a28a9",
    "band_axis": "288874dd5595031b",
    "band_count": "ff56cd61d1b760ca",
    "band_dpi": "388f6f7a5180588b",
    "crop_image": "b3d2c386111798eb",
    "crop_output_size": "6dcf4c503df1250b",
    "parse_box": "93143366d333cec3",
    "parse_region": "9f13d60f2ef0f8c9",
    "pixel_box": "c341073439a5103c",
    "plan_bands": "94d71ebf12d3c284",
    "region_dpi": "b79c641f5e272c5d",
    "turned_orientation": "61dc9c775a2116d7",
}

#: B-332(2026-11-21)有意改了、或新增的定义:裁图不再为整张图叠几份拷贝,方向只读文件头(行为见
#: ``test_b332_bounded_crop.py``,它把整张框与不用缩的框逐字节钉在 origin/main 上)。``_cap_pixels``
#: 被它取代、已删。以后再改这些,改这里的哈希(常数 ``UNDRAFTED_*`` 是校准值,由实测测试把关,
#: 不在此钉)。
_CHANGED_BY_B332 = {
    "_crop_open_image": "68a43d1d837d4dfa",
    "crop_upright": "2e964df0e4783dca",
    "image_size": "a6b9b4cdaac25c08",
    "exif_orientation": "7343239bf2c622e3",
    "header_orientation": "880da44e6a61cd3b",
    "_render_box": "a4e0f58d52b52b79",
    "_flatten_resized": "52f23caf31e900c0",
    "_flatten_region": "ca73c80ed80f8174",
    "_upright_rect": "396c3a89034f7b10",
    "_undrafted_peak_bytes": "cad9ca5557771e63",
}


def _current() -> dict[str, str]:
    source = Path(region_zoom.__file__).read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            name = node.name
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
        else:
            continue
        segment = ast.get_source_segment(source, node)
        assert segment is not None
        out[name] = hashlib.sha256(segment.encode("utf-8")).hexdigest()[:16]
    return out


@pytest.mark.parametrize("name", sorted(_MAIN_SOURCE))
def test_region_zoom_definition_is_unchanged_from_origin_main(name: str) -> None:
    current = _current()
    assert name in current, f"{name} 在 origin/main 上有,本分支删掉了"
    assert current[name] == _MAIN_SOURCE[name], (
        f"{name} 的源码与 origin/main 不同(Path B 的裁图被改了)"
    )


@pytest.mark.parametrize("name", sorted(_CHANGED_BY_B332))
def test_region_zoom_definition_changed_by_b332_is_pinned(name: str) -> None:
    assert _current()[name] == _CHANGED_BY_B332[name]
