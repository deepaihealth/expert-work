"""B-140 对外平面客户端:上传开局文件、流式跑一轮、取产物 / 工作区文件、归档会话。

只走 ``/v1/agents/{code}/...`` —— API key 打控制台平面(``/v1/sessions/...``)会被
``console_only()`` 直接 403。帧的形态见 ``docs/api/streaming-events.md``。
"""

from __future__ import annotations

import json
import time
from pathlib import PurePosixPath
from typing import Any

import httpx
from behavior_schema import ToolCall, TurnRecord

# 与 ``control_plane.api.uploads._DOC_EXT_BY_CONTENT_TYPE`` 一致(.py 不在其中)。
_CONTENT_TYPES = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_AI_TYPES = {"ai", "AIMessage", "AIMessageChunk"}
_TOOL_TYPES = {"tool", "ToolMessage"}


class StreamIncompleteError(RuntimeError):
    """流在 ``end`` 帧之前结束 —— 记为 infra_error,不算任务不过。"""


def content_type_for(filename: str) -> str:
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix not in _CONTENT_TYPES:
        raise ValueError(
            f"upload type not allowed for {filename!r} (allowed: {sorted(_CONTENT_TYPES)})"
        )
    return _CONTENT_TYPES[suffix]


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(b.get("text", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


class SseParser:
    """逐行喂 SSE;空行结束一个事件。忽略 ``:`` 注释,多行 ``data:`` 用换行拼接。"""

    def __init__(self) -> None:
        self._event = ""
        self._data: list[str] = []

    def feed(self, line: str) -> tuple[str, str] | None:
        if line == "":
            return self.flush()
        if line.startswith(":"):
            return None
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)
        return None

    def flush(self) -> tuple[str, str] | None:
        if not self._data:
            self._event = ""
            return None
        frame = (self._event or "message", "\n".join(self._data))
        self._event, self._data = "", []
        return frame


class TurnBuilder:
    """把一轮的帧累积成 :class:`TurnRecord`。"""

    def __init__(self, index: int) -> None:
        self.index = index
        self.session_id: str | None = None
        self._run_id: str | None = None
        self._end: dict[str, Any] | None = None
        self._final_text = ""
        self._calls: list[ToolCall] = []
        self._tool_errors = 0
        self._compactions = 0

    def on_frame(self, event: str, raw: str) -> None:
        if event == "compaction":  # 一帧 = 一次真正落地的摘要(压缩器每次调用至多发一帧)
            self._compactions += 1
            return
        if event not in {"metadata", "updates", "end"}:
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        if event == "metadata":
            self._run_id = data.get("run_id") or self._run_id
            self.session_id = data.get("thread_id") or self.session_id
        elif event == "updates":
            self._on_updates(data)
        else:
            self._end = data

    def _on_updates(self, data: dict[str, Any]) -> None:
        for node in data.values():
            if not isinstance(node, dict):
                continue
            for msg in node.get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                mtype = msg.get("type")
                if mtype in _AI_TYPES:
                    calls = [c for c in msg.get("tool_calls") or [] if isinstance(c, dict)]
                    for call in calls:
                        args = call.get("args")
                        self._calls.append(
                            ToolCall(
                                turn=self.index,
                                name=str(call.get("name", "")),
                                args=args if isinstance(args, dict) else {},
                            )
                        )
                    text = message_text(msg.get("content"))
                    if text.strip() and not calls:
                        self._final_text = text
                elif mtype in _TOOL_TYPES and msg.get("status") == "error":
                    self._tool_errors += 1

    def result(self, wall_s: float) -> TurnRecord:
        if self._end is None:
            raise StreamIncompleteError(
                f"turn {self.index}: stream ended without an end frame (run {self._run_id})"
            )
        end = self._end
        usage = end.get("usage_by_model")
        tokens_in: int | None = None
        tokens_out: int | None = None
        if isinstance(usage, list):  # 缺席 = 没有记录,不是 0
            tokens_in = sum(int(u.get("input_tokens") or 0) for u in usage if isinstance(u, dict))
            tokens_out = sum(int(u.get("output_tokens") or 0) for u in usage if isinstance(u, dict))
        artifacts = end.get("artifacts")
        return TurnRecord(
            index=self.index,
            run_id=str(end.get("run_id") or self._run_id or "") or None,
            status=end.get("status"),
            completed=end.get("completed"),
            exit_reason=end.get("exit_reason"),
            final_text=self._final_text,
            tool_calls=self._calls,
            tool_errors=self._tool_errors,
            artifacts=[a for a in artifacts if isinstance(a, dict)]
            if isinstance(artifacts, list)
            else [],
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            wall_s=round(wall_s, 1),
            compactions=self._compactions,
        )


class ExternalClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def upload(
        self, agent: str, user_id: str, filename: str, data: bytes, session_id: str | None
    ) -> tuple[str, str]:
        form = {"user_id": user_id}
        if session_id is not None:
            form["session_id"] = session_id
        resp = await self._http.post(
            f"/v1/agents/{agent}/uploads",
            data=form,
            files={"file": (filename, data, content_type_for(filename))},
        )
        resp.raise_for_status()
        body = resp.json()["data"]
        return str(body["upload_id"]), str(body["session_id"])

    async def run_turn(
        self,
        agent: str,
        user_id: str,
        session_id: str | None,
        prompt: str,
        upload_ids: list[str],
        index: int,
    ) -> tuple[TurnRecord, str | None]:
        payload: dict[str, Any] = {"user_id": user_id, "input": prompt, "mode": "stream"}
        if session_id is not None:
            payload["session_id"] = session_id
        if upload_ids:
            payload["files"] = [{"upload_id": u} for u in upload_ids]
        builder = TurnBuilder(index)
        parser = SseParser()
        started = time.monotonic()
        async with self._http.stream("POST", f"/v1/agents/{agent}/runs", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                frame = parser.feed(line)
                if frame is not None:
                    builder.on_frame(*frame)
        tail = parser.flush()
        if tail is not None:
            builder.on_frame(*tail)
        return builder.result(time.monotonic() - started), builder.session_id

    async def download_artifact(
        self, agent: str, user_id: str, entry: dict[str, Any]
    ) -> bytes | None:
        params: dict[str, Any] = {"user_id": user_id, "name": entry["name"]}
        if entry.get("version") is not None:
            params["version"] = entry["version"]
        resp = await self._http.get(f"/v1/agents/{agent}/artifacts/download", params=params)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content

    async def read_workspace_file(self, agent: str, user_id: str, path: str) -> bytes | None:
        resp = await self._http.get(
            f"/v1/agents/{agent}/workspace/file", params={"user_id": user_id, "path": path}
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content

    async def archive(self, agent: str, user_id: str, session_id: str) -> None:
        resp = await self._http.delete(
            f"/v1/agents/{agent}/sessions/{session_id}", params={"user_id": user_id}
        )
        resp.raise_for_status()
