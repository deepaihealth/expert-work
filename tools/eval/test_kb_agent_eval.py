"""kb_agent_eval.py:判分解析、归类、选题、计费、预算暂停 / 续跑、空跑不连网(B-206 批 0)。

全部走内存库 + 假嵌入 + 假模型;真模型那一轮在仓库外手工跑。
"""

from __future__ import annotations

import asyncio
import csv
import json
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
import yaml
from kb_agent_eval import (  # type: ignore[import-not-found]
    ANSWER_ENV,
    JUDGE_ENV,
    MAX_SEARCHES,
    PRICES,
    SEARCH_LIMIT_NOTE,
    BudgetGate,
    FakeModels,
    JudgeCache,
    Price,
    Spend,
    call_cost,
    category,
    done_case_ids,
    gold_passages,
    is_deepseek_offpeak,
    main,
    parse_verdict,
    resolve_price,
    run_agent,
    run_questions,
    select_cases,
    tally,
)
from kb_corpus_inventory import MANIFEST_FIELDS  # type: ignore[import-not-found]
from kb_dataset import KbCase, KbDataset, KbExpected  # type: ignore[import-not-found]
from kb_eval import (  # type: ignore[import-not-found]
    EVAL_TENANT_ID,
    build_fake_embedder,
    build_retriever,
    rls_tenant,
)

from expert_work.common.spotlight import DATAMARK_GLYPH, SPOTLIGHT_SYSTEM_CLAUSE, unspotlight
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import DocumentStatus
from orchestrator.llm.providers.openai import _to_openai_tool
from orchestrator.tools import knowledge
from orchestrator.tools.knowledge import (
    KnowledgeRetriever,
    KnowledgeSearchOutcome,
    KnowledgeSearchTool,
    RetrievedChunk,
)
from orchestrator.tools.knowledge_rerank import RerankRanking

_ANCHOR = "常温密封保存即可"
# 每节都比切块的短节合并线(约 100 token)长,两节各自成段,不会并成一段。
_DOC = (
    "# 产品说明书\n\n## 保存方式\n\n益生菌常温密封保存即可,不需要冷藏。"
    + "开封后请尽快用完,放在阴凉干燥处,避免阳光直射。" * 6
    + "\n\n## 成分\n\n每袋含活菌 100 亿。"
    + "配料为低聚果糖、菊粉和麦芽糊精,每袋净含量 2 克。" * 6
    + "\n"
)


# ---------------------------------------------------------------------------
# 判分解析 / 归类
# ---------------------------------------------------------------------------


def test_parse_verdict_reads_the_json_object_and_flags_anything_else() -> None:
    raw = '好的,结论如下:{"refused": true, "fabricated": false, "reason": "没写"} 以上'
    assert parse_verdict(raw) == {"refused": True, "fabricated": False, "reason": "没写"}
    assert parse_verdict("没有 JSON") == {"parse_error": "没有 JSON"}
    assert parse_verdict('{"refused": tru') == {"parse_error": '{"refused": tru'}
    assert parse_verdict("{not json}" + "x" * 300) == {
        "parse_error": ("{not json}" + "x" * 300)[:200]
    }


@pytest.mark.parametrize(
    ("answerable", "verdict", "expected"),
    [
        (True, {"parse_error": "x"}, "判分失败"),
        (True, {"fabricated": True, "matches_reference": "yes"}, "编造"),
        (True, {"matches_reference": "yes"}, "答对"),
        (True, {"matches_reference": "partial", "refused": True}, "答对"),
        (True, {"matches_reference": "no", "refused": True}, "误拒"),
        (True, {"matches_reference": "no", "refused": False}, "答偏"),
        (False, {"fabricated": True, "refused": True}, "编造"),
        (False, {"refused": True, "matches_reference": "n/a"}, "正确说没写"),
        (False, {"refused": False, "matches_reference": "n/a"}, "其它"),
        (False, {"parse_error": "x"}, "判分失败"),
    ],
)
def test_category(answerable: bool, verdict: dict[str, Any], expected: str) -> None:
    assert category(answerable, verdict) == expected


# ---------------------------------------------------------------------------
# 选题
# ---------------------------------------------------------------------------


def _case(case_id: str, *, answerable: bool) -> KbCase:
    expected = (KbExpected(document="manual.md", anchor=_ANCHOR, page=None),) if answerable else ()
    return KbCase(
        id=case_id,
        query=f"问题 {case_id}",
        source="faq_xlsx" if answerable else "no_answer",
        tags=(),
        expected=expected,
        answer=None,
    )


