"""B-301 —— ``ask_image`` 读图之前先问看图模型「主要文字是正的吗」,不正就试三个角度。

设计见 ``docs/superpowers/specs/2026-11-11-b301-image-orientation-design.md``
§4.1 / §4.3 / §4.4 / §6。本模块只放纯逻辑:问的那句话、回答的解析、按回答下结论、结论的缓存格式 /
轨迹字段 / 追加的说明、计数器,以及运维紧急开关。调用看图模型、裁候选副本、读写缓存文件在
:mod:`orchestrator.tools.vision`。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal, get_args

from expert_work.common.observability.metrics import expert_work_counter

#: 问的那句话(平台英文、通用,不带任何场景词;设计 §4.1)。
ORIENTATION_QUESTION: Final = (
    "Look only at orientation. Is the main text in this image upright, that is, readable "
    "without turning the image? Answer with exactly one word: yes, no, or none (the image "
    "has no readable text)."
)
#: 一次问方向的整体限时(秒),含排队等切块名额。普通快看测试环境 30 天最长 28 秒。
ORIENTATION_TIMEOUT_S: Final = 30.0
#: run 剩的时间扣掉留给读的那一份之后,不到这么多秒就不问了:这点时间多半等不到回答,
#: 只会白白多一次超时。
ORIENTATION_MIN_BUDGET_S: Final = 5.0
#: 第 2 步试的三个角度(顺时针,在 0 度候选上转)。
TURNS: Final = (90, 180, 270)
#: 结论的缓存文件名,放在本 run 这张图的裁图目录里。
CACHE_NAME: Final = "orientation.json"
#: 运维紧急开关(环境变量,默认开;不给智能体 / 控制台);``0`` / ``false`` / ``off`` / ``no``
#: (不分大小写)为关。
ORIENTATION_ENV: Final = "EXPERT_WORK_ASK_IMAGE_ORIENTATION"
_OFF_VALUES: Final = frozenset({"0", "false", "off", "no"})

Conclusion = Literal["upright", "no_text", "rotated", "undetermined", "not_asked"]
#: 一个方向上的回答:解析出的词,或没答成的原因。
Answer = Literal["yes", "no", "none", "off_topic", "timeout", "error"]
#: ``not_asked`` 的原因。
Reason = Literal["deadline", "no_copy", "no_answer", "error"]
_WORDS: Final = ("yes", "no", "none")
_ANSWERS: Final[dict[str, Answer]] = {answer: answer for answer in get_args(Answer)}
_CONCLUSIONS: Final[dict[str, Conclusion]] = {c: c for c in get_args(Conclusion)}
_REASONS: Final[dict[str, Reason]] = {r: r for r in get_args(Reason)}

ROTATED_NOTE: Final = (
    "(The image was stored turned; it was rotated {degrees} degrees clockwise before reading. "
    "Any region refers to the rotated image.)"
)
UNDETERMINED_NOTE: Final = (
    "(The text in this image does not look upright and its orientation could not be "
    "determined, so what was read from it may be wrong. Tell the user it could not be read "
    "reliably.)"
)

ask_image_orientation_total = expert_work_counter(
    "expert_work_ask_image_orientation_total",
    "ask_image orientation checks (B-301) by conclusion (upright, no_text, rotated, "
    "undetermined, not_asked) and the clockwise degrees applied before reading. Counted once "
    "per image per run, when the check is made (not when a later look reuses it).",
    ("conclusion", "degrees"),
)
ask_image_orientation_off_topic_total = expert_work_counter(
    "expert_work_ask_image_orientation_off_topic_total",
    "ask_image orientation questions (B-301) whose answer was not yes, no or none.",
    ("model",),
)

_THINK_CLOSE = re.compile(r"</think>", re.IGNORECASE)
_NOT_WORD = re.compile(r"[^\w\s]")


def parse_orientation_reply(text: str) -> Literal["yes", "no", "none"] | None:
    """去掉思考部分(最后一个 ``</think>`` 之前的全部)、markdown 符号与标点,转小写,取第一个词;
    不是 yes / no / none 就是 ``None``。"""
    answer = _THINK_CLOSE.split(text)[-1]
    words = _NOT_WORD.sub(" ", answer).lower().split()
    if not words:
        return None
    first = words[0]
    for word in _WORDS:
        if first == word:
            return word
    return None


def first_conclusion(answer: Answer) -> Conclusion | None:
    """第 1 步(0 度候选)的回答 → 结论;``None`` = 答 no,要做第 2 步。"""
    if answer == "yes":
        return "upright"
    if answer == "none":
        return "no_text"
    if answer == "no":
        return None
    return "not_asked"


def turned_conclusion(answers: Mapping[int, Answer]) -> tuple[Conclusion, int]:
    """第 2 步:三个角度都答成了、且恰好一个 yes → 转那个角度;其余一律判不出、按 0 度。"""
    answered = all(answers.get(turn) in _WORDS for turn in TURNS)
    yes = [turn for turn in TURNS if answers.get(turn) == "yes"]
    if answered and len(yes) == 1:
        return "rotated", yes[0]
    return "undetermined", 0


@dataclass(frozen=True)
class Orientation:
    """一张图在本 run 里的方向结论(设计 §4.4 的轨迹字段 + 缓存内容)。"""

    conclusion: Conclusion
    #: 读之前顺时针转的角度;只有 ``rotated`` 不是 0。
    degrees: int = 0
    #: 角度(字符串)→ 回答;没问到的角度不出现。
    answers: Mapping[str, Answer] = field(default_factory=dict)
    #: 角度(字符串)→ 实际应答的模型(备用链上可能换了模型);没答成是 ``None``。
    models: Mapping[str, str | None] = field(default_factory=dict)
    #: ``not_asked`` 的原因:``deadline``(run 剩的时间不够)/ ``no_copy``(副本裁不出来)/
    #: ``no_answer``(第 1 步没答成)/ ``error``(这一步自己出错);其余结论是 ``None``。
    reason: Reason | None = None
    from_cache: bool = False
    #: 这一次 ``ask_image`` 为问方向多花的调用数(用了缓存是 0)。
    extra_calls: int = 0

    def meta(self) -> dict[str, Any]:
        return {
            "conclusion": self.conclusion,
            "degrees": self.degrees,
            "answers": dict(self.answers),
            "models": dict(self.models),
            "reason": self.reason,
            "from_cache": self.from_cache,
            "extra_calls": self.extra_calls,
        }

    def note(self) -> str | None:
        """追加在工具结果末尾的说明(设计 §4.3 前两行);正放 / 无文字 / 没问成什么都不说。"""
        if self.conclusion == "rotated":
            return ROTATED_NOTE.format(degrees=self.degrees)
        if self.conclusion == "undetermined":
            return UNDETERMINED_NOTE
        return None

    def cache_bytes(self) -> bytes:
        payload = {
            "conclusion": self.conclusion,
            "degrees": self.degrees,
            "answers": dict(self.answers),
            "models": dict(self.models),
            "reason": self.reason,
        }
        return json.dumps(payload, sort_keys=True).encode()

    @classmethod
    def from_cache_bytes(cls, raw: bytes) -> Orientation | None:
        """读回缓存;格式不对(被改坏、旧版本)就是 ``None``,调用方重新问。"""
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        raw_conclusion, degrees = payload.get("conclusion"), payload.get("degrees")
        conclusion = _CONCLUSIONS.get(raw_conclusion) if isinstance(raw_conclusion, str) else None
        if conclusion is None or not isinstance(degrees, int) or isinstance(degrees, bool):
            return None
        if degrees not in (0, *TURNS) or (degrees != 0) != (conclusion == "rotated"):
            return None
        answers = _checked_answers(payload.get("answers"))
        models = _checked_models(payload["models"] if "models" in payload else _old_models(payload))
        raw_reason = payload.get("reason")
        reason = _REASONS.get(raw_reason) if isinstance(raw_reason, str) else None
        if answers is None or models is None or (raw_reason is not None and reason is None):
            return None
        return cls(conclusion, degrees, answers, models, reason, from_cache=True, extra_calls=0)


def _checked_answers(raw: object) -> dict[str, Answer] | None:
    if not isinstance(raw, dict):
        return None
    checked: dict[str, Answer] = {}
    for key, value in raw.items():
        answer = _ANSWERS.get(value) if isinstance(value, str) else None
        if not isinstance(key, str) or answer is None:
            return None
        checked[key] = answer
    return checked


def _old_models(payload: Mapping[str, object]) -> object:
    """上一版写的缓存只有一个 ``model``(回答第 1 步的模型),没有 ``models`` / ``reason``:
    跨一次发布的 run 照样认,``model`` 记成 0 度那一项。两样都没有就不是合法的缓存。"""
    if "model" not in payload:
        return None
    model = payload["model"]
    if model is None:
        return {}
    return {"0": model}


def _checked_models(raw: object) -> dict[str, str | None] | None:
    if not isinstance(raw, dict):
        return None
    if not all(isinstance(k, str) and (v is None or isinstance(v, str)) for k, v in raw.items()):
        return None
    return dict(raw)


def orientation_switch_on() -> bool:
    """运维紧急开关:默认开。装配时填进工具的布尔只看它 —— 问方向是底层能力,所有看图模型都做,
    不按模型、不按智能体开关(用户 11-12 改定,设计 §6)。"""
    return os.environ.get(ORIENTATION_ENV, "").strip().lower() not in _OFF_VALUES
