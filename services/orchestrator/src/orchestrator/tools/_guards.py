"""B3 token 熔断 — 共享预算对象 + guard 帧契约(spec:
docs/superpowers/specs/2026-07-20-token-budget-breaker-design.md)。

keys 定义在 tools 层(``_worker_events.WORKER_EVENT_SINK_KEY`` 同款理由):
``orchestrator.tools`` 是 ``graph_builder`` 的下层,反向 import 成包环。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

logger = logging.getLogger(__name__)

#: config["configurable"] key — run_agent 注入的全树共享 TokenBudget。
TOKEN_BUDGET_KEY = "token_budget"  # noqa: S105 — config key, not a credential
#: config["configurable"] key — guard marker 帧 sink(compaction sink 同款)。
GUARD_SINK_KEY = "guard_event_sink"

GuardSink = Callable[[dict[str, Any]], Awaitable[None]]

#: B-226 ④ —— ``policies.max_no_progress`` 为 0(默认)时的平台规则:同一次运行里第二次
#: 被 loop-detection 拦下就走无进展收尾。配了 N 就是第 N 次;都按本 run 累计, 中间干净的
#: 轮次不清零。拦下之后提醒在同一个 run 里交回模型, 按「连续」算的话「打转 → 换个调用 →
#: 再打转」会一路跑到步数上限。住在 tools 层:子代收尾说明(``_child_run``)也要用。
DEFAULT_LOOP_TRIPS_PER_RUN = 2


@dataclass
class TokenBudget:
    """全委托树共扣的 token 池 — 单事件循环,无锁。"""

    limit: int
    spent: int = 0
    #: warning 帧已发(挂共享对象 → 全树只发一次)。
    warned: bool = False

    WARN_PCT: ClassVar[float] = 0.8

    def add(self, n: int) -> None:
        self.spent += n

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.limit

    @property
    def warning(self) -> bool:
        return self.spent >= self.limit * self.WARN_PCT

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.spent)


def usage_total(usage_metadata: Mapping[str, Any] | None) -> int:
    """input + output + cache_creation + cache_read(与 TokenUsageMiddleware
    的抽取同源口径);形状异常按 0 计,绝不抛。"""
    if not isinstance(usage_metadata, Mapping):
        return 0
    total = 0
    for key in ("input_tokens", "output_tokens"):
        v = usage_metadata.get(key)
        if isinstance(v, int):
            total += v
    details = usage_metadata.get("input_token_details")
    if isinstance(details, Mapping):
        for key in ("cache_creation", "cache_read"):
            v = details.get(key)
            if isinstance(v, int):
                total += v
    return total


def build_guard_frame(*, kind: str, guard: str, detail: Mapping[str, Any]) -> dict[str, Any]:
    return {"kind": kind, "guard": guard, "detail": dict(detail)}


async def emit_guard_frame(sink: GuardSink | None, frame: dict[str, Any]) -> None:
    """Best-effort — guard 可见化故障绝不影响 run 本体."""
    if sink is None:
        return
    try:
        await sink(frame)
    except Exception as exc:
        logger.warning(
            "guards.frame_failed guard=%s err=%s", frame.get("guard", "?"), type(exc).__name__
        )