def _set(name: str, prefix: str, *, answerable: int, no_answer: int) -> KbDataset:
    cases = [_case(f"{prefix}-a{i:02d}", answerable=True) for i in range(answerable)]
    cases += [_case(f"{prefix}-n{i:02d}", answerable=False) for i in range(no_answer)]
    return KbDataset(name=name, corpus_manifest=None, corpus_tiers=("A",), cases=tuple(cases))


def test_select_cases_takes_every_no_answer_case_and_a_fixed_sample() -> None:
    main_set = _set("m", "m", answerable=20, no_answer=3)
    chat_set = _set("c", "c", answerable=18, no_answer=2)
    picked = select_cases(main_set, chat_set)
    assert picked == select_cases(main_set, chat_set)
    ids = [(label, case.id) for label, case in picked]
    assert ids[:3] == [("main", "m-n00"), ("main", "m-n01"), ("main", "m-n02")]
    assert ids[18:20] == [("chat", "c-n00"), ("chat", "c-n01")]
    assert len(ids) == 3 + 15 + 2 + 15
    # 钉住种子与抽法:改了种子 / 两个集各用一个随机源,抽到的就不是同一批题。
    assert [i for _label, i in ids[3:8]] == ["m-a06", "m-a10", "m-a02", "m-a14", "m-a03"]
    assert [i for _label, i in ids[20:25]] == ["c-a03", "c-a02", "c-a07", "c-a09", "c-a12"]


# ---------------------------------------------------------------------------
# 计费
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("utc", "offpeak"),
    [
        (datetime(2026, 10, 21, 2, 0, tzinfo=UTC), False),  # 周三北京 10:00
        (datetime(2026, 10, 21, 4, 30, tzinfo=UTC), True),  # 周三北京 12:30 午休
        (datetime(2026, 10, 21, 9, 59, tzinfo=UTC), False),  # 周三北京 17:59
        (datetime(2026, 10, 21, 10, 0, tzinfo=UTC), True),  # 周三北京 18:00
        (datetime(2026, 10, 20, 23, 0, tzinfo=UTC), True),  # 周三北京 07:00
        (datetime(2026, 10, 24, 2, 0, tzinfo=UTC), True),  # 周六北京 10:00
    ],
)
def test_deepseek_offpeak_is_judged_in_beijing_time(utc: datetime, offpeak: bool) -> None:
    assert is_deepseek_offpeak(utc) is offpeak


def test_call_cost_counts_cache_hits_and_halves_deepseek_off_peak() -> None:
    price = Price(input=8.0, cached=2.0, output=28.0)
    usage = {
        "prompt_tokens": 1_000_000,
        "prompt_tokens_details": {"cached_tokens": 400_000},
        "completion_tokens": 100_000,
    }
    peak = datetime(2026, 10, 21, 2, 0, tzinfo=UTC)
    night = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)
    expected = 0.6 * 8.0 + 0.4 * 2.0 + 0.1 * 28.0
    assert call_cost(usage, price, model="glm-5.3", at=night) == pytest.approx(expected)
    assert call_cost(usage, price, model="deepseek-v4-pro", at=peak) == pytest.approx(expected)
    assert call_cost(usage, price, model="deepseek-v4-pro", at=night) == pytest.approx(expected / 2)
    deepseek_usage = {"prompt_tokens": 1_000_000, "prompt_cache_hit_tokens": 1_000_000}
    assert call_cost(deepseek_usage, price, model="x", at=peak) == pytest.approx(2.0)


def test_unknown_models_need_an_explicit_price() -> None:
    assert resolve_price("glm-5.3", None) == PRICES["glm-5.3"]
    assert resolve_price("other-model", "1,0.5,2") == Price(1.0, 0.5, 2.0)
    with pytest.raises(SystemExit, match="no price"):
        resolve_price("other-model", None)
    with pytest.raises(SystemExit, match="input,cached,output"):
        resolve_price("other-model", "1,2")


# ---------------------------------------------------------------------------
# 答题:工具就是主干的 knowledge_search,渲染走工具节点
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FixedRetriever:
    results: tuple[RetrievedChunk, ...]

    async def search_detailed(self, **_kwargs: Any) -> KnowledgeSearchOutcome:
        return KnowledgeSearchOutcome(
            results=self.results, status="ok", candidates=len(self.results)
        )


