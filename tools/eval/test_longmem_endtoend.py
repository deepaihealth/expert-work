"""P1 end-to-end tier smoke + runner plumbing — Stream CM-N5 (CI, no network).

A scripted LLM caller plays both production roles (extraction +
reading), keyed off the system prompt each call carries, so the full
ingest → retrieve → answer → judge pipeline runs deterministically
against the synthetic fixtures.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from longmem.adapter import load_longmemeval
from longmem.embedders import KeywordEmbedder
from longmem.endtoend import EndToEndConfig, QAResult, run_end_to_end
from longmem.judge import ScriptedTextJudge
from longmem.retrieval import AblationConfig
from longmem.runner import (
    append_result,
    load_results,
    merge_results,
    resolve_arms,
    summarise,
    update_baseline,
)

FIXTURES = Path(__file__).parent / "datasets" / "longmem_fixture"


class _ScriptedCaller:
    """Routes on the system prompt: extraction vs reading vs anything else."""

    def __init__(self) -> None:
        self.extraction_calls = 0
        self.answer_calls = 0

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        del tools
        system = next(
            (
                m.content
                for m in messages
                if isinstance(m, SystemMessage) and isinstance(m.content, str)
            ),
            "",
        )
        body = "\n".join(str(m.content) for m in messages)
        if "memory extraction module" in system:
            self.extraction_calls += 1
            if "Kyoto" in body:
                memories = [
                    {
                        "kind": "episodic",
                        "content": "User visited Kyoto for the cherry blossom festival",
                    }
                ]
            elif "carbonara" in body:
                memories = [{"kind": "fact", "content": "User cooks pasta carbonara"}]
            elif "expert_work" in body:
                memories = [{"kind": "fact", "content": "User's favorite editor is expert_work"}]
            elif "vim" in body:
                memories = [{"kind": "fact", "content": "User's favorite editor was vim"}]
            elif "ferns" in body:
                memories = [{"kind": "episodic", "content": "User watered the garden ferns"}]
            else:
                memories = []
            return AIMessage(content=json.dumps({"memories": memories}))
        if "answering a question about a user" in system:
            self.answer_calls += 1
            if "What city" in body:
                return AIMessage(content="The user visited Kyoto.")
            if "favorite editor" in body:
                return AIMessage(content="Their favorite editor is expert_work.")
            return AIMessage(content="The information is not available.")
        # Reconcile (or any other) call — deliberately unparseable so the
        # CM-7 degrade-to-direct-ADD path is what gets exercised.
        return AIMessage(content="unparseable")


@pytest.mark.asyncio
async def test_end_to_end_fixture_pipeline() -> None:
    instances = load_longmemeval(FIXTURES / "longmemeval_mini.json")
    caller = _ScriptedCaller()
    judge = ScriptedTextJudge(
        {"The user visited Kyoto.": "yes", "Their favorite editor is expert_work.": "yes"}
    )
    report = await run_end_to_end(
        instances,
        benchmark="longmemeval",
        embedder=KeywordEmbedder(),
        llm_caller=caller,
        judge=judge,
        config=EndToEndConfig(reconcile=False),
    )
    assert report.n_instances == 2
    assert report.accuracy == 1.0
    # B-300 PR 1 —— 每段会话按角色拆开:用户的话一次(4 段),有助手回答的再一次(2 段)。
    assert caller.extraction_calls == 6
    assert caller.answer_calls == 2
    assert report.memories_written == 4
    assert report.by_question_type["knowledge-update"]["accuracy"] == 1.0


@pytest.mark.asyncio
async def test_end_to_end_reconcile_degrades_to_add() -> None:
    """reconcile=True with an unparseable reconcile reply must still
    write memories (CM-7: every reconcile failure degrades to ADD)."""
    instances = [
        i
        for i in load_longmemeval(FIXTURES / "longmemeval_mini.json")
        if i.question_id == "fixture-editor-2"
    ]
    caller = _ScriptedCaller()
    report = await run_end_to_end(
        instances,
        benchmark="longmemeval",
        embedder=KeywordEmbedder(),
        llm_caller=caller,
        judge=ScriptedTextJudge({"expert_work": "yes"}),
        config=EndToEndConfig(reconcile=True),
    )
    assert report.memories_written == 2
    assert report.accuracy == 1.0


@pytest.mark.asyncio
async def test_end_to_end_resume_skips_done_questions() -> None:
    instances = load_longmemeval(FIXTURES / "longmemeval_mini.json")
    caller = _ScriptedCaller()
    report = await run_end_to_end(
        instances,
        benchmark="longmemeval",
        embedder=KeywordEmbedder(),
        llm_caller=caller,
        judge=ScriptedTextJudge({}, default="yes"),
        config=EndToEndConfig(reconcile=False),
        done_ids=frozenset({"fixture-city-1"}),
    )
    # Only the editor question runs — and only its corpus is ingested.
    assert report.n_instances == 1
    assert report.results[0].question_id == "fixture-editor-2"
    assert caller.extraction_calls == 2


@pytest.mark.asyncio
async def test_abstention_judged_with_abstention_template() -> None:
    instances = [
        i
        for i in load_longmemeval(FIXTURES / "longmemeval_mini.json", include_abstention=True)
        if i.question_id.endswith("_abs")
    ]
    caller = _ScriptedCaller()
    seen_prompts: list[str] = []

    class _SpyJudge:
        async def complete(self, *, prompt: str) -> str:
            seen_prompts.append(prompt)
            return "yes"

    report = await run_end_to_end(
        instances,
        benchmark="longmemeval",
        embedder=KeywordEmbedder(),
        llm_caller=caller,
        judge=_SpyJudge(),
        config=EndToEndConfig(reconcile=False),
    )
    assert report.accuracy == 1.0
    assert "unanswerable" in seen_prompts[0]


# ---------------------------------------------------------------------------
# runner plumbing — resume / merge / baseline
# ---------------------------------------------------------------------------


def _qa(qid: str, *, correct: bool) -> QAResult:
    return QAResult(
        question_id=qid, question_type="t", hypothesis="h", correct=correct, n_memories=3
    )


def test_results_jsonl_round_trip_and_torn_line(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    append_result(path, _qa("q1", correct=True))
    append_result(path, _qa("q2", correct=False))
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"question_id": "q3", "torn')  # killed mid-write
    loaded = load_results(path)
    assert [r.question_id for r in loaded] == ["q1", "q2"]
    assert loaded[0].correct is True


def test_merge_results_fresh_wins() -> None:
    merged = merge_results([_qa("q1", correct=False)], [_qa("q1", correct=True)])
    assert len(merged) == 1
    assert merged[0].correct is True


def test_summarise_breakdown() -> None:
    summary = summarise([_qa("q1", correct=True), _qa("q2", correct=False)])
    assert summary["n_instances"] == 2
    assert summary["accuracy"] == 0.5
    assert summary["by_question_type"]["t"]["n"] == 2.0


def test_resolve_arms() -> None:
    arms = resolve_arms("default,no_decay")
    assert arms["no_decay"].decay is False
    assert resolve_arms("all").keys() >= {"default", "vector", "no_decay", "no_mmr", "rerank"}
    assert isinstance(arms["default"], AblationConfig)
    with pytest.raises(SystemExit):
        resolve_arms("bogus")


def test_update_baseline_merges_sections(tmp_path: Path) -> None:
    import yaml

    path = tmp_path / "baseline.yaml"
    update_baseline(
        path,
        tier="retrieval",
        benchmark="fixture_longmemeval",
        section={"default": {"mean_mrr": 1.0}},
        fingerprint={"embedder": "fake", "commit": "abc"},
    )
    update_baseline(
        path,
        tier="endtoend",
        benchmark="fixture_longmemeval",
        section={"accuracy": 1.0},
        fingerprint={"judge_model": "haiku"},
    )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["retrieval"]["fixture_longmemeval"]["default"]["mean_mrr"] == 1.0
    assert data["endtoend"]["fixture_longmemeval"]["accuracy"] == 1.0
    assert data["meta"]["fingerprints"]["retrieval/fixture_longmemeval"]["embedder"] == "fake"
    assert "updated_at" in data["meta"]


def test_stratified_sample_deterministic_and_covers_types() -> None:
    """S end-to-end re-scoped to a 150-question stratified sample
    (2026-06-10) — type coverage and reproducibility are the contract."""
    from longmem.runner import stratified_sample

    # QAResult doubles as a cheap carrier with a question_type field.
    population = []
    for i in range(60):
        population.append(
            QAResult(
                question_id=f"a{i}",
                question_type="alpha",
                hypothesis="",
                correct=True,
                n_memories=0,
            )
        )
    for i in range(30):
        population.append(
            QAResult(
                question_id=f"b{i}", question_type="beta", hypothesis="", correct=True, n_memories=0
            )
        )
    for i in range(10):
        population.append(
            QAResult(
                question_id=f"c{i}",
                question_type="gamma",
                hypothesis="",
                correct=True,
                n_memories=0,
            )
        )
    sample = stratified_sample(population, 20)
    assert len(sample) == 20
    types = {r.question_type for r in sample}
    assert types == {"alpha", "beta", "gamma"}  # every type represented
    # Proportions roughly preserved (60/30/10 -> ~12/6/2).
    from collections import Counter

    counts = Counter(r.question_type for r in sample)
    assert counts["alpha"] > counts["beta"] > counts["gamma"] >= 1
    # Deterministic across calls.
    again = stratified_sample(population, 20)
    assert [r.question_id for r in again] == [r.question_id for r in sample]
    # n >= len passes through untouched.
    assert stratified_sample(population, 200) is population


def test_user_text_kwargs_declares_the_corpus_as_user_text() -> None:
    """B-300 PR 1:入口有显式参数就把语料声明为用户文字;没有(改前的提交)就什么都不传。"""
    from langchain_core.messages import HumanMessage
    from longmem.endtoend import USER_TEXT_PARAM, user_text_kwargs

    session = HumanMessage(content="[Conversation session]\nhello")

    async def new_entry(messages: Any, *, user_text_messages: Any = None) -> int:
        return 0

    async def old_entry(messages: Any, *, reconcile: bool = False) -> int:
        return 0

    assert user_text_kwargs(new_entry, [session]) == {USER_TEXT_PARAM: [session]}
    assert user_text_kwargs(old_entry, [session]) == {}
    # B-300 的跑法要求有这个入口:没有就报错,不悄悄跳过(审查 M2)。
    from longmem.endtoend import MissingUserTextEntryError

    with pytest.raises(MissingUserTextEntryError, match=USER_TEXT_PARAM):
        user_text_kwargs(old_entry, [session], required=True)
    assert user_text_kwargs(new_entry, [session], required=True) == {USER_TEXT_PARAM: [session]}


@pytest.mark.asyncio
async def test_ingest_passes_the_explicit_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_ingest_corpus`` 真的把语料经显式参数交给提炼入口(参数存在时)。"""
    import asyncio

    from longmem import endtoend

    import orchestrator.graph_builder.memory as memory_mod
    from expert_work.persistence import InMemoryMemoryStore
    from expert_work.runtime.cancellation import CancellationToken

    seen: list[dict[str, Any]] = []

    async def fake_flush(messages: Any, *, user_text_messages: Any = None, **kw: Any) -> int:
        seen.append({"messages": list(messages), "user_text": user_text_messages})
        return 1

    monkeypatch.setattr(memory_mod, "flush_messages_to_memory", fake_flush)
    instances = load_longmemeval(FIXTURES / "longmemeval_mini.json")
    written = await endtoend._ingest_corpus(
        instances[0].docs,
        store=InMemoryMemoryStore(),
        embedder=KeywordEmbedder(),
        llm_caller=None,
        reconcile=False,
        token=CancellationToken(),
        sem=asyncio.Semaphore(1),
    )
    assert written == len(seen) > 0
    # 声明为用户文字的正好是这段会话里的用户消息(日期抬头 + 用户的每一轮),助手的回答不在里面。
    for row in seen:
        humans = [m for m in row["messages"] if isinstance(m, HumanMessage)]
        assert row["user_text"] == humans
        assert any(isinstance(m, AIMessage) for m in row["messages"]) or len(humans) >= 2


