"""B-301 PR 2 —— 体检的花费:止损线先于发送、厂商出错 / 用量不可信 / 输入超上限都停、超时按上限、
账本(预留先落盘、结算、没结算的按上限)、结果文件独占锁、凭据只报变量名。全部离线。"""

from __future__ import annotations

import fcntl
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage

sys.path.insert(0, str(Path(__file__).resolve().parent))

import b301_health as cli
import b301_health_run as runner
import b301_plan as health
import b301_spend as spend

from orchestrator.llm.providers.openai import _extract_usage_metadata
from orchestrator.tools.image_orientation import ORIENTATION_QUESTION
from orchestrator.tools.vision import _messages

MODEL = health.DEFAULT_MODELS[1]
PRICES = {MODEL: health.Price(6.0, 30.0, 1.2, "test")}
CAP = health.per_call_cap_yuan(PRICES[MODEL])


def _usage(tokens_in: int = 1000, tokens_out: int = 10) -> dict[str, int]:
    return {
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "total_tokens": tokens_in + tokens_out,
    }


class _Inner:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls = 0

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome  # type: ignore[no-any-return]


def _msgs() -> list[BaseMessage]:
    return _messages("expert_work://image/x", ORIENTATION_QUESTION)


def _guard(stop_line: float = 100.0, ledger: Path | None = None) -> spend.SpendGuard:
    book = spend.SpendLedger(ledger, "p1") if ledger is not None else None
    return spend.SpendGuard(stop_line=stop_line, prices=PRICES, ledger=book)


async def test_the_stop_line_refuses_the_next_call_before_it_is_sent() -> None:
    guard = _guard(stop_line=0.01)
    inner = _Inner(AIMessage(content="yes", usage_metadata=_usage(2000, 10)))
    caller = runner.MeteredCaller(inner, MODEL, guard)

    await caller(messages=_msgs(), tools=[])  # ¥0.0123,越过 ¥0.01
    with pytest.raises(spend.SpendStopError, match="stop line"):
        await caller(messages=_msgs(), tools=[])

    assert inner.calls == 1
    assert guard.spent == pytest.approx((2000 * 6 + 10 * 30) / 1e6)


async def test_an_unknown_provider_error_stops_everything() -> None:
    guard = _guard()
    failing = runner.MeteredCaller(_Inner(ValueError("HTTP 400")), MODEL, guard)
    healthy = _Inner(AIMessage(content="yes", usage_metadata=_usage()))

    with pytest.raises(ValueError):
        await failing(messages=_msgs(), tools=[])
    with pytest.raises(spend.SpendStopError, match="unknown status"):
        await runner.MeteredCaller(healthy, MODEL, guard)(messages=_msgs(), tools=[])

    assert healthy.calls == 0
    assert guard.spent == pytest.approx(CAP)


@pytest.mark.parametrize(
    "usage",
    [
        None,
        _extract_usage_metadata({"usage": {"total_tokens": 1505}}),  # 输入 / 输出被补成 0
        _extract_usage_metadata({"usage": {"completion_tokens": 7}}),  # 只有输出
        {"input_tokens": 1000, "output_tokens": 10, "total_tokens": 1505},  # 合计对不上
    ],
    ids=["none", "total-only", "completion-only", "total-mismatch"],
)
async def test_a_response_with_unusable_usage_stops_and_is_charged_the_cap(
    usage: dict[str, Any] | None,
) -> None:
    guard = _guard()
    response = AIMessage(content="yes", usage_metadata=usage)
    caller = runner.MeteredCaller(_Inner(response), MODEL, guard)

    with pytest.raises(spend.SpendStopError, match="no usable token usage"):
        await caller(messages=_msgs(), tools=[])
    assert guard.spent == pytest.approx(CAP)


async def test_a_model_without_a_price_and_an_oversized_input_both_stop() -> None:
    unpriced = spend.SpendGuard(stop_line=100.0, prices={})
    with pytest.raises(spend.SpendStopError, match="no validated price"):
        await runner.MeteredCaller(_Inner(None), MODEL, unpriced)(messages=_msgs(), tools=[])

    big = _Inner(AIMessage(content="yes", usage_metadata=_usage(9000, 1)))
    with pytest.raises(spend.SpendStopError, match="input tokens"):
        await runner.MeteredCaller(big, MODEL, _guard())(messages=_msgs(), tools=[])


async def test_a_timeout_is_charged_the_cap_but_does_not_stop() -> None:
    guard = _guard()
    calls: list[dict[str, Any]] = []
    token = runner._CALLS.set(calls)
    try:
        with pytest.raises(TimeoutError):
            await runner.MeteredCaller(_Inner(TimeoutError()), MODEL, guard)(
                messages=_msgs(), tools=[]
            )
    finally:
        runner._CALLS.reset(token)

    assert guard.blocked is None
    assert calls[0]["outcome"] == "timeout" and calls[0]["answer"] == "timeout"
    assert guard.spent == pytest.approx(CAP)


def _read(path: Path) -> list[dict[str, Any]]:
    return spend.read_ledger(path, broken_line_cost=CAP)


