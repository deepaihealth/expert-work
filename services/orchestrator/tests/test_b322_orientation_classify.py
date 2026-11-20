"""B-322 PR ① —— 主模型问方向的回答按 PR ① 自己的规则归类(设计 §4.1.3 结论表)。

B-301 的 ``turned_conclusion`` 在第 2 步「没全答成」时给 undetermined;PR ① 不看它,而是按回答
重新归类:第 0 度 no、第 2 步有任何一个角度没答成 → pending;只采信核实模型给的回答。
"""

from __future__ import annotations

import pytest

from orchestrator.tools.image_orientation import (
    MAIN_MODEL_ORIENTATION_ENV,
    classify_main_model_answers,
    main_model_orientation_switch_on,
    turned_conclusion,
)

_V = "qwen/qwen4-max"
_U = "deepseek/deepseek-flash"


def _trusted(label: str | None) -> bool:
    return label == _V


def _classify(answers: dict[str, str], models: dict[str, str | None] | None = None, **kw: bool):  # type: ignore[no-untyped-def]
    models = models if models is not None else dict.fromkeys(answers, _V)
    return classify_main_model_answers(answers, models, trusted=_trusted, **kw)  # type: ignore[arg-type]


def test_first_step_yes_and_none() -> None:
    assert _classify({"0": "yes"}).conclusion == "upright"
    assert _classify({"0": "none"}).conclusion == "no_text"


@pytest.mark.parametrize("first", ["timeout", "error", "off_topic"])
def test_first_step_not_answered_is_unjudged(first: str) -> None:
    assert _classify({"0": first}).conclusion == "unjudged"


def test_nothing_asked_is_unjudged() -> None:
    assert _classify({}).conclusion == "unjudged"


def test_exactly_one_yes_is_rotated() -> None:
    result = _classify({"0": "no", "90": "no", "180": "yes", "270": "no"})
    assert (result.conclusion, result.degrees) == ("rotated", 180)


def test_all_no_or_two_yes_is_undetermined() -> None:
    assert _classify({"0": "no", "90": "no", "180": "no", "270": "no"}).conclusion == (
        "undetermined"
    )
    assert _classify({"0": "no", "90": "yes", "180": "no", "270": "yes"}).conclusion == (
        "undetermined"
    )


@pytest.mark.parametrize(
    "turned",
    [
        {"90": "timeout", "180": "timeout", "270": "timeout"},
        {"90": "yes", "180": "error", "270": "no"},
        {"90": "off_topic", "180": "no", "270": "no"},
        {},  # 候选图没生成出来 / 时间不够,根本没问到
    ],
)
def test_second_step_incomplete_is_pending_not_undetermined(turned: dict[str, str]) -> None:
    answers = {"0": "no", **turned}
    assert _classify(answers).conclusion == "pending"
    # B-301 自己的判定在同样情形下仍是 undetermined(PR ① 不改它)。
    by_turn = {int(k): v for k, v in turned.items()}
    assert turned_conclusion(by_turn)[0] == "undetermined"  # type: ignore[arg-type]


def test_pending_after_its_one_retry_becomes_undetermined() -> None:
    answers = {"0": "no", "90": "timeout", "180": "no", "270": "no"}
    assert _classify(answers, final=True).conclusion == "undetermined"


def test_unjudged_after_its_one_retry_stays_unjudged() -> None:
    assert _classify({"0": "timeout"}, final=True).conclusion == "unjudged"


def test_unverified_first_answer_is_unjudged() -> None:
    result = _classify({"0": "yes"}, {"0": _U})
    assert result.conclusion == "unjudged"
    assert result.unverified_answer is True


def test_unverified_second_step_answer_makes_it_pending() -> None:
    answers = {"0": "no", "90": "yes", "180": "no", "270": "no"}
    result = _classify(answers, {"0": _V, "90": _U, "180": _V, "270": _V})
    assert result.conclusion == "pending"
    assert result.unverified_answer is True


def test_verified_answers_never_flag_unverified() -> None:
    answers = {"0": "no", "90": "yes", "180": "no", "270": "no"}
    assert _classify(answers).unverified_answer is False


@pytest.mark.parametrize(("raw", "on"), [("", True), ("1", True), ("off", False), ("NO", False)])
def test_emergency_switch(monkeypatch: pytest.MonkeyPatch, raw: str, on: bool) -> None:
    monkeypatch.setenv(MAIN_MODEL_ORIENTATION_ENV, raw)
    assert main_model_orientation_switch_on() is on
