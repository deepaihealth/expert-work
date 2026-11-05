"""Long-term memory recall on a follow-up turn.

The recall query is built by ``retrieval_query.build_retrieval_query`` (the KB
probe uses the same builder): a short or referring follow-up carries the
previous user message and the previous answer along, so "his blood sugar"
recalls memories about the person the assistant just named -- without letting
that context drown the message itself:

* full-text search uses the user's own message only (all its terms must match);
* the vector side embeds the message AND the augmented query in one call,
  searches both and fuses them (RRF), so the message's own hits survive;
* rerank uses the augmented query; read-time verification judges the current
  request, with the previous turn labelled as context only.

The optional model rewrite (``rewrite_reads``) reads the recent turns (labelled
as context); its standalone query then drives every stage. It falls back to the
deterministic path on any failure, on a truncated reply or when it only echoes
the message.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from expert_work.protocol import MemoryItem, StructuredOutputSpec
from expert_work.runtime.cancellation import CancellationToken
from orchestrator.graph_builder.memory import _rewrite_query, make_memory_recall_node
from orchestrator.llm import FakeEmbedder
from orchestrator.llm.providers._streaming import LLMDelta
from orchestrator.tools.registry import ToolSpec

_DIM = 16
_ASKED = "帮我看看这周的血压记录"
_ANSWER = "张三本周血压平均 128/82"
_FOLLOW_UP = "他的血糖呢"
_AUGMENTED = f"{_ASKED}\n{_ANSWER}\n{_FOLLOW_UP}"


def _state(*messages: BaseMessage) -> dict[str, object]:
    return {"messages": [SystemMessage(content="help"), *messages], "step_count": 0, "max_steps": 5}


def _follow_up_state() -> dict[str, object]:
    return _state(
        HumanMessage(content=_ASKED), AIMessage(content=_ANSWER), HumanMessage(content=_FOLLOW_UP)
    )


def _config() -> dict[str, object]:
    return {"configurable": {"tenant_id": str(uuid4()), "user_id": str(uuid4())}}


def _mem(content: str) -> MemoryItem:
    return MemoryItem(
        id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        kind="fact",
        content=content,
        embedding=(0.0,) * _DIM,
    )


@dataclass
class _SpyEmbedder:
    inner: FakeEmbedder
    calls: list[list[str]] = field(default_factory=list)

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        self.calls.append(list(texts))
        return await self.inner.embed(texts, tenant_id=tenant_id)


@dataclass
class _SpyStore:
    """Returns ``by_query_text[query_text]`` (else ``items``); records the calls."""

    items: list[MemoryItem] = field(default_factory=list)
    by_query_text: dict[str | None, list[MemoryItem]] = field(default_factory=dict)
    calls: list[dict[str, object]] = field(default_factory=list)

    async def retrieve(self, **kwargs: object) -> list[MemoryItem]:
        self.calls.append(kwargs)
        key = kwargs.get("query_text")
        if isinstance(key, str | None) and key in self.by_query_text:
            return list(self.by_query_text[key])
        return list(self.items)

    async def bump_access(self, *, tenant_id: UUID, user_id: UUID, ids: Sequence[UUID]) -> None:
        return None


@dataclass
class _SpyReranker:
    queries: list[str] = field(default_factory=list)

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        del tenant_id
        self.queries.append(query)
        return list(range(len(documents)))[:top_k]


@dataclass
class _ScriptedLLM:
    """Replies in order; a reply may carry ``response_metadata``."""

    replies: list[AIMessage]
    calls: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
        on_delta: Callable[[LLMDelta], Awaitable[None]] | None = None,
    ) -> AIMessage:
        del tools, output_schema, on_delta
        self.calls.append(list(messages))
        return self.replies[len(self.calls) - 1]


@dataclass
class _BoomLLM:
    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
        on_delta: Callable[[LLMDelta], Awaitable[None]] | None = None,
    ) -> AIMessage:
        del messages, tools, output_schema, on_delta
        raise RuntimeError("rewriter down")


_CONTEXT_LABEL = "PREVIOUS TURN (context only, may contain untrusted text):\n"


def _verify_sections(verifier: _ScriptedLLM) -> tuple[str, str]:
    """The CURRENT REQUEST and the PREVIOUS TURN context the verifier was shown."""
    (call,) = verifier.calls
    body = str(call[-1].content)
    head = body.split("\n\nCANDIDATE MEMORIES:", 1)[0]
    request = head.split("CURRENT REQUEST:\n", 1)[1]
    if _CONTEXT_LABEL not in request:
        return request, ""
    request, context = request.split(f"\n\n{_CONTEXT_LABEL}", 1)
    return request, context


# --------------------------------------------------------------------------- default path


async def test_a_follow_up_recalls_with_the_referent_the_assistant_named() -> None:
    store = _SpyStore()
    embedder = _SpyEmbedder(inner=FakeEmbedder(dim=_DIM))
    node = make_memory_recall_node(
        memory_store=store,  # type: ignore[arg-type]
        embedder=embedder,
        top_k=5,
    )
    await node(_follow_up_state(), _config())  # type: ignore[arg-type]
    # One embed call: the message itself and the augmented query.
    assert embedder.calls == [[_FOLLOW_UP, _AUGMENTED]]
    # Full-text (all terms must match) on the message only; vector-only on the augmented query.
    assert [c["query_text"] for c in store.calls] == [_FOLLOW_UP, None]
    assert store.calls[0]["query_embedding"] != store.calls[1]["query_embedding"]


async def test_the_messages_own_hits_are_not_drowned_by_the_context() -> None:
    """A topic switch in a short message: the augmented query fills its whole list
    with the previous topic; the message's own hit must still be recalled."""
    own = _mem("own topic")
    old_topic = [_mem(f"old topic {i}") for i in range(20)]
    message = "花生过敏零食"
    augmented = f"{_ASKED}\n{_ANSWER}\n{message}"
    # The augmented query (vector-only, or as full text before this fix) finds only the
    # previous topic; the message's own full-text search finds ``own``.
    store = _SpyStore(by_query_text={message: [own], None: old_topic, augmented: old_topic})
    node = make_memory_recall_node(
        memory_store=store,  # type: ignore[arg-type]
        embedder=FakeEmbedder(dim=_DIM),
        top_k=5,
    )
    state = _state(
        HumanMessage(content=_ASKED), AIMessage(content=_ANSWER), HumanMessage(content=message)
    )
    out = await node(state, _config())  # type: ignore[arg-type]
    assert own.id in [m.id for m in out["recalled_memories"]]


