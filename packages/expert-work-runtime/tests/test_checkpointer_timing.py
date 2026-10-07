"""Tests for :class:`TimingCheckpointSaver` — Stream HX-4 (§ 5.4).

Delegation semantics stay byte-identical; the timing layer adds
histogram samples and never fails the underlying call.
"""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel

from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.checkpointer.timing import (
    TimingCheckpointSaver,
    _checkpoint_op_seconds,
)


def _op_count(op: str) -> float:
    child = _checkpoint_op_seconds.labels(op=op)
    return float(child._sum.get())


def _config(thread_id: str = "t-1") -> Any:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


def _checkpoint(cid: str = "c-1") -> Any:
    return {
        "v": 4,
        "id": cid,
        "ts": "2026-06-11T00:00:00+00:00",
        "channel_values": {},
        "channel_versions": {},
        "versions_seen": {},
    }


@pytest.mark.asyncio
async def test_aput_and_aget_tuple_delegate_and_time() -> None:
    saver = TimingCheckpointSaver(InMemorySaver())
    before_put = _op_count("aput")
    before_get = _op_count("aget_tuple")

    stored_config = await saver.aput(_config(), _checkpoint(), {"step": 1}, {})
    assert stored_config["configurable"]["thread_id"] == "t-1"

    fetched = await saver.aget_tuple(stored_config)
    assert fetched is not None
    assert fetched.checkpoint["id"] == "c-1"

    assert _op_count("aput") > before_put
    assert _op_count("aget_tuple") > before_get


@pytest.mark.asyncio
async def test_aput_writes_times_and_delegates() -> None:
    saver = TimingCheckpointSaver(InMemorySaver())
    config = await saver.aput(_config("t-2"), _checkpoint("c-2"), {"step": 1}, {})
    before = _op_count("aput_writes")

    await saver.aput_writes(config, [("messages", "hello")], task_id="task-1")

    assert _op_count("aput_writes") > before


@pytest.mark.asyncio
async def test_inner_exception_propagates_with_sample() -> None:
    class _BoomSaver(InMemorySaver):
        async def aput(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("disk gone")

    saver = TimingCheckpointSaver(_BoomSaver())
    before = _op_count("aput")
    with pytest.raises(RuntimeError, match="disk gone"):
        await saver.aput(_config(), _checkpoint(), {}, {})
    # The finally-block sample still lands — failures are visible in the
    # histogram, not hidden by it.
    assert _op_count("aput") > before


@pytest.mark.asyncio
async def test_factory_wraps_memory_backend() -> None:
    async with make_checkpointer("memory") as saver:
        assert isinstance(saver, TimingCheckpointSaver)


class _Probe(BaseModel):
    value: int


@pytest.mark.asyncio
async def test_with_allowlist_reaches_the_inner_saver() -> None:
    """B-161 — in strict msgpack mode ``StateGraph.compile`` registers the state
    schema's types via ``checkpointer.with_allowlist(...)``. The inherited
    implementation clones the wrapper and swaps the *wrapper's* serde, while
    every IO call goes to ``_inner`` — so the registration silently never
    reached the serializer that actually loads the checkpoint."""
    strict_inner = InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=None))
    saver = TimingCheckpointSaver(strict_inner)

    widened = saver.with_allowlist({(_Probe.__module__, _Probe.__name__)})
    assert isinstance(widened, TimingCheckpointSaver)

    checkpoint: Any = {
        **_checkpoint(),
        "channel_values": {"probe": _Probe(value=1)},
        "channel_versions": {"probe": 1},
    }
    stored = await widened.aput(_config("t-allow"), checkpoint, {}, {"probe": 1})
    fetched = await widened.aget_tuple(stored)
    assert fetched is not None
    assert fetched.checkpoint["channel_values"]["probe"] == _Probe(value=1)