def test_session_messages_split_by_role() -> None:
    """B-300 PR 1 —— 一段会话拆成生产的形状:用户的每一轮一条 HumanMessage(经显式参数声明为用户
    文字),助手的每一轮一条 AIMessage;日期抬头单独一条用户消息。LoCoMo 两个说话人都是人,保留名字。"""
    from datetime import datetime

    from longmem.adapter import MemoryDoc
    from longmem.endtoend import _session_messages

    def doc(content: str) -> MemoryDoc:
        return MemoryDoc(doc_id=content[:8], session_id="s", content=content, timestamp=None)

    when = datetime(2023, 4, 5, 19, 30)
    out = _session_messages(
        [doc("user: I moved to Berlin"), doc("assistant: Nice!"), doc("user: It is cold")], when
    )
    assert [(type(m).__name__, m.content) for m in out] == [
        ("HumanMessage", "[Conversation session dated 2023-04-05 19:30]"),
        ("HumanMessage", "I moved to Berlin"),
        ("AIMessage", "Nice!"),
        ("HumanMessage", "It is cold"),
    ]
    # LongMemEval 的 session 粒度:一个 doc 里是整段会话,按行首的角色切开(续行归上一轮)。
    joined = _session_messages([doc("user: a\nmore of a\nassistant: b\nuser: c")], None)
    assert [(type(m).__name__, m.content) for m in joined] == [
        ("HumanMessage", "[Conversation session]"),
        ("HumanMessage", "a\nmore of a"),
        ("AIMessage", "b"),
        ("HumanMessage", "c"),
    ]
    locomo = _session_messages([doc("Caroline: I went hiking"), doc("Melanie: Where?")], None)
    assert [m.content for m in locomo[1:]] == ["Caroline: I went hiking", "Melanie: Where?"]
    assert all(isinstance(m, HumanMessage) for m in locomo)


