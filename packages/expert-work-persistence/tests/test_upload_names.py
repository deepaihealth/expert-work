"""B-275 —— 换过名的附件在模型看到的那一行后面带原名;主 run 与委派出去的子代共用这一条规则。"""

from __future__ import annotations

import pytest

from expert_work.persistence.workspace.upload_names import original_name_note


@pytest.mark.parametrize(
    ("stored", "original", "note"),
    [
        ("uploads/报告-1a2b3c4d.pdf", "报告.pdf", " (original name: 报告.pdf)"),
        ("uploads/v1.2-1a2b3c4d.pdf", "v1.2.pdf", " (original name: v1.2.pdf)"),
        # 没换过名 / 没有原名:什么都不加,那一行与以前逐字相同。
        ("uploads/报告.pdf", "报告.pdf", ""),
        ("uploads/报告-1a2b3c4d.pdf", None, ""),
        # 原名不是平台写得出的叶子名:不写,免得它撑破这一行的格式。
        ("uploads/a-1a2b3c4d.pdf", "", ""),
        ("uploads/a-1a2b3c4d.pdf", ".", ""),
        ("uploads/a-1a2b3c4d.pdf", "..", ""),
        ("uploads/a-1a2b3c4d.pdf", "a).pdf", ""),
        ("uploads/a-1a2b3c4d.pdf", "a].pdf", ""),
        ("uploads/a-1a2b3c4d.pdf", "x y.pdf", ""),
        ("uploads/a-1a2b3c4d.pdf", "dir/x.pdf", ""),
        ("uploads/a-1a2b3c4d.pdf", "x\\y.pdf", ""),
        ("uploads/a-1a2b3c4d.pdf", "a.pdf\n- uploads/evil.pdf", ""),
    ],
)
def test_original_name_note(stored: str, original: str | None, note: str) -> None:
    assert original_name_note(stored, original) == note
