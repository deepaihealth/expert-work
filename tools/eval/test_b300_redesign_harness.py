"""B-300 重做评测接进现有跑法(设计 2026-11-15 §11.1 / §11.7 / §11.8):新段落与回归闸门、报告汇总、
报价的单元形状与判官子预算、判官预留卡死的修法、本机写回记下重做要量的东西。离线、不花钱。"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import b300_eval as cli
import b300_live as live
import b300_phases as phases
import b300_plan as plan
import b300_redesign_base as base
import b300_redesign_scenarios as ns
import b300_report as rep
import b300_runs as runs
import b300_scenarios as sc
import b300_spend as spend
import httpx
import pytest
from b301_plan import Price
from longmem.embedders import KeywordEmbedder

GLM = ["--vendor", "glm=glm-5.3:glm-5.3-flash", "--single-vendor", "glm"]


def _quote(tmp_path: Path, *extra: str) -> dict[str, Any]:
    out: list[str] = []
    code = cli.main(
        ["quote", "--accept-documented-prices", "--state-dir", str(tmp_path / "state"), *extra],
        emit=out.append,
    )
    assert code == 0, out
    body: dict[str, Any] = json.loads(out[0])
    return body


# --------------------------------------------------------------------------- 段落


def test_new_phases_and_their_budgets() -> None:
    gate = phases.PHASES["gate"]
    assert gate.classes == ("P6", "P7") and gate.kind == "extraction" and not gate.baseline
    assert phases.budget_of("gate") == "G"
    for name in ("s", "shape", "a13", "a14", "a15", "sd"):
        assert phases.PHASES[name].kind == "extraction" and phases.budget_of(name) == "A"
    assert set(phases.PHASES["s"].classes) == set(ns.S_CLASSES)


def test_judge_calls_have_their_own_sub_budget() -> None:
    """§11.8 ③:判官单独一段子预算,不与写回、对话抢同一份余量。"""
    assert phases.budget_of("a-judge") == "A-judge"
    assert phases.budget_of("gate-judge") == "G-judge"
    assert phases.budget_of("b-judge") == "B-judge"
    assert phases.budget_of("calibration") == "B-judge"
    assert phases.budget_of("calibration-a") == "A-judge"
    assert phases.budget_of("calibration-g") == "G-judge"
    assert phases.budget_of("a-judge", pilot=True) == "pilot"
    assert runs.judge_phase("a") == "a-judge" and runs.judge_phase("calibration-a") == (
        "calibration-a"
    )


def test_gate_units_are_the_11_15_p6_p7_units_unchanged(tmp_path: Path) -> None:
    """§11.1 第 2 步:11-15 的 60 个 P6 / P7 单元原样重跑(场景摘要与 A 组那几个一模一样)。"""
    q = _quote(tmp_path, *GLM, "--phases", "g", "--stop-line", "20")
    gate = phases.build_units(phases.PHASES["gate"], q)
    assert len(gate) == 60 and {u["cls"] for u in gate} == {"P6", "P7"}
    a = {u["scenario_id"]: u["scenario_digest"] for u in phases.build_units(phases.PHASES["a"], q)}
    assert all(a[u["scenario_id"]] == u["scenario_digest"] for u in gate)


def test_s_phase_has_225_units(tmp_path: Path) -> None:
    q = _quote(tmp_path, *GLM, "--phases", "a", "--stop-line", "100")
    assert len(phases.build_units(phases.PHASES["s"], q)) == 225
    assert len(phases.build_units(phases.PHASES["shape"], q)) == 60


# --------------------------------------------------------------------------- 报告汇总


def _row(content: str, origin: str | None, *, live_: bool = True, rid: str = "1") -> dict[str, Any]:
    return {"id": rid, "kind": "fact", "content": content, "origin": origin, "live": live_}


def _write(
    out: Path, phase: str, records: list[dict[str, Any]], verdicts: list[dict[str, Any]]
) -> None:
    (out / phase).mkdir(parents=True, exist_ok=True)
    (out / phase / "records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    (out / phase / "verdicts.jsonl").write_text("".join(json.dumps(v) + "\n" for v in verdicts))


def _record(
    s: sc.Scenario, key: str, memories: list[dict[str, Any]], caps: dict[str, bool] | None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "seeded": {ns.BYSTANDER: "by"} if any(x.key == ns.BYSTANDER for x in s.seeds) else {},
        "rounds": [
            {"after_turn": i, "calls": [{"kind": "pass2", "reply": '{"memories": []}'}]}
            for i in s.writebacks
        ],
        "memories": memories,
    }
    if caps is not None:
        result["capabilities"] = caps
    return {
        "key": key,
        "scenario_id": s.scenario_id,
        "cls": s.cls,
        "vendor": "glm",
        "domain": s.domain,
        "result": result,
    }


def test_report_collects_s_lines_collateral_and_incomplete(tmp_path: Path) -> None:
    out = tmp_path / "out"
    s2 = next(s for s in ns.build_redesign() if s.cls == "S2")
    caps = dict.fromkeys(base.CAPABILITIES, True)
    digest = rep.a_judge_wording_digest()
    good = _record(
        s2, "k1", [_row(s2.x_key, "user_confirmed"), _row("b", "user_stated", rid="by")], caps
    )
    hit = _record(s2, "k2", [_row("b", "user_stated", live_=False, rid="by")], caps)
    old = _record(s2, "k3", [_row("b", "user_stated", rid="by")], None)
    _write(
        out,
        "s",
        [good, hit, old],
        [{"key": "k1|confirmed_grounded", "verdict": True, "wording_digest": digest}],
    )
    body = cli.report(out, None)
    line = body["lines"]["S2.confirmed_grounded"]["glm"]
    assert line["good"] == 1 and line["n"] == 2 and line["missing"] == {"user_confirmed": 1}
    assert line["verdict"].startswith("incomplete")
    coll = body["lines"]["ALL.collateral"]["glm"]
    assert coll["bad"] == 1 and coll["verdict"] == "fail"
    assert body["counts"]["s"]["S2.recalled_live"] == 1


def test_report_names_gate_lines_apart_from_the_a_group(tmp_path: Path) -> None:
    out = tmp_path / "out"
    p6 = next(s for s in sc.build_group("A") if s.cls == "P6")
    caps = dict.fromkeys(base.CAPABILITIES, True)
    rec = _record(p6, "g1", [_row(p6.x_key, "user_confirmed")], caps)
    digest = rep.a_judge_wording_digest()
    _write(
        out,
        "gate",
        [rec],
        [{"key": "g1|confirmed_grounded", "verdict": False, "wording_digest": digest}],
    )
    body = cli.report(out, None)
    assert body["lines"]["G-P6.pass2_empty"]["glm"]["bad"] == 1
    assert body["lines"]["G-P6.confirmed_grounded"]["glm"]["good"] == 0
    assert "P6.confirmed_grounded" not in body["lines"]


def test_report_event_date_verdicts_and_missing_field(tmp_path: Path) -> None:
    out = tmp_path / "out"
    s1 = next(s for s in ns.build_redesign() if s.cls == "S1")
    caps = dict.fromkeys(base.CAPABILITIES, True)
    dated = {**_row("read 5", "user_stated"), "occurred_on": "2026-11-15"}
    rec = _record(s1, "e1", [dated], caps)
    digest = rep.a_judge_wording_digest()
    _write(out, "s", [rec], [{"key": "e1|event_date|1", "verdict": True, "wording_digest": digest}])
    line = cli.report(out, None)["lines"]["ALL.event_date_not_event"]["glm"]
    assert line["bad"] == 1 and line["n"] == 1 and line["verdict"] == "fail"
    rec = _record(s1, "e2", [dated], {**caps, "event_date": False})
    _write(out, "s", [rec], [])
    line = cli.report(out, None)["lines"]["ALL.event_date_not_event"]["glm"]
    assert line["verdict"].startswith("incomplete") and "event_date" in line["verdict"]


# --------------------------------------------------------------------------- 报价


def test_gate_quote_prices_only_the_gate_with_a_judge_sub_budget(tmp_path: Path) -> None:
    q = _quote(tmp_path, *GLM, "--phases", "g", "--stop-line", "20")
    assert q["phases"] == ["G"]
    groups = q["planned"]["groups"]
    assert set(groups) == {"gate", "calibration-g"}
    kinds = groups["gate"]["by_vendor"]["glm"]["calls_by_kind"]
    assert kinds["extract@glm-5.3-flash"] == [120.0, 120.0]  # 60 次写回 x 两次提炼
    assert kinds["reconcile@glm-5.3-flash"] == [0.0, 120.0]  # 两段合并,每段 0~1 次
    assert set(q["phase_budgets"]) == {"G", "G-judge"}
    total = sum(b["budget_yuan"] for b in q["phase_budgets"].values())
    assert total == pytest.approx(20.0, abs=0.02)


def test_default_quote_does_not_carry_the_gate(tmp_path: Path) -> None:
    q = _quote(tmp_path)
    assert "gate" not in q["planned"]["groups"] and q["phases"] == ["B", "C", "A"]


def test_redesign_unit_shapes_are_priced_in_the_a_budget(tmp_path: Path) -> None:
    """§11.7 ①:S 组、形状留出集、A13~A15、同一天读数的单元形状进报价(每个场景每轮写回一次)。"""
    q = _quote(tmp_path, *GLM, "--phases", "a", "--stop-line", "100", "--no-compare")
    kinds = q["planned"]["groups"]["S"]["by_vendor"]["glm"]["calls_by_kind"]
    writebacks = sum(len(s.writebacks) for s in ns.build_redesign()) * plan.REPEATS
    assert writebacks == 942
    assert kinds["extract@glm-5.3-flash"] == [writebacks * 2.0] * 2
    assert plan.budget_of_group("S") == "A" and set(q["phase_budgets"]) == {"A", "A-judge"}


def test_judge_output_cap_is_sized_to_the_short_verdict() -> None:
    """§11.8 ④:判词很短,输出上限按需要设;报价里的高档也随之变小。"""
    assert live.JUDGE_MAX_TOKENS <= 400
    assert plan.CALL_TOKENS["judge"][1][1] <= live.JUDGE_MAX_TOKENS


def test_quote_says_so_when_the_stop_line_cannot_hold_the_in_flight_worst(tmp_path: Path) -> None:
    """§11.8 ①:止损线低于「预计花费 + 并发 x 单次最坏」就在报价阶段说出来。"""
    tight = _quote(tmp_path, *GLM, "--phases", "g", "--stop-line", "0.5")
    assert tight["headroom"]["G-judge"]["short"] is True and "headroom_warning" in tight
    roomy = _quote(tmp_path, *GLM, "--phases", "g", "--stop-line", "40")
    assert not any(h["short"] for h in roomy["headroom"].values())
    assert "headroom_warning" not in roomy
    h = roomy["headroom"]["G-judge"]
    assert h["in_flight_worst_yuan"] > 0
    assert h["needed_yuan"] == pytest.approx(h["high_estimate_yuan"] + h["in_flight_worst_yuan"])


# --------------------------------------------------------------------------- 判官预留


def _guard(stop: float, caps: dict[str, float] | None = None) -> spend.Guard:
    return spend.Guard(
        stop_line=stop,
        prices={"m": Price(1, 1, 1, "t")},
        phase_caps=caps or {},
        budget_of=phases.budget_of,
    )


def test_guard_fits_checks_every_line_without_blocking() -> None:
    g = _guard(10.0, {"A-judge": 2.0})
    assert g.fits("a-judge", 1.5) and not g.fits("a-judge", 2.5)
    g.reserve(item="x", model="m", cap=1.5, phase="a-judge")
    assert not g.fits("a-judge", 1.0) and g.fits("a", 1.0)
    assert g.blocked is None and g.may_spend("a-judge")


def test_room_gate_waits_for_in_flight_instead_of_stopping() -> None:
    """§11.8 ②:剩余装不下下一笔最坏值时,等在飞的结算后再发,而不是停下整个报价。"""
    g = _guard(10.0, {"A-judge": 2.5})

    async def main() -> list[str]:
        gate = spend.RoomGate(g, "a-judge", limit=4)
        order: list[str] = []

        async def one(name: str) -> None:
            slot = await gate.acquire(2.0)
            assert slot is not None
            gate.consume(slot, 2.0)
            call = g.reserve(item=name, model="m", cap=2.0, phase="a-judge")
            order.append(f"start {name}")
            await asyncio.sleep(0)
            g.settle(call, item=name, model="m", phase="a-judge", cost=0.1)
            order.append(f"end {name}")
            await gate.release(slot)

        await asyncio.gather(one("a"), one("b"))
        return order

    order = asyncio.run(main())
    assert order == ["start a", "end a", "start b", "end b"]  # 一次只飞一笔
    assert g.blocked is None


def test_room_gate_gives_up_only_when_nothing_is_in_flight() -> None:
    g = _guard(10.0, {"A-judge": 1.0})

    async def main() -> spend.Slot | None:
        return await spend.RoomGate(g, "a-judge", limit=4).acquire(2.0)

    assert asyncio.run(main()) is None and g.blocked is None


def test_judge_reserves_by_the_prompt_size_not_the_40k_default() -> None:
    """§11.8 ④:单次最坏值按这道题的实际长度算(UTF-8 字节数是 token 数的上界),不按 4 万。"""
    short = [{"role": "system", "content": "abc"}, {"role": "user", "content": "你好"}]
    assert live.judge_input_bound(short) == live.JUDGE_MIN_INPUT_TOKENS  # 短题按下限
    long = [{"role": "system", "content": "a" * 3000}, {"role": "user", "content": "你" * 1000}]
    bound = live.judge_input_bound(long)
    assert bound == 3000 + 3000 + live.JUDGE_TEMPLATE_OVERHEAD_TOKENS
    assert bound < live.MAX_CALL_INPUT_TOKENS


def test_judge_through_the_gate_reserves_the_small_cap() -> None:
    seen: list[float] = []

    class G(spend.Guard):
        def reserve(self, *, item: str, model: str, cap: float, phase: str) -> str:
            seen.append(cap)
            return super().reserve(item=item, model=model, cap=cap, phase=phase)

    guard = G(stop_line=5.0, prices={plan.JUDGE_MODEL: Price(9.0, 27.0, 0.3, "t")})

    def handler(request: httpx.Request) -> httpx.Response:
        body = {
            "choices": [{"message": {"content": '{"answer": "yes", "reason": "level 5"}'}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10},
        }
        return httpx.Response(200, json=body)

    async def main() -> list[Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            t = live.ChatTransport(
                plan.JUDGE_MODEL,
                "https://j",
                "k",
                guard,
                http,
                live.JUDGE_MAX_TOKENS,
                provider="deepseek",
            )
            judge = live.Judge(t, 1, gate=spend.RoomGate(guard, "a-judge", limit=4))
            return await judge.vote_with_usage("short prompt", item="i", phase="a-judge")

    got = asyncio.run(main())
    assert got[0][0] is True
    (cap,) = seen
    worst_40k = spend.call_cap_yuan(guard.prices[plan.JUDGE_MODEL], 40_000, live.JUDGE_MAX_TOKENS)
    assert cap < worst_40k / 10


# --------------------------------------------------------------------------- 本机写回


def test_classify_call_knows_the_new_prompts() -> None:
    """重做后第二次与去重合并的指令换了开头(附录 A / B),还要认得出来。"""
    assert (
        live.classify_call(
            "You are a memory extraction module. The input is part of a conversation, one "
            "message per\nline. Each line starts with its number in brackets and a tag:"
        )
        == "pass2"
    )
    assert (
        live.classify_call(
            'You compare newly extracted memories ("candidates") with existing memories'
        )
        == "reconcile"
    )
    first = (
        "The input is messages one user typed in a conversation, one per line, oldest first, "
        "each starting with its number in brackets. The other side of the conversation is "
        "not shown."
    )
    assert live.classify_call(first) == "pass1"


def _field_kept(name: str, value: Any) -> bool:
    """按被测代码的协议模型造一条记忆:这个字段收下了这个值没有(不认识的字段会被丢掉)。"""
    from uuid import uuid4

    from expert_work.protocol import MemoryItem

    try:
        item = MemoryItem(
            id=uuid4(),
            tenant_id=uuid4(),
            user_id=uuid4(),
            kind="fact",
            content="probe",
            embedding=(0.0,),
            confidence=0.5,
            importance=0.5,
            **{name: value},
        )
    except Exception:  # 认识这个字段但不收这个值,也算没有
        return False
    return getattr(item, name, None) == value


def _takes_slice_start() -> bool:
    from orchestrator.graph_builder import memory as memory_module

    entry = inspect.signature(memory_module.flush_messages_with_outcome).parameters
    return live.SLICE_START_PARAM in entry


#: 每个能力按被测代码**实际的行为**去探(不写死 True / False):PR M / R / X / S 合进来前后都成立。
_PROBES: dict[str, Callable[[], bool]] = {
    "user_confirmed": lambda: _field_kept("origin", "user_confirmed"),
    "evidence_pointer": lambda: _field_kept("evidence_message_id", "m-1"),
    "event_date": lambda: _field_kept("occurred_on", date(2026, 11, 15)),
    "confirmed_at": lambda: _field_kept("confirmed_at", datetime(2026, 11, 15, tzinfo=UTC)),
    "slicing": _takes_slice_start,
}


@pytest.mark.parametrize("name", base.CAPABILITIES)
def test_capabilities_reflect_the_code_under_test(name: str) -> None:
    caps = live.platform_capabilities()
    assert set(caps) == set(base.CAPABILITIES)
    assert caps[name] is _PROBES[name](), name


def test_slice_start_is_passed_only_when_the_entry_takes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orchestrator.graph_builder import memory as mem

    starts: list[int] = []

    async def fake(messages: Any, *, start_message_count: int = 0, **_: Any) -> Any:
        starts.append(start_message_count)
        return mem.FlushOutcome(0, False)

    monkeypatch.setattr(mem, "flush_messages_with_outcome", fake)
    assert live.platform_capabilities()["slicing"] is True
    s = next(x for x in ns.build_redesign() if x.cls == "S2")
    guard = spend.Guard(stop_line=5.0, prices={"mem": Price(1, 1, 1, "t")})

    async def main() -> dict[str, Any]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(500))
        ) as http:
            t = live.ChatTransport("mem", "https://v", "k", guard, http, 500, provider="deepseek")
            return await live.run_extraction(
                s, transport=t, embedder=KeywordEmbedder(), item="u", phase="s"
            )

    result = asyncio.run(main())
    first = len(live.turn_messages(s.turns[0], 0))
    assert starts == [0, first]
    assert [r["message_count"] for r in result["rounds"]] == [
        first,
        result["rounds"][1]["message_count"],
    ]
    assert all(r["sliced"] for r in result["rounds"])
    assert inspect.signature(fake).parameters[live.SLICE_START_PARAM]


def test_run_extraction_records_what_the_redesign_measures() -> None:
    s = next(x for x in ns.build_redesign() if x.cls == "S2")

    # 提炼出的内容与旁观记忆几乎一样(多几个字),去重合并一定会被叫到;假的去重合并对每条候选回
    # NOOP(按附录 B 的回答格式 ``{"ops": [...]}``,目标用材料里给的编号)。回答格式对不上时平台会
    # 退回直接新增,这条候选就会落库 —— 下面断言它没落库,所以走的一定是成功的那条路。
    bystander = next(x.content for x in s.seeds if x.key == ns.BYSTANDER)
    candidate = f"{bystander} 已确认"
    reconciled: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = " ".join(m["content"] for m in body["messages"] if m["role"] == "system")
        kind = live.classify_call(system)
        if kind == "reconcile":
            user = next(m["content"] for m in body["messages"] if m["role"] == "user")
            ops = [
                {"index": c["index"], "op": "NOOP", "target_id": c["existing"][0]["id"]}
                for c in json.loads(user)["candidates"]
                if c.get("existing")
            ]
            reply = json.dumps({"ops": ops})
            reconciled.append(reply)
        else:
            reply = json.dumps({"memories": [{"kind": "fact", "content": candidate}]})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": reply}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    guard = spend.Guard(stop_line=5.0, prices={"mem": Price(1, 1, 1, "t")})

    async def main() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            t = live.ChatTransport("mem", "https://v", "k", guard, http, 500, provider="deepseek")
            return await live.run_extraction(
                s, transport=t, embedder=KeywordEmbedder(), item="u", phase="s"
            )

    result = asyncio.run(main())
    assert reconciled, "the reconcile step was never reached"
    live_rows = [m["content"] for m in result["memories"] if m["live"]]
    assert candidate not in live_rows  # NOOP 被采纳:走的是成功的那条路,不是失败后直接新增
    assert all(not r["failed"] for r in result["rounds"])
    assert result["capabilities"] == live.platform_capabilities()
    assert len(result["user_message_ids"]) == len(s.turns) and all(result["user_message_ids"])
    for r in result["rounds"]:
        assert isinstance(r["live_after"], list) and "message_count" in r and r["sliced"] is False
        assert all("input" in c for c in r["calls"] if c["kind"] in {"pass1", "pass2"})
    row = result["memories"][0]
    for field in ("superseded_by", "evidence_message_id", "evidence_message_index", "occurred_on"):
        assert field in row
    assert set(result["seeded"]) == {ns.BYSTANDER}