def _tool() -> KnowledgeSearchTool:
    chunk = RetrievedChunk(content=f"益生菌 {_ANCHOR}。", filename="manual.md", chunk_index=0)
    retriever = cast(KnowledgeRetriever, _FixedRetriever((chunk,)))
    return KnowledgeSearchTool(retriever=retriever, knowledge_base_refs=("kb",))


class _KeepSearching:
    """Searches on every turn (one more than the eval allows), then answers."""

    answer_model = "scripted"
    judge_model = "scripted"

    def __init__(self) -> None:
        self.seen: list[list[dict[str, Any]]] = []
        self.tools: list[dict[str, Any]] = []

    async def agent(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], float]:
        self.seen.append(list(messages))
        self.tools = tools
        n = sum(1 for m in messages if m["role"] == "tool")
        if n > MAX_SEARCHES:
            return {"role": "assistant", "content": "答完了"}, 0.25
        args = json.dumps({"query": f"第 {n} 次"}, ensure_ascii=False)
        call = {"id": f"c{n}", "type": "function", "function": {"name": "x", "arguments": args}}
        return {"role": "assistant", "content": "", "tool_calls": [call]}, 0.25

    async def judge(self, prompt: str) -> tuple[str, float]:
        return "{}", 0.0


@pytest.mark.asyncio
async def test_run_agent_uses_the_tool_as_shipped_and_caps_searches() -> None:
    models = _KeepSearching()
    tool = _tool()
    spend = Spend()
    run, cost = await run_agent(
        models, tool, question="益生菌要冷藏吗", tenant_id=uuid4(), spend=spend
    )
    assert run["answer"] == "答完了"
    assert models.tools == [_to_openai_tool(tool.spec)]  # 说明 / 参数取自工具本身
    assert models.seen[0][0]["content"].endswith(SPOTLIGHT_SYSTEM_CLAUSE)
    searches = run["searches"]
    assert [s.get("skipped", False) for s in searches] == [False] * MAX_SEARCHES + [True]
    assert all(s["status"] == "ok" and _ANCHOR in s["content"] for s in searches[:MAX_SEARCHES])
    # 模型看到的是工具节点渲染的:防注入围栏里的结果 + 围栏外的提示;判分看的是工具原文。
    tool_messages = [m for m in models.seen[-1] if m["role"] == "tool"]
    first = tool_messages[0]["content"]
    assert first.startswith("«UNTRUSTED nonce=")
    assert DATAMARK_GLYPH in first.split("«/UNTRUSTED")[0]  # 围栏里的正文打了标记
    assert unspotlight(first) == f"{searches[0]['content']}\n\n{knowledge._RESULTS_NOTICE}"
    assert tool_messages[-1]["content"] == SEARCH_LIMIT_NOTE
    assert cost == pytest.approx(0.25 * (MAX_SEARCHES + 2))
    assert (spend.answer_cny, spend.answer_calls) == (pytest.approx(cost), MAX_SEARCHES + 2)


class _ThresholdRetriever(_FixedRetriever):
    """A search whose rerank ended ``partial`` and its base's threshold left out
    for another model (B-206 K-7b)."""

    async def search_detailed(self, **_kwargs: Any) -> KnowledgeSearchOutcome:
        return KnowledgeSearchOutcome(
            results=self.results,
            status="ok",
            candidates=len(self.results),
            rerank_outcome="partial",
            rerank_threshold_paused=True,
        )


@pytest.mark.asyncio
async def test_each_search_records_how_its_rerank_and_threshold_ended() -> None:
    chunk = RetrievedChunk(content="QK7 note", filename="manual.md", chunk_index=0)
    retriever = cast(KnowledgeRetriever, _ThresholdRetriever((chunk,)))
    tool = KnowledgeSearchTool(retriever=retriever, knowledge_base_refs=("kb",))
    run, _cost = await run_agent(
        _KeepSearching(), tool, question="q", tenant_id=uuid4(), spend=Spend()
    )
    first = run["searches"][0]
    assert (first["rerank_outcome"], first["rerank_threshold_paused"]) == ("partial", True)
    plain = (
        await run_agent(_KeepSearching(), _tool(), question="q", tenant_id=uuid4(), spend=Spend())
    )[0]
    assert (
        plain["searches"][0]["rerank_outcome"],
        plain["searches"][0]["rerank_threshold_paused"],
    ) == ("ok", False)