async def test_every_call_is_reserved_on_disk_before_it_is_sent_then_settled(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "p1.spend.jsonl"
    guard = _guard(ledger=ledger)
    seen_before_send: list[list[dict[str, Any]]] = []

    async def inner(*, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        seen_before_send.append(_read(ledger))
        return AIMessage(content="yes", usage_metadata=_usage(2000, 10))

    await runner.MeteredCaller(inner, MODEL, guard)(messages=_msgs(), tools=[])

    assert [row["reason"] for row in seen_before_send[0]] == ["reserve"]
    assert seen_before_send[0][0]["cost_yuan"] == pytest.approx(CAP)
    rows = _read(ledger)
    assert [row["reason"] for row in rows] == ["reserve", "ok"]
    assert rows[0]["call_id"] == rows[1]["call_id"] and rows[1]["plan_id"] == "p1"
    assert spend.ledger_total(rows) == pytest.approx((2000 * 6 + 10 * 30) / 1e6)


def test_a_reserve_that_was_never_settled_counts_at_the_cap(tmp_path: Path) -> None:
    """进程崩了 / Ctrl-C:只有预留行,没有结算行 —— 按单次上限算进账本合计。"""
    ledger = tmp_path / "p1.spend.jsonl"
    book = spend.SpendLedger(ledger, "p1")
    book.append({"call_id": "a", "model": MODEL, "cost_yuan": CAP, "reason": "reserve"})
    book.append({"call_id": "a", "model": MODEL, "cost_yuan": 0.01, "reason": "ok"})
    book.append({"call_id": "b", "model": MODEL, "cost_yuan": CAP, "reason": "reserve"})

    assert spend.ledger_total(_read(ledger)) == pytest.approx(0.01 + CAP)


def test_a_truncated_last_line_counts_at_the_cap_with_a_notice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """写到一半被打断:最后一行读不出来 —— 按单次上限算、提示,不建议删账本;之后再追加的行
    不会接在半行后面。"""
    ledger = tmp_path / "p1.spend.jsonl"
    book = spend.SpendLedger(ledger, "p1")
    book.append({"call_id": "a", "model": MODEL, "cost_yuan": 0.01, "reason": "ok"})
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write('{"plan_id": "p1", "call_id": "b", "cost_yu')

    assert spend.ledger_total(_read(ledger)) == pytest.approx(0.01 + CAP)
    notice = capsys.readouterr().err
    assert "line 2" in notice and "Keep the ledger" in notice and "delete" not in notice.lower()

    book.append({"call_id": "c", "model": MODEL, "cost_yuan": 0.02, "reason": "ok"})
    rows = _read(ledger)
    assert [row["call_id"] for row in rows] == ["a", "unreadable-line-2", "c"]
    assert spend.ledger_total(rows) == pytest.approx(0.01 + CAP + 0.02)


async def test_a_failed_reserve_write_stops_sends_nothing_and_reserves_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    guard = _guard(ledger=tmp_path / "p1.spend.jsonl")
    inner = _Inner(AIMessage(content="yes", usage_metadata=_usage()))

    def disk_full(self: spend.SpendLedger, row: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(spend.SpendLedger, "append", disk_full)
    with pytest.raises(spend.SpendStopError, match="ledger could not be written"):
        await runner.MeteredCaller(inner, MODEL, guard)(messages=_msgs(), tools=[])

    assert inner.calls == 0
    assert guard.spent == 0.0 and guard.blocked is not None


def test_error_stops_are_counted_per_model_from_the_ledger() -> None:
    rows = [
        {"call_id": "a", "model": MODEL, "cost_yuan": CAP, "reason": "cap_error"},
        {"call_id": "b", "model": "x", "cost_yuan": CAP, "reason": "cap_timeout"},
    ]
    assert spend.error_stops(rows) == {MODEL: 1}


def test_the_lock_and_ledger_are_per_plan_not_per_results_path(tmp_path: Path) -> None:
    assert spend.ledger_path(tmp_path, "p1") == tmp_path / "p1.spend.jsonl"
    with spend.lock_path(tmp_path, "p1").open("a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(spend.LockedError, match="another run"), spend.exclusive(tmp_path, "p1"):
            pass
        with spend.exclusive(tmp_path, "p2"):  # 别的计划不受影响
            pass
    with spend.exclusive(tmp_path, "p1"):  # 放开之后拿得到
        pass
    assert spend.DEFAULT_STATE_DIR == Path.home() / ".cache" / "b301-health"


def test_missing_credentials_name_the_variable_and_never_the_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for names in runner.KEY_VARIABLES.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)
    kube = tmp_path / "eval-llm.env"
    kube.write_text("ARK_API_KEY=ark-test-secret\nGLM_API_KEY=glm-test-secret\n", encoding="utf-8")
    helix = tmp_path / "helix.env"
    helix.write_text("HELIX_EVAL_LLM_API_KEY=sk-test-secret\n", encoding="utf-8")

    keys = runner.load_keys(["doubao", "qwen"], [kube, helix])
    assert keys == {"doubao": "ark-test-secret", "qwen": "sk-test-secret"}
    with pytest.raises(runner.RunRefusedError) as refused:
        runner.load_keys(["doubao", "deepseek", "qwen"], [kube])
    message = str(refused.value)
    assert "DEEPSEEK_API_KEY" in message and "HELIX_EVAL_LLM_API_KEY" in message
    assert "secret" not in message
    assert cli.DEFAULT_ENV_FILE == Path.home() / ".kube" / "eval-llm.env"
