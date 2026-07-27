"""first_output_seconds 的两条 source 路径 —— 一期 Task 3。

判断准则是"用户第一次看到内容"。有 token 流时走 token 帧;judge 开启 /
cache 命中 / provider 不流式这三类 run 一个 token 帧都没有,必须由第一个
**agent** 节点的 updates 帧兜底,否则最慢的那批 run 全部落在盲区。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from prometheus_client import REGISTRY

from expert_work.runtime.runs import RunManager, RunRecord
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from orchestrator.graph_builder._config import TOKEN_SINK_KEY
from orchestrator.sse import run_agent

_METRIC = "expert_work_first_output_seconds"


def _histogram_count(source: str) -> float:
    """``get_sample_value`` returns ``None`` before the label combo has ever
    been observed — treat that as 0, not a crash."""
    value = REGISTRY.get_sample_value(f"{_METRIC}_count", {"source": source})
    return value if value is not None else 0.0


def _histogram_sum(source: str) -> float:
    value = REGISTRY.get_sample_value(f"{_METRIC}_sum", {"source": source})
    return value if value is not None else 0.0


async def _new_record(rm: RunManager) -> RunRecord:
    return await rm.create(run_id=uuid4(), thread_id=uuid4(), tenant_id=uuid4())


@dataclass
class _TokenGraph:
    """Stub graph that fires one token frame through the sink ``run_agent``
    wires up (``effective_config[TOKEN_SINK_KEY]`` — the real
    ``_publish_token`` closure, not a test double), then yields
    ``node_chunks`` as ``updates`` frames."""

    node_chunks: list[Any]
    final_state: dict[str, Any] = field(default_factory=dict)

    async def astream(
        self, input: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del input, stream_mode
        token_sink = config["configurable"][TOKEN_SINK_KEY]
        await token_sink({"step": 0, "content": "hi"})
        for chunk in self.node_chunks:
            yield chunk

    async def aget_state(self, config: Any) -> Any:
        del config
        return SimpleNamespace(values=dict(self.final_state))


@dataclass
class _NodeOnlyGraph:
    """Stub graph with no token stream at all — only ``updates`` node
    chunks, spaced by ``chunk_delay_s`` so a test can prove *which* chunk
    the metric latched onto (not just that it fired once)."""

    node_chunks: list[Any]
    chunk_delay_s: float = 0.0
    final_state: dict[str, Any] = field(default_factory=dict)

    async def astream(
        self, input: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del input, config, stream_mode
        for chunk in self.node_chunks:
            if self.chunk_delay_s:
                await asyncio.sleep(self.chunk_delay_s)
            yield chunk

    async def aget_state(self, config: Any) -> Any:
        del config
        return SimpleNamespace(values=dict(self.final_state))


@pytest.mark.asyncio
async def test_token_frame_records_source_token() -> None:
    """有 token 流时,第一帧 token 打 source="token"。"""
    before = _histogram_count("token")
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_TokenGraph(node_chunks=[{"agent": {"x": 1}}]),
        graph_input={"messages": []},
        config={},
    )
    assert _histogram_count("token") == before + 1


@pytest.mark.asyncio
async def test_agent_updates_frame_records_source_node_when_no_tokens() -> None:
    """judge-on 这类无 token 流的 run,由 agent 节点的 updates 帧兜底。"""
    before = _histogram_count("node")
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_NodeOnlyGraph(node_chunks=[{"agent": {"x": 1}}]),
        graph_input={"messages": []},
        config={},
    )
    assert _histogram_count("node") == before + 1


@pytest.mark.asyncio
async def test_recall_chunk_does_not_count_as_first_output() -> None:
    """入口链节点的 updates 帧不算首字 —— 用户看不到 recall 的输出。

    这是本 task 的命门:沿用现有的 first_chunk_seen(sse.py,认任意第一个
    chunk)会把 memory_recall 完成的时刻当成首字,数字比真实值早好几秒,
    优化前后的对比会完全失真。

    只检查计数不够 —— 一个记 recall、一个记 agent 的实现,计数看起来
    一模一样。所以在两帧之间插入真实延迟,再断言观测到的耗时反映的是
    到 *agent* 帧的时间,而不是 recall 帧那个接近零的时间戳。
    """
    before_count = _histogram_count("node")
    before_sum = _histogram_sum("node")
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    delay_s = 0.05
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_NodeOnlyGraph(
            node_chunks=[{"memory_recall": {"y": 1}}, {"agent": {"x": 1}}],
            chunk_delay_s=delay_s,
        ),
        graph_input={"messages": []},
        config={},
    )
    # 只记一次。
    assert _histogram_count("node") == before_count + 1
    # 记的是 agent 那帧(两次 delay_s 之后),不是 recall 那帧(一次 delay_s
    # 之后)——留足调度抖动余量,仍能把两者分开。
    observed = _histogram_sum("node") - before_sum
    assert observed >= delay_s * 1.5, (
        f"observed={observed!r} too small — looks like it latched onto the "
        "recall chunk instead of the agent chunk"
    )


@pytest.mark.asyncio
async def test_records_at_most_once_per_run() -> None:
    """token 帧记过之后,后续 agent updates 帧不再重复记。"""
    before_t = _histogram_count("token")
    before_n = _histogram_count("node")
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_TokenGraph(
            node_chunks=[{"agent": {"x": 1}}, {"agent": {"x": 2}}],
        ),
        graph_input={"messages": []},
        config={},
    )
    assert _histogram_count("token") == before_t + 1
    assert _histogram_count("node") == before_n
