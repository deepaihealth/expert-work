"""B-84 item 7 —— eager(``lazy_load is False``)正文的上架上限。

护栏是**条件**的:只有 eager 技能受 8,000 字符限制(它的正文每轮都整段
进系统提示词),lazy 技能继续走 256 KiB 字节上限(正文只在 ``skill_view``
时才加载)。

:func:`test_lazy_body_of_the_same_size_is_accepted` 是这组里最重要的一条:
它钉住的不是"有没有上限",而是"上限是**条件**的"。谁把判定改成无条件,
它当场红。
"""

from __future__ import annotations

import inspect
import pathlib
from types import ModuleType

import pytest
from pydantic import BaseModel

from control_plane.api import platform_skills as platform_skills_api
from control_plane.api import skills as skills_api
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


# ─── 两条 JSON add-version 路径的耦合 ─────────────────────────────────────
#
# 这两条路径的 ``store.add_version`` 调用都**没传** ``lazy_load``,所以行落在
# ``DEFAULT_SKILL_LAZY_LOAD``,moderation 也就照这个常量去判。这是个"两处必须
# 同步改"的耦合:哪天 body 模型长出 ``lazy_load`` 字段而 moderation 那一行没跟
# 着改,eager 正文上限会在这条路径上**静默失效**。
#
# 修法不是等字段出现再补测试,是现在就把"它不存在"这个事实钉住 —— 加字段的人
# 会被下面这条直接领到该改的那一行。


_MODERATION_CALL_MARKER = "lazy_load=DEFAULT_SKILL_LAZY_LOAD"


def _moderation_call_site(module: ModuleType) -> str:
    """定位 ``moderate_prompt_fragment(..., lazy_load=DEFAULT_SKILL_LAZY_LOAD)``。

    现算行号而不是写死 —— 写死的行号一次重排就过期,而过期的指路比不指路更坏。
    顺带:这一行要是整个没了(有人把接线删了),这里会先炸。
    """
    path = inspect.getsourcefile(module)
    assert path is not None
    lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    hits = [i for i, line in enumerate(lines, start=1) if _MODERATION_CALL_MARKER in line]
    assert len(hits) == 1, (
        f"{pathlib.Path(path).name} 里 {_MODERATION_CALL_MARKER!r} 命中 {len(hits)} 处"
        f"(预期 1 处)—— JSON add-version 路径的 moderation 接线变了,"
        f"请一并复核 eager 正文上限还成不成立(B-84 item 7)。"
    )
    return f"{pathlib.Path(path).name}:{hits[0]}"


@pytest.mark.parametrize(
    ("model", "module"),
    [
        (skills_api._AddVersionBody, skills_api),
        (platform_skills_api._AddPlatformVersionBody, platform_skills_api),
    ],
    ids=["tenant", "platform"],
)
def test_json_add_version_body_still_has_no_lazy_load_field(
    model: type[BaseModel], module: ModuleType
) -> None:
    call_site = _moderation_call_site(module)
    assert "lazy_load" not in model.model_fields, (
        f"{model.__name__} 新增了 lazy_load 字段 —— 请同时把 {call_site} 的 "
        f"moderate_prompt_fragment(..., lazy_load=DEFAULT_SKILL_LAZY_LOAD) "
        f"改成读这个字段,否则 eager 正文上限会在这条路径上静默失效"
        f"(B-84 item 7)。"
    )