@pytest.mark.asyncio
async def test_ingest_through_pr1_extracts_user_turns_only() -> None:
    """真走生产入口:第一次提炼看到的是用户的话、看不到助手的回答;写进库里。"""
    import asyncio

    from longmem import endtoend

    from expert_work.persistence import InMemoryMemoryStore
    from expert_work.runtime.cancellation import CancellationToken

    first_inputs: list[str] = []

    class _Spy(_ScriptedCaller):
        async def __call__(
            self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]
        ) -> AIMessage:
            if "The other side of the conversation is not shown." in str(messages[0].content):
                first_inputs.append(str(messages[1].content))
            return await super().__call__(messages=messages, tools=tools)

    instances = load_longmemeval(FIXTURES / "longmemeval_mini.json")
    written = await endtoend._ingest_corpus(
        instances[0].docs,
        store=InMemoryMemoryStore(),
        embedder=KeywordEmbedder(),
        llm_caller=_Spy(),
        reconcile=False,
        token=CancellationToken(),
        sem=asyncio.Semaphore(1),
        expect_user_text=True,
    )
    assert written == 2
    assert len(first_inputs) == 2
    assert all(
        "wonderful trip" not in text and "classic Roman" not in text for text in first_inputs
    )
    assert any("Kyoto" in text for text in first_inputs)


@pytest.mark.asyncio
async def test_a_corpus_that_writes_nothing_raises() -> None:
    """一整段语料一条记忆都没写(入口没提炼、模型全回空)不能悄悄去打分:报错。"""
    import asyncio

    from longmem import endtoend

    from expert_work.persistence import InMemoryMemoryStore
    from expert_work.runtime.cancellation import CancellationToken

    class _Empty:
        async def __call__(self, *, messages: Any, tools: Any) -> AIMessage:
            del messages, tools
            return AIMessage(content=json.dumps({"memories": []}))

    instances = load_longmemeval(FIXTURES / "longmemeval_mini.json")
    with pytest.raises(endtoend.NothingIngestedError):
        await endtoend._ingest_corpus(
            instances[0].docs,
            store=InMemoryMemoryStore(),
            embedder=KeywordEmbedder(),
            llm_caller=_Empty(),
            reconcile=False,
            token=CancellationToken(),
            sem=asyncio.Semaphore(1),
        )
