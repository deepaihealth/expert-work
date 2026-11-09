"""B-206 11-09 尺子修正:评测不加「用手上的答」这类压力;每题判几票取多数。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from kb_agent_eval import (  # type: ignore[import-not-found]
    READ_LIMIT_NOTE,
    SEARCH_LIMIT_NOTE,
    BudgetGate,
    Spend,
    run_questions,
)
from kb_agent_judge import JudgeCache  # type: ignore[import-not-found]
from kb_dataset import KbCase, KbExpected  # type: ignore[import-not-found]

from orchestrator.tools.registry import ToolContext, ToolResult, ToolSpec


@pytest.mark.parametrize("note", [SEARCH_LIMIT_NOTE, READ_LIMIT_NOTE])
def test_limit_notes_state_the_cap_without_pushing_the_model_to_answer(note: str) -> None:
    lowered = note.lower()
    assert "limit" in lowered
    # 11-05 记录:旧措辞「answer with what you have」把模型往「有就答」推,生产里没有这句。
    for pushy in ("answer", "what you have", "anyway", "best you can", "回答", "作答"):
        assert pushy not in lowered, pushy


# ---------------------------------------------------------------------------
# 多数票接进跑题流程
# ---------------------------------------------------------------------------

_FACT = "注意与中西药间隔30分钟食用"


class _OneResultTool:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="knowledge_search", description="search", parameters={})

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        return ToolResult(content=f"[D1#1] 说明书:{_FACT}。", meta={"status": "ok"})


def _verdict(support: str) -> str:
    claims = [{"claim": "c", "support": support, "quote": _FACT}]
    return json.dumps({"claims": claims, "refused": True, "matches_reference": "n/a"})


class _FlipFlopJudge:
    """Answers once after one search; the judge alternates as recorded on 11-05 (0006)."""

    answer_model = "scripted"
    judge_model = "scripted"

    def __init__(self, verdicts: list[str]) -> None:
        self.verdicts = verdicts
        self.judge_calls = 0

    async def agent(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], float]:
        if not any(m["role"] == "tool" for m in messages):
            fn = {"name": "knowledge_search", "arguments": json.dumps({"query": "q"})}
            call = {"id": "c0", "type": "function", "function": fn}
            return {"role": "assistant", "content": "", "tool_calls": [call]}, 0.0
        return {"role": "assistant", "content": f"资料没直接写;只写了{_FACT}。"}, 0.0

    async def judge(self, prompt: str) -> tuple[str, float]:
        raw = self.verdicts[self.judge_calls % len(self.verdicts)]
        self.judge_calls += 1
        return raw, 0.1


def _partial_case() -> KbCase:
    related = (KbExpected(document="说明书.docx", anchor=_FACT, page=None),)
    return KbCase(
        id="chat_no_answer-0008",
        query="益生菌和降压药一起吃有影响吗?",
        source="no_answer",
        tags=(),
        expected=(),
        answer=None,
        label="partial",
        related=related,
    )


async def _run(tmp_path: Path, models: _FlipFlopJudge, votes: int) -> list[dict[str, Any]]:
    out = tmp_path / "agent.jsonl"
    await run_questions(
        todo=[("chat", _partial_case())],
        models=models,
        tool=_OneResultTool(),
        tenant_id=uuid4(),
        gold={},
        cache=JudgeCache(tmp_path / "judge.sqlite"),
        out=out,
        gate=BudgetGate(budget=100.0, control=tmp_path / "x.budget"),
        spend=Spend(),
        concurrency=1,
        votes=votes,
    )
    return [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_three_votes_outvote_a_single_flipped_judgement(tmp_path: Path) -> None:
    models = _FlipFlopJudge([_verdict("inferred_unlabeled"), _verdict("stated")])
    [row] = await _run(tmp_path, models, votes=3)
    assert models.judge_calls == 3
    # 票:编造 / B 式 / 编造 → 编造 2/3;同一回答只判一次时,结论随第一票翻
    assert row["votes"] == ["编造", "B式作答", "编造"]
    assert (row["category"], row["agreement"], row["label"]) == ("编造", "2/3", "partial")
    assert row["verdict"]["fabricated"] is True
    assert row["cost_cny"]["judge"] == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_each_vote_is_cached_on_its_own(tmp_path: Path) -> None:
    first = _FlipFlopJudge([_verdict("stated")])
    await _run(tmp_path, first, votes=3)
    (tmp_path / "agent.jsonl").unlink()
    again = _FlipFlopJudge([_verdict("unsupported")])
    [row] = await _run(tmp_path, again, votes=3)
    assert again.judge_calls == 0
    assert row["category"] == "B式作答" and row["agreement"] == "3/3"
