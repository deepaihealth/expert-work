"""B-161 — our checkpoint state types must load under strict msgpack.

langgraph-checkpoint rebuilds a stored pydantic model / dataclass / enum only
when ``(module, class name)`` is allowlisted; strict mode (the announced future
default, or ``LANGGRAPH_STRICT_MSGPACK=true`` today) hands anything else back
as a bare ``dict``. These tests run strict and check the serializer
:func:`make_checkpointer` actually builds.

``LANGGRAPH_STRICT_MSGPACK`` is read once, at import time, into module state,
so it cannot be flipped per test. :func:`strict_msgpack` recreates what a
process started with it set would have: the flag in
``langgraph.checkpoint.serde._msgpack`` (read by ``JsonPlusSerializer`` when
constructed) and ``langgraph._internal._serde`` (read by ``StateGraph.compile``
to merge the schema-derived allowlist into the checkpointer), plus the
class-level default serializer every saver built without ``serde=`` shares —
``BaseCheckpointSaver.serde`` is constructed at import, so it has to be rebuilt
under the flag.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph._internal._serde import collect_allowlist_from_schemas
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde import _msgpack
from langgraph.checkpoint.serde.event_hooks import SerdeEvent, register_serde_event_listener
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from expert_work.protocol import ApprovalRequest, MemoryItem, Plan, Reflection, SubAgentInvocation
from expert_work.protocol.plan import PlanStep
from expert_work.protocol.subagent import SubagentStatus
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.checkpointer.serde import CHECKPOINT_MSGPACK_ALLOWLIST
from orchestrator.state import AgentState
from orchestrator.tools.error_classifier import ClassifiedToolError

_NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
_UUID = UUID("00000000-0000-0000-0000-000000000161")


def _state_values() -> dict[str, Any]:
    """One instance of every custom type ``AgentState`` can hold, keyed by channel."""
    return {
        "plan": Plan(
            goal="draft the report",
            steps=(
                PlanStep(id="1", description="collect", status="completed"),
                PlanStep(id="2", description="write", status="in_progress", execution="delegate"),
            ),
        ),
        "reflections": [Reflection(verdict="revise", critique="too short", run_id="r-1")],
        "recalled_memories": [
            MemoryItem(
                id=_UUID,
                tenant_id=_UUID,
                user_id=_UUID,
                kind="fact",
                content="prefers metric units",
                embedding=(0.1, 0.2),
                created_at=_NOW,
            )
        ],
        "tool_failures": [
            ClassifiedToolError(
                tool_name="write_file",
                error_class="mutation_not_landed",
                summary="file unchanged",
                retryable=False,
                advice="re-read then edit",
                path="/workspace/a.md",
            )
        ],
        "unresolved_failures": [
            ClassifiedToolError(
                tool_name="save_artifact",
                error_class="unknown",
                summary="boom",
                retryable=True,
                advice="retry once",
            )
        ],
        "subagent_invocations": [
            SubAgentInvocation(
                task_id=_UUID,
                sub_thread_id=_UUID,
                name="researcher",
                agent_ref="researcher",
                child_depth=1,
                status=SubagentStatus.COMPLETED,
                result_excerpt="done",
                started_at=_NOW,
                finished_at=_NOW,
            )
        ],
        "pending_approval": ApprovalRequest(
            request_id="apr-1",
            node="tools",
            reason_kind="policy_gate",
            action_summary="send email",
            proposed_args={"to": "a@example.com"},
            requested_at=_NOW,
            timeout_at=_NOW,
        ),
    }


@pytest.fixture
def strict_msgpack(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process state ``LANGGRAPH_STRICT_MSGPACK=true`` produces (module docstring)."""
    monkeypatch.setattr(_msgpack, "STRICT_MSGPACK_ENABLED", True)
    monkeypatch.setattr("langgraph._internal._serde.STRICT_MSGPACK_ENABLED", True)
    monkeypatch.setattr(BaseCheckpointSaver, "serde", JsonPlusSerializer())


