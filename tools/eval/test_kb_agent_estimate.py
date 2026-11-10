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
    a, b = json.dumps({"reason": "x" * 80}), json.dumps({"reason": "y" * 180})
    con.executemany("INSERT INTO j VALUES (?, ?)", [("a", a), ("b", b), ("c", "not json")])
    con.commit()
    assert average_reply_chars(db) == pytest.approx((len(a) + len(b)) / 2)
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
                "expected": [{"document": "d.md", "anchor": "合成锚文本第一句", "page": None}],
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
    entries = [{"id": f"m-a{i}", "query": "q", "anchors": ["合成锚文本第一句"]} for i in range(5)]
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
    assert "2.50 次/题" in out  # 历史每题答题次数不取整
    assert no_network == []


def test_reply_length_comes_only_from_old_style_cached_replies(tmp_path: Path) -> None:
    db = tmp_path / "judge.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE j (k TEXT PRIMARY KEY, v TEXT)")
    old = json.dumps({"refused": True, "fabricated": False, "reason": "x" * 80})
    new = json.dumps({"claims": [{"claim": "y" * 900}], "refused": True})
    con.executemany("INSERT INTO j VALUES (?, ?)", [("a", old), ("b", new)])
    con.commit()
    assert average_reply_chars(db) == pytest.approx(len(old))


def test_an_interrupted_history_without_a_run_row_still_estimates(tmp_path: Path) -> None:
    path = _history_file(tmp_path)
    lines = [x for x in path.read_text(encoding="utf-8").splitlines() if '"run"' not in x]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    h = history_from(path)
    assert h.answer_calls_per_q is None and h.rerank_cny_per_q is None
    assert h.judge_cny_per_call == pytest.approx(0.1)  # 每题一次判分(旧尺子)
    run = estimate_run("标准轮", 10, h, votes=3, judge_call=0.1)
    assert run.rerank_cny == 0.0 and run.answer_calls is None


# ---------------------------------------------------------------------------
# 11-10:难负例答前复核的判分钱 + 已有结果重判的免费估价
# ---------------------------------------------------------------------------


def _cli_argv(tmp_path: Path, *extra: str) -> list[str]:
    main_set, chat_set = _datasets(tmp_path)
    spec = tmp_path / "hn.yaml"
    entries = [{"id": f"m-a{i}", "query": "q", "anchors": ["合成锚文本第一句"]} for i in range(5)]
    spec.write_text(yaml.safe_dump({"cases": entries}), encoding="utf-8")
    return [
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
        *extra,
    ]


def test_the_hard_negative_run_prices_the_pre_answer_recheck(
    tmp_path: Path, no_network: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    from kb_agent_estimate import recheck_call_cny  # type: ignore[import-not-found]

    assert main(_cli_argv(tmp_path)) == 0
    out = capsys.readouterr().out
    call = recheck_call_cny(PRICES["deepseek-v4-pro"])
    assert call > 0
    assert f"答前复核 15 次 ¥{5 * 3 * call:.2f}" in out  # 5 道难负例乘3 票
    assert main(_cli_argv(tmp_path, "--no-recheck")) == 0
    assert "答前复核 0 次 ¥0.00" in capsys.readouterr().out
    assert no_network == []


def _result_rows(path: Path, *, votes: int = 3, excluded: int = 0) -> Path:
    rows: list[dict[str, Any]] = [
        {
            "case_id": f"a{i}",
            "label": "answerable",
            "question": "问" * 10,
            "answer": "答" * 90,
            "searches": [{"content": "段" * 400}],
            "reads": [{"content": "读" * 100}],
            "category": "答对",
            "judge_votes": votes,
            "judge_model": "deepseek-v4-pro",
            "cost_cny": {"answer": 0.2, "judge": 0.3},
        }
        for i in range(4)
    ]
    rows += [
        {**rows[0], "case_id": f"x{i}", "category": "excluded_answer_still_present"}
        for i in range(excluded)
    ]
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")
    return path


def test_rejudge_estimate_counts_calls_and_prompt_tokens_from_the_rows(tmp_path: Path) -> None:
    from kb_agent_estimate import (  # type: ignore[import-not-found]
        GOLD_PASSAGE_CHARS,
        rejudge_estimate,
    )

    path = _result_rows(tmp_path / "r.jsonl", excluded=2)
    price = PRICES["deepseek-v4-pro"]
    est = rejudge_estimate(path, price=price, reply_chars=500.0)
    assert (est.questions, est.calls) == (4, 12)  # 复核剔除的两行不重判
    reference = 2 * GOLD_PASSAGE_CHARS  # 旧行没记 gold:按假设的两段估
    per_prompt = _NEW_TEMPLATE + 10 + 90 + 400 + 100 + len("\n\n=====\n\n") + reference
    assert est.input_tokens == pytest.approx(12 * per_prompt * TOKENS_PER_CHAR, rel=0.05)
    full = (est.input_tokens * price.input + est.output_tokens * price.output) / 1e6
    assert est.full_cny == pytest.approx(full)
    assert est.cached_cny < est.full_cny  # 同一提示词的第 2、3 票走提示词缓存
    # 按原文件判分实花推(含看不见的思考 token、原来的忙闲时):每行实花乘新旧提示词长度比
    from kb_agent_estimate import YARDSTICK_1109_TEMPLATE_CHARS  # type: ignore[import-not-found]

    rest = per_prompt - _NEW_TEMPLATE
    ratio = (_NEW_TEMPLATE + rest) / (YARDSTICK_1109_TEMPLATE_CHARS + rest)
    assert est.history_cny == pytest.approx(4 * 0.3 * ratio, rel=0.01)


def test_the_1109_template_size_is_the_one_measured_on_its_commit() -> None:
    from kb_agent_estimate import YARDSTICK_1109_TEMPLATE_CHARS  # type: ignore[import-not-found]

    # 3b074f291 的 JUDGE_TEMPLATE(尺子 2026-11-09.3+902042cf)去掉四个占位符后 1003 字
    assert YARDSTICK_1109_TEMPLATE_CHARS == 1003


def test_rejudge_cli_estimates_each_file_without_any_call(
    tmp_path: Path, no_network: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    a = _result_rows(tmp_path / "a.jsonl")
    b = _result_rows(tmp_path / "b.jsonl", votes=1)
    argv = ["--rejudge", str(a), str(b), "--judge-cache", str(tmp_path / "none.sqlite")]
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "重判报价(不调任何模型" in out
    assert "a.jsonl | 4 题 | 判分 12 次" in out
    assert "按原文件判分实花推" in out
    assert "b.jsonl | 4 题 | 判分 4 次" in out
    assert "合计 8 题 / 16 次" in out
    assert no_network == []
