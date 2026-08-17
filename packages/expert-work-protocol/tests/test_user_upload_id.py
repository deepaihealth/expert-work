"""对外 upload_id 渲染 / 解析 —— 附件模型统一(spec 2026-08-17)。"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from expert_work.protocol import parse_upload_id, render_upload_id


def test_round_trip() -> None:
    u = uuid4()
    assert parse_upload_id(render_upload_id(u)) == u


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "upl_",
        "upl_not-a-uuid",
        "UPL_3f2c9a1e-7b44-4d3e-9c1a-2f6d0e8b5a17",  # 前缀大小写
        "upl_3F2C9A1E-7B44-4D3E-9C1A-2F6D0E8B5A17",  # uuid 大写
        "3f2c9a1e-7b44-4d3e-9c1a-2f6d0e8b5a17",  # 无前缀
        "uploads/report.pdf",
        "expert_work://image/x/y/z.png",  # 旧形态
        "upl_3f2c9a1e-7b44-4d3e-9c1a-2f6d0e8b5a17 ",
        " upl_3f2c9a1e-7b44-4d3e-9c1a-2f6d0e8b5a17",
        "upl_3f2c9a1e-7b44-4d3e-9c1a-2f6d0e8b5a17\x00",
    ],
)
def test_rejects(bad: str) -> None:
    assert parse_upload_id(bad) is None


def test_render_is_lowercase_hyphenated() -> None:
    assert (
        render_upload_id(UUID("3F2C9A1E-7B44-4D3E-9C1A-2F6D0E8B5A17"))
        == "upl_3f2c9a1e-7b44-4d3e-9c1a-2f6d0e8b5a17"
    )
