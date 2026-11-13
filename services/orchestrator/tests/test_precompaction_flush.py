"""Stream CM-3 — shared memory-flush core + compressor pre-compaction hook.

Covers ``flush_messages_to_memory`` (the extraction core now shared by the
run-end write-back node and the CM-3 pre-compaction flush) across its
happy / empty / blocked / DLQ / cancellation paths, and the compressor's
``on_pre_compaction`` callback (fired with ``split.middle`` before the
middle is summarised away, best-effort, no-op when absent).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from expert_work.persistence import InMemoryMemoryStore
from expert_work.persistence.memory import InMemoryMemoryWritebackDLQ
from expert_work.persistence.memory.base import BlockedMemory, MemoryWriteResult
from expert_work.protocol import MemoryItem
from expert_work.runtime.cancellation import CancellationToken, RunCancelledError
from orchestrator.context import ContextCompressor
from orchestrator.graph_builder.memory import flush_messages_to_memory
from orchestrator.llm import FakeEmbedder
from orchestrator.tools.registry import ToolSpec

_DIM = 32


@dataclass
class _RecordingLLM:
    responses: list[AIMessage]
    calls: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del tools
        idx = len(self.calls)
        self.calls.append(list(messages))
        if idx >= len(self.responses):
            raise RuntimeError(f"scripted LLM ran out at call {idx}")
        return self.responses[idx]


def _trajectory() -> list[BaseMessage]:
    return [HumanMessage(content="please remember my prefs"), AIMessage(content="noted")]


# ---------------------------------------------------------------------------
# flush_messages_to_memory — shared extraction core
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flush_extracts_embeds_and_persists() -> None:
    store = InMemoryMemoryStore()
    tenant, user = uuid4(), uuid4()
    llm = _RecordingLLM(
        responses=[AIMessage(content='{"memories": [{"kind": "fact", "content": "likes tea"}]}')]
    )
    written = await flush_messages_to_memory(
        _trajectory(),
        memory_store=store,
        embedder=FakeEmbedder(dim=_DIM),
        llm_caller=llm,
        tenant_id=tenant,
        user_id=user,
        thread_id=uuid4(),
        token=CancellationToken(),
    )
    assert written == 1
    stored = await store.retrieve(
        tenant_id=tenant, user_id=user, query_embedding=(0.0,) * _DIM, limit=10
    )
    assert [m.content for m in stored] == ["likes tea"]


@pytest.mark.asyncio
async def test_flush_returns_zero_on_empty_extraction() -> None:
    store = InMemoryMemoryStore()
    llm = _RecordingLLM(responses=[AIMessage(content='{"memories": []}')])
    written = await flush_messages_to_memory(
        _trajectory(),
        memory_store=store,
        embedder=FakeEmbedder(dim=_DIM),
        llm_caller=llm,
        tenant_id=uuid4(),
        user_id=uuid4(),
        thread_id=None,
        token=CancellationToken(),
    )
    assert written == 0


@pytest.mark.asyncio
async def test_flush_returns_zero_when_blocked_by_scanner() -> None:
    @dataclass
    class _BlockingStore:
        """B-300 D2 — the store hands every item back as blocked."""

        async def write(self, items: Sequence[MemoryItem]) -> MemoryWriteResult:
            return MemoryWriteResult(
                blocked=tuple(
                    BlockedMemory(item_id=i.id, content_length=len(i.content), findings=())
                    for i in items
                )
            )

    llm = _RecordingLLM(
        responses=[AIMessage(content='{"memories": [{"kind": "fact", "content": "x"}]}')]
    )
    written = await flush_messages_to_memory(
        _trajectory(),
        memory_store=_BlockingStore(),  # type: ignore[arg-type]
        embedder=FakeEmbedder(dim=_DIM),
        llm_caller=llm,
        tenant_id=uuid4(),
        user_id=uuid4(),
        thread_id=None,
        token=CancellationToken(),
    )
    assert written == 0


@pytest.mark.asyncio
async def test_flush_enqueues_dlq_on_embed_failure() -> None:
    dlq = InMemoryMemoryWritebackDLQ()

    @dataclass
    class _FailingEmbedder:
        async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
            del texts, tenant_id
            raise RuntimeError("embed-down")

    written = await flush_messages_to_memory(
        _trajectory(),
        memory_store=InMemoryMemoryStore(),
        embedder=_FailingEmbedder(),  # type: ignore[arg-type]
        llm_caller=_RecordingLLM(
            responses=[AIMessage(content='{"memories": [{"kind": "fact", "content": "espresso"}]}')]
        ),
        tenant_id=uuid4(),
        user_id=uuid4(),
        thread_id=None,
        token=CancellationToken(),
        dlq=dlq,
    )
    assert written == 0
    assert await dlq.count() == 1


@pytest.mark.asyncio
async def test_flush_reraises_cancellation() -> None:
    @dataclass
    class _CancellingLLM:
        async def __call__(
            self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
        ) -> AIMessage:
            del messages, tools
            raise RunCancelledError

    with pytest.raises(RunCancelledError):
        await flush_messages_to_memory(
            _trajectory(),
            memory_store=InMemoryMemoryStore(),
            embedder=FakeEmbedder(dim=_DIM),
            llm_caller=_CancellingLLM(),
            tenant_id=uuid4(),
            user_id=uuid4(),
            thread_id=None,
            token=CancellationToken(),
        )


# ---------------------------------------------------------------------------
# ContextCompressor.on_pre_compaction hook
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedSummariser:
    events: list[str]
    summary_text: str = "- bullet one\n- bullet two"

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del messages, tools
        self.events.append("summarise")
        return AIMessage(content=self.summary_text)


def _big_conversation() -> list[BaseMessage]:
    # System + head/tail + a fat middle that must be summarised away.
    msgs: list[BaseMessage] = [SystemMessage(content="sys")]
    for i in range(12):
        msgs.append(HumanMessage(content=f"user-{i} " + "w" * 400))
        msgs.append(AIMessage(content=f"assistant-{i} " + "w" * 400))
    return msgs


def _compressor(summariser: _ScriptedSummariser) -> ContextCompressor:
    # Small window so the fat conversation is over threshold.
    return ContextCompressor(
        llm_caller=summariser,
        context_window=1000,
        threshold_pct=0.7,
        head_keep=2,
        tail_keep=2,
        max_passes=3,
    )


@pytest.mark.asyncio
async def test_hook_called_with_middle_before_summarise() -> None:
    events: list[str] = []
    summariser = _ScriptedSummariser(events=events)
    seen_middle: list[BaseMessage] = []

    async def hook(middle: Sequence[BaseMessage]) -> None:
        events.append("flush")
        seen_middle.extend(middle)

    msgs = _big_conversation()
    await _compressor(summariser).compress(msgs, on_pre_compaction=hook)

    # Flush fired before the summariser LLM call on the first pass.
    assert events[0] == "flush"
    assert "summarise" in events
    # The hook saw the middle slice (neither head nor tail messages).
    assert seen_middle, "hook received an empty middle"
    assert all(isinstance(m, HumanMessage | AIMessage) for m in seen_middle)


@pytest.mark.asyncio
async def test_compress_without_hook_is_unchanged() -> None:
    summariser = _ScriptedSummariser(events=[])
    msgs = _big_conversation()
    out = await _compressor(summariser).compress(msgs)
    # Still compresses (a <context-summary> SystemMessage lands in the middle).
    assert any(isinstance(m, SystemMessage) and "<context-summary>" in str(m.content) for m in out)