@pytest.mark.asyncio
async def test_a_bad_tool_call_reaches_the_model_as_the_tools_node_error() -> None:
    class _NoQuery(_KeepSearching):
        async def agent(
            self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
        ) -> tuple[dict[str, Any], float]:
            if any(m["role"] == "tool" for m in messages):
                return {"role": "assistant", "content": "答完了"}, 0.0
            call = {"id": "c", "type": "function", "function": {"name": "x", "arguments": "{"}}
            return {"role": "assistant", "content": "", "tool_calls": [call]}, 0.0

    run, _cost = await run_agent(
        _NoQuery(), _tool(), question="q", tenant_id=uuid4(), spend=Spend()
    )
    assert run["searches"][0]["error"].startswith("[invalid args]")


@pytest.mark.asyncio
async def test_gold_passages_are_the_chunks_holding_the_anchor() -> None:
    from control_plane.knowledge.ingestion import ingest_markdown

    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    with rls_tenant(tenant):
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename="manual.md", content=None
        )
        count = await ingest_markdown(
            store=store,
            embedder=build_fake_embedder(),
            tenant_id=tenant,
            document_id=doc.id,
            kb_id=base.id,
            markdown=_DOC,
            chunk_max_tokens=base.chunk_max_tokens,
            chunk_overlap_tokens=base.chunk_overlap_tokens,
        )
        await store.set_document_status(
            tenant_id=tenant, document_id=doc.id, status=DocumentStatus.READY, chunk_count=count
        )
        gold = await gold_passages(
            store,
            tenant_id=tenant,
            kb_id=base.id,
            cases=[_case("a", answerable=True), _case("n", answerable=False)],
        )
    assert count > 1
    assert len(gold["a"]) == 1
    assert _ANCHOR in gold["a"][0]
    assert "活菌" not in gold["a"][0]  # 只给含锚文本的那段,不是整篇
    assert gold["n"] == []


# ---------------------------------------------------------------------------
# 预算暂停 / 续跑
# ---------------------------------------------------------------------------


class _Costly(FakeModels):
    """FakeModels' script, but every answer-model call costs ¥1 and a verdict ¥0.5."""

    answer_model = "costly"
    judge_model = "costly"

    async def agent(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], float]:
        message, _cost = await super().agent(messages, tools)
        return message, 1.0

    async def judge(self, prompt: str) -> tuple[str, float]:
        raw, _cost = await super().judge(prompt)
        return raw, 0.5


async def _run_four(
    tmp_path: Path, *, budget: float, control: str | None, todo: Sequence[tuple[str, KbCase]]
) -> tuple[int, Spend]:
    out = tmp_path / "agent.jsonl"
    gate = BudgetGate(budget=budget, control=tmp_path / "agent.jsonl.budget", poll_s=0.01)
    if control is not None:
        gate.control.write_text(control, encoding="utf-8")
    spend = Spend()
    written = await run_questions(
        todo=todo,
        models=_Costly(),
        tool=_tool(),
        tenant_id=uuid4(),
        gold={},
        cache=JudgeCache(tmp_path / "judge.sqlite"),
        out=out,
        gate=gate,
        spend=spend,
        concurrency=1,
    )
    return written, spend


def _four() -> list[tuple[str, KbCase]]:
    return [("main", _case(f"q{i}", answerable=True)) for i in range(4)]


@pytest.mark.asyncio
async def test_budget_pauses_keeps_what_was_done_and_a_rerun_resumes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 每题 ¥2.5:第 1、2 题开题时没到 ¥4,第 3 题开题时已花 ¥5 → 暂停;控制文件写着 stop → 收尾。
    written, spend = await _run_four(tmp_path, budget=4.0, control="stop", todo=_four())
    assert written == 2
    assert spend.answer_cny + spend.judge_cny == pytest.approx(5.0)
    assert "PAUSED" in capsys.readouterr().err
    out = tmp_path / "agent.jsonl"
    done = done_case_ids(out, answer_model="costly", judge_model="costly")
    assert done == {"q0", "q1"}
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [r["cost_cny"] for r in rows] == [{"answer": 2.0, "judge": 0.5}] * 2

    # 重跑同一个 --out:只做没做完的两题。
    todo = [(label, c) for label, c in _four() if c.id not in done]
    written, _spend = await _run_four(tmp_path, budget=100.0, control=None, todo=todo)
    assert written == 2
    done = done_case_ids(out, answer_model="costly", judge_model="costly")
    assert done == {"q0", "q1", "q2", "q3"}
    assert len(out.read_text(encoding="utf-8").splitlines()) == 4


