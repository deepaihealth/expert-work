"""B-300 评测脚本审查后的加固(H1~H4、M1、M6):离线,不连库、不调模型、不连网。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import b300_eval as cli
import b300_inpod as inpod
import b300_live as live
import b300_plan as plan
import b300_scenarios as sc
import b300_spend as spend
import httpx
import pytest
from b301_plan import Price

PRICES = {"m": Price(1.0, 10.0, 0.1, "test")}
TENANT = UUID("11111111-1111-4111-8111-111111111111")

# --------------------------------------------------------------------------- H1


_WORKERS = (
    "create_app",
    "RunQueueWorker",
    "MemoryDLQWorker",
    "MemoryWritebackWorker",
    "MemoryConsolidator",
    "ApprovalTimeoutSweep",
    "QualityMonitor",
    "CurationWorker",
)


@pytest.mark.asyncio
async def test_bootstrap_builds_no_worker_and_starts_no_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H1:引导不进 lifespan、不建任何 worker / sweep;建了就炸。"""
    import control_plane.app as app_mod

    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a worker / the app was constructed by the in-pod bootstrap")

    for name in _WORKERS:
        if hasattr(app_mod, name):
            monkeypatch.setattr(app_mod, name, boom)
    before = set(asyncio.all_tasks())
    async with inpod.bootstrap() as deps:
        assert set(asyncio.all_tasks()) - before == set()
        assert type(deps.memory).__name__ == "SqlMemoryStore"
        assert deps.token_usage is not None
        consolidator, _aux = deps.consolidator_factory(0)
        assert type(consolidator).__name__ == "MemoryConsolidator"
        assert not consolidator.is_running


def test_bootstrap_guard_fails_closed_on_a_background_task() -> None:
    async def scenario() -> None:
        stray = asyncio.create_task(asyncio.sleep(10))
        try:
            with pytest.raises(SystemExit, match="background task"):
                inpod.assert_no_background_tasks(baseline=set())
        finally:
            stray.cancel()

    asyncio.run(scenario())


def test_bootstrap_does_not_enter_the_app_lifespan() -> None:
    source = cli.INPOD_SOURCE.read_text(encoding="utf-8")
    assert "lifespan_context" not in source and "create_app(" not in source


# --------------------------------------------------------------------------- H2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model", "cap_field", "thinking"),
    [
        ("qwen", "qwen3.8-max-0902", "max_completion_tokens", {"enable_thinking": False}),
        (
            "doubao",
            "doubao-seed-2-1-pro-260915",
            "max_completion_tokens",
            {"thinking": {"type": "disabled"}},
        ),
        ("deepseek", "deepseek-v4-pro", "max_tokens", {"thinking": {"type": "disabled"}}),
        ("glm", "glm-5.3-flash", "max_tokens", {"reasoning_effort": "low"}),
    ],
)
async def test_requests_carry_the_products_thinking_off_fields(
    provider: str, model: str, cap_field: str, thinking: dict[str, Any]
) -> None:
    """H2:A 组提炼与判官的请求体与产品的「关思考 + 输出上限」同一套字段。"""
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    guard = spend.Guard(stop_line=10.0, prices={model: Price(1.0, 1.0, 0.1, "t")})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        t = live.ChatTransport(model, "https://v", "k", guard, http, 777, provider=provider)
        await t.complete([{"role": "user", "content": "hi"}], item="i", phase="p")
    body = bodies[0]
    assert body[cap_field] == 777
    other = {"max_tokens", "max_completion_tokens"} - {cap_field}
    assert not other & set(body)
    for key, value in thinking.items():
        assert body[key] == value


def test_transport_needs_a_provider() -> None:
    with pytest.raises(ValueError, match="provider"):
        live.request_fields("", "m", max_tokens=10)


# --------------------------------------------------------------------------- H3


def test_one_spending_process_per_state_dir(tmp_path: Path) -> None:
    with spend.state_lock(tmp_path), pytest.raises(spend.LockedError):
        with spend.state_lock(tmp_path):
            pass
    with spend.state_lock(tmp_path):  # 释放后能再拿
        pass


