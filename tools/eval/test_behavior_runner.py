"""B-140 behavior_runner 单元测试(假客户端,不连真栈)。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import behavior_runner as runner
import httpx
import pytest
from behavior_client import StreamIncompleteError
from behavior_schema import Case, ResultHeader, RunRecord, TurnRecord, read_results


def _case(**kw: Any) -> Case:
    raw: dict[str, Any] = {
        "id": "g99-x",
        "agent": "eval-general",
        "shape": "test",
        "fixtures": ["a.md"],
        "turns": [{"prompt": "一"}, {"prompt": "二"}],
        "checks": [
            {"type": "completed"},
            {"type": "artifact_contains", "name": "*.md", "all": ["好"]},
        ],
    }
    raw.update(kw)
    return Case.model_validate(raw)


def _turn(index: int, **kw: Any) -> TurnRecord:
    base: dict[str, Any] = {
        "index": index,
        "status": "success",
        "completed": True,
        "exit_reason": "text_response",
        "input_tokens": 100,
        "output_tokens": 10,
        "wall_s": 1.0,
    }
    base.update(kw)
    return TurnRecord.model_validate(base)


class FakeClient:
    def __init__(self, failures: list[Exception] | None = None) -> None:
        self.failures = list(failures or [])
        self.calls: list[tuple[str, Any]] = []

    async def upload(
        self, agent: str, user_id: str, filename: str, data: bytes, session_id: str | None
    ) -> tuple[str, str]:
        self.calls.append(("upload", (agent, filename, session_id)))
        return "upl_1", "s1"

    async def run_turn(
        self,
        agent: str,
        user_id: str,
        session_id: str | None,
        prompt: str,
        upload_ids: list[str],
        index: int,
    ) -> tuple[TurnRecord, str | None]:
        self.calls.append(("run_turn", (agent, session_id, prompt, list(upload_ids), index)))
        if self.failures:
            raise self.failures.pop(0)
        arts = [{"name": "out.md", "version": 1}] if index == 2 else []
        return _turn(index, artifacts=arts), "s1"

    async def download_artifact(
        self, agent: str, user_id: str, entry: dict[str, Any]
    ) -> bytes | None:
        self.calls.append(("download", entry["name"]))
        return "好".encode()

    async def read_workspace_file(self, agent: str, user_id: str, path: str) -> bytes | None:
        return None

    async def archive(self, agent: str, user_id: str, session_id: str) -> None:
        self.calls.append(("archive", session_id))


def _fixtures(tmp_path: Path) -> Path:
    (tmp_path / "a.md").write_text("原文\n", encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_run_case_once_wires_uploads_session_and_archive(tmp_path: Path) -> None:
    client = FakeClient()
    record, files = await runner.run_case_once(
        client,
        _case(),
        rep=1,
        label="base",
        agent_map={"eval-general": "eval-general-b"},
        fixtures_dir=_fixtures(tmp_path),
    )
    runs = [c[1] for c in client.calls if c[0] == "run_turn"]
    assert runs[0] == ("eval-general-b", "s1", "一", ["upl_1"], 1)
    assert runs[1] == ("eval-general-b", "s1", "二", [], 2)
    assert record.session_id == "s1" and record.user_id.startswith("b140-base-g99-x-1-")
    assert files == {("artifact", "*.md"): ("out.md", "好".encode())}
    assert client.calls[-1] == ("archive", "s1")


@pytest.mark.asyncio
async def test_run_one_scores_a_passing_case(tmp_path: Path) -> None:
    res = await runner.run_one(
        FakeClient(), _case(), 1, label="base", agent_map={}, fixtures_dir=_fixtures(tmp_path)
    )
    assert res.passed is True and [v.passed for v in res.verdicts] == [True, True]
    assert res.metrics == {"tool_calls": 0.0, "wall_s": 2.0, "tokens_in": 200.0, "tokens_out": 20.0}


@pytest.mark.asyncio
async def test_run_one_retries_transport_errors_then_records_infra(tmp_path: Path) -> None:
    fx = _fixtures(tmp_path)
    flaky = FakeClient([httpx.ConnectError("boom"), StreamIncompleteError("cut")])
    assert (
        await runner.run_one(flaky, _case(), 1, label="b", agent_map={}, fixtures_dir=fx)
    ).passed is True
    dead = FakeClient([httpx.ConnectError("boom")] * 3)
    res = await runner.run_one(dead, _case(), 1, label="b", agent_map={}, fixtures_dir=fx)
    assert res.passed is None and "attempt 3" in (res.infra_error or "")


@pytest.mark.asyncio
async def test_run_one_does_not_retry_4xx(tmp_path: Path) -> None:
    req = httpx.Request("POST", "http://t/v1/agents/eval-general/runs")
    err = httpx.HTTPStatusError("bad", request=req, response=httpx.Response(422, request=req))
    client = FakeClient([err, err, err])
    res = await runner.run_one(
        client, _case(), 1, label="b", agent_map={}, fixtures_dir=_fixtures(tmp_path)
    )
    assert res.passed is None and "HTTP 422" in (res.infra_error or "")
    assert sum(1 for c in client.calls if c[0] == "run_turn") == 1


@pytest.mark.asyncio
async def test_run_suite_writes_header_and_one_line_per_rep(tmp_path: Path) -> None:
    out = tmp_path / "out" / "r.jsonl"
    header = ResultHeader(
        label="b",
        started_at="t",
        base_url="http://localhost",
        case_set_hash="h" * 12,
        repeats=3,
        case_ids=["g99-x"],
    )
    results = await runner.run_suite(
        FakeClient(),
        [_case()],
        header=header,
        out_path=out,
        repeats=3,
        concurrency=2,
        agent_map={},
        fixtures_dir=_fixtures(tmp_path),
    )
    got_header, got = read_results(out)
    assert got_header == header
    assert sorted(r.rep for r in got) == [1, 2, 3] and len(results) == 3


def test_check_base_url() -> None:
    runner.check_base_url("https://expert-work-test.deepaihealth.com")
    runner.check_base_url("http://localhost:8000")
    with pytest.raises(SystemExit):
        runner.check_base_url("https://expert-work.deepaihealth.com")


def test_find_artifact_prefers_latest() -> None:
    turns = [
        _turn(1, artifacts=[{"name": "a.docx", "version": 1}]),
        _turn(2, artifacts=[{"name": "b.docx", "version": 1}, {"name": "c.docx", "version": 1}]),
    ]
    assert runner.find_artifact(turns, "*.docx") == {"name": "c.docx", "version": 1}
    assert runner.find_artifact(turns, "*.pptx") is None


def test_metrics_skip_tokens_when_any_turn_lacks_usage() -> None:
    rec = RunRecord(
        case_id="g99-x",
        rep=1,
        user_id="u",
        turns=[_turn(1), _turn(2, input_tokens=None, output_tokens=None)],
    )
    assert runner.metrics_of(rec) == {"tool_calls": 0.0, "wall_s": 2.0}


def test_main_refuses_missing_token_and_existing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXPERT_WORK_API_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="EXPERT_WORK_API_TOKEN"):
        runner.main(["--label", "x", "--base-url", "http://localhost"])
    monkeypatch.setenv("EXPERT_WORK_API_TOKEN", "test-token")
    out = tmp_path / "x.jsonl"
    out.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit, match="exists"):
        runner.main(["--label", "x", "--base-url", "http://localhost", "--out", str(out)])
