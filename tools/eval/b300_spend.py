"""B-300 评测的花费:按报价的账本、先预留后发送、止损线、跨报价的 ¥400 总账。

账本格式与落盘规矩沿用 ``b301_spend``(``SpendLedger`` / ``read_ledger`` / ``ledger_total`` /
``exclusive``):每个报价一份 ``<状态目录>/<quote_id>.spend.jsonl``,状态目录默认
``~/.cache/b300-eval``(与结果写到哪无关;换一个结果目录续跑,止损照样从这个报价已花的钱起算)。

* 每次调用 / 每次运行发出**之前**写一行 ``reserve``(``cost_yuan`` = 这一下的上限),落盘后才发;
  结束后写一行结算(``ok`` 按用量,``cap_*`` 按上限)。没有结算行的(进程崩了)按预留算。
* **预留前判线**:「已结算 + 在飞的预留 + 这一笔的上限」超过止损线就不发、止损。所以只要每一笔的
  结算不超过它的上限,花费就到不了止损线以上。
* **¥400 是跨报价的总账**:花钱的进程先拿状态目录的独占锁(:func:`state_lock`),在锁里读出
  **别的**报价已花的钱,每次预留都按「别的 + 本报价 + 这一笔」判 ¥400;同一时间只有一个进程在花。
* 停下(过线、计不了账、账本写不进)写一行 ``blocked`` 落盘;续跑要显式 ``unblock``,原因也落盘。
"""

from __future__ import annotations

import contextlib
import os
import sys
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
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
    "TEST_STATE_DIR_ENV",
    "Guard",
    "LockedError",
    "SpendLedger",
    "SpendStopError",
    "StateDirError",
    "call_cap_yuan",
    "check_state_dir",
    "exclusive",
    "history_total",
    "ledger_path",
    "state_lock",
    "usage_cost",
]

DEFAULT_STATE_DIR = Path.home() / ".cache" / "b300-eval"
#: 读不出来的账本行(写到一半被打断)每行按这个数算(元):比任何一笔预留都大(一次运行的上限见
#: ``b300_live.RUN_TOKEN_BUDGET``,最贵的型号约 ¥7)。
BROKEN_LINE_YUAN = 10.0


def ledger_path(state_dir: Path, quote_id: str) -> Path:
    return state_dir / f"{quote_id}.spend.jsonl"


#: 只有测试打开这个开关时,才许用默认以外的状态目录(``tools/eval/conftest.py`` 打开)。
TEST_STATE_DIR_ENV = "B300_EVAL_ALLOW_TEST_STATE_DIR"


class StateDirError(RuntimeError):
    """花钱的状态目录只有一个:默认的那个(审查第四轮 M1)。"""


def check_state_dir(state_dir: Path) -> Path:
    """只有一个状态目录(``~/.cache/b300-eval``):总账、锁、每个报价的账本都在这里,换目录绕不开
    ¥400。别的目录只给测试用(要设 :data:`TEST_STATE_DIR_ENV`)。"""
    if Path(state_dir).resolve() != Path(DEFAULT_STATE_DIR).resolve() and not os.environ.get(
        TEST_STATE_DIR_ENV
    ):
        raise StateDirError(
            f"spending always uses the default state dir {DEFAULT_STATE_DIR}; {state_dir} is only "
            f"accepted in tests ({TEST_STATE_DIR_ENV}=1)"
        )
    return Path(state_dir)


def history_total(state_dir: Path, *, exclude: str | None = None) -> dict[str, float]:
    """状态目录里每个报价已花的钱(``exclude`` 那个报价除外)。"""
    out: dict[str, float] = {}
    if not state_dir.exists():
        return out
    for path in sorted(state_dir.glob("*.spend.jsonl")):
        quote_id = path.name.removesuffix(".spend.jsonl")
        if quote_id != exclude:
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


#: 跨全部报价的总上限(用户拍板 ¥400)。与 ``b300_plan.CAP_YUAN`` 同一个数;这里不 import plan,
#: 免得花费模块依赖报价模块。
GLOBAL_CAP_YUAN = 400.0
_LOCK_NAME = "b300-state"