@pytest.mark.asyncio
async def test_a_raised_budget_continues_in_the_same_process(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    written, _spend = await _run_four(tmp_path, budget=4.0, control="100", todo=_four())
    assert written == 4
    err = capsys.readouterr().err
    assert "PAUSED" in err
    assert "RESUMED: budget ¥100.00" in err


@pytest.mark.asyncio
async def test_rerank_spend_counts_toward_the_budget(tmp_path: Path) -> None:
    gate = BudgetGate(budget=50.0, control=tmp_path / "x.budget", poll_s=0.01)
    gate.control.write_text("stop", encoding="utf-8")
    written = await run_questions(
        todo=_four(),
        models=_Costly(),
        tool=_tool(),
        tenant_id=uuid4(),
        gold={},
        cache=JudgeCache(tmp_path / "judge.sqlite"),
        out=tmp_path / "agent.jsonl",
        gate=gate,
        spend=Spend(),
        other_cost=lambda: 60.0,
        concurrency=1,
    )
    assert written == 0


def test_judge_cache_is_keyed_by_judge_model_and_prompt(tmp_path: Path) -> None:
    cache = JudgeCache(tmp_path / "judge.sqlite")
    cache.put("deepseek-v4-pro", "prompt", '{"refused": true}')
    assert cache.get("deepseek-v4-pro", "prompt") == '{"refused": true}'
    assert cache.get("deepseek-v4-pro", "other prompt") is None
    assert cache.get("another-judge", "prompt") is None
    assert JudgeCache(tmp_path / "judge.sqlite").get("deepseek-v4-pro", "prompt") is not None


@pytest.mark.asyncio
async def test_a_cached_verdict_is_not_paid_for_again(tmp_path: Path) -> None:
    _written, first = await _run_four(tmp_path, budget=100.0, control=None, todo=_four())
    (tmp_path / "agent.jsonl").unlink()
    _written, second = await _run_four(tmp_path, budget=100.0, control=None, todo=_four())
    assert (first.judge_calls, first.judge_cny) == (4, pytest.approx(2.0))
    assert (second.judge_calls, second.judge_cny) == (0, 0.0)
    rows = (tmp_path / "agent.jsonl").read_text(encoding="utf-8").splitlines()
    assert {json.loads(r)["cost_cny"]["judge"] for r in rows} == {0.0}


@pytest.mark.asyncio
async def test_budget_gate_ignores_a_budget_not_above_what_was_spent(tmp_path: Path) -> None:
    gate = BudgetGate(budget=1.0, control=tmp_path / "x.budget", poll_s=0.01)

    async def operator() -> None:
        for value in ("2", "abc", "10"):
            while gate.control.exists():
                await asyncio.sleep(0.005)
            gate.control.write_text(value, encoding="utf-8")

    writer = asyncio.create_task(operator())
    assert await asyncio.wait_for(gate.wait_for_raise(lambda: 3.0), timeout=5) is True
    await asyncio.wait_for(writer, timeout=5)
    assert gate.budget == 10.0
    gate.control.write_text("stop", encoding="utf-8")
    assert await asyncio.wait_for(gate.wait_for_raise(lambda: 12.0), timeout=5) is False


def test_resume_refuses_a_file_written_by_other_models(tmp_path: Path) -> None:
    out = tmp_path / "agent.jsonl"
    row = {"case_id": "q", "answer_model": "glm-5.3", "judge_model": "deepseek-v4-pro"}
    out.write_text(json.dumps(row) + "\n" + json.dumps({"run": {}}) + "\n", encoding="utf-8")
    assert done_case_ids(out, answer_model="glm-5.3", judge_model="deepseek-v4-pro") == {"q"}
    with pytest.raises(SystemExit, match="another --out"):
        done_case_ids(out, answer_model="dry", judge_model="dry")


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------


def _row(case_id: str, label: str, answerable: bool, cat: str, **searches: Any) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "set": label,
        "answerable": answerable,
        "category": cat,
        "searches": [dict(searches)] if searches else [],
        "cost_cny": {"answer": 0.25, "judge": 0.1},
    }


