"""Helpers to lift per-run objects out of ``RunnableConfig``.

Shared by the graph nodes (``builder``, ``planner``) — kept in its own
module so neither node module has to import the other (no import cycle).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast
from uuid import UUID

from langchain_core.runnables import RunnableConfig

from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.cancellation import CANCELLATION_TOKEN_KEY, CancellationToken

#: Stream TE-2 — key under which the run's :class:`AuditLogger` travels in
#: ``config["configurable"]`` (a live object, like the cancellation token —
#: not checkpoint-serialisable, injected per-invocation by ``sse.run_agent``).
AUDIT_LOGGER_KEY = "audit_logger"

#: Stream RT-2 PR-4 — key under which the run's COMPACTION event sink travels in
#: ``config["configurable"]``. A live async callback (like the audit logger)
#: that publishes + persists a ``"compaction"`` SSE frame; injected per-run by
#: ``sse.run_agent`` (which owns the bridge / event store), so ``agent_node``
#: can surface a compaction without importing the SSE layer.
COMPACTION_SINK_KEY = "compaction_event_sink"

#: The compaction sink's shape — awaited with the event payload dict.
CompactionEventSink = Callable[[dict[str, Any]], Awaitable[None]]

#: 子项目 2 — config key under which run_agent injects the async token-frame sink
#: (mirrors COMPACTION_SINK_KEY). The agent node lifts it out via
#: ``token_sink_from_config`` and feeds it to a TokenSink.
TOKEN_SINK_KEY = "token_event_sink"  # noqa: S105 — a config dict key name, not a credential

#: B-66 — ``config["configurable"][LLM_CACHE_BYPASS_KEY] is True`` 时,本 run 的每次 LLM
#: 调用都跳过 E.13 响应缓存的**查找**(写入照常)。``:regenerate`` 的 run 带它:重放的
#: [System, Human] 与原轮字节相同,不跳过就必然命中、把旧答案原样端回来。设置方是
#: control-plane 的两个 regenerate 入口(``spawn_run`` / ``RunQueueWorker``)。
LLM_CACHE_BYPASS_KEY = "llm_cache_bypass"

#: An async callable that ships one token frame ``{step, channel, text}`` to the
#: SSE bridge.
TokenEventSink = Callable[[dict[str, Any]], Awaitable[None]]


def configurable_uuid(config: RunnableConfig, key: str) -> UUID | None:
    """Parse ``config['configurable'][key]`` as a UUID, or ``None``.

    Run-scoped bindings (``tenant_id`` / ``user_id`` / …) travel via
    ``config['configurable']`` as strings; nodes lift them with this.
    """
    raw = (config.get("configurable") or {}).get(key)
    if isinstance(raw, UUID):
        return raw
    if isinstance(raw, str):
        try:
            return UUID(raw)
        except ValueError:
            return None
    return None


def current_run_id(config: RunnableConfig) -> str | None:
    """The run's id from ``config['configurable']``.

    Distinguishes one graph invocation from the next on the same
    checkpointed thread — used to scope per-run counters whose channels
    would otherwise accumulate across runs (e.g. the reflect budget).
    """
    raw = (config.get("configurable") or {}).get("run_id")
    return str(raw) if raw is not None else None


def cancellation_token(config: RunnableConfig) -> CancellationToken:
    """Lift the run's :class:`CancellationToken` out of ``config``.

    The token travels via ``config["configurable"]`` (not ``AgentState``
    — a live :class:`asyncio.Event` is not checkpoint-serialisable).
    When absent — dev / unit-test path that never cancels — a fresh,
    never-cancelled token is returned so node code is uniform.
    """
    configurable = config.get("configurable") or {}
    token = configurable.get(CANCELLATION_TOKEN_KEY)
    if isinstance(token, CancellationToken):
        return token
    return CancellationToken()


def audit_logger_from_config(config: RunnableConfig) -> AuditLogger | None:
    """Lift the run's :class:`AuditLogger` out of ``config`` (Stream TE-2).

    Like the cancellation token it travels via ``config["configurable"]``
    (a live object, not checkpoint-serialisable). ``None`` when absent —
    the dev / unit-test path, or a control-plane that wires no audit sink;
    callers must treat the tool-call audit emit as best-effort.
    """
    configurable = config.get("configurable") or {}
    logger = configurable.get(AUDIT_LOGGER_KEY)
    if isinstance(logger, AuditLogger):
        return logger
    return None


def compaction_sink_from_config(config: RunnableConfig) -> CompactionEventSink | None:
    """Lift the run's COMPACTION event sink out of ``config`` (RT-2 PR-4).

    Travels via ``config["configurable"]`` like the audit logger — a live
    async callback injected by ``sse.run_agent``. ``None`` when absent (dev /
    unit-test path, or a driver that wires no bridge); the caller then simply
    does not surface a compaction event.
    """
    configurable = config.get("configurable") or {}
    sink = configurable.get(COMPACTION_SINK_KEY)
    if callable(sink):
        # ``callable()`` can't narrow to the parametrised Callable alias, so
        # cast the confirmed-callable sink to its declared shape.
        return cast(CompactionEventSink, sink)
    return None


def token_sink_from_config(config: RunnableConfig) -> TokenEventSink | None:
    """Lift the run's token-frame sink out of ``config`` (子项目 2).

    ``None`` when run_agent injected no sink (e.g. a non-streaming execution
    path) — the node then simply does not token-stream.
    """
    configurable = config.get("configurable") or {}
    sink = configurable.get(TOKEN_SINK_KEY)
    return sink if callable(sink) else None
