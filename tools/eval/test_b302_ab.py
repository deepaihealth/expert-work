"""B-302 PR 2 A/B —— 运行脚本(设计 §5.3、§5.5):计划、报价、止损、单次输入上限、帧解析、交付件读取。

全部离线:HTTP 用 ``httpx.MockTransport`` 假装平台,不碰任何模型。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import b302_ab as ab
import b302_docs as docs
import httpx
import pytest
import yaml

#: 豆包的被测型号(§6.2;260628 已下架)与单价(元 / 百万 token,输入 / 输出 / 缓存读)。
DOUBAO = "doubao-seed-2-1-pro-260915"
DOUBAO_PRICE = (6.0, 30.0, 1.2)
_DOUBAO_ARGS = ["--doubao-model", DOUBAO, "--doubao-price", "6,30,1.2"]


def _docs_dir(tmp_path: Path) -> Path:
    incident = {
        "key": "incident",
        "language": "en",
        "title": "Synthetic sample",
        "fields": [
            {"label": f"F{i}", "kind": k}
            for i, k in enumerate(
                [
                    "name",
                    "org",
                    "date",
                    "code",
                    "phrase",
                    "address",
                    "code",
                    "name",
                    "amount",
                    "quantity",
                ]
            )
        ],
        "filler": ["A synthetic line."],
        "sections": ["One", "Two"],
    }
    path = tmp_path / "incident.json"
    path.write_text(json.dumps(incident), encoding="utf-8")
    out = tmp_path / "docs"
    docs.write_set(out, seed=5, incident=docs.load_incident_template(path), font=None, render=False)
    return out


# --------------------------------------------------------------------------- plan and quote


def test_the_plan_matches_the_spec_counts(tmp_path: Path) -> None:
    sessions = ab.plan(ab.load_docs(_docs_dir(tmp_path)), ab.default_models(DOUBAO, DOUBAO_PRICE))
    runs = ab.runs_per_model(sessions)
    assert runs == {"glm-5.3-flash": 105, "deepseek-flash": 93, DOUBAO: 63}
    per_arm = ab.runs_per_model(s for s in sessions if s.phase == "main")
    assert per_arm == {"glm-5.3-flash": 90, "deepseek-flash": 90, DOUBAO: 60}


def test_the_plan_runs_pilots_then_arm_a_then_the_rest(tmp_path: Path) -> None:
    sessions = ab.plan(ab.load_docs(_docs_dir(tmp_path)), ab.default_models(DOUBAO, DOUBAO_PRICE))
    phases = [ab.phase_of(s) for s in sessions]
    assert phases == sorted(phases, key=["pilot", "arm_a", "rest"].index)
    assert {s.arm for s in sessions if ab.phase_of(s) == "arm_a"} == {"A"}
    assert all(s.arm == "A" and s.turns == 3 for s in sessions if s.phase == "pilot")
    assert {s.model for s in sessions if s.phase == "regression"} == {"glm-5.3-flash"}
    assert {s.arm for s in sessions if s.model == DOUBAO} == {"A", "C"}


def test_an_incomplete_document_set_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "partial"
    docs.write_set(out, seed=5, incident=None, font=None, render=False)
    with pytest.raises(ab.PlanError, match="incident"):
        ab.load_docs(out)


def test_the_quote_reproduces_the_spec_numbers() -> None:
    caps = {
        m.name: round(ab.run_cap_yuan(m.price), 2) for m in ab.default_models(DOUBAO, DOUBAO_PRICE)
    }
    assert caps == {"glm-5.3-flash": 23.36, "deepseek-flash": 64.87, DOUBAO: 233.52}
    expected = {
        m.name: round(ab.run_expected_yuan(m.price), 3)
        for m in ab.default_models(DOUBAO, DOUBAO_PRICE)
    }
    assert expected == {"glm-5.3-flash": 0.486, "deepseek-flash": 1.26, DOUBAO: 4.05}
    # 设计 §5.7 / §11 D4:止损线 ¥650 时实际硬上限约 ¥1,294(650 + 177 + 2 x 233.5,按整数取)
    assert ab.hard_ceiling_yuan(650, ab.default_models(DOUBAO, DOUBAO_PRICE)) == pytest.approx(
        1294, abs=1
    )


def test_the_call_estimate(tmp_path: Path) -> None:
    sessions = ab.plan(ab.load_docs(_docs_dir(tmp_path)), ab.default_models(DOUBAO, DOUBAO_PRICE))
    estimate = ab.call_estimate(sessions)
    assert estimate["runs"] == 261
    assert estimate["expected_calls"] == 261 * 15
    assert estimate["max_calls"] == 261 * (40 + 2 + 26)


# --------------------------------------------------------------------------- spend and guards


def test_the_stop_line_alone_stops_new_runs() -> None:
    """缓存读 / 没单价 / 没用量的计费在 ``test_b302_live`` 按平台真实形状测。"""
    tracker = ab.SpendTracker(stop_line=1.0, prices={"glm-5.3-flash": (0.8, 2.8, 0.23)})
    tracker.add([{"model": "glm-5.3-flash", "input_tokens": 1_000_000, "output_tokens": 0}])
    assert tracker.may_start() is True
    tracker.add([{"model": "glm-5.3-flash", "input_tokens": 1_000_000, "output_tokens": 0}])
    assert tracker.may_start() is False


# ------------------------------------------------------------------ quote id and stop line (LOW-2)


def test_the_quote_id_is_stable_and_follows_what_was_quoted(tmp_path: Path) -> None:
    docs_dir = _docs_dir(tmp_path)
    models = ab.default_models(DOUBAO, DOUBAO_PRICE)
    sessions = ab.plan(ab.load_docs(docs_dir), models, docs_dir)
    q = ab.quote(sessions, models, 650.0)
    assert q["quote_id"] == ab.quote(sessions, models, 650.0)["quote_id"]
    assert q["quote_id"] != ab.quote(sessions, models, 400.0)["quote_id"]
    assert q["quote_id"] != ab.quote(sessions[:-1], models, 650.0)["quote_id"]
    out: list[str] = []
    ab.main(["plan", "--docs", str(docs_dir), *_DOUBAO_ARGS], emit=out.append)
    assert json.loads(out[0])["quote_id"] == q["quote_id"]


@pytest.mark.parametrize("bad", ["nan", "inf", "0", "-1", "2000.01", "abc"])
def test_a_stop_line_must_be_a_sane_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], bad: str
) -> None:
    monkeypatch.setenv("EXPERT_WORK_EVAL_API_KEY", "k")
    docs_dir = _docs_dir(tmp_path)
    args = [
        "run", "--phase", "pilot", "--docs", str(docs_dir), "--out", str(tmp_path / "o"),
        *_DOUBAO_ARGS, "--base-url", "http://platform.invalid",
        "--approved-quote", "x", "--stop-line", bad,
    ]  # fmt: skip
    with pytest.raises(SystemExit):
        ab.main(args)
    assert "--stop-line" in capsys.readouterr().err


def _run_args(tmp_path: Path, docs_dir: Path, quote_id: str) -> list[str]:
    return [
        "run", "--phase", "pilot", "--docs", str(docs_dir), "--out", str(tmp_path / "o"),
        *_DOUBAO_ARGS, "--base-url", "http://platform.invalid",
        "--approved-quote", quote_id, "--stop-line", "650",
    ]  # fmt: skip


def test_the_approved_quote_must_be_the_quote_of_this_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EXPERT_WORK_EVAL_API_KEY", "k")
    docs_dir = _docs_dir(tmp_path)
    with pytest.raises(SystemExit):
        ab.main(_run_args(tmp_path, docs_dir, "not-the-quote"))
    assert "does not match" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


def test_the_quote_id_is_written_into_spent_and_every_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """文档没渲染(没有 PDF):每个会话在上传之前就出错 —— 不碰网络,正好走一遍记账与阻塞。"""
    monkeypatch.setenv("EXPERT_WORK_EVAL_API_KEY", "k")
    docs_dir = _docs_dir(tmp_path)
    models = ab.default_models(DOUBAO, DOUBAO_PRICE)
    qid = ab.quote(ab.plan(ab.load_docs(docs_dir), models, docs_dir), models, 650.0)["quote_id"]
    ab.main(_run_args(tmp_path, docs_dir, qid), emit=lambda _l: None)
    spent = json.loads((tmp_path / "o" / "spent.json").read_text())
    assert spent["quote_id"] == qid
    assert spent["blocked"] is not None and "FileNotFoundError" in spent["blocked"]
    records = [json.loads(x) for x in (tmp_path / "o" / "records.jsonl").read_text().splitlines()]
    assert records and all(r["quote_id"] == qid for r in records)


# --------------------------------------------------------------------------- frames


def _sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


def _ai(
    content: str, *, thinking: str = "", calls: list[dict] | None = None, tokens_in: int = 100
) -> dict:
    return {
        "agent": {
            "messages": [
                {
                    "type": "ai",
                    "content": content,
                    "tool_calls": calls or [],
                    "additional_kwargs": {"reasoning_content": thinking},
                    "usage_metadata": {
                        "input_tokens": tokens_in,
                        "output_tokens": 10,
                        "output_token_details": {"reasoning": 7},
                    },
                }
            ]
        }
    }


def test_the_collector_turns_frames_into_steps() -> None:
    collector = ab.TurnCollector(max_input_tokens=250_000)
    frames = [
        ("metadata", {"run_id": "r1", "thread_id": "s1"}),
        (
            "updates",
            _ai(
                "",
                thinking="看一下",
                calls=[{"name": "read_page", "args": {"path": "a.pdf", "units": [1]}, "id": "c"}],
            ),
        ),
        ("guard", {"kind": "warning", "guard": "max_steps", "detail": {}}),
        (
            "updates",
            _ai(
                "写好了 out.pptx",
                calls=[{"name": "exec_python", "args": {"code": "save('out.pptx')"}, "id": "d"}],
            ),
        ),
        ("updates", _ai("完成")),
        ("end", {"exit_reason": "text_response", "usage_by_model": [], "status": "success"}),
    ]
    for event, data in frames:
        collector.on_frame(event, json.dumps(data))
    assert collector.run_id == "r1"
    assert [s.thinking for s in collector.steps] == ["看一下", "", ""]
    assert collector.steps[0].calls[0].name == "read_page"
    assert collector.steps[0].reasoning_tokens == 7
    assert collector.warnings == ["max_steps"]
    assert collector.end is not None and collector.end["exit_reason"] == "text_response"
    assert ab.first_deliverable_step(collector.steps, "slides") == 1
    assert ab.first_deliverable_step(collector.steps, "spreadsheet") is None


def test_one_oversized_call_asks_for_a_cancel() -> None:
    collector = ab.TurnCollector(max_input_tokens=250_000)
    collector.on_frame("updates", json.dumps(_ai("x", tokens_in=250_000)))
    assert collector.cancel_reason is None
    collector.on_frame("updates", json.dumps(_ai("x", tokens_in=250_001)))
    assert collector.cancel_reason is not None and "250001" in collector.cancel_reason


# --------------------------------------------------------------------------- deliverables


def _ooxml(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return buffer.getvalue()


def test_text_and_structure_of_the_three_deliverable_kinds() -> None:
    pptx = _ooxml(
        {
            "ppt/slides/slide1.xml": "<p:sld><a:t>概要</a:t><a:t>QX-1O5S</a:t></p:sld>",
            "ppt/slides/slide2.xml": "<p:sld><a:t>关键信息</a:t></p:sld>",
        }
    )
    assert ab.deliverable_text(pptx) == "概要 QX-1O5S 关键信息"
    assert ab.deliverable_structure(pptx, "slides") == 2
    xlsx = _ooxml(
        {
            "xl/worksheets/sheet1.xml": "<x/>",
            "xl/worksheets/sheet2.xml": "<x/>",
            "xl/sharedStrings.xml": "<sst><si><t>型号</t></si></sst>",
        }
    )
    assert ab.deliverable_structure(xlsx, "spreadsheet") == 2
    assert "型号" in ab.deliverable_text(xlsx)
    docx = _ooxml(
        {
            "word/document.xml": (
                '<w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:t>一</w:t></w:p>'
                '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:t>二</w:t></w:p>'
                "<w:p><w:t>正文</w:t></w:p></w:body>"
            )
        }
    )
    assert ab.deliverable_structure(docx, "document") == 2
    assert ab.template_followed("一 正文 二", ["一", "二"]) is True
    assert ab.template_followed("二 一", ["一", "二"]) is False


# --------------------------------------------------------------------------- manifests


def test_the_three_arms_differ_only_in_the_two_override_fields(tmp_path: Path) -> None:
    out = tmp_path / "agents"
    paths = ab.write_manifests(out, ab.default_models(DOUBAO, DOUBAO_PRICE))
    glm = {arm: yaml.safe_load(paths[("glm-5.3-flash", arm)].read_text()) for arm in "ABC"}
    policies = {arm: glm[arm]["spec"]["policies"] for arm in "ABC"}
    assert policies["A"]["reading_rule"] is False and policies["A"]["figure_view_steps"] == 0
    assert policies["B"]["reading_rule"] is True and policies["B"]["figure_view_steps"] == 0
    assert policies["C"]["reading_rule"] is True and policies["C"]["figure_view_steps"] == 3

    def _strip(doc: dict) -> dict:
        doc = json.loads(json.dumps(doc))
        doc["metadata"].pop("name")
        doc["spec"]["policies"].pop("reading_rule")
        doc["spec"]["policies"].pop("figure_view_steps")
        return doc

    assert _strip(glm["A"]) == _strip(glm["B"]) == _strip(glm["C"])
    spec = glm["A"]["spec"]
    assert spec["policies"]["run_deadline_s"] == 5400
    assert spec["policies"]["token_budget"] == 3_000_000
    assert spec["model"]["max_tokens"] == 32_768
    assert spec["model"]["supports_vision"] is True
    assert spec["workflow"] == {"type": "react", "max_iterations": 40}
    for absent in ("memory", "reflection", "vision", "knowledge", "subagents"):
        assert absent not in spec
    assert spec["dynamic_workers"] == {"enabled": False}
    assert (DOUBAO, "B") not in paths
    from expert_work.protocol import AgentSpec

    for path in paths.values():
        AgentSpec.model_validate(yaml.safe_load(path.read_text()))


# --------------------------------------------------------------------------- the run gate


def test_running_needs_an_approval_and_a_stop_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EXPERT_WORK_EVAL_API_KEY", "k")
    docs_dir = _docs_dir(tmp_path)
    base = [
        "run",
        "--phase",
        "pilot",
        "--docs",
        str(docs_dir),
        "--out",
        str(tmp_path / "o"),
        *_DOUBAO_ARGS,
        "--base-url",
        "http://platform.invalid",
    ]
    for missing, args in (
        ("--approved-quote", [*base, "--stop-line", "650"]),
        ("--stop-line", [*base, "--approved-quote", "q-1"]),
    ):
        with pytest.raises(SystemExit):
            ab.main(args)
        assert missing in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


async def test_one_session_end_to_end_against_a_fake_platform(tmp_path: Path) -> None:
    docs_dir = _docs_dir(tmp_path)
    doc = next(d for d in ab.load_docs(docs_dir) if d["correction"] and d["template"])
    (docs_dir / f"{doc['doc_id']}.pdf").write_bytes(b"%PDF-1.4 synthetic")
    deliverable = _ooxml({"ppt/slides/slide1.xml": "<a:t>" + doc["fields"][0]["value"] + "</a:t>"})
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/uploads"):
            return httpx.Response(200, json={"data": {"upload_id": "u1", "session_id": "s1"}})
        if request.url.path.endswith("/runs"):
            body = _sse(
                [
                    ("metadata", {"run_id": "r1", "thread_id": "s1"}),
                    ("updates", _ai("ok")),
                    (
                        "end",
                        {
                            "exit_reason": "text_response",
                            "status": "success",
                            "artifacts": [{"name": "out.pptx", "version": 1}],
                            "usage_by_model": [
                                {
                                    "model": "glm-5.3-flash",
                                    "input_tokens": 1000,
                                    "output_tokens": 10,
                                }
                            ],
                        },
                    ),
                ]
            )
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
        if request.url.path.endswith("/artifacts/download"):
            return httpx.Response(200, content=deliverable)
        if request.method == "DELETE":
            return httpx.Response(200, json={})
        return httpx.Response(404)

    session = ab.Session(
        phase="main", model="glm-5.3-flash", arm="A", doc_id=doc["doc_id"], turns=3
    )
    tracker = ab.SpendTracker(stop_line=10.0, prices={"glm-5.3-flash": (0.8, 2.8, 0.23)})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://platform"
    ) as http:
        record = await ab.run_session(
            http, session, doc=doc, docs_dir=docs_dir, agent="eval-b302-glm-a", tracker=tracker
        )
    assert [t["turn"] for t in record["turns"]] == [1, 2, 3]
    assert record["turns"][1]["deliverable_text"] == doc["fields"][0]["value"]
    assert sum("/uploads" in s for s in seen) == 2  # 文档 + 模板
    assert tracker.spent > 0
    assert any(s.startswith("DELETE") for s in seen)


def test_a_session_record_becomes_a_document_result(tmp_path: Path) -> None:
    doc = next(d for d in ab.load_docs(_docs_dir(tmp_path)) if d["correction"] and d["template"])
    values = [f["value"] for f in doc["fields"]]
    ext = ab.DELIVERABLE_EXT[doc["deliverable"]]
    pdf = f"{doc['doc_id']}.pdf"

    def _s(i: int, calls: list[dict[str, Any]] | None = None, reply: str = "") -> dict[str, Any]:
        return {
            "index": i,
            "thinking": "",
            "reply": reply,
            "calls": calls or [],
            "reasoning_tokens": 5 + i,
        }

    read = {"name": "read_page", "args": {"path": pdf, "units": [1]}}
    second = {
        "turn": 2,
        "steps": [
            _s(0, [read]),
            _s(1, [{"name": "exec_python", "args": {"code": f"save('out{ext}')"}}]),
            _s(2),
            _s(3, reply="done"),
        ],
        "exit_reason": "text_response",
        "warnings": [],
        "wall_s": 12.0,
        "usage_by_model": [{"model": "glm-5.3-flash", "input_tokens": 100, "output_tokens": 20}],
        "deliverable_text": " ".join([*doc["template"], *values[:9]]),
        "deliverable_structure": 3,
    }
    first = {"turn": 1, "steps": [_s(0, [read])]}
    third = {"turn": 3, "steps": [], "deliverable_text": doc["correction"]["value"]}
    result = ab.doc_result({"turns": [first, second, third]}, doc, view_steps=3)
    assert result.correct == 9
    assert result.calls_after_deliverable == 2
    # 每次运行分别计(独立审查 MEDIUM-2):第 1 轮第一次读不算;第 2 轮再读上一轮读过的 = 1;第 3 轮没读
    assert result.refetches == (0, 1, 0)
    assert result.no_progress_runs == 0
    assert result.correction_applied is True
    assert result.template_ok is True
    assert result.tokens == 120
    assert result.thinking_median == 6.5
    assert result.runaway is False


def test_the_report_deduplicates_and_counts_what_could_not_be_scored(tmp_path: Path) -> None:
    """MEDIUM-3:同键留最后一条;待确认 3:没登记的交付件、没有交付件、出错与被拦下的会话
    都单独计数。"""
    docs_dir = _docs_dir(tmp_path)
    doc = ab.load_docs(docs_dir)[0]
    out = tmp_path / "o"
    out.mkdir()

    def _rec(arm: str, source: str, **kw: Any) -> dict[str, Any]:
        second = {
            "turn": 2,
            "steps": [],
            "exit_reason": "text_response",
            "warnings": [],
            "wall_s": 1.0,
            "usage_by_model": [],
            "deliverable_source": source,
            "deliverable_text": None,
            "deliverable_structure": None,
        }
        base = {
            "phase": "main",
            "model": "glm-5.3-flash",
            "arm": arm,
            "doc_id": doc["doc_id"],
            "turns": 3,
            "repeat": 0,
            "quote_id": "q",
            "stopped": None,
        }
        return {**base, "turns": [second], **kw}

    records = [
        _rec("A", "missing", stopped="stop line reached"),
        _rec("A", "registered"),
        _rec("C", "workspace_unregistered"),
        {"phase": "main", "model": "glm-5.3-flash", "arm": "B", "doc_id": "x", "repeat": 0,
         "quote_id": "q", "error": "ConnectError()"},
    ]  # fmt: skip
    (out / "records.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
    (out / "spent.json").write_text(json.dumps({"spent": 1.0, "quote_id": "q", "blocked": "x"}))
    result = ab.report(docs_dir, out)
    assert result["sessions"] == {"records": 3, "errors": 1, "stopped": 0}
    assert result["deliverables"]["glm-5.3-flash"] == {
        "A": {"registered": 1},
        "C": {"workspace_unregistered": 1},
    }
    assert result["blocked"] == "x"
    assert "glm-5.3-flash" in result["verdicts"]


# ------------------------------------------------------------ 复审:豆包型号与单价


def test_the_doubao_model_must_be_a_live_vision_model_in_the_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert ab.default_models(DOUBAO, DOUBAO_PRICE)[-1].price == DOUBAO_PRICE
    with pytest.raises(ab.PlanError, match="not in the model catalog"):
        ab.default_models("doubao-made-up", DOUBAO_PRICE)
    with pytest.raises(ab.PlanError, match="deprecated"):
        ab.default_models("doubao-seed-2-1-pro-260628", (6.0, 30.0, 1.2))
    # 目录里眼下没有「在用、但看不了图」的豆包型号,拿当前型号改一个字段造一个。
    real = ab.catalog_entry("doubao", DOUBAO)
    assert real is not None
    blind = real.model_copy(update={"vision": False})
    monkeypatch.setattr(ab, "catalog_entry", lambda _provider, _name: blind)
    with pytest.raises(ab.PlanError, match="cannot see images"):
        ab.default_models(DOUBAO, DOUBAO_PRICE)


def test_the_doubao_rate_card_comes_from_migration_0187() -> None:
    """第三轮 MEDIUM-P:单价以价目表迁移为准(0187 种的那一行),不是抄 runbook。"""
    assert ab.doubao_rate_card()[DOUBAO] == (6.0, 30.0, 1.2)


@pytest.mark.parametrize(
    "price",
    [
        (5.0, 30.0, 1.2),  # 与 0187 不一致
        (6.0, 30.0, 1.0),
        (30.0, 6.0, 1.2),  # 输入 / 输出颠倒(同时也与 0187 不一致)
    ],
)
def test_a_price_that_contradicts_the_rate_card_is_refused(
    price: tuple[float, float, float],
) -> None:
    with pytest.raises(ab.PlanError, match="rate card"):
        ab.default_models(DOUBAO, price)


def test_a_catalog_model_missing_from_the_rate_card_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ab, "doubao_rate_card", lambda: {})
    with pytest.raises(ab.PlanError, match="no rate card row"):
        ab.default_models(DOUBAO, DOUBAO_PRICE)


@pytest.mark.parametrize("raw", ["0,0,0", "6,0,1.2", "30,6,1.2", "6,30,-1", "6,30"])
def test_a_doubao_price_must_be_positive_with_output_not_below_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], raw: str
) -> None:
    with pytest.raises(SystemExit):
        ab.main(
            ["plan", "--docs", str(_docs_dir(tmp_path)), "--doubao-model", DOUBAO,
             "--doubao-price", raw]
        )  # fmt: skip
    assert "--doubao-price" in capsys.readouterr().err


def test_the_rate_card_price_passes(tmp_path: Path) -> None:
    out: list[str] = []
    ab.main(["plan", "--docs", str(_docs_dir(tmp_path)), *_DOUBAO_ARGS], emit=out.append)
    assert json.loads(out[0])["models"][DOUBAO]["runs"] == 63


def test_the_doubao_price_is_required_on_the_command_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        ab.main(["plan", "--docs", str(_docs_dir(tmp_path)), "--doubao-model", DOUBAO])
    assert "--doubao-price" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        ab.main(
            [
                "plan", "--docs", str(_docs_dir(tmp_path)),
                "--doubao-model", DOUBAO, "--doubao-price", "6,30",
            ]
        )  # fmt: skip


# ------------------------------------------------------------ 复审 R6:跨报价账本、解除阻塞


def test_the_quote_id_does_not_depend_on_int_or_float(tmp_path: Path) -> None:
    docs_dir = _docs_dir(tmp_path)
    models = ab.default_models(DOUBAO, DOUBAO_PRICE)
    sessions = ab.plan(ab.load_docs(docs_dir), models, docs_dir)
    assert (
        ab.quote(sessions, models, 650)["quote_id"] == ab.quote(sessions, models, 650.0)["quote_id"]
    )


def test_plan_prints_what_was_already_spent_under_the_output_root(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    for name, spent in (("first", 120.5), ("second", 30.25)):
        (root / name).mkdir(parents=True)
        (root / name / "spent.json").write_text(json.dumps({"spent": spent, "quote_id": name}))
    out: list[str] = []
    ab.main(
        ["plan", "--docs", str(_docs_dir(tmp_path)), *_DOUBAO_ARGS, "--out-root", str(root)],
        emit=out.append,
    )
    history = json.loads(out[0])["spent_history"]
    assert history["total_yuan"] == pytest.approx(150.75)
    assert len(history["runs"]) == 2


def test_a_blocked_run_stays_blocked_unless_unblocked_with_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXPERT_WORK_EVAL_API_KEY", "k")
    docs_dir = _docs_dir(tmp_path)
    models = ab.default_models(DOUBAO, DOUBAO_PRICE)
    qid = ab.quote(ab.plan(ab.load_docs(docs_dir), models, docs_dir), models, 650.0)["quote_id"]
    out = tmp_path / "o"
    out.mkdir()
    spent_file = out / "spent.json"
    spent_file.write_text(json.dumps({"spent": 1.0, "quote_id": qid, "blocked": "no usage"}))
    ab.main(_run_args(tmp_path, docs_dir, qid), emit=lambda _l: None)
    assert not (out / "records.jsonl").exists()  # 阻塞着:一个会话都没派
    assert json.loads(spent_file.read_text())["blocked"] == "no usage"
    ab.main([*_run_args(tmp_path, docs_dir, qid), "--unblock", "checked the bill"], emit=print)
    saved = json.loads(spent_file.read_text())
    assert saved["unblocks"] == [{"reason": "checked the bill", "was": "no usage"}]
    assert (out / "records.jsonl").exists()  # 解除后真的派了(没有 PDF,随即出错再阻塞)


def test_cleanup_needs_the_doubao_model_to_know_the_exact_codes(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXPERT_WORK_EVAL_CONSOLE_TOKEN", "t")
    with pytest.raises(SystemExit):
        ab.main(["cleanup", "--base-url", "http://platform.invalid"])
    assert "--doubao-model" in capsys.readouterr().err
    assert ab.temporary_agent_codes(DOUBAO) == {
        ab.agent_code(m.name, arm)
        for m in ab.default_models(DOUBAO, DOUBAO_PRICE)
        for arm in m.arms
    }


def test_cleanup_codes_work_for_an_old_snapshot_too() -> None:
    """第三轮 LOW-c:旧快照(已下架、价目表里也没有)导过的临时智能体照样能列出来删。"""
    codes = ab.temporary_agent_codes("doubao-seed-2-1-pro-260628")
    assert "eval-b302-doubao-seed-2-1-pro-260628-a" in codes
    assert "eval-b302-doubao-seed-2-1-pro-260628-c" in codes
    assert "eval-b302-doubao-seed-2-1-pro-260628-b" not in codes
    assert len(codes) == 8
