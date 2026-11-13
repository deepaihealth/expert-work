"""B-300 真跑那一段的离线测试:假的 HTTP(``httpx.MockTransport``)、假嵌入,不连网、不花钱。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import b300_live as live
import b300_scenarios as sc
import httpx
import pytest
from b300_spend import Guard, SpendStopError
from b301_plan import Price
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from longmem.embedders import KeywordEmbedder

PRICES = {"mem": Price(1.0, 4.0, 0.25, "test"), "main": Price(2.0, 8.0, 0.5, "test")}


def _guard(stop: float = 100.0) -> Guard:
    return Guard(stop_line=stop, prices=PRICES)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _chat(content: str, prompt: int = 1000, completion: int = 100) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
        },
    )


def test_classify_call() -> None:
    assert live.classify_call("x The other side of the conversation is not shown. y") == "pass1"
    assert live.classify_call("Every line starts with a tag:\n- [user]") == "pass2"
    assert live.classify_call(live.OLD_EXTRACT_MARK + " durable") == "extract_old"
    assert live.classify_call("You reconcile newly extracted memories against") == "reconcile"
    assert live.classify_call("something else") == "other"


def test_vendor_env_reads_only_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXPERT_WORK_EVAL_QWEN_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="EXPERT_WORK_EVAL_QWEN_"):
        live.vendor_env("qwen")
    monkeypatch.setenv("EXPERT_WORK_EVAL_QWEN_BASE_URL", "https://x")
    monkeypatch.setenv("EXPERT_WORK_EVAL_QWEN_API_KEY", "k")
    assert live.vendor_env("qwen") == ("https://x", "k")


@pytest.mark.asyncio
async def test_transport_reserves_then_settles_by_usage() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _chat("ok", prompt=1_000_000, completion=100_000)

    guard = _guard()
    async with _client(handler) as http:
        t = live.ChatTransport("mem", "https://v", "k", guard, http, 500)
        text, usage = await t.complete([{"role": "user", "content": "hi"}], item="i", phase="p")
    assert text == "ok" and usage["prompt_tokens"] == 1_000_000
    assert guard.settled == pytest.approx(1.0 + 0.4)
    assert seen[0]["max_tokens"] == 500 and seen[0]["model"] == "mem"


@pytest.mark.asyncio
async def test_transport_rate_limit_then_error_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    codes = iter([429, 500])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(codes), json={})

    guard = _guard()

    async def no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(live, "_sleep", no_sleep)
    async with _client(handler) as http:
        t = live.ChatTransport("mem", "https://v", "k", guard, http, 500)
        with pytest.raises(SpendStopError):
            await t.complete([{"role": "user", "content": "hi"}], item="i", phase="p")
    assert "HTTP 500" in (guard.blocked or "")
    cap = live.call_cap_yuan(PRICES["mem"], live.MAX_CALL_INPUT_TOKENS, 500)
    assert guard.settled == pytest.approx(cap)  # 429 那次记 0,500 那次按上限


@pytest.mark.asyncio
async def test_transport_missing_usage_blocks() -> None:
    guard = _guard()
    async with _client(
        lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})
    ) as http:
        t = live.ChatTransport("mem", "https://v", "k", guard, http, 500)
        with pytest.raises(SpendStopError):
            await t.complete([{"role": "user", "content": "hi"}], item="i", phase="p")
    assert "no usage" in (guard.blocked or "")


def test_turn_messages_use_the_production_constructor() -> None:
    by = {s.cls: s for s in sc.build_group("A") if s.fact_key == "budget"}
    p2 = live.turn_messages(by["P2"].turns[0], 0)
    assert isinstance(p2[0], HumanMessage) and isinstance(p2[1], AIMessage)
    assert isinstance(p2[2], ToolMessage) and "120" in str(p2[2].content)
    assert isinstance(p2[3], AIMessage) and not p2[3].tool_calls
    p3 = live.turn_messages(by["P3"].turns[0], 0)[0]
    assert isinstance(p3.content, list) and any(b.get("type") != "text" for b in p3.content)
    p4 = live.turn_messages(by["P4"].turns[0], 0)[0]
    text = p4.content if isinstance(p4.content, str) else json.dumps(p4.content, ensure_ascii=False)
    # 外部内容走生产的围栏(空白处会插标记字符,设计 §8.4),所以只认数字本身。
    assert "UNTRUSTED" in text and "120" in text


def test_parse_extraction() -> None:
    reply = '```json\n{"memories": [{"kind": "fact", "content": "a"}, {"kind": "fact"}]}\n```'
    assert live.parse_extraction(reply) == [{"kind": "fact", "content": "a"}]
    assert live.parse_extraction("not json") == []


@pytest.mark.asyncio
async def test_run_extraction_end_to_end_with_fakes() -> None:
    """生产写回入口 + 进程内记忆库 + 假模型:当前 main 上只有一次提炼(``extract_old``)。"""
    scenario = next(s for s in sc.build_group("A") if s.cls == "P9" and s.fact_key == "return-no")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = " ".join(m["content"] for m in body["messages"] if m["role"] == "system")
        if live.classify_call(system) in {"extract_old", "pass1", "pass2"}:
            memory = {
                "kind": "fact",
                "content": "The user's return number is RT-48977.",
                "importance": 0.9,
                "confidence": 0.9,
                "is_correction": True,
            }
            return _chat(json.dumps({"memories": [memory]}))
        return _chat(json.dumps({"operations": []}))

    guard = _guard()
    async with _client(handler) as http:
        transport = live.ChatTransport("mem", "https://v", "k", guard, http, 500)
        result = await live.run_extraction(
            scenario, transport=transport, embedder=KeywordEmbedder(), item="u1", phase="a"
        )
    assert set(result["seeded"]) == {"x"}
    (round_,) = result["rounds"]
    assert round_["after_turn"] == 1 and round_["calls"]
    assert {c["kind"] for c in round_["calls"]} <= {"extract_old", "pass1", "pass2", "reconcile"}
    contents = [m["content"] for m in result["memories"] if m["live"]]
    assert any("RT-48977" in c for c in contents)
    assert guard.settled > 0 and guard.blocked is None


def _sse(*frames: tuple[str, dict[str, Any]]) -> bytes:
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in frames).encode()


@pytest.mark.asyncio
async def test_run_conversation_settles_from_the_end_frame() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/uploads"):
            return httpx.Response(200, json={"data": {"upload_id": "up1", "session_id": "s1"}})
        body = json.loads(request.content)
        assert body["files"] == [{"upload_id": "up1"}] and body["session_id"] == "s1"
        ai = {
            "type": "ai",
            "content": "The record says 120.",
            "usage_metadata": {"input_tokens": 10},
        }
        stream = _sse(
            ("metadata", {"run_id": "r1", "thread_id": "s1"}),
            ("updates", {"agent": {"messages": [ai]}}),
            (
                "end",
                {
                    "status": "success",
                    "usage_by_model": [
                        {"model": "main", "input_tokens": 1_000_000, "output_tokens": 0}
                    ],
                },
            ),
        )
        return httpx.Response(200, content=stream, headers={"content-type": "text/event-stream"})

    scenario = next(s for s in sc.build_group("B") if s.cls == "B1" and s.fact_key == "budget")
    guard = _guard()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://cp"
    ) as http:
        conv = await live.run_conversation(
            http,
            agent="eval-b300-glm",
            user="u",
            turn=scenario.conversations[0],
            guard=guard,
            models=["main", "mem"],
            item="k",
            phase="b",
        )
    assert conv["run_id"] == "r1" and conv["final_text"] == "The record says 120."
    assert guard.settled == pytest.approx(2.0)
    assert calls[0].endswith("/uploads")


@pytest.mark.asyncio
async def test_run_conversation_without_end_cancels_and_charges_the_cap() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/usage"):
            return httpx.Response(200, json={"run_status": "running", "usage_by_model": []})
        if request.url.path.endswith(":cancel"):
            return httpx.Response(202, json={})
        stream = _sse(("metadata", {"run_id": "r2", "thread_id": "s2"}))
        return httpx.Response(200, content=stream)

    guard = _guard()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://cp"
    ) as http:
        await live.run_conversation(
            http,
            agent="a",
            user="u",
            turn=sc.Turn("hi"),
            guard=guard,
            models=["main"],
            item="k",
            phase="b",
        )
    assert guard.settled == pytest.approx(live.run_cap_yuan(PRICES, ["main"]))
    assert guard.blocked is not None


def test_writeback_is_charged_at_the_cap() -> None:
    guard = _guard()
    live.charge_writeback(guard, item="k", model="mem", phase="c4")
    assert guard.settled == pytest.approx(live.writeback_cap_yuan(PRICES["mem"]))


@pytest.mark.asyncio
async def test_judge_votes_and_majority() -> None:
    answers = iter(['{"answer": "yes"}', '{"answer": "no"}', "garbage"])
    guard = _guard()
    async with _client(lambda r: _chat(next(answers))) as http:
        judge = live.Judge(live.ChatTransport("mem", "https://j", "k", guard, http, 200), votes=3)
        votes = await judge.vote("prompt", item="i", phase="p")
    assert votes == [True, False, None]
    assert live.majority(votes) is None
    assert live.majority([True, True, False]) is True
    assert live.majority([False, False, None]) is False