@contextlib.contextmanager
def state_lock(state_dir: Path) -> Iterator[None]:
    """一个状态目录同一时间只许一个花钱的进程(``run`` / ``judge`` / ``calibrate`` / 整理的
    ``bundle`` 与 ``ingest``)。拿不到就 :class:`LockedError`,不等。审查 H3:¥400 总账只有在
    「别的报价已花」读出来之后不再变的前提下才守得住,所以读账、判线、花钱都在这把锁里。"""
    with exclusive(check_state_dir(state_dir), _LOCK_NAME):
        yield


def _replay(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, float], dict[str, tuple[float, str]], str | None]:
    """账本 → (已结算的每笔, 只有预留的每笔 (上限, 段), 当前是否停着)。"""
    settled: dict[str, float] = {}
    reserved: dict[str, tuple[float, str]] = {}
    blocked: str | None = None
    for row in rows:
        reason, call_id = row["reason"], str(row["call_id"])
        if reason == "blocked":
            blocked = str(row.get("detail") or "blocked")
        elif reason == "unblock":
            blocked = None
        elif reason == "reserve":
            if call_id not in settled:
                reserved[call_id] = (float(row["cost_yuan"]), str(row.get("phase") or ""))
        else:
            reserved.pop(call_id, None)
            settled[call_id] = float(row["cost_yuan"])
    return settled, reserved, blocked


