"""kb_agent_eval.py 接上两个派生题集(B-206 11-09):``--distractors`` / ``--hard-negatives``。

空跑:内存库 + 假嵌入 + 假模型,不连网;合成语料。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from kb_agent_cases import select_cases  # type: ignore[import-not-found]
from kb_agent_eval import ANSWER_ENV, JUDGE_ENV, main  # type: ignore[import-not-found]
from kb_agent_models import FakeModels  # type: ignore[import-not-found]
from kb_corpus_inventory import MANIFEST_FIELDS  # type: ignore[import-not-found]
from kb_dataset import load_dataset  # type: ignore[import-not-found]

_KEEP = "甲一净水器的滤芯建议每六个月更换一次"
_DELETE = "甲一净水器的额定流量是每分钟一点八升"
_GONE = "甲一净水器运行时的噪音低于四十分贝"
# 每段都比切块的短节合并线长,两节各自成段。
_DOC_A = (
    "# 甲一净水器说明书(合成)\n\n## 滤芯\n\n"
    + _KEEP
    + "。更换前请先关闭进水阀,取下旧滤芯后再装新滤芯。" * 4
    + "\n\n## 参数\n\n"
    + _DELETE
    + "。机身尺寸与安装方式见包装内的安装图,安装时需要预留检修空间。" * 4
    + "\n\n## 噪音\n\n"
    + _GONE
    + "。夜间可以切换到静音模式,静音模式下出水速度会变慢一些。" * 4
    + "\n"
)
_DOC_B = (
    "# 甲二净水器说明书(合成)\n\n## 滤芯\n\n甲二净水器的滤芯建议每九个月更换一次。"
    + "甲二适合办公室使用,出水口高度可以调节。" * 4
    + "\n"
)


def _manifest_row(path: str) -> dict[str, str]:
    row = dict.fromkeys(MANIFEST_FIELDS, "")
    row.update(path=path, top_dir="docs", ext="md", size="100", tier="A", pages="0")
    return row


def _case(case_id: str, query: str, anchor: str, document: str) -> dict[str, Any]:
    expected = [{"document": document, "anchor": anchor, "page": None}]
    return {"id": case_id, "query": query, "source": "faq_xlsx", "expected": expected}


def _corpus(tmp_path: Path) -> list[str]:
    root = tmp_path / "corpus"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "说明书:甲一.md").write_text(_DOC_A, encoding="utf-8")
    (root / "docs" / "说明书:甲二.md").write_text(_DOC_B, encoding="utf-8")
    manifest = tmp_path / "manifest.csv"
    with manifest.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(MANIFEST_FIELDS))
        writer.writeheader()
        writer.writerow(_manifest_row("docs/说明书:甲一.md"))
        writer.writerow(_manifest_row("docs/说明书:甲二.md"))
    paths = []
    for name in ("unit_kb", "unit_kb_chat"):
        # 每个集 15 道有答案 + 3 道点名「甲一」的有答案题(固定抽样取 15 道)+ 1 道无答案题
        cases = [
            _case(f"{name}-a{i}", f"滤芯多久换 {i}", _KEEP, "说明书:甲一.md") for i in range(15)
        ]
        cases += [
            _case(f"{name}-named{i}", f"甲一的额定流量是多少? {i}", _DELETE, "说明书:甲一.md")
            for i in range(3)
        ]
        cases.append(
            {"id": f"{name}-n0", "query": "含多少咖啡因", "source": "no_answer", "expected": []}
        )
        corpus = {"manifest": str(manifest), "tiers": ["A"]}
        path = tmp_path / f"{name}.yaml"
        path.write_text(
            yaml.safe_dump({"name": name, "corpus": corpus, "cases": cases}, allow_unicode=True),
            encoding="utf-8",
        )
        paths.append(str(path))
    return [
        "--dataset",
        paths[0],
        "--chat-dataset",
        paths[1],
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
    ]


def _lines(tmp_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    lines = [json.loads(x) for x in (tmp_path / "agent.jsonl").read_text().splitlines()]
    return [x for x in lines if "case_id" in x], next(x["run"] for x in lines if "run" in x)


@pytest.fixture(autouse=True)
def _no_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*ANSWER_ENV, *JUDGE_ENV):
        monkeypatch.delenv(name, raising=False)


def test_distractors_join_the_standard_run_as_their_own_set(tmp_path: Path) -> None:
    argv = [*_corpus(tmp_path), "--distractors", "5"]
    sets = [load_dataset(Path(argv[1])), load_dataset(Path(argv[3]))]
    sampled = {c.id for _label, c in select_cases(*sets)}
    # 没被固定抽样抽走的点名题(问句点了「甲一」、语料里还有「甲二」)才会成为干扰题
    left_out = sorted(c.id for ds in sets for c in ds.cases if "named" in c.id)
    expected = [i for i in left_out if i not in sampled]
    assert expected  # 这组数据下至少有一道,否则本用例测不到东西
    assert main(argv) == 0
    rows, run = _lines(tmp_path)
    distractors = [r for r in rows if r["set"] == "distractor"]
    assert sorted(r["case_id"] for r in distractors) == expected
    assert all(r["label"] == "answerable" for r in distractors)
    assert len(rows) == 2 * (1 + 15) + len(expected)
    assert run["distractors"] == len(expected)


def test_no_distractors_when_turned_off(tmp_path: Path) -> None:
    assert main([*_corpus(tmp_path), "--distractors", "0"]) == 0
    rows, run = _lines(tmp_path)
    assert not [r for r in rows if r["set"] == "distractor"] and run["distractors"] == 0


def _spec(tmp_path: Path, anchor: str) -> Path:
    spec = tmp_path / "hn.yaml"
    entry = {"id": "unit_kb-noise", "set": "main", "query": "甲一运行时噪音多大?"}
    spec.write_text(
        yaml.safe_dump({"cases": [{**entry, "anchors": [anchor]}]}, allow_unicode=True),
        encoding="utf-8",
    )
    return spec


def test_hard_negative_run_deletes_the_answer_sentence_and_keeps_the_controls(
    tmp_path: Path,
) -> None:
    argv = _corpus(tmp_path)
    assert main([*argv, "--hard-negatives", str(_spec(tmp_path, _GONE))]) == 0
    rows, run = _lines(tmp_path)
    [hn] = [r for r in rows if r["set"] == "hard_negative"]
    assert hn["case_id"] == "hn-unit_kb-noise" and hn["label"] == "no_answer"
    # 对照:30 道有答案题照跑;标准集的无答案题、干扰题不在这一臂里
    assert sorted({r["set"] for r in rows}) == ["chat", "hard_negative", "main"]
    assert sum(1 for r in rows if r["label"] == "answerable") == 30
    seen = "".join(s.get("content") or "" for r in rows for s in r["searches"])
    assert "四十分贝" not in seen  # 删掉的句子检索不到
    assert "静音模式" in seen  # 相邻上下文还在
    assert run["hard_negatives"] == {
        "cases": 1,
        "anchors": 1,
        "residue": [],
        "controls_without_gold": 0,
    }


def test_a_spec_that_deletes_a_control_answer_stops_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _corpus(tmp_path)
    sets = [load_dataset(Path(argv[1])), load_dataset(Path(argv[3]))]
    hit = sum(1 for _label, c in select_cases(*sets) if "named" in c.id)
    assert hit  # 抽样里有点名题,它们的答案正是被删的这句
    # 11-09 评审:伤到对照题就停,不是打个 WARNING 接着花钱
    with pytest.raises(SystemExit, match="removes control answers"):
        main([*argv, "--hard-negatives", str(_spec(tmp_path, _DELETE))])
    assert not (tmp_path / "agent.jsonl").exists() or not [
        line for line in (tmp_path / "agent.jsonl").read_text().splitlines() if "case_id" in line
    ]
    assert "ControlAnchorDeletedError" in capsys.readouterr().err


def test_hard_negatives_and_distractors_are_separate_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = tmp_path / "hn.yaml"
    spec.write_text("cases: []\n", encoding="utf-8")
    argv = [*_corpus(tmp_path), "--hard-negatives", str(spec), "--distractors", "3"]
    with pytest.raises(SystemExit):
        main(argv)
    assert "--hard-negatives" in capsys.readouterr().err


def test_the_judge_is_told_about_the_near_miss_and_the_deleted_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: dict[str, str] = {}
    original = FakeModels.judge

    async def spy(self: FakeModels, prompt: str) -> tuple[str, float]:
        question = prompt.split("【用户问题】\n", 1)[1].split("\n", 1)[0]
        prompts[question] = prompt
        raw, cost = await original(self, prompt)
        return str(raw), float(cost)

    monkeypatch.setattr(FakeModels, "judge", spy)
    argv = _corpus(tmp_path)
    assert main([*argv, "--distractors", "5"]) == 0
    named = [p for q, p in prompts.items() if q.startswith("甲一的额定流量")]
    assert any("名字相近的「甲二」" in p for p in named)
    (tmp_path / "agent.jsonl").unlink()
    assert main([*argv, "--hard-negatives", str(_spec(tmp_path, _GONE))]) == 0
    assert "已从资料里删掉" in prompts["甲一运行时噪音多大?"]
    assert _GONE in prompts["甲一运行时噪音多大?"]


def test_the_cli_will_not_resume_into_another_vote_count_or_run(tmp_path: Path) -> None:
    argv = [*_corpus(tmp_path), "--distractors", "0"]
    assert main(argv) == 0
    assert main(argv) == 0  # 同一轮续跑:没做完的题补上,不报错
    with pytest.raises(SystemExit, match="judge_votes"):
        main([*argv, "--judge-votes", "1"])
    hn = [a for a in argv if a not in ("--distractors", "0")]
    with pytest.raises(SystemExit, match="run_kind"):
        main([*hn, "--hard-negatives", str(_spec(tmp_path, _GONE))])


def test_deleted_text_still_found_after_ingest_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def still_there(*args: Any, **kwargs: Any) -> list[str]:
        return [_GONE]

    monkeypatch.setattr("kb_agent_eval.store_residue", still_there)
    with pytest.raises(SystemExit, match="not usable"):
        main([*_corpus(tmp_path), "--hard-negatives", str(_spec(tmp_path, _GONE))])