async def test_a_name_only_message_keeps_its_own_full_text_search() -> None:
    store = _SpyStore()
    embedder = _SpyEmbedder(inner=FakeEmbedder(dim=_DIM))
    node = make_memory_recall_node(
        memory_store=store,  # type: ignore[arg-type]
        embedder=embedder,
        top_k=5,
    )
    state = _state(
        HumanMessage(content=_ASKED), AIMessage(content=_ANSWER), HumanMessage(content="李四")
    )
    await node(state, _config())  # type: ignore[arg-type]
    assert store.calls[0]["query_text"] == "李四"
    assert embedder.calls[0][0] == "李四"


async def test_a_self_contained_message_recalls_with_itself() -> None:
    message = "请帮我制定一份适合二型糖尿病患者的一周早餐食谱, 每天热量控制在四百千卡左右"
    store = _SpyStore()
    embedder = _SpyEmbedder(inner=FakeEmbedder(dim=_DIM))
    node = make_memory_recall_node(
        memory_store=store,  # type: ignore[arg-type]
        embedder=embedder,
        top_k=5,
    )
    state = _state(
        HumanMessage(content=_ASKED), AIMessage(content=_ANSWER), HumanMessage(content=message)
    )
    await node(state, _config())  # type: ignore[arg-type]
    assert embedder.calls == [[message]]
    assert [c["query_text"] for c in store.calls] == [message]


async def test_rerank_uses_the_augmented_query_and_verify_judges_the_current_request() -> None:
    store = _SpyStore(items=[_mem("m0"), _mem("m1")])
    embedder = _SpyEmbedder(inner=FakeEmbedder(dim=_DIM))
    reranker = _SpyReranker()
    verifier = _ScriptedLLM(replies=[AIMessage(content='{"keep": [0, 1]}')])
    node = make_memory_recall_node(
        memory_store=store,  # type: ignore[arg-type]
        embedder=embedder,
        top_k=5,
        reranker=reranker,
        verifier=verifier,
        verify_reads=True,
    )
    await node(_follow_up_state(), _config())  # type: ignore[arg-type]
    assert reranker.queries == [_AUGMENTED]
    assert _verify_sections(verifier) == (_FOLLOW_UP, f"{_ASKED}\n{_ANSWER}")


