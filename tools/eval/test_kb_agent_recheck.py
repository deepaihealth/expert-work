"""kb_agent_recheck.py:难负例入库后、答题前的「答案还在不在库里」复核(B-206 11-10)。
全部离线:检索是替身工具,判分模型是脚本。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from kb_agent_cost import Spend  # type: ignore[import-not-found]
from kb_agent_judge import EXCLUDED, JudgeCache  # type: ignore[import-not-found]
from kb_agent_recheck import (  # type: ignore[import-not-found]
    DEFAULT_RECHECK_K,
    excluded_row,
    parse_recheck,
    recheck_hard_negatives,
    recheck_prompt,
    recheck_references,
)
from kb_dataset import KbCase  # type: ignore[import-not-found]

from orchestrator.tools.registry import ToolContext, ToolResult, ToolSpec


def _hn(case_id: str, query: str) -> KbCase:
    return KbCase(
        id=case_id,
        query=query,
        source="hard_negative",
        tags=(),
        expected=(),
        answer=None,
        label="no_answer",
    )


class _Search:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="knowledge_search", description="s", parameters={})

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        self.calls.append(dict(args))
        return ToolResult(content=f"[D1#1] 关于「{args['query']}」的段落", meta={"status": "ok"})


class _Judge:
    judge_model = "scripted"
    answer_model = "scripted"

    def __init__(self, replies: Mapping[str, list[str]]) -> None:
        self.replies = {k: list(v) for k, v in replies.items()}
        self.prompts: list[str] = []

    async def judge(self, prompt: str) -> tuple[str, float]:
        self.prompts.append(prompt)
        for question, queue in self.replies.items():
            if question in prompt:
                return queue.pop(0) if len(queue) > 1 else queue[0], 0.05
        raise AssertionError("unexpected prompt")


def _yes(flag: bool) -> str:
    return json.dumps({"answerable": flag, "reason": "r"})


def test_the_prompt_carries_question_reference_and_passages() -> None:
    prompt = recheck_prompt("甲款多久换滤芯?", "每6个月", "[D1#1] 滤芯说明")
    assert "甲款多久换滤芯?" in prompt and "每6个月" in prompt and "[D1#1] 滤芯说明" in prompt
    assert '"answerable"' in prompt


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (_yes(True), True),
        (_yes(False), False),
        ('前言 {"answerable": true} 后记', True),
        ('{"answerable": "yes"}', None),
        ("not json", None),
    ],
)
def test_parse_recheck(raw: str, expected: bool | None) -> None:
    assert parse_recheck(raw) is expected


async def _recheck(tmp_path: Path, judge: _Judge, search: _Search, **kw: Any) -> dict[str, Any]:
    cases = [_hn("hn-a", "甲款多久换滤芯?"), _hn("hn-b", "乙款多重?")]
    spend = Spend()
    result: dict[str, Any] = await recheck_hard_negatives(
        cases=cases,
        search=search,
        tenant_id=uuid4(),
        models=judge,
        cache=JudgeCache(tmp_path / "j.sqlite"),
        references={"hn-a": "每6个月", "hn-b": "5斤"},
        spend=spend,
        **kw,
    )
    result["spend"] = spend
    return result


@pytest.mark.asyncio
async def test_a_majority_of_answerable_votes_excludes_the_case(tmp_path: Path) -> None:
    judge = _Judge(
        {
            "甲款": [_yes(True), _yes(False), _yes(True)],
            "乙款": [_yes(False), _yes(True), _yes(False)],
        }
    )
    search = _Search()
    result = await _recheck(tmp_path, judge, search, votes=3)
    assert result["hn-a"]["excluded"] is True and result["hn-a"]["votes"] == [True, False, True]
    assert result["hn-b"]["excluded"] is False
    # 与答题同一条检索,取前 k 段(默认 20)
    assert search.calls == [
        {"query": "甲款多久换滤芯?", "limit": DEFAULT_RECHECK_K},
        {"query": "乙款多重?", "limit": DEFAULT_RECHECK_K},
    ]
    assert "关于「甲款多久换滤芯?」的段落" in judge.prompts[0]
    assert result["spend"].judge_calls == 6
    assert result["hn-a"]["cost_cny"] == pytest.approx(0.15)


@pytest.mark.asyncio
async def test_k_is_passed_to_the_search_and_votes_are_cached(tmp_path: Path) -> None:
    judge = _Judge({"甲款": [_yes(True)], "乙款": [_yes(False)]})
    await _recheck(tmp_path, judge, _Search(), votes=1, k=5)
    again = _Judge({"甲款": [_yes(False)], "乙款": [_yes(True)]})
    search = _Search()
    result = await _recheck(tmp_path, again, search, votes=1, k=5)
    assert {c["limit"] for c in search.calls} == {5}
    assert again.prompts == []  # 同样的提示词走缓存,不再花钱
    assert result["hn-a"]["excluded"] is True and result["hn-b"]["excluded"] is False


@pytest.mark.asyncio
async def test_an_unparsable_reply_keeps_the_case_and_is_not_cached(tmp_path: Path) -> None:
    judge = _Judge({"甲款": ["not json"], "乙款": [_yes(False)]})
    result = await _recheck(tmp_path, judge, _Search(), votes=1)
    assert result["hn-a"]["excluded"] is False and result["hn-a"]["votes"] == [None]
    fixed = _Judge({"甲款": [_yes(True)], "乙款": [_yes(False)]})
    result = await _recheck(tmp_path, fixed, _Search(), votes=1)
    assert result["hn-a"]["excluded"] is True


def test_the_excluded_row_stays_out_of_scoring_but_keeps_its_cost() -> None:
    row = excluded_row(
        "hard_negative",
        _hn("hn-a", "甲款多久换滤芯?"),
        {"excluded": True, "votes": [True, True, False], "cost_cny": 0.15, "k": 20},
        meta={"answer_model": "m", "judge_model": "j", "yardstick": "y"},
    )
    assert row["category"] == EXCLUDED and row["agreement"] == "2/3"
    assert row["searches"] == [] and row["reads"] == [] and row["answer"] is None
    assert row["cost_cny"] == {"answer": 0.0, "judge": 0.15}
    assert (row["answer_model"], row["judge_model"], row["yardstick"]) == ("m", "j", "y")


def test_references_join_the_original_answer_and_the_deleted_text() -> None:
    refs = recheck_references(
        {"hn-a": ("滤芯每6个月更换",), "hn-b": ("乙款重5斤",)}, {"hn-a": "半年换一次"}
    )
    assert refs == {"hn-a": "半年换一次\n原文:滤芯每6个月更换", "hn-b": "原文:乙款重5斤"}


class _BrokenSearch(_Search):
    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        raise RuntimeError("search backend down")


@pytest.mark.asyncio
async def test_a_failed_search_is_recorded_and_not_judged(tmp_path: Path) -> None:
    judge = _Judge({"甲款": [_yes(True)], "乙款": [_yes(True)]})
    result = await _recheck(tmp_path, judge, _BrokenSearch(), votes=1)
    assert judge.prompts == []
    assert result["hn-a"]["search_error"] is True and result["hn-a"]["excluded"] is False


@pytest.mark.asyncio
async def test_the_recheck_stops_when_admission_is_refused(tmp_path: Path) -> None:
    judge = _Judge({"甲款": [_yes(True)], "乙款": [_yes(True)]})
    seen: list[int] = []

    async def admit() -> bool:
        seen.append(1)
        return len(seen) < 2  # 第一题放行,第二题停

    result = await _recheck(tmp_path, judge, _Search(), votes=1, admit=admit)
    assert sorted(k for k in result if k.startswith("hn-")) == ["hn-a"]