@dataclass
class Guard:
    """一个报价的止损与记账。``spent`` = 已结算 + 在飞的预留(含上次进程留下没结算的预留)。

    每次预留前判四条线:止损线、¥400 总账(``others`` = 别的报价已花,锁里读出)、所属段的分预算
    (``phase_caps``,段名由 ``budget_of`` 从记账用的 phase 映射)、单价存在。一旦停下,写一行
    ``blocked`` 落盘;续跑要显式 ``unblock``(原因也落盘)。"""

    stop_line: float
    prices: Mapping[str, Price]
    ledger: SpendLedger | None = None
    settled: float = 0.0
    blocked: str | None = None
    others: float = 0.0
    global_cap: float = GLOBAL_CAP_YUAN
    phase_caps: Mapping[str, float] = field(default_factory=dict)
    budget_of: Callable[[str], str] | None = None
    #: 超了分预算的段(审查 M-b):只拒这一段,**不**写进账本、不停整份报价。
    phase_refused: dict[str, str] = field(default_factory=dict)
    _open: dict[str, tuple[float, str]] = field(default_factory=dict, repr=False)
    _phase_settled: dict[str, float] = field(default_factory=dict, repr=False)

    @classmethod
    def resume(
        cls,
        *,
        stop_line: float,
        prices: Mapping[str, Price],
        state_dir: Path | None,
        quote_id: str,
        unblock: str | None = None,
        phase_caps: Mapping[str, float] | None = None,
        budget_of: Callable[[str], str] | None = None,
    ) -> Guard:
        """从这个报价的账本接着记。``state_dir=None`` 只记内存(测试)。调用方应已持有
        :func:`state_lock`。"""
        if state_dir is None:
            return cls(
                stop_line=stop_line, prices=prices, phase_caps=phase_caps or {}, budget_of=budget_of
            )
        path = ledger_path(check_state_dir(state_dir), quote_id)
        rows = read_ledger(path, broken_line_cost=BROKEN_LINE_YUAN)
        settled, reserved, blocked = _replay(rows)
        guard = cls(
            stop_line=stop_line,
            prices=prices,
            ledger=SpendLedger(path, quote_id),
            settled=sum(settled.values()),
            blocked=blocked,
            others=sum(history_total(state_dir, exclude=quote_id).values()),
            phase_caps=phase_caps or {},
            budget_of=budget_of,
        )
        guard._open = dict(reserved)
        phase_of = {str(r["call_id"]): str(r.get("phase") or "") for r in rows}
        for call_id, cost in settled.items():
            budget = guard._budget(phase_of.get(call_id, ""))
            guard._phase_settled[budget] = guard._phase_settled.get(budget, 0.0) + cost
        if guard.blocked is not None and unblock:
            guard._mark("unblock", f"{unblock} (was: {guard.blocked})")
            guard.blocked = None
        return guard

    @property
    def spent(self) -> float:
        return self.settled + sum(cost for cost, _phase in self._open.values())

    def _budget(self, phase: str) -> str:
        return self.budget_of(phase) if self.budget_of is not None else ""

    def phase_spent(self, budget: str) -> float:
        open_part = sum(c for c, p in self._open.values() if self._budget(p) == budget)
        return self._phase_settled.get(budget, 0.0) + open_part

    def may_spend(self, phase: str) -> bool:
        return self.blocked is None and self._budget(phase) not in self.phase_refused

    def block(self, reason: str) -> None:
        if self.blocked is None:
            self.blocked = reason
            self._mark("blocked", reason)

    def _mark(self, reason: str, detail: str) -> None:
        if self.ledger is None:
            return
        row = {
            "call_id": f"{reason}-{uuid.uuid4().hex}",
            "cost_yuan": 0.0,
            "reason": reason,
            "detail": detail,
        }
        try:
            self.ledger.append(row)
        except OSError:
            pass  # 已经停了;写不进去也只是少一行说明

    def reserve(self, *, item: str, model: str, cap: float, phase: str) -> str:
        """判线 → 按上限预留并落账 → 返回编号;之后才能发。

        超任一条线 / 已停 / 没单价 → :class:`SpendStopError`。"""
        budget = self._budget(phase)
        if self.blocked is None and model not in self.prices:
            self.block(f"no validated price for {model}")
        if self.blocked is None and self.spent + cap > self.stop_line:
            self.block(
                f"stop line ¥{self.stop_line:.2f} would be crossed "
                f"(spent ¥{self.spent:.4f} + this one up to ¥{cap:.4f})"
            )
        if self.blocked is None and self.others + self.spent + cap > self.global_cap:
            self.block(
                f"the ¥{self.global_cap:g} cap across quotes would be crossed (other quotes "
                f"¥{self.others:.2f} + this quote ¥{self.spent:.4f} + up to ¥{cap:.4f})"
            )
        if self.blocked is not None:
            raise SpendStopError(self.blocked)
        if budget in self.phase_refused:
            raise SpendStopError(self.phase_refused[budget])
        if budget in self.phase_caps and self.phase_spent(budget) + cap > self.phase_caps[budget]:
            self.phase_refused[budget] = (
                f"the {budget} sub-budget ¥{self.phase_caps[budget]:.2f} would be crossed "
                f"(spent ¥{self.phase_spent(budget):.4f} + up to ¥{cap:.4f}); only this phase stops"
            )
            raise SpendStopError(self.phase_refused[budget])
        call_id = uuid.uuid4().hex
        self._write(call_id, item, model, phase, cap, "reserve")
        self._open[call_id] = (cap, phase)
        return call_id

    def settle(
        self, call_id: str, *, item: str, model: str, phase: str, cost: float, reason: str = "ok"
    ) -> float:
        opened = self._open.pop(call_id, None)
        self.settled += cost
        budget = self._budget(phase)
        self._phase_settled[budget] = self._phase_settled.get(budget, 0.0) + cost
        self._write(call_id, item, model, phase, cost, reason)
        if opened is not None and cost > opened[0] + 1e-9:
            self.block(
                f"{item}: cost ¥{cost:.4f} exceeded its cap ¥{opened[0]:.4f}; check the caps"
            )
        return cost

    def open_cap(self, call_id: str) -> float:
        opened = self._open.get(call_id)
        return opened[0] if opened is not None else 0.0

    def settle_cap(self, call_id: str, *, item: str, model: str, phase: str, reason: str) -> float:
        """说不清花了多少:按预留的上限结算(并不自动止损;要止损的调用方另调 :meth:`block`)。"""
        cap = self.open_cap(call_id)
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
            cost = self.settle_cap(
                call_id, item=item, model=model, phase=phase, reason="cap_no_usage"
            )
            self.block(f"{item}: no usage reported; charged at the cap")
            return cost
        try:
            cost = usage_cost(usage_by_model, self.prices)
        except KeyError as exc:
            charged = self.settle_cap(
                call_id, item=item, model=model, phase=phase, reason="cap_no_price"
            )
            self.block(f"{item}: no price for model {exc}; charged at the cap")
            return charged
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
            self.blocked = (
                self.blocked or f"the spend ledger could not be written ({type(exc).__name__})"
            )
            raise SpendStopError(self.blocked) from exc