def test_tally_counts_each_category_with_totals_and_spend(tmp_path: Path) -> None:
    out = tmp_path / "agent.jsonl"
    rows = [
        _row(
            "n1",
            "main",
            False,
            "正确说没写",
            status="ok",
            embedding_degraded=True,
            rerank_outcome="partial",
            rerank_threshold_paused=True,
        ),
        _row("n2", "chat", False, "编造", status="ok", degraded=True, rerank_outcome="ok"),
        _row("a1", "main", True, "答对", error="[tool error] x"),
        _row("a2", "main", True, "误拒", skipped=True),
        _row("a3", "chat", True, "编造"),
    ]
    run = {"run": {"spend_cny": {"answer": 1.25, "judge": 0.5, "rerank": 0.2}}}
    lines = [json.dumps(r, ensure_ascii=False) for r in [*rows, run]]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    text = tally(out).splitlines()
    assert text[0] == "## 资料没写"
    assert text[1] == "  all  n=2 正确说没写 1 编造 1 其它 0 判分失败 0"
    assert text[2] == "  main n=1 正确说没写 1 编造 0 其它 0 判分失败 0"
    assert text[3] == "  chat n=1 正确说没写 0 编造 1 其它 0 判分失败 0"
    assert text[4] == "## 有答案"
    assert text[5] == "  all  n=3 答对 1 误拒 1 编造 1 答偏 0 判分失败 0"
    assert text[6] == "  main n=2 答对 1 误拒 1 编造 0 答偏 0 判分失败 0"
    assert text[8] == "total n=5 正确说没写 1 答对 1 编造 2 误拒 1"
    assert text[9] == (
        "searches per question 0.80; degraded 1, embedding degraded 1, errors 1, skipped 1"
    )
    # B-206 K-7b — how each search's rerank ended, and its threshold left out.
    assert text[10] == "rerank outcomes ok 1, partial 1; threshold paused 1"
    assert text[11] == "spend ¥1.95 (answer ¥1.25, judge ¥0.50, rerank ¥0.20 over 1 run(s))"


# ---------------------------------------------------------------------------
# 空跑:不连网、不花钱、结果可续
# ---------------------------------------------------------------------------