@pytest.fixture
def blocked_types() -> Iterator[list[str]]:
    """Every type the serializer refused to rebuild (``msgpack_blocked``) or
    rebuilt only with the "unregistered" warning."""
    seen: list[str] = []

    def _listener(event: SerdeEvent) -> None:
        if event["kind"] in ("msgpack_blocked", "msgpack_unregistered_allowed"):
            seen.append(f"{event['module']}.{event['name']}")

    unregister = register_serde_event_listener(_listener)
    yield seen
    unregister()


def _assert_types_survived(loaded: dict[str, Any], expected: dict[str, Any]) -> None:
    for channel, value in expected.items():
        assert loaded.get(channel) == value, channel
        got = loaded[channel]
        if isinstance(value, list):
            assert [type(v) for v in got] == [type(v) for v in value], channel
        else:
            assert type(got) is type(value), channel
    # The enum nested in the SubAgentInvocation dump, rebuilt as the enum
    # rather than its bare string value.
    assert type(loaded["subagent_invocations"][0].status) is SubagentStatus


@pytest.mark.usefixtures("strict_msgpack")
async def test_factory_saver_round_trips_state_types_under_strict_msgpack(
    blocked_types: list[str],
) -> None:
    """Serializer level: write a checkpoint holding every custom type through
    the memory saver the factory builds, read it back, get the same objects."""
    values = _state_values()
    async with make_checkpointer("memory") as saver:
        config: Any = {"configurable": {"thread_id": "b161", "checkpoint_ns": ""}}
        checkpoint: Any = {
            "v": 4,
            "id": "c-1",
            "ts": _NOW.isoformat(),
            "channel_values": values,
            "channel_versions": dict.fromkeys(values, 1),
            "versions_seen": {},
        }
        stored = await saver.aput(config, checkpoint, {"step": 1}, dict.fromkeys(values, 1))
        fetched = await saver.aget_tuple(stored)

    assert fetched is not None
    _assert_types_survived(fetched.checkpoint["channel_values"], values)
    assert blocked_types == []


def _write_state(_state: AgentState) -> dict[str, Any]:
    return {"messages": [AIMessage(content="ok")], **_state_values()}


@pytest.mark.usefixtures("strict_msgpack")
async def test_graph_turn_state_survives_strict_msgpack(blocked_types: list[str]) -> None:
    """Graph level: a turn's state — written by a node, read back from the
    checkpoint by ``aget_state`` — keeps its types under strict mode."""
    async with make_checkpointer("memory") as saver:
        builder = StateGraph(AgentState)
        builder.add_node("write", _write_state)
        builder.add_edge(START, "write")
        builder.add_edge("write", END)
        graph = builder.compile(checkpointer=saver)
        config: Any = {"configurable": {"thread_id": "b161-graph"}}
        await graph.ainvoke(
            {"messages": [HumanMessage(content="hi")], "step_count": 0, "max_steps": 3},
            config,
        )
        snapshot = await graph.aget_state(config)

    _assert_types_survived(snapshot.values, _state_values())
    assert blocked_types == []


def test_allowlist_covers_every_custom_type_in_agent_state() -> None:
    """Guard: a new typed ``AgentState`` field (or a nested model / enum in an
    existing one) must be registered. This is the same walk ``StateGraph.compile``
    does in strict mode; anything it finds that is neither langgraph's safe set
    nor in our list would come back as a ``dict`` once strict is the default."""
    derived = collect_allowlist_from_schemas(schemas=[AgentState])
    missing = derived - _msgpack.SAFE_MSGPACK_TYPES - set(CHECKPOINT_MSGPACK_ALLOWLIST)
    assert missing == set(), f"add to CHECKPOINT_MSGPACK_ALLOWLIST: {sorted(missing)}"


@pytest.mark.parametrize(("module", "name"), CHECKPOINT_MSGPACK_ALLOWLIST)
def test_allowlist_entries_name_real_classes(module: str, name: str) -> None:
    """Guard against a rename / move: a stale entry silently stops matching."""
    obj = getattr(importlib.import_module(module), name)
    assert isinstance(obj, type)
    assert (obj.__module__, obj.__name__) == (module, name)
