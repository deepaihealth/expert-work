"""B-301 PR 2 体检的花费:止损线、逐次调用的花费账本、结果文件的独占锁。

账本 ``<结果>.spend.jsonl`` 与结果文件并排,每次调用两行:

1. 发出**之前**写一行 ``reason="reserve"``、``cost_yuan`` = 单次上限,落盘(fsync)后才发;
2. 调用结束写一行结算:``ok``(按回答里的用量)/ ``cap_timeout`` / ``cap_error`` /
   ``cap_no_usage``(后三种按单次上限)。

账本合计 = 每次调用有结算行的按结算、没有结算行的(进程崩了、Ctrl-C)按上限。续跑时止损线从
账本合计起算 —— 作废的条目、出错的那次调用、止损后别的路跑完的条目、崩溃时在飞的调用,钱都在账上。
结果文件只放跑完的条目(给「已完成」和报告用)。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import b301_health as health

SETTLE_REASONS = ("ok", "cap_timeout", "cap_error", "cap_no_usage")


class SpendStopError(BaseException):
    """止损:不再发任何调用。故意不是 ``Exception`` —— 工具里的兜底接不住,整条作废。"""


class LockedError(RuntimeError):
    """另一个进程正拿着同一个结果文件在跑。"""


def ledger_path(results: Path) -> Path:
    return results.with_suffix(".spend.jsonl")


def lock_path(results: Path) -> Path:
    return results.with_suffix(".lock")


@contextlib.contextmanager
def exclusive(results: Path) -> Iterator[None]:
    """整个 run 期间独占这个结果文件(``flock``,不等);拿不到就 :class:`LockedError`。"""
    path = lock_path(results)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            msg = f"{results} is being used by another run ({path} is locked)"
            raise LockedError(msg) from exc
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def ledger_total(rows: Sequence[Mapping[str, Any]]) -> float:
    """账本合计:有结算行的按结算,只有预留行的按预留(= 单次上限)。"""
    by_call: dict[str, float] = {}
    settled: set[str] = set()
    for row in rows:
        call_id, cost = str(row["call_id"]), float(row["cost_yuan"])
        if row["reason"] == "reserve":
            if call_id not in settled:
                by_call[call_id] = cost
        else:
            by_call[call_id] = cost
            settled.add(call_id)
    return sum(by_call.values())


def error_stops(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """每个模型「厂商返回超时以外的错误 → 停」的次数。"""
    counts: dict[str, int] = {}
    for row in rows:
        if row["reason"] == "cap_error":
            counts[str(row["model"])] = counts.get(str(row["model"]), 0) + 1
    return counts


@dataclass
class SpendLedger:
    """往账本追加一行并落盘;``path`` 为 ``None`` 时只记在内存里(测试)。"""

    path: Path | None
    plan_id: str

    def append(self, row: Mapping[str, Any]) -> None:
        if self.path is None:
            return
        line = json.dumps({"plan_id": self.plan_id, **row}, ensure_ascii=False) + "\n"
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())


@dataclass
class SpendGuard:
    """止损线 + 计费。``spent`` = 已结算 + 在飞调用按上限预留的部分。"""

    stop_line: float
    prices: Mapping[str, health.Price]
    ledger: SpendLedger | None = None
    settled: float = 0.0
    blocked: str | None = None
    _open: dict[str, float] = field(default_factory=dict, repr=False)

    @property
    def spent(self) -> float:
        return self.settled + sum(self._open.values())

    def block(self, reason: str) -> None:
        if self.blocked is None:
            self.blocked = reason

    def check(self, model: str) -> None:
        """每次调用之前:已停 / 没单价 / 到线 → 停。"""
        if self.blocked is None and model not in self.prices:
            self.block(f"no validated price for {model}")
        if self.blocked is None and self.spent >= self.stop_line:
            self.block(f"stop line ¥{self.stop_line} reached (spent ¥{self.spent:.4f})")
        if self.blocked is not None:
            raise SpendStopError(self.blocked)

    def reserve(self, model: str, *, item: str, phase: str) -> str:
        """过止损线,再按单次上限预留并落账,返回调用编号。之后才能发。"""
        self.check(model)
        call_id = uuid.uuid4().hex
        cap = health.per_call_cap_yuan(self.prices[model])
        self._open[call_id] = cap
        self._write(call_id, model, item, phase, cap, "reserve")
        return call_id

    def settle(
        self, call_id: str, model: str, cost: float, reason: str, *, item: str, phase: str
    ) -> float:
        self._open.pop(call_id, None)
        self.settled += cost
        self._write(call_id, model, item, phase, cost, reason)
        return cost

    def settle_cap(self, call_id: str, model: str, reason: str, *, item: str, phase: str) -> float:
        cap = health.per_call_cap_yuan(self.prices[model])
        return self.settle(call_id, model, cap, reason, item=item, phase=phase)

    def settle_usage(
        self,
        call_id: str,
        model: str,
        usage: Mapping[str, Any] | None,
        *,
        item: str,
        phase: str,
    ) -> tuple[float, int, int]:
        """按回答里的用量结算。用量不可信(缺、不是整数、输入 ≤ 0、给了合计却对不上输入 + 输出)
        → 按单次上限结算并停;输入超上限 → 照实结算并停。"""
        tokens_in = usage.get("input_tokens") if usage else None
        tokens_out = usage.get("output_tokens") if usage else None
        total = usage.get("total_tokens") if usage else None
        usable = (
            isinstance(tokens_in, int)
            and isinstance(tokens_out, int)
            and tokens_in > 0
            and tokens_out >= 0
            and (total is None or total == tokens_in + tokens_out)
        )
        if not usable or not isinstance(tokens_in, int) or not isinstance(tokens_out, int):
            cost = self.settle_cap(call_id, model, "cap_no_usage", item=item, phase=phase)
            self.block(f"{model}: a response carried no usable token usage ({usage})")
            return cost, -1, -1
        price = self.prices[model]
        cost = (tokens_in * price.input + tokens_out * price.output) / 1e6
        self.settle(call_id, model, cost, "ok", item=item, phase=phase)
        if tokens_in > health.MAX_INPUT_TOKENS_PER_CALL:
            limit = health.MAX_INPUT_TOKENS_PER_CALL
            self.block(f"{model}: one call used {tokens_in} input tokens > {limit}")
        return cost, tokens_in, tokens_out

    def _write(
        self, call_id: str, model: str, item: str, phase: str, cost: float, reason: str
    ) -> None:
        if self.ledger is not None:
            row = {
                "call_id": call_id,
                "item": item,
                "model": model,
                "phase": phase,
                "cost_yuan": round(cost, 8),
                "reason": reason,
            }
            self.ledger.append(row)
