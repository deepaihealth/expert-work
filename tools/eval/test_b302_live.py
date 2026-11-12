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
from expert_work.runtime.runs import external_run_status

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
        usage_status: list[str] | None = None,
        endpoint_usage: dict[str, list[dict[str, Any]] | None] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        workspace: dict[str, bytes] | None = None,
        steps: list[dict[str, Any]] | None = None,
        broken_stream: bool = False,
        download: bytes | None = None,
        break_before_run_id: bool = False,
        runs_status: int = 200,
        cancel_error: bool = False,
        usage_error: bool = False,
        usage_http_status: int = 200,
    ) -> None:
        self.seen: list[str] = []
        self.runs = 0
        self.usage = usage if usage is not None else _wire(input_tokens=1000, output_tokens=10)
        self.end = end
        self.run_error = run_error
        #: ``GET .../usage`` 依次回的 ``run_status``(最后一个一直重复)与各状态下的用量。
        self.usage_status = usage_status or ["success"]
        self.endpoint_usage = endpoint_usage or {}
        self.usage_calls = 0
        self.cancels = 0
        self.broken_stream = broken_stream
        self.download = download
        #: 流断在第一帧之前(请求已发出,但没收到 run id)。
        self.break_before_run_id = break_before_run_id
        #: ``POST /runs`` 的状态码(429 / 409 / 422 = 运行没创建)。
        self.runs_status = runs_status
        #: 取消 / 补账接口本身连不上。
        self.cancel_error = cancel_error
        self.usage_error = usage_error
        #: 补账接口回的 HTTP 状态码(非 200 = 查不到,``run_status`` 也就不知道)。
        self.usage_http_status = usage_http_status
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
            if self.runs_status != 200:
                return httpx.Response(self.runs_status, json={"error": "busy"})
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
            if self.broken_stream or self.break_before_run_id:
                head = b"" if self.break_before_run_id else _sse(events[:1])
                return httpx.Response(
                    200, stream=_Breaks(head), headers={"content-type": "text/event-stream"}
                )
            return httpx.Response(
                200, content=_sse(events), headers={"content-type": "text/event-stream"}
            )
        if path.endswith(":cancel"):
            self.cancels += 1
            if self.cancel_error:
                raise httpx.ConnectError("platform went away")
            return httpx.Response(200, json={"success": True})
        if path.endswith("/usage"):
            if self.usage_error:
                raise httpx.ConnectError("platform went away")
            if self.usage_http_status != 200:
                return httpx.Response(self.usage_http_status, json={"error": "unavailable"})
            return httpx.Response(200, json=self._usage_body(path.split("/")[-2]))
        if path.endswith("/artifacts/download"):
            body = self.download or _ooxml({"ppt/slides/slide1.xml": "<a:t>X</a:t>"})
            return httpx.Response(200, content=body)
        if path.endswith("/workspace/file"):
            data = self.workspace.get(str(request.url.params.get("path")))
            return httpx.Response(200, content=data) if data is not None else httpx.Response(404)
        if request.method == "DELETE":
            return httpx.Response(200, json={})
        return httpx.Response(404)

    def _usage_body(self, run_id: str) -> dict[str, Any]:
        """与 ``external_runs.run_usage`` 同形:``run_status`` 恒有,``usage_by_model`` 无记录时
        缺席。"""
        status = self.usage_status[min(self.usage_calls, len(self.usage_status) - 1)]
        self.usage_calls += 1
        body: dict[str, Any] = {"run_id": run_id, "run_status": external_run_status(status, None)}
        usage = self.endpoint_usage.get(status)
        if usage is not None:
            body["usage_by_model"] = usage
        return body


class _Breaks(httpx.AsyncByteStream):
    """先给一段正常的帧,然后连接断掉。"""

    def __init__(self, head: bytes) -> None:
        self._head = head

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield self._head
        raise httpx.ReadError("connection reset mid-stream")


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
    platform = _Platform(end=False, endpoint_usage={"success": _wire(input_tokens=1_000_000)})
    tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES)
    record = await _run_session(platform, tmp_path, tracker)
    assert any("/runs/r1/usage" in s for s in platform.seen)
    assert tracker.spent == pytest.approx(0.8 * platform.runs)
    assert tracker.blocked is None
    assert record["turns"][0]["usage_source"] == "usage_endpoint"


