"""B-140 behavior_client 单元测试(httpx.MockTransport,不连真栈)。"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from behavior_client import ExternalClient, SseParser, StreamIncompleteError


def _sse(*frames: tuple[str, object]) -> str:
    out = [": keepalive", ""]
    for event, data in frames:
        out += [f"event: {event}", f"data: {json.dumps(data, ensure_ascii=False)}", ""]
    return "\n".join(out) + "\n"


_FRAMES = [
    ("metadata", {"run_id": "r1", "thread_id": "s1"}),
    (
        "updates",
        {
            "agent": {
                "messages": [
                    {
                        "type": "ai",
                        "content": "",
                        "tool_calls": [
                            {"name": "edit_file", "args": {"path": "report.md"}, "id": "c1"}
                        ],
                    }
                ]
            }
        },
    ),
    (
        "updates",
        {
            "tools": {
                "messages": [
                    {"type": "tool", "name": "edit_file", "status": "error", "content": "x"}
                ]
            }
        },
    ),
    (
        "updates",
        {
            "agent": {
                "messages": [
                    {
                        "type": "ai",
                        "content": [{"type": "text", "text": "已改好"}],
                        "tool_calls": [],
                    }
                ]
            }
        },
    ),
    ("token", {"t": "已"}),
    (
        "end",
        {
            "status": "success",
            "run_id": "r1",
            "completed": True,
            "exit_reason": "text_response",
            "artifacts": [{"name": "report.md", "version": 2}],
            "usage_by_model": [
                {"provider": "glm", "model": "glm-5.3", "input_tokens": 1000, "output_tokens": 40},
                {
                    "provider": "glm",
                    "model": "glm-5.3-flash",
                    "input_tokens": 200,
                    "output_tokens": 10,
                },
            ],
        },
    ),
]


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> ExternalClient:
    return ExternalClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://t")
    )


def test_parser_handles_comments_and_multiline_data() -> None:
    p = SseParser()
    got = [p.feed(line) for line in [": ping", "event: end", 'data: {"a":', "data: 1}", ""]]
    assert got[-1] == ("end", '{"a":\n1}')
    assert json.loads(got[-1][1]) == {"a": 1}
    assert [g for g in got[:-1] if g is not None] == []


@pytest.mark.asyncio
async def test_run_turn_collects_tools_text_end_and_usage() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, text=_sse(*_FRAMES), headers={"content-type": "text/event-stream"}
        )

    rec, session_id = await _client(handler).run_turn(
        "eval-general", "u1", None, "改三处", ["upl_1"], 1
    )
    assert seen["path"] == "/v1/agents/eval-general/runs"
    assert seen["body"] == {
        "user_id": "u1",
        "input": "改三处",
        "mode": "stream",
        "files": [{"upload_id": "upl_1"}],
    }
    assert session_id == "s1"
    assert rec.run_id == "r1" and rec.status == "success" and rec.completed is True
    assert [c.name for c in rec.tool_calls] == ["edit_file"] and rec.tool_calls[0].args == {
        "path": "report.md"
    }
    assert rec.tool_errors == 1
    assert rec.final_text == "已改好"
    assert rec.artifacts == [{"name": "report.md", "version": 2}]
    assert (rec.input_tokens, rec.output_tokens) == (1200, 50)


@pytest.mark.asyncio
async def test_run_turn_continues_session_and_keeps_usage_absent() -> None:
    frames = [f for f in _FRAMES if f[0] != "end"] + [
        ("end", {"status": "success", "run_id": "r1"})
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["session_id"] == "s0"
        return httpx.Response(200, text=_sse(*frames))

    rec, _ = await _client(handler).run_turn("eval-general", "u1", "s0", "再改", [], 2)
    assert rec.index == 2
    assert rec.input_tokens is None and rec.output_tokens is None
    assert rec.completed is None


@pytest.mark.asyncio
async def test_run_turn_without_end_raises_stream_incomplete() -> None:
    frames = [f for f in _FRAMES if f[0] != "end"]
    client = _client(lambda request: httpx.Response(200, text=_sse(*frames)))
    with pytest.raises(StreamIncompleteError):
        await client.run_turn("eval-general", "u1", None, "p", [], 1)


@pytest.mark.asyncio
async def test_upload_sends_multipart_with_user_and_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8", errors="replace")
        assert request.url.path == "/v1/agents/eval-general/uploads"
        assert 'name="user_id"' in body and "u1" in body
        assert "text/markdown" in body and "report.md" in body
        return httpx.Response(
            201, json={"success": True, "data": {"upload_id": "upl_9", "session_id": "s9"}}
        )

    assert await _client(handler).upload(
        "eval-general", "u1", "report.md", "内容".encode(), None
    ) == ("upl_9", "s9")


@pytest.mark.asyncio
async def test_upload_rejects_disallowed_type() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        await _client(lambda r: httpx.Response(500)).upload(
            "eval-general", "u1", "a.py", b"x", None
        )


@pytest.mark.asyncio
async def test_downloads_return_none_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/artifacts/download"):
            assert request.url.params["name"] == "a.docx" and request.url.params["version"] == "3"
            return httpx.Response(200, content=b"DOCX")
        return httpx.Response(404, json={"success": False})

    client = _client(handler)
    assert (
        await client.download_artifact("eval-ahp", "u1", {"name": "a.docx", "version": 3})
        == b"DOCX"
    )
    assert await client.read_workspace_file("eval-ahp", "u1", "report.md") is None


@pytest.mark.asyncio
async def test_archive_passes_user_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/agents/eval-general/sessions/s1"
        assert request.url.params["user_id"] == "u1"
        return httpx.Response(200, json={"success": True})

    await _client(handler).archive("eval-general", "u1", "s1")
