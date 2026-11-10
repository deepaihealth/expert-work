"""kb_agent_rejudge.py:用新尺子只重判已有结果、不重新答题(B-206 11-10)。全部离线。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import kb_agent_eval  # type: ignore[import-not-found]
import kb_agent_rejudge  # type: ignore[import-not-found]
import pytest
from kb_agent_cost import Spend  # type: ignore[import-not-found]
from kb_agent_judge import EXCLUDED, JudgeCache, judge_prompt  # type: ignore[import-not-found]
from kb_agent_rejudge import (  # type: ignore[import-not-found]
    YARDSTICK,
    check_spec,
    done_ids,
    rejudge_rows,
    rows_gold,
)
from kb_dataset import KbCase  # type: ignore[import-not-found]
from test_kb_agent_derived_sets import _GONE, _corpus, _spec  # type: ignore[import-not-found]

_OLD = "2026-11-09.3+902042cf"


@pytest.fixture(autouse=True)
def _no_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*kb_agent_eval.ANSWER_ENV, *kb_agent_eval.JUDGE_ENV):
        monkeypatch.delenv(name, raising=False)


class _Judge:
    judge_model = "scripted"

    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = json.dumps(reply)
        self.prompts: list[str] = []

    async def judge(self, prompt: str) -> tuple[str, float]:
        self.prompts.append(prompt)
        return self.reply, 0.1


def _case() -> KbCase:
    return KbCase(
        id="n1",
        query="甲款能和Z药一起吃吗?",
        source="no_answer",
        tags=(),
        expected=(),
        answer=None,
        label="no_answer",
    )


def _old_row(**kw: Any) -> dict[str, Any]:
    return {
        "case_id": "n1",
        "set": "chat",
        "answerable": False,
        "label": "no_answer",
        "question": "甲款能和Z药一起吃吗?",
        "answer": "资料没写能不能一起吃;空腹喝效果更好。",
        "searches": [{"query": "q", "content": "检索段落甲"}],
        "reads": [{"args": {}, "content": "读到的段落乙"}],
        "verdict": {"fabricated": True},
        "votes": ["编造", "编造", "编造"],
        "agreement": "3/3",
        "category": "编造",
        "answer_model": "glm-5.3",
        "judge_model": "deepseek-v4-pro",
        "yardstick": _OLD,
        "judge_votes": 3,
        "run_kind": "standard",
        "cost_cny": {"answer": 0.3, "judge": 0.2},
        **kw,
    }


_SIDE = {
    "claims": [{"claim": "空腹喝效果更好", "support": "unsupported", "core": False, "quote": ""}],
    "refused": True,
    "matches_reference": "n/a",
    "reason": "r",
}


@pytest.mark.asyncio
async def test_a_row_is_rejudged_from_its_stored_answer_and_passages(tmp_path: Path) -> None:
    out = tmp_path / "new.jsonl"
    judge = _Judge(_SIDE)
    written = await rejudge_rows(
        [_old_row()],
        cases={"n1": _case()},
        notes={"n1": "补充说明X"},
        deleted={},
        gold={},
        models=judge,
        cache=JudgeCache(tmp_path / "j.sqlite"),
        out=out,
        votes=3,
        spend=Spend(),
        source="old.jsonl",
    )
    assert written == 1
    # 评委看到的与原来一样拼:问题 + searches/reads 原文 + 回答 + 参考信息与补充说明
    run = {
        "answer": _old_row()["answer"],
        "searches": _old_row()["searches"],
        "reads": _old_row()["reads"],
    }
    assert judge.prompts == [judge_prompt(_case(), run, {}, "补充说明X")] * 3
    assert "检索段落甲\n\n=====\n\n读到的段落乙" in judge.prompts[0]
    [row] = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert row["category"] == "夹带推断"  # 旧尺子一票否决记「编造」,新尺子只算夹带推断
    assert (row["yardstick"], row["judge_votes"], row["judge_model"]) == (YARDSTICK, 3, "scripted")
    assert row["rejudged_from"] == {
        "file": "old.jsonl",
        "yardstick": _OLD,
        "category": "编造",
        "cost_cny": {"answer": 0.3, "judge": 0.2},
    }
    assert row["cost_cny"] == {"answer": 0.0, "judge": pytest.approx(0.3)}
    assert row["answer"] == _old_row()["answer"] and row["answer_model"] == "glm-5.3"


@pytest.mark.asyncio
async def test_a_recheck_excluded_row_is_carried_over_without_judging(tmp_path: Path) -> None:
    out = tmp_path / "new.jsonl"
    judge = _Judge(_SIDE)
    excluded = _old_row(category=EXCLUDED, answer=None, searches=[], reads=[])
    await rejudge_rows(
        [excluded],
        cases={},
        notes={},
        deleted={},
        gold={},
        models=judge,
        cache=JudgeCache(tmp_path / "j.sqlite"),
        out=out,
        votes=3,
        spend=Spend(),
        source="old.jsonl",
    )
    [row] = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert judge.prompts == [] and row["category"] == EXCLUDED and row["yardstick"] == YARDSTICK


def test_an_output_judged_with_another_yardstick_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "new.jsonl"
    out.write_text(json.dumps(_old_row()) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="yardstick"):
        done_ids(out)
    out.write_text(json.dumps(_old_row(yardstick=YARDSTICK)) + "\n", encoding="utf-8")
    assert done_ids(out) == {"n1"}


def test_the_hard_negative_spec_must_be_the_one_the_source_ran_with(tmp_path: Path) -> None:
    spec = tmp_path / "hn.yaml"
    spec.write_text("cases: []\n", encoding="utf-8")
    import hashlib

    digest = hashlib.sha256(spec.read_bytes()).hexdigest()[:12]
    check_spec(f"hn:{digest}", spec)
    check_spec(f"hn:{digest}:recheck20", spec)
    with pytest.raises(SystemExit, match="same spec"):
        check_spec("hn:0123456789ab", spec)
    with pytest.raises(SystemExit, match="--hard-negatives"):
        check_spec(f"hn:{digest}", None)
    with pytest.raises(SystemExit, match="standard run"):
        check_spec("standard", spec)


def test_rows_without_gold_need_a_rebuild() -> None:
    answerable = _old_row(case_id="a1", label="answerable", answerable=True)
    assert rows_gold([_old_row(), answerable]) is None
    assert rows_gold([_old_row(gold=[]), {**answerable, "gold": ["段落"]}]) == {
        "n1": [],
        "a1": ["段落"],
    }


# ---------------------------------------------------------------------------
# 命令行:空跑出一份结果,当成旧文件重判
# ---------------------------------------------------------------------------


def _as_old(path: Path) -> None:
    """把空跑结果改成 11-10 之前的样子:旧尺子、没有 gold。"""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        if "case_id" in item:
            item = {k: v for k, v in item.items() if k != "gold"} | {"yardstick": _OLD}
        lines.append(json.dumps(item, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _rejudge_argv(argv: list[str], out: Path) -> list[str]:
    keep = {"--dataset", "--chat-dataset", "--corpus-root"}
    picked = [x for i, x in enumerate(argv) if x in keep or (i and argv[i - 1] in keep)]
    return [*picked, "--from", str(out.parent / "agent.jsonl"), "--out", str(out), "--dry"]


def test_the_cli_rejudges_an_old_file_and_rebuilds_gold_offline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [*_corpus(tmp_path), "--distractors", "0"]
    assert kb_agent_eval.main(argv) == 0
    _as_old(tmp_path / "agent.jsonl")
    out = tmp_path / "rejudged.jsonl"
    rejudge = _rejudge_argv(argv, out)
    no_root = [x for i, x in enumerate(rejudge) if "--corpus-root" not in (x, rejudge[i - 1])]
    with pytest.raises(SystemExit, match="--corpus-root"):
        kb_agent_rejudge.main(no_root)
    assert kb_agent_rejudge.main(rejudge) == 0
    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    rows = [x for x in lines if "case_id" in x]
    [run] = [x["run"] for x in lines if "run" in x]
    assert len(rows) == 32 and {r["yardstick"] for r in rows} == {YARDSTICK}
    assert all(r["rejudged_from"]["yardstick"] == _OLD for r in rows)
    answerable = [r for r in rows if r["label"] == "answerable"]
    assert answerable and all(r["gold"] for r in answerable)  # 库在本机重建,取回了含答案段落
    assert run["rejudge"]["source"] == "agent.jsonl" and run["rejudge"]["yardstick"] == YARDSTICK
    assert run["calls"]["answer"] == 0
    assert kb_agent_rejudge.main(rejudge) == 0  # 续跑:全做完了,只补一行 run
    assert len([x for x in out.read_text().splitlines() if "case_id" in x]) == 32
    assert "# 答题模型 dry" in capsys.readouterr().out


def test_the_cli_rejudges_a_hard_negative_file_with_its_spec(tmp_path: Path) -> None:
    spec = _spec(tmp_path, _GONE)
    argv = [*_corpus(tmp_path), "--hard-negatives", str(spec), "--no-recheck"]
    assert kb_agent_eval.main(argv) == 0
    out = tmp_path / "rejudged.jsonl"
    rejudge = _rejudge_argv(argv, out)
    with pytest.raises(SystemExit, match="hard-negative run"):
        kb_agent_rejudge.main(rejudge)
    assert kb_agent_rejudge.main([*rejudge, "--hard-negatives", str(spec)]) == 0
    rows = [json.loads(x) for x in out.read_text().splitlines() if "case_id" in x]
    [hn] = [r for r in rows if r["set"] == "hard_negative"]
    assert hn["yardstick"] == YARDSTICK and hn["run_kind"].startswith("hn:")
