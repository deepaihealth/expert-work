"""B-300 评测的报价核对与花钱前的批准(从 ``b300_eval`` 拆出,``b300_steps`` 也用)。

* :func:`check_quote`:按同样的参数重算报价、核对编号(不花钱的子命令也要:单元取决于报价)。
* :func:`approved`:花钱之前 —— 编号、``stop_and_ask`` 要有用户原话、¥400 总账、分预算、账本里
  记着的停止;**必须在** :func:`spending` **里调**(审查 H3:同一状态目录同一时间只有一个花钱的
  进程,别的报价已花多少在锁里读出来就不会再变)。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import b300_plan as plan  # noqa: E402
import b300_scenarios as sc  # noqa: E402
import b300_spend as spend  # noqa: E402
from b300_phases import EvalError, budget_of, record_approval  # noqa: E402


def vendors_of(raw: Sequence[str] | None) -> list[plan.Vendor]:
    return [plan.parse_vendor(v) for v in raw] if raw else list(plan.DEFAULT_VENDORS)


def token_table(path: Path | None) -> Mapping[str, Any]:
    import json

    if path is None:
        return plan.CALL_TOKENS
    return plan.load_token_overrides(json.loads(path.read_text(encoding="utf-8")))


def make_quote(args: argparse.Namespace, *, exclude: str | None = None) -> dict[str, Any]:
    vendors = vendors_of(args.vendor)
    prices = plan.prices_for(vendors, accept_documented=args.accept_documented_prices)
    already = sum(spend.history_total(args.state_dir, exclude=exclude).values())
    return plan.quote(
        vendors,
        prices,
        already_spent=already,
        single_vendor=args.single_vendor,
        stop_line=args.stop_line,
        scenario_digest=sc.scenario_digest([*sc.build_all(), *sc.build_compare()]),
        include_compare=not args.no_compare,
        tokens=token_table(args.tokens),
        a_votes=args.a_judge_votes,
        pilot=args.pilot,
        embed_price=args.embed_price,
    )


def check_quote(args: argparse.Namespace) -> dict[str, Any]:
    """重算报价(不计这个报价自己的账)并核对编号。"""
    q = make_quote(args, exclude=args.approved_quote)
    if q["quote_id"] != args.approved_quote:
        raise EvalError(
            f"--approved-quote {args.approved_quote} does not match this plan's quote "
            f"{q['quote_id']}; run `quote` with the same arguments and get that one approved"
        )
    return q


def approved(
    args: argparse.Namespace, *, cmd: str, allow_blocked: bool = False
) -> tuple[dict[str, Any], spend.Guard]:
    """花钱之前的全部核对;从这个报价的账本接着记。用户原话记进 ``approvals.jsonl``。
    ``allow_blocked``:只结算、不新花的步骤(收整理 / 写回的实际用量)在停着时也能做。"""
    q = check_quote(args)
    if q["status"] != "within_cap" and not args.user_said:
        raise EvalError(
            "the quote says stop_and_ask (design §8.6 step 4): ask the user first; if they say go "
            "ahead, pass their words with --user-said (kept in approvals.jsonl)"
        )
    if q["already_spent_yuan"] + args.stop_line > plan.CAP_YUAN:
        raise EvalError(
            f"other quotes already spent ¥{q['already_spent_yuan']}; stop line ¥{args.stop_line} "
            f"would cross the ¥{plan.CAP_YUAN:g} cap"
        )
    prices = plan.prices_for(
        vendors_of(args.vendor), accept_documented=args.accept_documented_prices
    )
    caps = {name: float(b["budget_yuan"]) for name, b in q["phase_budgets"].items()}
    pilot = bool(q["pilot"])
    guard = spend.Guard.resume(
        stop_line=args.stop_line,
        prices=prices,
        state_dir=args.state_dir,
        quote_id=q["quote_id"],
        unblock=args.unblock,
        phase_caps=caps,
        budget_of=lambda phase: budget_of(phase, pilot=pilot),
    )
    if guard.blocked is not None and not allow_blocked:
        raise EvalError(
            f"this quote is stopped: {guard.blocked}. Check, then re-run with --unblock "
            '"<why it is safe to go on>" (recorded in the ledger)'
        )
    record_approval(args.out, cmd=cmd, quote_id=q["quote_id"], user_said=args.user_said)
    return q, guard


@contextmanager
def spending(args: argparse.Namespace) -> Iterator[None]:
    """一个状态目录同一时间只有一个花钱的进程;拿不到锁就 ``LockedError``(不等)。"""
    with spend.state_lock(args.state_dir):
        yield