async def test_no_usage_anywhere_blocks_further_dispatch(tmp_path: Path) -> None:
    platform = _Platform(end=False)  # 终态,但没有用量记录
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


async def test_cleanup_deletes_only_the_exact_codes_across_pages() -> None:
    """复审 R4:只删 ``write_manifests`` 会生成的确切 code;按 offset 翻页;打印租户与数量。"""
    seen: list[str] = []
    tenant = "11111111-1111-1111-1111-111111111111"
    names = [f"agent-{i}" for i in range(5)] + [
        "eval-b302-glm-5-3-flash-a",
        "eval-b302-glm-5-3-flash-a-copy",  # 前缀对得上,但不是生成的 code
        "eval-b302-deepseek-flash-c",
    ]
    rows = [{"name": n, "version": "1.0.0", "tenant_id": tenant} for n in names]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}?{request.url.query.decode()}")
        if request.method == "GET":
            offset = int(request.url.params["offset"])
            limit = int(request.url.params["limit"])
            items = rows[offset : offset + limit]
            return httpx.Response(200, json={"success": True, "data": {"items": items}})
        return httpx.Response(204)

    codes = {"eval-b302-glm-5-3-flash-a", "eval-b302-deepseek-flash-c", "eval-b302-x-b"}
    lines: list[str] = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://platform"
    ) as http:
        listed = await live.cleanup(http, codes=codes, yes=False, emit=lines.append, page=3)
        assert sorted(n for n, _v in listed) == [
            "eval-b302-deepseek-flash-c",
            "eval-b302-glm-5-3-flash-a",
        ]
        assert not any(s.startswith("DELETE") for s in seen)
        assert sum(s.startswith("GET") for s in seen) >= 3  # 8 行、每页 3 行:翻了页
        assert any(tenant in line for line in lines)
        assert any("2 matching" in line for line in lines)
        await live.cleanup(http, codes=codes, yes=True, emit=lines.append, page=3)
    deletes = sorted(s.split("?")[0] for s in seen if s.startswith("DELETE"))
    assert deletes == [
        "DELETE /v1/agents/eval-b302-deepseek-flash-c/1.0.0",
        "DELETE /v1/agents/eval-b302-glm-5-3-flash-a/1.0.0",
    ]


# ------------------------------------------------------------ 复审 R1:补账接口说 run 还在跑


@pytest.fixture
def _fast_settle(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """假时钟(复审 LOW-b):轮询里的「睡」只推进假时钟,超时由调用次数决定,不受 GC 停顿影响。"""
    clock = [0.0]
    sleeps: list[float] = []

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(live, "_now", lambda: clock[0])
    monkeypatch.setattr(live, "_sleep", _sleep)
    return sleeps


@pytest.mark.usefixtures("_fast_settle")
async def test_a_running_run_is_cancelled_and_billed_only_once_it_settles(tmp_path: Path) -> None:
    partial, full = _wire(input_tokens=100_000), _wire(input_tokens=1_000_000)
    platform = _Platform(
        end=False,
        usage_status=["running", "running", "interrupted"],
        endpoint_usage={"running": partial, "interrupted": full},
    )
    tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES)
    record = await _run_session(platform, tmp_path, tracker)
    first = record["turns"][0]
    assert platform.cancels >= 1
    assert first["usage_source"] == "usage_endpoint"
    assert first["usage_by_model"] == full  # 不是还在跑时那份「到目前为止」的
    assert tracker.blocked is None


@pytest.mark.usefixtures("_fast_settle")
async def test_a_run_that_never_settles_is_charged_the_cap_and_blocks(tmp_path: Path) -> None:
    platform = _Platform(
        end=False, usage_status=["running"], endpoint_usage={"running": _wire(input_tokens=10)}
    )
    tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES, run_caps={"glm-5.3-flash": 23.36})
    record = await _run_session(platform, tmp_path, tracker)
    assert platform.runs == 1
    assert tracker.spent == pytest.approx(23.36)
    assert tracker.blocked is not None and "did not settle" in tracker.blocked
    assert record["turns"][0]["usage_source"] == "cap_charged"