def test_reserve_counts_other_quotes_against_the_global_cap(tmp_path: Path) -> None:
    q1 = spend.Guard.resume(stop_line=400.0, prices=PRICES, state_dir=tmp_path, quote_id="q1")
    call = q1.reserve(item="x", model="m", cap=300.0, phase="b")
    q1.settle(call, item="x", model="m", phase="b", cost=300.0)
    q2 = spend.Guard.resume(stop_line=200.0, prices=PRICES, state_dir=tmp_path, quote_id="q2")
    assert q2.others == pytest.approx(300.0)
    q2.reserve(item="y", model="m", cap=90.0, phase="b")
    with pytest.raises(spend.SpendStopError, match="400"):
        q2.reserve(item="z", model="m", cap=20.0, phase="b")  # 300 + 90 + 20 > 400
    assert q2.others + q2.spent <= plan.CAP_YUAN


def _quote(tmp_path: Path, *extra: str) -> dict[str, Any]:
    out: list[str] = []
    code = cli.main(
        ["quote", "--accept-documented-prices", "--state-dir", str(tmp_path / "state"), *extra],
        emit=out.append,
    )
    assert code == 0, out
    return json.loads(out[0])


def test_spending_commands_refuse_while_another_holds_the_state_lock(tmp_path: Path) -> None:
    q = _quote(tmp_path, "--stop-line", "100")
    out: list[str] = []
    with spend.state_lock(tmp_path / "state"):
        code = cli.main(
            [
                "run",
                "--phase",
                "a",
                "--out",
                str(tmp_path / "out"),
                "--approved-quote",
                q["quote_id"],
                "--stop-line",
                "100",
                "--user-said",
                "go",
                "--accept-documented-prices",
                "--state-dir",
                str(tmp_path / "state"),
            ],
            emit=out.append,
        )
    assert code == 2 and "locked" in out[-1]


# --------------------------------------------------------------------------- H4


@pytest.mark.asyncio
async def test_inpod_reads_actual_writeback_usage_numbers_only() -> None:
    from expert_work.persistence import InMemoryTenantUserStore
    from expert_work.persistence.token_usage_store import InMemoryTokenUsageStore, TokenUsageRecord

    users = InMemoryTenantUserStore()
    usage = InMemoryTokenUsageStore()
    deps = inpod.Deps(
        memory=None,
        users=users,
        run_events=None,
        embedder=None,
        consolidator_factory=lambda n: (None, None),
        consolidation_model="glm-5.3-flash",
        token_usage=usage,
    )
    from control_plane.api._external import resolve_external_user_id

    uid = await resolve_external_user_id(tenant_id=TENANT, user_id="b300-u", users=users)
    for kind, agent, n in (
        ("platform_overhead", "eval-b300-glm-wb", 1000),
        ("platform_overhead", "eval-b300-glm-wb", 500),
        ("conversation", "eval-b300-glm-wb", 99_999),  # 不是写回
        ("platform_overhead", "other-agent", 77_777),  # 别的智能体
    ):
        await usage.insert(
            TokenUsageRecord(
                tenant_id=TENANT,
                agent_name=agent,
                agent_version="1",
                model="glm-5.3-flash",
                user_id=uid,
                input_tokens=n,
                output_tokens=n // 10,
                usage_kind=kind,
            )
        )
    out: list[str] = []
    since = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    await inpod.dispatch(
        deps,
        {
            "op": "writeback_usage",
            "since": since,
            "users": [
                {
                    "key": "b300-u",
                    "tenant_id": str(TENANT),
                    "user": "b300-u",
                    "agent_name": "eval-b300-glm-wb",
                }
            ],
        },
        out.append,
    )
    (row,) = [json.loads(x) for x in out]
    assert row["models"] == [
        {
            "model": "glm-5.3-flash",
            "input_tokens": 1500,
            "output_tokens": 150,
            "cache_read_tokens": 0,
            "rows": 2,
        }
    ]
    assert set(row) == {"op", "key", "models"}  # 只有数字,没有内容


