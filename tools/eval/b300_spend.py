"""B-300 评测的花费:按报价的账本、先预留后发送、止损线、跨报价的 ¥400 总账。

账本格式与落盘规矩沿用 ``b301_spend``(``SpendLedger`` / ``read_ledger`` / ``ledger_total`` /
``exclusive``):每个报价一份 ``<状态目录>/<quote_id>.spend.jsonl``,状态目录默认
``~/.cache/b300-eval``(与结果写到哪无关;换一个结果目录续跑,止损照样从这个报价已花的钱起算)。

* 每次调用 / 每次运行发出**之前**写一行 ``reserve``(``cost_yuan`` = 这一下的上限),落盘后才发;
  结束后写一行结算(``ok`` 按用量,``cap_*`` 按上限)。没有结算行的(进程崩了)按预留算。
* **预留前判线**:「已结算 + 在飞的预留 + 这一笔的上限」超过止损线就不发、止损。所以只要每一笔的
  结算不超过它的上限,花费就到不了止损线以上。
* **¥400 是跨报价的总账**:``run`` 开始前把状态目录里**别的**报价已花的钱加起来,止损线 + 它们
  超过 :data:`b300_plan.CAP_YUAN` 就拒跑(先按 §8.6 的顺序删减、重新报价)。
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from b301_plan import Price  # noqa: E402
from b301_spend import (  # noqa: E402
    LockedError,
    SpendLedger,
    SpendStopError,
    exclusive,
    ledger_total,
    read_ledger,
)

__all__ = [
    "DEFAULT_STATE_DIR",
    "Guard",
    "LockedError",
    "SpendLedger",
    "SpendStopError",
    "call_cap_yuan",
    "exclusive",
    "history_total",
    "ledger_path",
    "usage_cost",
]

DEFAULT_STATE_DIR = Path.home() / ".cache" / "b300-eval"
#: 读不出来的账本行(写到一半被打断)每行按这个数算(元):比任何一笔预留都大(一次运行的上限见
#: ``b300_live.RUN_TOKEN_BUDGET``,最贵的型号约 ¥7)。
BROKEN_LINE_YUAN = 10.0


def ledger_path(state_dir: Path, quote_id: str) -> Path:
    return state_dir / f"{quote_id}.spend.jsonl"


def history_total(state_dir: Path, *, exclude: str | None = None) -> dict[str, float]:
    """状态目录里每个报价已花的钱(``exclude`` 那个报价除外)。"""
    out: dict[str, float] = {}
    if not state_dir.exists():
        return out
    for path in sorted(state_dir.glob("*.spend.jsonl")):
        quote_id = path.name.removesuffix(".spend.jsonl")
        if quote_id == exclude:
            continue
        out[quote_id] = ledger_total(read_ledger(path, broken_line_cost=BROKEN_LINE_YUAN))
    return out


def call_cap_yuan(price: Price, max_input: int, max_output: int) -> float:
    """一次调用的上限:输入、输出都按上限算,输入不打缓存折扣。"""
    return (max_input * price.input + max_output * price.output) / 1e6


def usage_cost(usage_by_model: Iterable[Mapping[str, Any]], prices: Mapping[str, Price]) -> float:
    """对外形状的用量(``end`` 帧 ``usage_by_model``,缓存读在 ``cache_read_tokens``)→ 元。
    没有单价的型号抛 ``KeyError``(调用方按上限计并止损)。"""
    cost = 0.0
    for bucket in usage_by_model:
        model = str(bucket.get("model"))
        price = prices[model]
        cached = int(bucket.get("cache_read_tokens") or 0)
        total_in = int(bucket.get("input_tokens") or 0)
        output = int(bucket.get("output_tokens") or 0)
        cost += (
            (total_in - cached) * price.input + cached * price.cache_read + output * price.output
        ) / 1e6
    return cost


@dataclass
class Guard:
    """一个报价的止损与记账。``spent`` = 已结算 + 在飞的预留。"""

    stop_line: float
    prices: Mapping[str, Price]
    ledger: SpendLedger | None = None
    settled: float = 0.0
    blocked: str | None = None
    _open: dict[str, float] = field(default_factory=dict, repr=False)

    @classmethod
    def resume(
        cls, *, stop_line: float, prices: Mapping[str, Price], state_dir: Path | None, quote_id: str
    ) -> Guard:
        """从这个报价的账本接着记(没有账本 = 从 0 起)。``state_dir=None`` 只记内存(测试)。"""
        if state_dir is None:
            return cls(stop_line=stop_line, prices=prices)
        path = ledger_path(state_dir, quote_id)
        already = ledger_total(read_ledger(path, broken_line_cost=BROKEN_LINE_YUAN))
        return cls(
            stop_line=stop_line,
            prices=prices,
            ledger=SpendLedger(path, quote_id),
            settled=already,
        )

    @property
    def spent(self) -> float:
        return self.settled + sum(self._open.values())

    def block(self, reason: str) -> None:
        if self.blocked is None:
            self.blocked = reason

    def reserve(self, *, item: str, model: str, cap: float, phase: str) -> str:
        """判线 → 按上限预留并落账 → 返回编号;之后才能发。

        超线 / 已停 / 没单价 → :class:`SpendStopError`。"""
        if self.blocked is None and model not in self.prices:
            self.block(f"no validated price for {model}")
        if self.blocked is None and self.spent + cap > self.stop_line:
            self.block(
                f"stop line ¥{self.stop_line:.2f} would be crossed "
                f"(spent ¥{self.spent:.4f} + this one up to ¥{cap:.4f})"
            )
        if self.blocked is not None:
            raise SpendStopError(self.blocked)
        call_id = uuid.uuid4().hex
        self._write(call_id, item, model, phase, cap, "reserve")
        self._open[call_id] = cap
        return call_id

    def settle(
        self, call_id: str, *, item: str, model: str, phase: str, cost: float, reason: str = "ok"
    ) -> float:
        cap = self._open.pop(call_id, None)
        self.settled += cost
        self._write(call_id, item, model, phase, cost, reason)
        if cap is not None and cost > cap + 1e-9:
            self.block(f"{item}: cost ¥{cost:.4f} exceeded its cap ¥{cap:.4f}; check the caps")
        return cost

    def settle_cap(self, call_id: str, *, item: str, model: str, phase: str, reason: str) -> float:
        """说不清花了多少:按预留的上限结算(并不自动止损;要止损的调用方另调 :meth:`block`)。"""
        cap = self._open.get(call_id, 0.0)
        return self.settle(call_id, item=item, model=model, phase=phase, cost=cap, reason=reason)

    def settle_usage(
        self,
        call_id: str,
        *,
        item: str,
        model: str,
        phase: str,
        usage_by_model: Iterable[Mapping[str, Any]] | None,
    ) -> float:
        """按用量结算;没有用量 / 有没单价的型号 → 按上限结算并止损。"""
        if usage_by_model is None:
            self.block(f"{item}: no usage reported; charged at the cap")
            return self.settle_cap(
                call_id, item=item, model=model, phase=phase, reason="cap_no_usage"
            )
        try:
            cost = usage_cost(usage_by_model, self.prices)
        except KeyError as exc:
            self.block(f"{item}: no price for model {exc}; charged at the cap")
            return self.settle_cap(
                call_id, item=item, model=model, phase=phase, reason="cap_no_price"
            )
        return self.settle(call_id, item=item, model=model, phase=phase, cost=cost)

    def _write(
        self, call_id: str, item: str, model: str, phase: str, cost: float, reason: str
    ) -> None:
        if self.ledger is None:
            return
        row = {
            "call_id": call_id,
            "item": item,
            "model": model,
            "phase": phase,
            "cost_yuan": round(cost, 8),
            "reason": reason,
        }
        try:
            self.ledger.append(row)
        except OSError as exc:
            self.block(f"the spend ledger could not be written ({type(exc).__name__})")
            raise SpendStopError(self.blocked) from exc
