"""B-84 item 7 —— eager(``lazy_load is False``)正文的上架上限。

护栏是**条件**的:只有 eager 技能受 8,000 字符限制(它的正文每轮都整段
进系统提示词),lazy 技能继续走 256 KiB 字节上限(正文只在 ``skill_view``
时才加载)。

:func:`test_lazy_body_of_the_same_size_is_accepted` 是这组里最重要的一条:
它钉住的不是"有没有上限",而是"上限是**条件**的"。谁把判定改成无条件,
它当场红。
"""

from __future__ import annotations

import pytest

from control_plane.api._skill_moderation import (
    MAX_EAGER_PROMPT_FRAGMENT_CHARS,
    MAX_PROMPT_FRAGMENT_BYTES,
    ModerationError,
    moderate_prompt_fragment,
)


def test_eager_body_over_the_char_limit_is_rejected() -> None:
    text = "x" * (MAX_EAGER_PROMPT_FRAGMENT_CHARS + 1)
    with pytest.raises(ModerationError) as excinfo:
        moderate_prompt_fragment(text, lazy_load=False)
    assert excinfo.value.code == "eager_prompt_fragment_too_large"
    detail = excinfo.value.detail
    # 文案要说清超了多少字符…
    assert f"{MAX_EAGER_PROMPT_FRAGMENT_CHARS + 1} characters" in detail
    assert "1 over" in detail
    # …以及两条出路:改成 lazy,或者拆成多个技能。
    assert "lazy: true" in detail
    assert "split it into" in detail


def test_lazy_body_of_the_same_size_is_accepted() -> None:
    """同样 8,001 字符,lazy 放行 —— 这条专门防后人把条件改成无条件。

    2026-09-20 测试环境实测 ``prompt_fragment`` 最大 46,741 / p90 19,181 /
    中位 7,527 字符;8,000 一刀切会打死存量里一大半 lazy 技能,而它们
    并没有在每轮付这笔钱。
    """
    text = "x" * (MAX_EAGER_PROMPT_FRAGMENT_CHARS + 1)
    moderate_prompt_fragment(text, lazy_load=True)  # 不抛 = 通过


def test_eager_body_exactly_at_the_char_limit_is_accepted() -> None:
    text = "x" * MAX_EAGER_PROMPT_FRAGMENT_CHARS
    moderate_prompt_fragment(text, lazy_load=False)  # 不抛 = 通过


def test_lazy_byte_cap_is_untouched() -> None:
    """lazy 路径的 256 KiB 字节上限一个字节都没动。"""
    text = "x" * (MAX_PROMPT_FRAGMENT_BYTES + 1)
    with pytest.raises(ModerationError) as excinfo:
        moderate_prompt_fragment(text, lazy_load=True)
    assert excinfo.value.code == "prompt_fragment_too_large"
