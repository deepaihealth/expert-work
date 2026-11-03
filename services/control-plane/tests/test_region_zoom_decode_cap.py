"""B-266 —— 「放大局部」宿主裁图的解码上限与上传 / 知识库图片(B-263)是同一个值。

编排层不能 import control-plane,所以 ``orchestrator.tools.region_zoom`` 另记了一份;这里钉两边相等。
"""

from __future__ import annotations

from control_plane.knowledge.recognition.figure_images import MAX_DECODE_PIXELS
from orchestrator.tools import region_zoom


def test_region_crop_decode_cap_matches_the_upload_cap() -> None:
    assert region_zoom.MAX_DECODE_PIXELS == MAX_DECODE_PIXELS