async def test_settling_polls_on_the_clock_until_the_timeout(
    tmp_path: Path, _fast_settle: list[float]
) -> None:
    platform = _Platform(end=False, usage_status=["running"])
    tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES, run_caps={"glm-5.3-flash": 1.0})
    await _run_session(platform, tmp_path, tracker)
    assert _fast_settle == [live.SETTLE_POLL_S] * int(live.SETTLE_TIMEOUT_S / live.SETTLE_POLL_S)
    assert platform.usage_calls == len(_fast_settle) + 1


# ------------------------------------------------------------ 复审 R3:流中途断掉


@pytest.mark.usefixtures("_fast_settle")
async def test_a_broken_stream_cancels_and_bills_the_run_then_raises(tmp_path: Path) -> None:
    platform = _Platform(
        broken_stream=True,
        usage_status=["interrupted"],
        endpoint_usage={"interrupted": _wire(input_tokens=1_000_000)},
    )
    tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES)
    with pytest.raises(httpx.ReadError):
        await _run_session(platform, tmp_path, tracker)
    assert platform.cancels == 1
    assert tracker.spent == pytest.approx(0.8)
    assert any(s.startswith("DELETE") for s in platform.seen)


@pytest.mark.usefixtures("_fast_settle")
async def test_a_broken_stream_without_usage_is_charged_the_cap(tmp_path: Path) -> None:
    platform = _Platform(broken_stream=True, usage_status=["interrupted"])
    tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES, run_caps={"glm-5.3-flash": 23.36})
    with pytest.raises(httpx.ReadError):
        await _run_session(platform, tmp_path, tracker)
    assert tracker.spent == pytest.approx(23.36)
    assert tracker.blocked is not None


# ------------------------------------------------------------ 复审 R2:交付件读不了


async def test_an_unreadable_deliverable_is_scored_as_none_and_the_session_goes_on(
    tmp_path: Path,
) -> None:
    platform = _Platform(artifacts=[{"name": "x.pptx", "version": 1}], download=b"not a zip")
    tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES)
    record = await _run_session(platform, tmp_path, tracker)
    assert [t["turn"] for t in record["turns"]] == [1, 2, 3]
    second = record["turns"][1]
    assert second["deliverable_source"] == "unreadable"
    assert second["deliverable_text"] is None
    assert second["deliverable_structure"] is None


# ------------------------------------------------------------ 复审 R5:交付件路径


def _call(tool: str, **args: Any) -> Any:
    import b302_metrics as m

    return m.Step(index=0, calls=(m.Call(tool, args),))


def test_path_arguments_come_first_and_parent_paths_are_dropped() -> None:
    steps = [
        _call("exec_python", code="prs.save('decks/old deck.pptx')"),
        _call("write_file", path="/workspace/out dir/my report.pptx", content="x"),
        _call("save_artifact", name="final.pptx", path="reports/final v2.pptx"),
        _call("bash", command="cp a.pptx ../escape.pptx"),
        _call("write_file", path="../outside.pptx", content="x"),
    ]
    found = live.deliverable_candidates(steps, ".pptx")
    assert found[:2] == ["reports/final v2.pptx", "out dir/my report.pptx"]
    assert "decks/old deck.pptx" in found
    assert all(".." not in c for c in found)


# ------------------------------------------------------------ 第三轮 MEDIUM-R3':补账时网络也断了


_CAPS = {"glm-5.3-flash": 23.36}


@pytest.mark.usefixtures("_fast_settle")
async def test_a_dead_network_while_settling_charges_the_cap_and_keeps_the_original_error(
    tmp_path: Path,
) -> None:
    platform = _Platform(broken_stream=True, cancel_error=True, usage_error=True)
    tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES, run_caps=_CAPS)
    with pytest.raises(httpx.ReadError):  # 原来的异常,不是取消 / 补账时的 ConnectError
        await _run_session(platform, tmp_path, tracker)
    assert tracker.spent == pytest.approx(23.36)
    assert tracker.blocked is not None


