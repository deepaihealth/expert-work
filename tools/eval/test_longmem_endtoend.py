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
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
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
    # One extraction call per session (2 + 2), one answer per question.
    assert caller.extraction_calls == 4
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
    assert all(row["user_text"] == row["messages"] for row in seen)
