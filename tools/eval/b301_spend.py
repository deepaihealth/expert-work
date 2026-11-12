"""B-301 PR 2 体检的花费:止损线、逐次调用的花费账本、按计划的独占锁。

账本与锁**按计划**放在一个固定的地方,和 ``--results`` 写到哪无关:
``<状态目录>/<plan_id>.spend.jsonl`` 与 ``<plan_id>.lock``,状态目录默认
``~/.cache/b301-health``(只有显式的 ``--state-dir`` 能改,测试用)。换一个结果路径续跑,止损线
照样从这个计划已经花掉的钱起算;两个终端用不同的结果路径跑同一个计划,第二个拿不到锁。

每次调用两行:

1. 发出**之前**写一行 ``reason="reserve"``、``cost_yuan`` = 单次上限,落盘(fsync)后才发;
2. 调用结束写一行结算:``ok``(按回答里的用量)/ ``cap_timeout`` / ``cap_error`` /
   ``cap_no_usage``(后三种按单次上限)。

账本合计 = 每次调用有结算行的按结算、没有结算行的(进程崩了、Ctrl-C)按上限;读不出来的行
(写到一半被打断)每行按单次上限算,并提示 —— **账本不能删**,删了就等于把已经花的钱清零。
账本写不进去(磁盘满、权限……)= 止损:这次调用不发(预留没写成就不算),这一条作废、续跑时重做。
结果文件只放跑完的条目(给「已完成」和报告用)。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import b301_plan as health

SETTLE_REASONS = ("ok", "cap_timeout", "cap_error", "cap_no_usage")
DEFAULT_STATE_DIR = Path.home() / ".cache" / "b301-health"


class SpendStopError(BaseException):
    """止损:不再发任何调用。故意不是 ``Exception`` —— 工具里的兜底接不住,整条作废。"""


class LockedError(RuntimeError):
    """另一个进程正在跑同一个计划。"""


def ledger_path(state_dir: Path, plan_id: str) -> Path:
    return state_dir / f"{plan_id}.spend.jsonl"


def lock_path(state_dir: Path, plan_id: str) -> Path:
    return state_dir / f"{plan_id}.lock"


@contextlib.contextmanager
def exclusive(state_dir: Path, plan_id: str) -> Iterator[None]:
    """整个 run 期间独占这个计划(``flock``,不等);拿不到就 :class:`LockedError`。"""
    path = lock_path(state_dir, plan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            msg = f"plan {plan_id} is being used by another run ({path} is locked)"
            raise LockedError(msg) from exc
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def read_ledger(path: Path, *, broken_line_cost: float) -> list[dict[str, Any]]:
    """读账本。读不出来的行(写到一半被打断)每行当作一次按 ``broken_line_cost``(单次上限)
    预留、没结算的调用,并在 stderr 提示。"""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or "call_id" not in row or "cost_yuan" not in row:
                raise ValueError(line)
        except ValueError:
            sys.stderr.write(
                f"notice: line {number} of {path} cannot be read (an interrupted write); it is "
                f"counted as one call at the per-call cap ¥{broken_line_cost:.4f}. Keep the "
                "ledger: it is the record of what this plan has already spent.\n"
            )
            row = {
                "call_id": f"unreadable-line-{number}",
                "model": "unknown",
                "cost_yuan": broken_line_cost,
                "reason": "reserve",
                "unreadable": True,
            }
        rows.append(row)
    return rows


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
    """往账本追加一行并落盘;``path`` 为 ``None`` 时只记在内存里(测试)。上一行没写完(没有换行)
    就先补一个换行,新的一行不会接在半行后面。"""

    path: Path | None
    plan_id: str

    def append(self, row: Mapping[str, Any]) -> None:
        if self.path is None:
            return
        line = json.dumps({"plan_id": self.plan_id, **row}, ensure_ascii=False) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+b") as fh:
            if fh.tell() > 0:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    line = "\n" + line
            fh.write(line.encode("utf-8"))
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
        """过止损线,再按单次上限预留并落账,返回调用编号。之后才能发。落账失败 = 这次不发、
        不算预留,并止损(:meth:`_write`)。"""
        self.check(model)
        call_id = uuid.uuid4().hex
        cap = health.per_call_cap_yuan(self.prices[model])
        self._write(call_id, model, item, phase, cap, "reserve")
        self._open[call_id] = cap
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
        """落账;写不进去就止损并抛 :class:`SpendStopError` —— 它不是 ``Exception``,工具的兜底
        接不住,这一条作废、续跑时重做(结算行没写成的,续跑时按那条预留 = 单次上限算)。"""
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
