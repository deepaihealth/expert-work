"""kb_agent_estimate.py:按历史结果文件 + 判分缓存免费报价,不调任何模型(B-206 11-09)。"""

from __future__ import annotations

import json
import socket
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from kb_agent_cost import PRICES  # type: ignore[import-not-found]
from kb_agent_estimate import (  # type: ignore[import-not-found]
    CLAIMS_OUTPUT_FACTOR,
    OLD_TEMPLATE_CHARS,
    TOKENS_PER_CHAR,
    History,
    average_reply_chars,
    estimate_run,
    history_from,
    judge_call_cny,
    main,
)
from kb_agent_judge import JUDGE_TEMPLATE  # type: ignore[import-not-found]

_NEW_TEMPLATE = len(JUDGE_TEMPLATE.format(question="", passages="", reference="", answer=""))


def _history_file(tmp_path: Path) -> Path:
    rows = [
        {
            "case_id": f"q{i}",
            "question": "问" * 10,
            "answer": "答" * 90,
            "searches": [{"content": "段" * 400}],
            "reads": [],
            "cost_cny": {"answer": 0.2, "judge": 0.1},
            "answer_model": "glm-5.3",
            "judge_model": "deepseek-v4-pro",
        }
        for i in range(4)
    ]
    run = {
        "questions": 4,
        "calls": {"answer": 10, "judge": 4},
        "spend_cny": {"answer": 0.8, "judge": 0.4, "rerank": 0.12},
        "answer_model": "glm-5.3",
    }
    path = tmp_path / "history.jsonl"
    lines = [json.dumps(r, ensure_ascii=False) for r in rows] + [json.dumps({"run": run})]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_history_gives_per_question_costs_and_the_new_prompt_size_ratio(tmp_path: Path) -> None:
    h = history_from(_history_file(tmp_path))
    assert h.questions == 4
    assert h.answer_cny_per_q == pytest.approx(0.2)
    assert h.answer_calls_per_q == pytest.approx(2.5)
    assert h.judge_cny_per_call == pytest.approx(0.1)
    assert h.rerank_cny_per_q == pytest.approx(0.03)
    rest = 10 + 90 + 400
    assert h.input_ratio == pytest.approx((_NEW_TEMPLATE + rest) / (OLD_TEMPLATE_CHARS + rest))
    assert h.judge_model == "deepseek-v4-pro"


def test_old_template_size_is_the_one_measured_before_the_change() -> None:
    # 拆出判分前 ca1c9f0f8 的 JUDGE_TEMPLATE 去掉四个占位符后 472 字;新模板更长
    assert OLD_TEMPLATE_CHARS == 472
    assert _NEW_TEMPLATE > OLD_TEMPLATE_CHARS


def test_a_new_judge_call_costs_the_bigger_prompt_plus_the_claims_output() -> None:
    h = History(
        questions=60,
        answer_cny_per_q=0.14,
        answer_calls_per_q=2.8,
        judge_cny_per_call=0.1,
        rerank_cny_per_q=0.027,
        input_ratio=1.05,
        answer_model="glm-5.3",
        judge_model="deepseek-v4-pro",
    )
    price = PRICES["deepseek-v4-pro"]
    extra = (CLAIMS_OUTPUT_FACTOR - 1) * 150 * TOKENS_PER_CHAR * price.output / 1e6
    assert judge_call_cny(h, reply_chars=150, price=price) == pytest.approx(0.105 + extra)

    run = estimate_run("标准轮", 75, h, votes=3, judge_call=0.11, embed_cny=0.5)
    assert run.answer_calls == pytest.approx(75 * 2.8)
    assert run.judge_calls == 225
    assert run.answer_cny == pytest.approx(75 * 0.14)
    assert run.judge_cny == pytest.approx(225 * 0.11)
    assert run.rerank_cny == pytest.approx(75 * 0.027)
    assert run.total == pytest.approx(75 * 0.14 + 225 * 0.11 + 75 * 0.027 + 0.5)


def test_average_reply_chars_reads_the_judge_cache(tmp_path: Path) -> None:
    db = tmp_path / "judge.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE j (k TEXT PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO j VALUES (?, ?)", [("a", "x" * 100), ("b", "y" * 200)])
    con.commit()
    assert average_reply_chars(db) == pytest.approx(150)
    assert average_reply_chars(tmp_path / "missing.sqlite") is None


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    attempts: list[str] = []

    def refuse(self: socket.socket, address: Any, *args: Any) -> Any:
        attempts.append(repr(address))
        raise OSError("network disabled in this test")

    async def refuse_send(self: httpx.AsyncClient, request: httpx.Request, **kw: Any) -> Any:
        attempts.append(str(request.url))
        raise httpx.ConnectError("network disabled in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse_send)
    return attempts


def _datasets(tmp_path: Path) -> list[str]:
    paths = []
    for name in ("m", "c"):
        cases: list[dict[str, Any]] = [
            {
                "id": f"{name}-a{i}",
                "query": f"q{i}",
                "source": "faq_xlsx",
                "expected": [{"document": "d.md", "anchor": "常温密封保存即可", "page": None}],
            }
            for i in range(20)
        ]
        cases += [{"id": f"{name}-n0", "query": "n", "source": "no_answer", "expected": []}]
        path = tmp_path / f"{name}.yaml"
        path.write_text(yaml.safe_dump({"name": name, "cases": cases}), encoding="utf-8")
        paths.append(str(path))
    return paths


def test_cli_prints_baseline_and_after_change_without_any_call(
    tmp_path: Path, no_network: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    main_set, chat_set = _datasets(tmp_path)
    spec = tmp_path / "hn.yaml"
    entries = [{"id": f"m-a{i}", "query": "q", "anchors": ["常温密封保存即可"]} for i in range(5)]
    spec.write_text(
        yaml.safe_dump({"touched": {"documents": 2, "chars": 100_000}, "cases": entries}),
        encoding="utf-8",
    )
    argv = [
        "--dataset",
        main_set,
        "--chat-dataset",
        chat_set,
        "--history",
        str(_history_file(tmp_path)),
        "--judge-cache",
        str(tmp_path / "none.sqlite"),
        "--hard-negatives",
        str(spec),
        "--distractors",
        "4",
    ]
    assert main(argv) == 0
    out = capsys.readouterr().out
    # 标准轮:每集 1 道无答案 + 15 道有答案 = 32,外加最多 4 道干扰题(这里没有语料清单 → 按 4 道估)
    assert "标准轮 | 36 题" in out
    # 难负例轮:30 道对照 + spec 里 5 道
    assert "难负例轮 | 35 题" in out
    assert "改动前基线(当前 main)" in out and "改动后一轮" in out
    assert "不调任何模型" in out
    assert no_network == []
