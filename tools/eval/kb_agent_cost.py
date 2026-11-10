"""知识库智能体层评测的计费与预算(从 ``kb_agent_eval.py`` 拆出,B-206 11-09)。"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

#: 暂停时多久看一次 ``<out>.budget``(秒)。
BUDGET_POLL_S = 5.0
_BEIJING = ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------------------
# 计费
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Price:
    """元 / 百万 token,价目表上的(忙时)价。"""

    input: float
    cached: float
    output: float


PRICES: dict[str, Price] = {
    "glm-5.3": Price(input=8.0, cached=2.0, output=28.0),
    "deepseek-v4-pro": Price(input=9.0, cached=0.3, output=27.0),
}


def is_deepseek_offpeak(at: datetime) -> bool:
    """DeepSeek 忙时 = 北京时间工作日 9-12 点、14-18 点;其余时间(含周末全天)半价。"""
    local = at.astimezone(_BEIJING)
    if local.weekday() >= 5:
        return True
    return not (9 <= local.hour < 12 or 14 <= local.hour < 18)


def call_cost(usage: Mapping[str, Any], price: Price, *, model: str, at: datetime) -> float:
    """一次调用的花费(元);DeepSeek 的模型在闲时按半价。"""
    prompt = int(usage.get("prompt_tokens") or 0)
    cached = int(
        (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        or usage.get("prompt_cache_hit_tokens")
        or 0
    )
    out = int(usage.get("completion_tokens") or 0)
    cny = ((prompt - cached) * price.input + cached * price.cached + out * price.output) / 1e6
    return cny / 2 if model.startswith("deepseek") and is_deepseek_offpeak(at) else cny


def parse_price(raw: str) -> Price:
    try:
        values = [float(v) for v in raw.split(",")]
    except ValueError:
        values = []
    if len(values) != 3:
        raise SystemExit(f"price must be 'input,cached,output' CNY per million tokens: {raw!r}")
    return Price(*values)


def resolve_price(model: str, raw: str | None) -> Price:
    if raw is not None:
        return parse_price(raw)
    if model not in PRICES:
        raise SystemExit(f"no price for model {model!r}: pass --answer-price / --judge-price")
    return PRICES[model]


@dataclass
class Spend:
    answer_cny: float = 0.0
    judge_cny: float = 0.0
    answer_calls: int = 0
    judge_calls: int = 0


# ---------------------------------------------------------------------------
# 预算
# ---------------------------------------------------------------------------


@dataclass
class BudgetGate:
    """``--budget``:花到上限就不再开新题;暂停时库与已落盘的结果都留着,等操作者往
    ``control`` 写新上限(元)继续,或写 ``stop`` 收尾。"""

    budget: float
    control: Path
    poll_s: float = BUDGET_POLL_S

    async def wait_for_raise(self, spent: Callable[[], float]) -> bool:
        """暂停到上限被调高(True)或收到 ``stop``(False)。"""
        print(
            f"PAUSED: spent ¥{spent():.2f} >= budget ¥{self.budget:.2f}. Write a new budget "
            f"(CNY) into {self.control} to continue, or 'stop' to finish; finished questions "
            "are kept and a rerun with the same --out resumes.",
            file=sys.stderr,
        )
        while True:
            raw = self._take()
            if raw == "stop":
                return False
            if raw is not None:
                try:
                    value = float(raw)
                except ValueError:
                    value = None
                if value is not None and value > spent():
                    self.budget = value
                    print(f"RESUMED: budget ¥{value:.2f}", file=sys.stderr)
                    return True
                print(
                    f"ignored {raw!r} in {self.control}: need a number above ¥{spent():.2f} "
                    "or 'stop'",
                    file=sys.stderr,
                )
            await asyncio.sleep(self.poll_s)

    def _take(self) -> str | None:
        try:
            raw = self.control.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        if not raw:  # 操作者的 ``echo 30 > …`` 还没写完,下一轮再读
            return None
        self.control.unlink(missing_ok=True)
        return raw