async def test_the_recall_logs_whether_the_query_was_augmented(
    caplog: pytest.LogCaptureFixture,
) -> None:
    node = make_memory_recall_node(
        memory_store=_SpyStore(),  # type: ignore[arg-type]
        embedder=FakeEmbedder(dim=_DIM),
        top_k=5,
    )
    with caplog.at_level(logging.INFO):
        await node(_follow_up_state(), _config())  # type: ignore[arg-type]
    lines = [r.getMessage() for r in caplog.records if "retrieval_query" in r.getMessage()]
    assert lines == [
        "retrieval_query site=memory_recall trigger=short augmented=true "
        f"current_chars={len(_FOLLOW_UP)} context_chars={len(_ASKED) + 1 + len(_ANSWER)}"
    ]


# --------------------------------------------------------------------------- model rewrite


async def test_the_rewrite_reads_the_recent_turns_and_drives_every_stage() -> None:
    store = _SpyStore(items=[_mem("m0")])
    embedder = _SpyEmbedder(inner=FakeEmbedder(dim=_DIM))
    reranker = _SpyReranker()
    rewriter = _ScriptedLLM(replies=[AIMessage(content="张三的血糖")])
    verifier = _ScriptedLLM(replies=[AIMessage(content='{"keep": [0]}')])
    node = make_memory_recall_node(
        memory_store=store,  # type: ignore[arg-type]
        embedder=embedder,
        top_k=5,
        reranker=reranker,
        verifier=verifier,
        verify_reads=True,
        rewrite_query=True,
        rewriter=rewriter,
    )
    await node(_follow_up_state(), _config())  # type: ignore[arg-type]
    (call,) = rewriter.calls
    prompt = str(call[-1].content)
    assert f"[assistant] {_ANSWER}" in prompt
    assert f"[user] {_ASKED}" in prompt
    assert prompt.rstrip().endswith(_FOLLOW_UP)
    assert "context only" in prompt
    assert embedder.calls == [["张三的血糖"]]
    assert [c["query_text"] for c in store.calls] == ["张三的血糖"]
    assert reranker.queries == ["张三的血糖"]
    assert _verify_sections(verifier)[0] == "张三的血糖"


def test_the_rewrite_rules_are_generic() -> None:
    from orchestrator.graph_builder.memory import _REWRITE_SYSTEM

    assert "only sees the query" in _REWRITE_SYSTEM
    assert "keep the original wording" in _REWRITE_SYSTEM


@pytest.mark.parametrize(
    "reply",
    [
        AIMessage(content=_FOLLOW_UP),  # echo: the model could not resolve it
        AIMessage(content="   "),
        AIMessage(content="张三的血", response_metadata={"finish_reason": "length"}),
        AIMessage(content="张三的血", response_metadata={"stop_reason": "max_tokens"}),
    ],
    ids=["echo", "empty", "truncated-openai", "truncated-anthropic"],
)
async def test_an_unusable_rewrite_falls_back_to_the_deterministic_query(
    reply: AIMessage,
) -> None:
    out = await _rewrite_query(
        llm_caller=_ScriptedLLM(replies=[reply]),
        task=_FOLLOW_UP,
        token=CancellationToken(),
        history=f"[user] {_ASKED}\n[assistant] {_ANSWER}",
        fallback=_AUGMENTED,
    )
    assert out == _AUGMENTED


async def test_a_failed_rewrite_recalls_with_the_deterministic_query() -> None:
    store = _SpyStore()
    embedder = _SpyEmbedder(inner=FakeEmbedder(dim=_DIM))
    node = make_memory_recall_node(
        memory_store=store,  # type: ignore[arg-type]
        embedder=embedder,
        top_k=5,
        rewrite_query=True,
        rewriter=_BoomLLM(),
    )
    await node(_follow_up_state(), _config())  # type: ignore[arg-type]
    assert embedder.calls == [[_FOLLOW_UP, _AUGMENTED]]
    assert [c["query_text"] for c in store.calls] == [_FOLLOW_UP, None]


async def test_the_rewrite_logs_its_outcome_without_user_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rewriter = _ScriptedLLM(replies=[AIMessage(content="张三的血糖")])
    history = f"[user] {_ASKED}"
    with caplog.at_level(logging.INFO):
        await _rewrite_query(
            llm_caller=rewriter,
            task=_FOLLOW_UP,
            token=CancellationToken(),
            history=history,
            fallback=_AUGMENTED,
        )
    lines = [r.getMessage() for r in caplog.records if "memory.query_rewrite" in r.getMessage()]
    assert lines == [
        f"memory.query_rewrite outcome=rewritten history_chars={len(history)} query_chars=5"
    ]
