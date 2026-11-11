"""B-302 PR 1 —— 每一轮的时限按「实际运行了多少秒」算(spec
``docs/superpowers/specs/2026-11-11-b302-runaway-reverify-design.md`` §4.1.2)。

以前各入口写一个本进程的单调时钟截止时刻(``time.monotonic() + 秒数``):换实例就失效,
审批续跑那一段根本不带,孤儿恢复又给满额。现在:

* **运行配置**(每次起跑现写,5 个入口都调 :func:`run_segment_config`):这一轮的时限秒数、
  本段令牌(每次起跑现生成的随机串)、本段起跑时的单调时刻。没有令牌 = 不记账、不限时
  (例如控制台 live 评测;平台与智能体都填 0 时也是这样)。
* **检查点**(``AgentState``):已用秒数、上次记账的单调时刻、上次记账时的本段令牌。
  **不变式:三个键永远在同一次状态更新里一起写**(只写一两个,下一次记账就拿错基准)。
  ``agent_node`` 每步开头算一次(判提醒 / 收尾),模型调用返回之后再算一次写进状态。
* 令牌与本段相同 → 已用 += 现在 - 上次记账时刻;令牌不同(新的一段:审批续跑、孤儿恢复、
  新一轮)→ 已用 += 现在 - 本段起跑时刻。所以等人审批、实例挂掉的时间不算,孤儿恢复接着算。
* 委派出去的子代**不记账**:父的 tools 节点算出两个本进程的绝对时刻(80% 与 100%)放进子代
  配置(:data:`DEADLINE_NOTICE_AT_KEY` / :data:`DEADLINE_AT_KEY`),子代只认这两个时刻。

住在 tools 层:``_child_run`` / ``subagent`` / ``spawn_worker`` 也要用这些键,而
``orchestrator.tools`` 是 ``graph_builder`` 的下层。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from uuid import uuid4

#: 运行配置里的三个值(:func:`run_segment_config` 写,``agent_node`` / tools 节点读)。
RUN_LIMIT_KEY = "run_limit_s"
RUN_SEGMENT_KEY = "run_segment"
RUN_SEGMENT_STARTED_KEY = "run_segment_started_at"
#: 子代配置里父算好的两个绝对时刻(本进程单调时钟)。``deadline_at`` 沿用旧键名。
DEADLINE_AT_KEY = "deadline_at"
DEADLINE_NOTICE_AT_KEY = "deadline_notice_at"

#: 检查点里的三个记账键 + 「已提醒」。
ACTIVE_S_KEY = "run_active_s"
MARK_KEY = "run_clock_mark"
SEGMENT_KEY = "run_clock_segment"
WARNED_KEY = "deadline_warned"

#: 到这个比例提醒「别再开新的核对,交付已有的」,子代就地收尾,不再派新的子代。
NOTICE_FRACTION = 0.8

#: 新一轮与「重新生成」的图输入都带上:三个记账键一起清(只清已用、不清令牌的话,下一轮
#: 第一步会把上一轮的令牌当成同一段),「已提醒」清零。
RUN_CLOCK_TURN_RESET: Mapping[str, Any] = MappingProxyType(
    {ACTIVE_S_KEY: 0.0, MARK_KEY: None, SEGMENT_KEY: None, WARNED_KEY: False}
)


def now() -> float:
    """本进程的单调时钟。所有记账与截止时刻都经它(测试换成假时钟)。"""
    return time.monotonic()


def run_segment_config(limit_s: int) -> dict[str, Any]:
    """一次起跑要放进运行配置的三个值;``limit_s <= 0``(平台与智能体都是 0)= 不限时。"""
    if limit_s <= 0:
        return {}
    return {
        RUN_LIMIT_KEY: int(limit_s),
        RUN_SEGMENT_KEY: uuid4().hex,
        RUN_SEGMENT_STARTED_KEY: now(),
    }


def _number(raw: object) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    return float(raw)


def instant_from_config(configurable: Mapping[str, Any], key: str) -> float | None:
    """子代配置里父给的绝对时刻;没有 / 类型不对 = ``None``。"""
    return _number(configurable.get(key))


@dataclass(frozen=True)
class RunClock:
    """这一段的时限与令牌;记账读写的是检查点里的三个键。"""

    limit_s: float
    segment: str
    segment_started_at: float

    @classmethod
    def from_config(cls, configurable: Mapping[str, Any]) -> RunClock | None:
        """运行配置里没有令牌(或时限不是正数)= ``None``:不记账、不限时。"""
        limit = _number(configurable.get(RUN_LIMIT_KEY))
        segment = configurable.get(RUN_SEGMENT_KEY)
        started = _number(configurable.get(RUN_SEGMENT_STARTED_KEY))
        if limit is None or limit <= 0 or not isinstance(segment, str) or not segment:
            return None
        if started is None:
            return None
        return cls(limit_s=limit, segment=segment, segment_started_at=started)

    def _base(self, state: Mapping[str, Any]) -> tuple[float, float]:
        """(检查点里的已用秒数, 从哪一刻接着算)。"""
        used = _number(state.get(ACTIVE_S_KEY)) or 0.0
        mark = _number(state.get(MARK_KEY))
        if state.get(SEGMENT_KEY) == self.segment and mark is not None:
            return used, mark
        return used, self.segment_started_at

    def used_s(self, state: Mapping[str, Any], *, at: float) -> float:
        used, since = self._base(state)
        return used + max(0.0, at - since)

    def checkpoint(self, state: Mapping[str, Any], *, at: float) -> dict[str, Any]:
        """记一次账:三个键一起写。"""
        return {ACTIVE_S_KEY: self.used_s(state, at=at), MARK_KEY: at, SEGMENT_KEY: self.segment}

    def instants(self, state: Mapping[str, Any]) -> tuple[float, float]:
        """(80% 时刻, 100% 时刻),本进程单调时钟。

        = 上次记账时刻 + (界线 - 已用);令牌不同时从本段起跑时刻算。不是「现在 + 剩余」——
        那样会把记账之后到现在花掉的时间白送出去。
        """
        used, since = self._base(state)
        return (
            since + (self.limit_s * NOTICE_FRACTION - used),
            since + (self.limit_s - used),
        )


#: 过了 80% 再委派时给模型的拒绝文字(``subagent`` / ``spawn_worker`` 共用)。
FINAL_PHASE_DELEGATION_REFUSAL = (
    "[delegation refused: this run has entered its final phase (time limit); "
    "complete the work with what you already have instead of delegating]"
)


def past_notice(notice_at: float | None) -> bool:
    """这一轮是否已过 80%(没有时限 = 否)。"""
    return notice_at is not None and now() >= notice_at


def format_minutes(seconds: float) -> str:
    """提醒与收尾指令里的分钟数:最多一位小数(90、72.3)。"""
    return f"{round(seconds / 60, 1):g}"


def deadline_notice_text(*, used_s: float, limit_s: float, registers: bool) -> str:
    """到 80% 之后每步附在提示词尾部的一行(只进这一次视图,不落库)。

    与步数预警的措辞故意相反:步数多不代表卡住;时限提醒是「别再开新的核对,交付已有的」。
    ``registers``:本 agent 绑了 ``save_artifact`` 才提「登记」。
    """
    register = ", and register finished files with save_artifact" if registers else ""
    return (
        f"[time limit notice] This run has been working for {format_minutes(used_s)} of its "
        f"{format_minutes(limit_s)} minutes. Do not start new checks, re-reads or verification "
        "passes. Finish the requested result from what you already have and deliver it"
        f"{register}. In your reply, say plainly what was not checked."
    )
