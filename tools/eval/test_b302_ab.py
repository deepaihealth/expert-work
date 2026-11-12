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
    sessions = ab.plan(ab.load_docs(_docs_dir(tmp_path)), ab.default_models("doubao-x"))
    runs = ab.runs_per_model(sessions)
    assert runs == {"glm-5.3-flash": 105, "deepseek-flash": 93, "doubao-x": 63}
    per_arm = ab.runs_per_model(s for s in sessions if s.phase == "main")
    assert per_arm == {"glm-5.3-flash": 90, "deepseek-flash": 90, "doubao-x": 60}


def test_the_plan_runs_pilots_then_arm_a_then_the_rest(tmp_path: Path) -> None:
    sessions = ab.plan(ab.load_docs(_docs_dir(tmp_path)), ab.default_models("doubao-x"))
    phases = [ab.phase_of(s) for s in sessions]
    assert phases == sorted(phases, key=["pilot", "arm_a", "rest"].index)
    assert {s.arm for s in sessions if ab.phase_of(s) == "arm_a"} == {"A"}
    assert all(s.arm == "A" and s.turns == 3 for s in sessions if s.phase == "pilot")
    assert {s.model for s in sessions if s.phase == "regression"} == {"glm-5.3-flash"}
    assert {s.arm for s in sessions if s.model == "doubao-x"} == {"A", "C"}


def test_an_incomplete_document_set_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "partial"
    docs.write_set(out, seed=5, incident=None, font=None, render=False)
    with pytest.raises(ab.PlanError, match="incident"):
        ab.load_docs(out)


def test_the_quote_reproduces_the_spec_numbers() -> None:
    caps = {m.name: round(ab.run_cap_yuan(m.price), 2) for m in ab.default_models("doubao-x")}
    assert caps == {"glm-5.3-flash": 23.36, "deepseek-flash": 64.87, "doubao-x": 233.52}
    expected = {m.name: round(ab.run_expected_yuan(m.price), 3) for m in ab.default_models("d")}
    assert expected == {"glm-5.3-flash": 0.486, "deepseek-flash": 1.26, "d": 4.05}
    # 设计 §5.7 / §11 D4:止损线 ¥650 时实际硬上限约 ¥1,294(650 + 177 + 2 x 233.5,按整数取)
    assert ab.hard_ceiling_yuan(650, ab.default_models("d")) == pytest.approx(1294, abs=1)


def test_the_call_estimate(tmp_path: Path) -> None:
    sessions = ab.plan(ab.load_docs(_docs_dir(tmp_path)), ab.default_models("doubao-x"))
    estimate = ab.call_estimate(sessions)
    assert estimate["runs"] == 261
    assert estimate["expected_calls"] == 261 * 15
    assert estimate["max_calls"] == 261 * (40 + 2 + 26)


# --------------------------------------------------------------------------- spend and guards


def test_spend_counts_cache_reads_at_their_price_and_stops_at_the_line() -> None:
    tracker = ab.SpendTracker(stop_line=1.0, prices={"glm-5.3-flash": (0.8, 2.8, 0.23)})
    cost = tracker.add(
        [
            {
                "model": "glm-5.3-flash",
                "input_tokens": 1_000_000,
                "output_tokens": 100_000,
                "input_token_details": {"cache_read": 400_000},
            }
        ]
    )
    assert cost == pytest.approx(0.6 * 0.8 + 0.4 * 0.23 + 0.1 * 2.8)
    assert tracker.may_start() is True
    tracker.add([{"model": "glm-5.3-flash", "input_tokens": 1_000_000, "output_tokens": 0}])
    assert tracker.may_start() is False


def test_an_unpriced_model_is_an_error_not_free() -> None:
    tracker = ab.SpendTracker(stop_line=1.0, prices={})
    with pytest.raises(ab.PlanError, match="no price"):
        tracker.add([{"model": "other", "input_tokens": 1, "output_tokens": 1}])


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
    paths = ab.write_manifests(out, ab.default_models("doubao-x"))
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
    assert ("doubao-x", "B") not in paths
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
        "--doubao-model",
        "d",
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
    assert result.refetches == 1  # 上一轮读过的文档,本轮第一次再读 = 过期后重取
    assert result.correction_applied is True
    assert result.template_ok is True
    assert result.tokens == 120
    assert result.thinking_median == 6.5
    assert result.runaway is False