@pytest.fixture
def network_attempts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Refuses (and records) every outbound connection: sockets and httpx."""
    attempts: list[str] = []

    def refuse(self: socket.socket, address: Any, *args: Any) -> Any:
        attempts.append(repr(address))
        raise OSError("network disabled in this test")

    async def refuse_send(self: httpx.AsyncClient, request: httpx.Request, **kwargs: Any) -> Any:
        attempts.append(str(request.url))
        raise httpx.ConnectError("network disabled in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse_send)
    return attempts


def _manifest_row(path: str) -> dict[str, str]:
    row = dict.fromkeys(MANIFEST_FIELDS, "")
    row.update(path=path, top_dir="docs", ext="md", size="100", tier="A", pages="0")
    return row


def _write_corpus(tmp_path: Path) -> tuple[Path, Path, Path]:
    """``(主集, 真实问法集, 资料根目录)``:一份文档,每个集 16 道有答案 + 2 道没答案。"""
    root = tmp_path / "corpus"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "manual.md").write_text(_DOC, encoding="utf-8")
    manifest = tmp_path / "manifest.csv"
    with manifest.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(MANIFEST_FIELDS))
        writer.writeheader()
        writer.writerow(_manifest_row("docs/manual.md"))
    paths = []
    for name, source in (("unit_kb", "faq_xlsx"), ("unit_kb_chat", "chat_qa")):
        cases: list[dict[str, Any]] = [
            {
                "id": f"{name}-a{i}",
                "query": f"益生菌要不要冷藏 {i}",
                "source": source,
                "expected": [{"document": "manual.md", "anchor": _ANCHOR, "page": None}],
                "answer": None,
            }
            for i in range(16)
        ]
        cases += [
            {
                "id": f"{name}-n{i}",
                "query": f"含多少咖啡因 {i}",
                "source": "no_answer",
                "expected": [],
            }
            for i in range(2)
        ]
        corpus = {"manifest": str(manifest), "tiers": ["A"]}
        path = tmp_path / f"{name}.yaml"
        path.write_text(
            yaml.safe_dump({"name": name, "corpus": corpus, "cases": cases}, allow_unicode=True),
            encoding="utf-8",
        )
        paths.append(path)
    return paths[0], paths[1], root


def _dry_argv(tmp_path: Path, *extra: str) -> list[str]:
    main_set, chat_set, root = _write_corpus(tmp_path)
    return [
        "--dataset",
        str(main_set),
        "--chat-dataset",
        str(chat_set),
        "--corpus-root",
        str(root),
        "--store",
        "memory",
        "--embedder",
        "fake",
        "--rerank",
        "off",
        "--out",
        str(tmp_path / "agent.jsonl"),
        "--judge-cache",
        str(tmp_path / "judge.sqlite"),
        "--dry",
        *extra,
    ]


def test_dry_run_makes_no_network_call_spends_nothing_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    network_attempts: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in (*ANSWER_ENV, *JUDGE_ENV):
        monkeypatch.delenv(name, raising=False)
    argv = _dry_argv(tmp_path)
    assert main(argv) == 0
    out = tmp_path / "agent.jsonl"
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    rows = [line for line in lines if "case_id" in line]
    runs = [line["run"] for line in lines if "run" in line]
    assert len(rows) == 2 * (2 + 15)
    assert len({r["case_id"] for r in rows}) == len(rows)
    assert {r["category"] for r in rows if r["answerable"]} == {"答偏"}
    assert {r["category"] for r in rows if not r["answerable"]} == {"其它"}
    assert all(r["searches"][0]["status"] == "ok" for r in rows)
    assert all(r["answer_model"] == r["judge_model"] == "dry" for r in rows)
    assert runs[0]["spend_cny"] == {"answer": 0.0, "judge": 0.0, "rerank": 0.0}
    assert "spend ¥0.00" in capsys.readouterr().out
    assert network_attempts == []
    assert not (tmp_path / "judge.sqlite").exists() or JudgeCache(
        tmp_path / "judge.sqlite"
    ).db.execute("SELECT COUNT(*) FROM j").fetchone() == (0,)

    # 删掉三题的结果再跑:只补这三题。
    kept = [line for line in lines if line.get("case_id") not in {r["case_id"] for r in rows[:3]}]
    out.write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in kept), encoding="utf-8"
    )
    assert main(argv) == 0
    rows_after = [
        json.loads(line)
        for line in out.read_text(encoding="utf-8").splitlines()
        if "case_id" in json.loads(line)
    ]
    assert sorted(r["case_id"] for r in rows_after) == sorted(r["case_id"] for r in rows)
    assert network_attempts == []


def test_rerank_arm_flags_reach_the_tools_retriever_and_the_run_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    network_attempts: list[str],
) -> None:
    """B-206 K-14 A/B through the agent layer: the flags set the retriever the
    ``knowledge_search`` tool searches with, and the run row says which arm it was."""
    built: list[KnowledgeRetriever] = []

    def _spy(**kwargs: Any) -> KnowledgeRetriever:
        retriever = cast(KnowledgeRetriever, build_retriever(**kwargs))
        built.append(retriever)
        return retriever

    monkeypatch.setattr("kb_agent_eval.build_retriever", _spy)
    for name in (*ANSWER_ENV, *JUDGE_ENV):
        monkeypatch.delenv(name, raising=False)
    argv = _dry_argv(
        tmp_path, "--limit", "1", "--rerank-view", "single_clean", "--rerank-pool", "60"
    )
    assert main(argv) == 0
    (retriever,) = built
    assert (retriever.rerank_view, retriever.rerank_pool, retriever.rerank_instruction) == (
        "single_clean",
        60,
        False,
    )
    lines = (tmp_path / "agent.jsonl").read_text(encoding="utf-8").splitlines()
    (run,) = [json.loads(line)["run"] for line in lines if "run" in json.loads(line)]
    assert run["rerank_arm"] == {
        "view": "single_clean",
        "pool": 60,
        "instruct": False,
        "model": None,
        "threshold": None,
    }
    assert network_attempts == []


class _OtherModelReranker:
    """Scores every passage 0.5, as another model than the base's threshold was set under."""

    model = "qwen3-vl-rerank"

    async def rerank(self, **kwargs: Any) -> list[int]:
        return [i for i, _ in await self.rerank_scored(**kwargs)]

    async def rerank_scored(
        self, *, documents: Sequence[str], top_k: int, **_: Any
    ) -> list[tuple[int, float | None]]:
        ranked = [(i, 0.5) for i in range(min(len(documents), top_k))]
        return RerankRanking(ranked, model="qwen/qwen3-vl-rerank")


