"""B-302 PR 2 A/B —— 真跑那一段(``b302_live``):止损、计费、断流、续跑、清理。

全部离线:``httpx.MockTransport`` 假装平台,不碰任何模型(独立审查 HIGH-1/2、MEDIUM-1/3、LOW-3/6)。
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import b302_live as live
import httpx
import pytest

from control_plane.api._run_usage import usage_buckets_to_wire
from expert_work.persistence.token_usage_store import ModelTokenTotals

_PRICES = {"glm-5.3-flash": (0.8, 2.8, 0.23)}


def _wire(**kw: Any) -> list[dict[str, Any]]:
    """平台对外 ``usage_by_model`` 的真实形状(直接用平台的装配函数生成)。"""
    base = {"provider": "glm", "model": "glm-5.3-flash", "input_tokens": 0, "output_tokens": 0}
    return usage_buckets_to_wire([ModelTokenTotals(**{**base, **kw})])


def _sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


def _ai(content: str = "ok", *, tokens_in: int = 100, calls: list[dict] | None = None) -> dict:
    return {
        "agent": {
            "messages": [
                {
                    "type": "ai",
                    "content": content,
                    "tool_calls": calls or [],
                    "additional_kwargs": {},
                    "usage_metadata": {"input_tokens": tokens_in, "output_tokens": 10},
                }
            ]
        }
    }


def _ooxml(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return buffer.getvalue()


class _Platform:
    """可编排的假平台:记下每个请求;``/runs`` 的回包由 ``run_body`` 决定。"""

    def __init__(
        self,
        *,
        usage: list[dict[str, Any]] | None = None,
        end: bool = True,
        run_error: Callable[[int], bool] | None = None,
        usage_endpoint: dict[str, Any] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        workspace: dict[str, bytes] | None = None,
        steps: list[dict[str, Any]] | None = None,
    ) -> None:
        self.seen: list[str] = []
        self.runs = 0
        self.usage = usage if usage is not None else _wire(input_tokens=1000, output_tokens=10)
        self.end = end
        self.run_error = run_error
        self.usage_endpoint = usage_endpoint
        self.artifacts = artifacts or []
        self.workspace = workspace or {}
        self.steps = steps or [_ai()]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.seen.append(f"{request.method} {path}")
        if path.endswith("/uploads"):
            return httpx.Response(200, json={"data": {"upload_id": "u1", "session_id": "s1"}})
        if path.endswith("/runs") and request.method == "POST":
            self.runs += 1
            if self.run_error is not None and self.run_error(self.runs):
                raise httpx.ConnectError("platform went away")
            run_id = f"r{self.runs}"
            events: list[tuple[str, dict[str, Any]]] = [
                ("metadata", {"run_id": run_id, "thread_id": "s1"})
            ]
            events.extend(("updates", s) for s in self.steps)
            if self.end:
                events.append(
                    (
                        "end",
                        {
                            "run_id": run_id,
                            "exit_reason": "text_response",
                            "status": "success",
                            "artifacts": self.artifacts,
                            "usage_by_model": self.usage,
                        },
                    )
                )
            return httpx.Response(
                200, content=_sse(events), headers={"content-type": "text/event-stream"}
            )
        if path.endswith("/usage"):
            return httpx.Response(200, json=self.usage_endpoint or {"run_id": "r"})
        if path.endswith("/artifacts/download"):
            return httpx.Response(200, content=_ooxml({"ppt/slides/slide1.xml": "<a:t>X</a:t>"}))
        if path.endswith("/workspace/file"):
            data = self.workspace.get(str(request.url.params.get("path")))
            return httpx.Response(200, content=data) if data is not None else httpx.Response(404)
        if request.method == "DELETE":
            return httpx.Response(200, json={})
        return httpx.Response(404)


def _doc(tmp_path: Path, *, deliverable: str = "slides") -> dict[str, Any]:
    (tmp_path / "d1.pdf").write_bytes(b"%PDF-1.4 synthetic")
    return {
        "doc_id": "d1",
        "deliverable": deliverable,
        "template": None,
        "prompts": {"turn1": "t1", "turn2": "t2", "turn3": "t3"},
    }


def _session(**kw: Any) -> live.Session:
    base: dict[str, Any] = {"phase": "main", "model": "glm-5.3-flash", "arm": "A", "doc_id": "d1"}
    return live.Session(**{**base, "turns": 3, **kw})


async def _run_session(
    platform: _Platform, tmp_path: Path, tracker: live.SpendTracker, **kw: Any
) -> dict[str, Any]:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(platform), base_url="http://platform"
    ) as http:
        return await live.run_session(
            http,
            _session(),
            doc=_doc(tmp_path, **kw),
            docs_dir=tmp_path,
            agent="eval-b302-glm-a",
            tracker=tracker,
        )


# ------------------------------------------------------------ spend (MEDIUM-1, HIGH-2)


def test_cache_reads_are_billed_from_the_real_wire_shape() -> None:
    usage = _wire(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=400_000)
    assert "cache_read_tokens" in usage[0]  # 顶层字段,不是 input_token_details.cache_read
    tracker = live.SpendTracker(stop_line=10.0, prices=_PRICES)
    assert tracker.add(usage) == pytest.approx(0.6 * 0.8 + 0.4 * 0.23 + 0.1 * 2.8)


def test_an_unpriced_model_blocks_instead_of_crashing() -> None:
    tracker = live.SpendTracker(stop_line=10.0, prices=_PRICES)
    usage = [*_wire(input_tokens=1_000_000), *_wire(model="someone-else", input_tokens=5)]
    assert tracker.add(usage) == pytest.approx(0.8)
    assert tracker.may_start() is False
    assert tracker.blocked is not None and "someone-else" in tracker.blocked


def test_unknown_usage_blocks() -> None:
    tracker = live.SpendTracker(stop_line=10.0, prices=_PRICES)
    tracker.add(None)
    assert tracker.may_start() is False
    assert tracker.blocked is not None and "usage" in tracker.blocked


# ------------------------------------------------------------ HIGH-1: per-turn stop line


async def test_a_session_stops_between_turns_once_the_line_is_crossed(tmp_path: Path) -> None:
    platform = _Platform(usage=_wire(input_tokens=2_000_000))  # ¥1.6 一轮
    tracker = live.SpendTracker(stop_line=1.0, prices=_PRICES)
    record = await _run_session(platform, tmp_path, tracker)
    assert platform.runs == 1
    assert [t["turn"] for t in record["turns"]] == [1]
    assert record["stopped"] is not None and "stop line" in record["stopped"]
    assert any(s.startswith("DELETE") for s in platform.seen)  # 会话照样归档


# ------------------------------------------------------------ HIGH-2: no end / no usage


async def test_a_missing_end_frame_is_filled_from_the_usage_endpoint(tmp_path: Path) -> None:
    platform = _Platform(
        end=False,
        usage_endpoint={"run_id": "r1", "usage_by_model": _wire(input_tokens=1_000_000)},
    )
    tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES)
    record = await _run_session(platform, tmp_path, tracker)
    assert any("/runs/r1/usage" in s for s in platform.seen)
    assert tracker.spent == pytest.approx(0.8 * platform.runs)
    assert tracker.blocked is None
    assert record["turns"][0]["usage_source"] == "usage_endpoint"


async def test_no_usage_anywhere_blocks_further_dispatch(tmp_path: Path) -> None:
    platform = _Platform(end=False, usage_endpoint={"run_id": "r1"})
    tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES)
    record = await _run_session(platform, tmp_path, tracker)
    assert platform.runs == 1
    assert tracker.blocked is not None and "r1" in tracker.blocked
    assert record["stopped"] is not None


async def test_steps_without_input_tokens_block_the_run(tmp_path: Path) -> None:
    """待确认 1:updates 帧不带 ``usage_metadata`` 时 25 万护栏失效 —— 停下,不假装在护。"""
    platform = _Platform(steps=[_ai(tokens_in=0), _ai(tokens_in=0)])
    tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES)
    await _run_session(platform, tmp_path, tracker)
    assert platform.runs == 1
    assert tracker.blocked is not None and "usage_metadata" in tracker.blocked


# ------------------------------------------------------------ 待确认 3: unregistered deliverable


async def test_an_unregistered_deliverable_is_read_from_the_workspace(tmp_path: Path) -> None:
    write = {
        "name": "exec_python",
        "args": {"code": "prs.save('/workspace/out/report.pptx')"},
        "id": "c",
    }
    pptx = _ooxml({"ppt/slides/slide1.xml": "<a:t>VALUE</a:t>"})
    platform = _Platform(
        steps=[_ai(calls=[write]), _ai()],
        workspace={"threads/s1/work/out/report.pptx": pptx},
    )
    tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES)
    record = await _run_session(platform, tmp_path, tracker)
    second = record["turns"][1]
    assert second["deliverable_source"] == "workspace_unregistered"
    assert second["deliverable_text"] == "VALUE"


async def test_no_deliverable_at_all_is_marked_missing(tmp_path: Path) -> None:
    platform = _Platform()
    tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES)
    record = await _run_session(platform, tmp_path, tracker)
    assert record["turns"][1]["deliverable_source"] == "missing"
    registered = _Platform(artifacts=[{"name": "x.pptx", "version": 1}])
    record = await _run_session(registered, tmp_path, live.SpendTracker(100.0, _PRICES))
    assert record["turns"][1]["deliverable_source"] == "registered"


# ------------------------------------------------------------ run_phase (HIGH-1/2, MEDIUM-3, LOW-6)


class _Slow(httpx.AsyncBaseTransport):
    """每个请求先让出 10 毫秒 —— 不然 MockTransport 不让出事件循环,并发的会话会被串成
    一个接一个。"""

    def __init__(self, handler: _Platform) -> None:
        self._inner = httpx.MockTransport(handler)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)
        return await self._inner.handle_async_request(request)


async def _run_phase(
    platform: _Platform, tmp_path: Path, sessions: list[live.Session], tracker: live.SpendTracker
) -> list[dict[str, Any]]:
    docs = {"d1": _doc(tmp_path), "d2": {**_doc(tmp_path), "doc_id": "d2"}}
    (tmp_path / "d2.pdf").write_bytes(b"%PDF-1.4 synthetic")
    async with httpx.AsyncClient(transport=_Slow(platform), base_url="http://platform") as http:
        await live.run_phase(
            sessions,
            docs=docs,
            docs_dir=tmp_path,
            out=tmp_path / "out",
            http=http,
            tracker=tracker,
            vendor={"glm-5.3-flash": "glm"},
            agent_for=lambda s: "eval-b302-glm-a",
            quote_id="q-1",
            emit=lambda _line: None,
        )
    sink = tmp_path / "out" / "records.jsonl"
    return [json.loads(x) for x in sink.read_text().splitlines()] if sink.exists() else []


async def test_the_phase_stops_dispatching_at_the_stop_line(tmp_path: Path) -> None:
    sessions = [_session(doc_id=d, repeat=r) for d in ("d1", "d2") for r in range(2)]
    platform = _Platform(usage=_wire(input_tokens=2_000_000))
    tracker = live.SpendTracker(stop_line=1.0, prices=_PRICES)
    records = await _run_phase(platform, tmp_path, sessions, tracker)
    # 并发 2 路:最多 2 个会话各跑完手上的 1 轮;其余不再开始。
    assert platform.runs == 2
    assert all(r["quote_id"] == "q-1" for r in records)
    assert all(r["stopped"] for r in records)


async def test_one_failing_session_blocks_the_rest_but_does_not_cancel_them(
    tmp_path: Path,
) -> None:
    sessions = [_session(doc_id="d1"), _session(doc_id="d2")]
    platform = _Platform(run_error=lambda n: n == 1)
    tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES)
    records = await _run_phase(platform, tmp_path, sessions, tracker)
    assert tracker.blocked is not None and "ConnectError" in tracker.blocked
    errors = [r for r in records if r.get("error")]
    assert len(errors) == 1
    # 另一个在跑的会话没被取消:它的那一轮跑完并记了账、写了记录(之后因阻塞不再开下一轮)。
    others = [r for r in records if not r.get("error")]
    assert len(others) == 1 and len(others[0]["turns"]) >= 1
    assert tracker.spent > 0
    assert platform.runs <= 3


async def test_a_rerun_skips_sessions_already_recorded_complete(tmp_path: Path) -> None:
    sessions = [_session(doc_id="d1", turns=1), _session(doc_id="d2", turns=1)]
    platform = _Platform()
    tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES)
    await _run_phase(platform, tmp_path, sessions, tracker)
    assert platform.runs == 2
    again = _Platform()
    records = await _run_phase(again, tmp_path, sessions, live.SpendTracker(100.0, _PRICES))
    assert again.runs == 0
    assert len(records) == 2


def test_records_are_deduplicated_keeping_the_last() -> None:
    first = {**live.session_fields(_session()), "turns": [], "stopped": "stop line"}
    last = {**live.session_fields(_session()), "turns": [{"turn": 1}], "stopped": None}
    other = {**live.session_fields(_session(arm="C")), "turns": []}
    assert live.dedupe([first, other, last]) == [other, last]


# ------------------------------------------------------------ LOW-3: cleanup


async def test_cleanup_is_a_dry_run_unless_told_yes() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.method == "GET":
            items = [
                {"name": "eval-b302-glm-5-3-flash-a", "version": "1.0.0"},
                {"name": "eval-b302-deepseek-flash-c", "version": "1.0.0"},
                {"name": "customer-agent", "version": "2.0.0"},
            ]
            return httpx.Response(200, json={"success": True, "data": {"items": items}})
        return httpx.Response(204)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://platform"
    ) as http:
        listed = await live.cleanup(http, yes=False, emit=lambda _l: None)
        assert [n for n, _v in listed] == [
            "eval-b302-glm-5-3-flash-a",
            "eval-b302-deepseek-flash-c",
        ]
        assert not any(s.startswith("DELETE") for s in seen)
        await live.cleanup(http, yes=True, emit=lambda _l: None)
    deletes = [s for s in seen if s.startswith("DELETE")]
    assert deletes == [
        "DELETE /v1/agents/eval-b302-glm-5-3-flash-a/1.0.0",
        "DELETE /v1/agents/eval-b302-deepseek-flash-c/1.0.0",
    ]