def _wb_guard(tmp_path: Path) -> spend.Guard:
    return spend.Guard.resume(
        stop_line=100.0,
        prices={"glm-5.3-flash": Price(0.8, 2.8, 0.23, "t")},
        state_dir=tmp_path,
        quote_id="q",
    )


def test_writeback_reservation_stays_open_then_settles_by_actuals(tmp_path: Path) -> None:
    guard = _wb_guard(tmp_path)
    call = live.reserve_writeback(guard, item="k|c0", model="glm-5.3-flash", phase="c4")
    cap = live.writeback_cap_yuan(guard.prices["glm-5.3-flash"])
    assert guard.settled == 0 and guard.spent == pytest.approx(cap)  # 没按上限结算
    usage = [{"model": "glm-5.3-flash", "input_tokens": 1_000_000, "output_tokens": 0}]
    cost = live.settle_writebacks(guard, [call], usage, phase="c4")
    assert cost == pytest.approx(0.8)
    resumed = _wb_guard(tmp_path)
    assert resumed.spent == pytest.approx(0.8)  # 账本按实际


def test_writeback_settles_at_cap_only_when_unreadable(tmp_path: Path) -> None:
    guard = _wb_guard(tmp_path)
    calls = [
        live.reserve_writeback(guard, item=f"k|c{i}", model="glm-5.3-flash", phase="c4")
        for i in range(2)
    ]
    live.settle_writebacks(guard, calls, None, phase="c4")
    cap = live.writeback_cap_yuan(guard.prices["glm-5.3-flash"])
    assert guard.settled == pytest.approx(2 * cap)


# --------------------------------------------------------------------------- M1


@pytest.mark.asyncio
async def test_a_failed_model_call_marks_the_unit_as_error() -> None:
    """M1:产品代码会吞掉提炼 / 去重合并里的异常 —— 我们自己记下来,这一条判为出错。"""
    from longmem.embedders import KeywordEmbedder

    scenario = next(s for s in sc.build_group("A") if s.cls == "P9" and s.fact_key == "budget")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(request.content)
        system = " ".join(m["content"] for m in body["messages"] if m["role"] == "system")
        # 最后一次调用超时(当前 main 上写回只有这一次提炼;PR 1 之后是第二次提炼或去重合并)。
        if live.classify_call(system) in {"extract_old", "pass2", "reconcile"}:
            raise httpx.ReadTimeout("timed out")
        memory = {"kind": "fact", "content": "budget 150", "importance": 0.9, "confidence": 0.9}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps({"memories": [memory]})}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    guard = spend.Guard(stop_line=100.0, prices={"m": Price(1.0, 1.0, 0.1, "t")})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        transport = live.ChatTransport("m", "https://v", "k", guard, http, 500, provider="glm")
        with pytest.raises((RuntimeError, spend.SpendStopError)):
            await live.run_extraction(
                scenario, transport=transport, embedder=KeywordEmbedder(), item="u", phase="a"
            )


# --------------------------------------------------------------------------- M6


def test_a_stop_is_persisted_and_needs_an_ack(tmp_path: Path) -> None:
    guard = spend.Guard.resume(stop_line=1.0, prices=PRICES, state_dir=tmp_path, quote_id="q")
    with pytest.raises(spend.SpendStopError):
        guard.reserve(item="x", model="m", cap=5.0, phase="b")
    again = spend.Guard.resume(stop_line=10.0, prices=PRICES, state_dir=tmp_path, quote_id="q")
    assert again.blocked is not None
    acked = spend.Guard.resume(
        stop_line=10.0,
        prices=PRICES,
        state_dir=tmp_path,
        quote_id="q",
        unblock="raised the stop line after asking",
    )
    assert acked.blocked is None
    rows = [json.loads(x) for x in spend.ledger_path(tmp_path, "q").read_text().splitlines()]
    assert [r["reason"] for r in rows if r["reason"] in {"blocked", "unblock"}] == [
        "blocked",
        "unblock",
    ]
    assert (
        spend.Guard.resume(stop_line=10.0, prices=PRICES, state_dir=tmp_path, quote_id="q").blocked
        is None
    )


_ = Callable  # 类型别名在部分用例里用
