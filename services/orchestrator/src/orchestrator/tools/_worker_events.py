"""B2 worker 可观测性 — worker 帧构建纯函数 + sink 契约.

spec: docs/superpowers/specs/2026-07-19-worker-observability-design.md

sink key / 类型定义在本模块(而非 ``graph_builder/_config``,那是其余
sink 的家):``orchestrator.tools`` 是 ``graph_builder`` 的下层,反向
import 会成包环(``tools/approval.py`` 同款先例)。``sse.py`` /
``builder.py`` 从这里向下 import。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

#: config["configurable"] key —— run_agent 注入的异步 worker 帧 sink
#: (镜像 COMPACTION_SINK_KEY 的注入模式)。
WORKER_EVENT_SINK_KEY = "worker_event_sink"

#: 一个 worker 帧(信封 dict)送进父 run bridge + 事件库的异步回调。
WorkerEventSink = Callable[[dict[str, Any]], Awaitable[None]]

WORKER_CONTENT_EXCERPT = 500
WORKER_ARGS_EXCERPT = 200
WORKER_RESULT_EXCERPT = 500


@dataclass(frozen=True)
class WorkerIdentity:
    """一个 child run 的帧信封身份 — 每帧原样携带."""

    worker_id: str
    parent_worker_id: str | None
    parent_tool_call_id: str | None
    label: str
    agent_ref: str
    depth: int


def build_worker_start_frame(
    ident: WorkerIdentity, *, wseq: int, task: str, role: str | None, max_steps: int
) -> dict[str, Any]:
    return _envelope(
        ident,
        kind="start",
        wseq=wseq,
        data={
            "task_excerpt": _excerpt(task, WORKER_CONTENT_EXCERPT),
            "role": role,
            "max_steps": max_steps,
        },
    )


def build_worker_update_frame(
    ident: WorkerIdentity, *, wseq: int, node: str, writes: Mapping[str, Any], duration_ms: int
) -> dict[str, Any]:
    data: dict[str, Any] = {"node": node, "_duration_ms": duration_ms}
    step_raw = writes.get("step_count")
    if isinstance(step_raw, int):
        data["step_count"] = step_raw
    data["messages"] = [_summarize_message(m) for m in _messages_of(writes)]
    return _envelope(ident, kind="update", wseq=wseq, data=data)


def build_worker_end_frame(
    ident: WorkerIdentity,
    *,
    wseq: int,
    outcome: str,
    iteration_used: int,
    llm_call_count: int,
    wall_clock_ms: int,
) -> dict[str, Any]:
    return _envelope(
        ident,
        kind="end",
        wseq=wseq,
        data={
            "outcome": outcome,
            "iteration_used": iteration_used,
            "llm_call_count": llm_call_count,
            "wall_clock_ms": wall_clock_ms,
        },
    )


def _envelope(
    ident: WorkerIdentity, *, kind: str, wseq: int, data: dict[str, Any]
) -> dict[str, Any]:
    return {
        "worker_id": ident.worker_id,
        "parent_worker_id": ident.parent_worker_id,
        "parent_tool_call_id": ident.parent_tool_call_id,
        "label": ident.label,
        "agent_ref": ident.agent_ref,
        "depth": ident.depth,
        "kind": kind,
        "wseq": wseq,
        "data": data,
    }


def _excerpt(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _text(content: Any) -> str:
    return content if isinstance(content, str) else str(content)


def _messages_of(writes: Mapping[str, Any]) -> list[BaseMessage]:
    raw = writes.get("messages")
    if isinstance(raw, BaseMessage):
        return [raw]
    if isinstance(raw, Sequence) and not isinstance(raw, str | bytes):
        return [m for m in raw if isinstance(m, BaseMessage)]
    return []


def _summarize_message(msg: BaseMessage) -> dict[str, Any]:
    if isinstance(msg, AIMessage):
        summary: dict[str, Any] = {
            "type": "ai",
            "content_excerpt": _excerpt(_text(msg.content), WORKER_CONTENT_EXCERPT),
        }
        calls = [
            {
                "name": str(call.get("name", "")),
                "args_excerpt": _excerpt(
                    json.dumps(call.get("args") or {}, ensure_ascii=False, default=str),
                    WORKER_ARGS_EXCERPT,
                ),
            }
            for call in (msg.tool_calls or [])
        ]
        if calls:
            summary["tool_calls"] = calls
        return summary
    if isinstance(msg, ToolMessage):
        summary = {
            "type": "tool",
            "name": msg.name or "",
            "tool_result_excerpt": _excerpt(_text(msg.content), WORKER_RESULT_EXCERPT),
        }
        # PR-D — sandbox exec results carry structured streams in ``artifact``
        # (format_sandbox_outcome.meta); the content excerpt is datamarked and
        # unparseable. Excerpt them to the same summary budget as the content.
        artifact = getattr(msg, "artifact", None)
        if isinstance(artifact, dict):
            exit_code = artifact.get("exit_code")
            if isinstance(exit_code, int) and not isinstance(exit_code, bool):
                summary["exec"] = {
                    "exit_code": exit_code,
                    "timed_out": bool(artifact.get("timed_out", False)),
                    "stdout_excerpt": _excerpt(
                        str(artifact.get("stdout", "")), WORKER_RESULT_EXCERPT
                    ),
                    "stderr_excerpt": _excerpt(
                        str(artifact.get("stderr", "")), WORKER_RESULT_EXCERPT
                    ),
                }
        return summary
    return {
        "type": msg.type,
        "content_excerpt": _excerpt(_text(msg.content), WORKER_CONTENT_EXCERPT),
    }
