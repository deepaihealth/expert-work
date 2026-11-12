"""B-302 PR 2 —— 两个按智能体覆盖的字段(§4.2.9,T2-10)。

``policies.reading_rule``(第 1 层读取规矩)与 ``policies.figure_view_steps``(第 2 层页图只留几步):
空 = 跟平台设置。仿 B-126「默认值不落库」:存量配置的内容与指纹不变,回滚到没有这两个字段的镜像时
不填的配置照样能读(``PolicySpec`` 是 ``extra="forbid"``)。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from expert_work.protocol.agent_spec import PolicySpec


def test_both_overrides_default_to_follow_the_platform() -> None:
    policy = PolicySpec()
    assert policy.reading_rule is None
    assert policy.figure_view_steps is None


def test_unset_overrides_are_not_written_out() -> None:
    for dumped in (PolicySpec().model_dump(), PolicySpec().model_dump(mode="json")):
        assert "reading_rule" not in dumped
        assert "figure_view_steps" not in dumped
    # 已有字段照旧全量写出(只省这两个)。
    assert "run_deadline_s" in PolicySpec().model_dump()


def test_set_overrides_are_kept_including_off_and_zero() -> None:
    """``False`` / ``0`` 是「这个智能体明确关掉」,与「跟平台」不同,必须落库。"""
    on = PolicySpec(reading_rule=True, figure_view_steps=3).model_dump(mode="json")
    assert on["reading_rule"] is True
    assert on["figure_view_steps"] == 3
    off = PolicySpec(reading_rule=False, figure_view_steps=0).model_dump()
    assert off["reading_rule"] is False
    assert off["figure_view_steps"] == 0


def test_a_dump_round_trips() -> None:
    policy = PolicySpec(figure_view_steps=3)
    assert PolicySpec.model_validate(policy.model_dump()) == policy
    assert PolicySpec.model_validate(PolicySpec().model_dump()) == PolicySpec()


@pytest.mark.parametrize("value", [-1, 51])
def test_the_window_is_bounded(value: int) -> None:
    with pytest.raises(ValidationError):
        PolicySpec(figure_view_steps=value)