def test_a_base_threshold_is_set_bound_and_each_search_says_how_it_ended(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    network_attempts: list[str],
) -> None:
    """B-206 K-7b — ``--rerank-threshold`` sets the base's own threshold, bound to
    the run's rerank model, so the agent layer meets it as production does; every
    search records its rerank outcome and whether the threshold was left out."""
    built: list[KnowledgeRetriever] = []

    def _spy(**kwargs: Any) -> KnowledgeRetriever:
        retriever = cast(KnowledgeRetriever, build_retriever(**kwargs))
        built.append(retriever)
        return retriever

    monkeypatch.setattr("kb_agent_eval.build_retriever", _spy)
    monkeypatch.setattr(
        "kb_agent_eval.build_rerank_mode",
        lambda mode, *, model=None: (_OtherModelReranker(), None),
    )
    monkeypatch.setenv("EXPERT_WORK_EVAL_EMBED_API_KEY", "offline-cache-only")
    for name in (*ANSWER_ENV, *JUDGE_ENV):
        monkeypatch.delenv(name, raising=False)
    argv = _dry_argv(
        tmp_path,
        "--limit",
        "1",
        "--rerank",
        "dashscope",
        "--rerank-model",
        "qwen3-rerank",
        "--rerank-threshold",
        "0.7",
    )
    assert main(argv) == 0
    (retriever,) = built
    (base,) = asyncio.run(
        cast(InMemoryKnowledgeStore, retriever.store).list_bases(tenant_id=EVAL_TENANT_ID)
    )
    assert (base.rerank_score_threshold, base.rerank_threshold_model) == (0.7, "qwen/qwen3-rerank")
    lines = [json.loads(line) for line in (tmp_path / "agent.jsonl").read_text().splitlines()]
    searches = [s for line in lines if "case_id" in line for s in line["searches"]]
    assert searches
    assert all(s["rerank_outcome"] == "ok" for s in searches)
    assert all(s["rerank_threshold_paused"] is True for s in searches)
    (run,) = [line["run"] for line in lines if "run" in line]
    assert run["rerank_arm"]["threshold"] == 0.7
    assert network_attempts == []


def test_a_base_threshold_needs_the_native_rerank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in (*ANSWER_ENV, *JUDGE_ENV):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit):
        main(_dry_argv(tmp_path, "--rerank-threshold", "0.7"))
    assert "--rerank-threshold needs --rerank dashscope" in capsys.readouterr().err


def test_rerank_model_reaches_the_reranker_and_the_run_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    network_attempts: list[str],
) -> None:
    """B-206 K-7b — ``--rerank-model`` (with ``--rerank dashscope``) picks the
    native rerank model, and the run row says which."""
    asked: list[tuple[str, str | None]] = []

    def _build(mode: str, *, model: str | None = None) -> tuple[None, None]:
        asked.append((mode, model))
        return None, None

    monkeypatch.setattr("kb_agent_eval.build_rerank_mode", _build)
    monkeypatch.setenv("EXPERT_WORK_EVAL_EMBED_API_KEY", "offline-cache-only")
    for name in (*ANSWER_ENV, *JUDGE_ENV):
        monkeypatch.delenv(name, raising=False)
    argv = _dry_argv(
        tmp_path, "--limit", "1", "--rerank", "dashscope", "--rerank-model", "qwen3-rerank"
    )
    assert main(argv) == 0
    assert asked == [("dashscope", "qwen3-rerank")]
    lines = (tmp_path / "agent.jsonl").read_text(encoding="utf-8").splitlines()
    (run,) = [json.loads(line)["run"] for line in lines if "run" in json.loads(line)]
    assert run["rerank_arm"]["model"] == "qwen3-rerank"
    assert network_attempts == []
    assert network_attempts == []


def test_an_instruction_with_the_dual_view_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Default view is dual, which sends no instruction: refused before any ingest.
    for name in (*ANSWER_ENV, *JUDGE_ENV):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit):
        main(_dry_argv(tmp_path, "--rerank-instruct"))
    assert "--rerank-instruct needs --rerank-view single" in capsys.readouterr().err
    assert not (tmp_path / "agent.jsonl").exists()


def test_real_run_needs_the_model_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in (*ANSWER_ENV, *JUDGE_ENV):
        monkeypatch.delenv(name, raising=False)
    argv = [a for a in _dry_argv(tmp_path) if a != "--dry"]
    with pytest.raises(SystemExit):
        main(argv)
    err = capsys.readouterr().err
    assert all(name in err for name in (*ANSWER_ENV, *JUDGE_ENV))
    assert not (tmp_path / "agent.jsonl").exists()


def test_dry_run_with_the_real_embedder_needs_the_offline_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EXPERT_WORK_EVAL_EMBED_API_KEY", "a-real-looking-key")
    argv = _dry_argv(tmp_path, "--embedder", "real")
    with pytest.raises(SystemExit):
        main(argv)
    assert "EXPERT_WORK_EVAL_EMBED_API_KEY=offline-cache-only" in capsys.readouterr().err
    assert not (tmp_path / "agent.jsonl").exists()


def test_tally_mode_reads_the_file_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "agent.jsonl"
    out.write_text(json.dumps(_row("n1", "main", False, "正确说没写"), ensure_ascii=False) + "\n")
    assert main(["--tally", str(out)]) == 0
    assert "  all  n=1 正确说没写 1" in capsys.readouterr().out