@pytest.mark.usefixtures("_fast_settle")
async def test_no_end_frame_and_a_dead_usage_endpoint_charges_the_cap(tmp_path: Path) -> None:
    platform = _Platform(end=False, usage_error=True)
    tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES, run_caps=_CAPS)
    record = await _run_session(platform, tmp_path, tracker)
    assert tracker.spent == pytest.approx(23.36)
    assert tracker.blocked is not None and "ConnectError" in tracker.blocked
    assert record["turns"][0]["usage_source"] == "cap_charged"
    assert record["stopped"] is not None


# ------------------------------------------------------------ 第三轮 LOW-a:根本没发生模型调用的运行


async def test_a_refused_run_is_billed_zero_and_blocks(tmp_path: Path) -> None:
    for status in (429, 409, 422):
        platform = _Platform(runs_status=status)
        tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES, run_caps=_CAPS)
        with pytest.raises(httpx.HTTPStatusError):
            await _run_session(platform, tmp_path, tracker)
        assert tracker.spent == 0.0
        assert tracker.blocked is not None and str(status) in tracker.blocked


async def test_a_run_that_could_not_be_sent_is_billed_zero(tmp_path: Path) -> None:
    platform = _Platform(run_error=lambda _n: True)  # ConnectError:请求没发出去
    tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES, run_caps=_CAPS)
    with pytest.raises(httpx.ConnectError):
        await _run_session(platform, tmp_path, tracker)
    assert tracker.spent == 0.0
    assert tracker.blocked is not None


async def test_a_stream_that_breaks_before_the_run_id_is_charged_the_cap(tmp_path: Path) -> None:
    platform = _Platform(break_before_run_id=True)
    tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES, run_caps=_CAPS)
    with pytest.raises(httpx.ReadError):
        await _run_session(platform, tmp_path, tracker)
    assert tracker.spent == pytest.approx(23.36)
    assert tracker.blocked is not None


@pytest.mark.usefixtures("_fast_settle")
async def test_a_run_that_settled_without_any_model_step_is_billed_zero(tmp_path: Path) -> None:
    platform = _Platform(end=False, steps=[], usage_status=["error"])  # 终态,没有用量,没有一步
    platform.steps = []
    tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES, run_caps=_CAPS)
    record = await _run_session(platform, tmp_path, tracker)
    assert tracker.spent == 0.0
    assert tracker.blocked is not None and "no model step" in tracker.blocked
    assert record["turns"][0]["usage_source"] == "none_no_steps"


async def test_an_end_frame_without_any_model_step_blocks(tmp_path: Path) -> None:
    platform = _Platform(usage=[])
    platform.steps = []
    tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES, run_caps=_CAPS)
    await _run_session(platform, tmp_path, tracker)
    assert tracker.spent == 0.0
    assert tracker.blocked is not None and "no model step" in tracker.blocked


# ---------------------------------------------- 第四轮 LOW-1 / LOW-2:状态不明就按上限计


@pytest.mark.usefixtures("_fast_settle")
async def test_an_unknown_run_status_with_no_steps_is_cancelled_and_charged_the_cap(
    tmp_path: Path,
) -> None:
    # 没有 end 帧、一步都没看到、补账接口 500:不知道这次运行停没停 —— 不能当终态记 0。
    platform = _Platform(end=False, usage_http_status=500)
    platform.steps = []
    tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES, run_caps=_CAPS)
    record = await _run_session(platform, tmp_path, tracker)
    assert platform.cancels >= 1
    assert tracker.spent == pytest.approx(23.36)
    assert tracker.blocked is not None
    assert record["turns"][0]["usage_source"] == "cap_charged"


async def test_a_5xx_on_post_runs_is_charged_the_cap(tmp_path: Path) -> None:
    # 网关 502 / 504 时上游可能已经建好并在跑这次运行,而我们拿不到 run id 去取消。
    for status in (502, 504):
        platform = _Platform(runs_status=status)
        tracker = live.SpendTracker(stop_line=100.0, prices=_PRICES, run_caps=_CAPS)
        with pytest.raises(httpx.HTTPStatusError):
            await _run_session(platform, tmp_path, tracker)
        assert tracker.spent == pytest.approx(23.36)
        assert tracker.blocked is not None
