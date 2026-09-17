"""Agent-loop structured finalization — Stream RT-1 PR-3 (RT-ADR-4).

Covers STREAM-RT-DESIGN § 7 (RT-ADR-4 + the § 7.4 cache-wiring hard
requirement):

- no ``output_schema`` → byte-identical single-call behaviour;
- intermediate tool-calling rounds are never constrained;
- a conforming terminal candidate costs zero extra calls (``parsed``
  attached in place);
- a non-conforming candidate triggers ONE schema-enforced resend
  (``tools=[]``, ``output_schema`` forwarded, candidate + correction
  appended) and only the final response is persisted to state;
- BOTH anchors receive the ``StructuredOutputSpec`` INSTANCE in
  ``payload["output_schema"]`` on the resend pass — proven twice: spy
  middlewares assert identity, and the real E.13 lookup/store pair
  round-trips a structured entry (write → read) while a different
  schema misses (fingerprint keying);
- a still-invalid resend propagates :class:`LLMOutputValidationError`;
- the budget-exhausted wrap-up turn is a terminal reply and is enforced
  too.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from expert_work.protocol import StructuredOutputSpec
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.llm.cache import InMemoryRedisCache, LLMResponseCache
from expert_work.runtime.middleware import (
    CallNext,
    LLMOutputValidationError,
    MiddlewareChain,
    MiddlewareContext,
)
from expert_work.runtime.middleware.llm_cache import (
    LLMCacheLookupMiddleware,
    LLMCacheStoreMiddleware,
)
from orchestrator import (
    LLM_CACHE_BYPASS_KEY,
    AgentState,
    GraphRunner,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"score": {"type": "integer"}},
    "required": ["score"],
    "additionalProperties": False,
}
_SPEC = StructuredOutputSpec(schema=_SCHEMA, name="verdict")

_VALID = '{"score": 4}'
_INVALID = "here is my answer: four out of five"


@dataclass
class _RecordingCaller:
    """LLMCaller double — scripted responses, records every call's kwargs.

    The last response is sticky. ``raise_on_structured`` simulates the
    router exhausting its RT-ADR-1 validation retries on the resend.
    """

    responses: list[AIMessage]
    calls: list[dict[str, object]] = field(default_factory=list)
    raise_on_structured: BaseException | None = None

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
    ) -> AIMessage:
        self.calls.append(
            {
                "messages": list(messages),
                "tools": list(tools),
                "output_schema": output_schema,
            }
        )
        if output_schema is not None and self.raise_on_structured is not None:
            raise self.raise_on_structured
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[idx]


@dataclass
class _SpyMiddleware:
    """Records selected payload entries per invocation (by reference, so
    tests can assert instance identity on ``output_schema``)."""

    name: str
    anchor: str
    log: list[dict[str, object]]
    snapshot_keys: tuple[str, ...] = ()
    after: tuple[str, ...] = field(default_factory=tuple)
    before: tuple[str, ...] = field(default_factory=tuple)

    async def __call__(self, ctx: MiddlewareContext, call_next: CallNext) -> None:
        self.log.append({k: ctx.payload.get(k) for k in self.snapshot_keys})
        await call_next(ctx)


@dataclass
class _ScriptedTool:
    name: str

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=f"scripted {self.name}")

    async def call(self, args: object, *, ctx: object) -> ToolResult:
        del args, ctx
        return ToolResult(content="tool-ok")


def _config(tenant: str | None = None) -> RunnableConfig:
    configurable: dict[str, object] = {"thread_id": str(uuid4())}
    if tenant is not None:
        configurable["tenant_id"] = tenant
    return {"configurable": configurable}


def _initial(max_steps: int = 5, step_count: int = 0) -> AgentState:
    return {
        "messages": [HumanMessage(content="rate this")],
        "step_count": step_count,
        "max_steps": max_steps,
    }


async def _run(
    caller: _RecordingCaller,
    *,
    output_schema: StructuredOutputSpec | None,
    registry: ToolRegistry | None = None,
    before_llm_chain: MiddlewareChain | None = None,
    after_llm_chain: MiddlewareChain | None = None,
    config: RunnableConfig | None = None,
    initial: AgentState | None = None,
    output_dlp: bool = False,
) -> AgentState:
    graph = build_react_graph(
        llm_caller=caller,
        tool_registry=registry if registry is not None else ToolRegistry(),
        before_llm_chain=before_llm_chain,
        after_llm_chain=after_llm_chain,
        output_schema=output_schema,
        output_dlp=output_dlp,
    )
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        return await compiled.ainvoke(
            initial if initial is not None else _initial(),
            config=config if config is not None else _config(),
        )


# ---------------------------------------------------------------------------
# RT-ADR-4 — enforcement scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_schema_configured_is_single_unstructured_call() -> None:
    """``output_schema=None`` (every pre-PR-3 agent) — zero behaviour change:
    one call, no schema forwarded, no ``parsed`` attached."""
    caller = _RecordingCaller(responses=[AIMessage(content=_INVALID)])

    final = await _run(caller, output_schema=None)

    assert len(caller.calls) == 1
    assert caller.calls[0]["output_schema"] is None
    last = final["messages"][-1]
    assert isinstance(last, AIMessage)
    assert "parsed" not in last.additional_kwargs


@pytest.mark.asyncio
async def test_intermediate_tool_round_is_not_constrained() -> None:
    """A tool-calling round under a configured schema goes out unstructured
    and its response is not validated — only the terminal turn is."""
    tool = _ScriptedTool("echo")
    registry = ToolRegistry()
    registry.register(tool)
    caller = _RecordingCaller(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "echo", "args": {}, "id": "tc-1", "type": "tool_call"}],
                id="ai-1",
            ),
            AIMessage(content=_VALID, id="ai-2"),
        ]
    )

    final = await _run(caller, output_schema=_SPEC, registry=registry)

    # Two primary calls (tool round + terminal), neither carried the schema —
    # the terminal candidate conformed so no resend happened.
    assert len(caller.calls) == 2
    assert all(c["output_schema"] is None for c in caller.calls)
    last = final["messages"][-1]
    assert isinstance(last, AIMessage)
    assert last.additional_kwargs["parsed"] == {"score": 4}


@pytest.mark.asyncio
async def test_conforming_candidate_costs_zero_extra_calls() -> None:
    caller = _RecordingCaller(responses=[AIMessage(content=_VALID)])

    final = await _run(caller, output_schema=_SPEC)

    assert len(caller.calls) == 1
    assert caller.calls[0]["output_schema"] is None
    last = final["messages"][-1]
    assert isinstance(last, AIMessage)
    assert last.additional_kwargs["parsed"] == {"score": 4}


@pytest.mark.asyncio
async def test_nonconforming_candidate_triggers_one_structured_resend() -> None:
    caller = _RecordingCaller(responses=[AIMessage(content=_INVALID), AIMessage(content=_VALID)])

    final = await _run(caller, output_schema=_SPEC)

    assert len(caller.calls) == 2
    # The resend carries the spec instance and binds NO tools (a
    # finalization must answer; the tool_call/prompt paths cannot carry
    # regular tools next to a schema anyway).
    resend = caller.calls[1]
    assert resend["output_schema"] is _SPEC
    assert resend["tools"] == []
    # Resend prompt = original prompt + failed candidate + correction.
    resend_messages = resend["messages"]
    assert isinstance(resend_messages, list)
    assert isinstance(resend_messages[-2], AIMessage)
    assert resend_messages[-2].content == _INVALID
    correction = resend_messages[-1]
    assert isinstance(correction, HumanMessage)
    assert "failed validation" in str(correction.content)
    assert "not valid JSON" in str(correction.content)
    # Only the final response persists — the candidate / correction
    # exchange is ephemeral (mirrors the router's RT-ADR-1 loop).
    assert len(final["messages"]) == 2
    last = final["messages"][-1]
    assert isinstance(last, AIMessage)
    assert last.content == _VALID
    assert last.additional_kwargs["parsed"] == {"score": 4}


@pytest.mark.asyncio
async def test_resend_still_invalid_propagates_validation_error() -> None:
    caller = _RecordingCaller(
        responses=[AIMessage(content=_INVALID)],
        raise_on_structured=LLMOutputValidationError("still invalid"),
    )

    with pytest.raises(LLMOutputValidationError):
        await _run(caller, output_schema=_SPEC)


@pytest.mark.asyncio
async def test_budget_exhausted_wrapup_turn_is_enforced_too() -> None:
    """The graceful wrap-up turn is a terminal reply — the Tier3 contract
    applies to it like any other finalization."""
    caller = _RecordingCaller(responses=[AIMessage(content=_INVALID), AIMessage(content=_VALID)])

    final = await _run(
        caller,
        output_schema=_SPEC,
        initial=_initial(max_steps=1, step_count=1),
    )

    assert len(caller.calls) == 2
    assert caller.calls[1]["output_schema"] is _SPEC
    last = final["messages"][-1]
    assert isinstance(last, AIMessage)
    assert last.additional_kwargs["parsed"] == {"score": 4}


# ---------------------------------------------------------------------------
# § 7.4 hard requirement — the spec INSTANCE on BOTH anchors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_anchors_receive_spec_instance_on_resend() -> None:
    before_log: list[dict[str, object]] = []
    after_log: list[dict[str, object]] = []
    before = MiddlewareChain.from_middlewares(
        "before_llm_call",
        [
            _SpyMiddleware(
                "before_spy",
                "before_llm_call",
                before_log,
                snapshot_keys=("output_schema", "tools"),
            )
        ],
    )
    after = MiddlewareChain.from_middlewares(
        "after_llm_call",
        [
            _SpyMiddleware(
                "after_spy",
                "after_llm_call",
                after_log,
                snapshot_keys=("output_schema", "response", "prompt_messages", "cache_hit"),
            )
        ],
    )
    caller = _RecordingCaller(responses=[AIMessage(content=_INVALID), AIMessage(content=_VALID)])

    final = await _run(
        caller,
        output_schema=_SPEC,
        before_llm_chain=before,
        after_llm_chain=after,
    )

    # before anchor: primary pass carries NO schema; resend pass carries
    # the very INSTANCE (identity, not equality — § 7.4).
    assert len(before_log) == 2
    assert before_log[0]["output_schema"] is None
    assert before_log[1]["output_schema"] is _SPEC
    assert before_log[1]["tools"] == []

    # after anchor: one pass per real call. The primary-accounting pass
    # carries the candidate and no schema; the resend pass carries the
    # final response + the same INSTANCE + the resend's exact prompt.
    assert len(after_log) == 2
    assert after_log[0]["output_schema"] is None
    primary_response = after_log[0]["response"]
    assert isinstance(primary_response, AIMessage)
    assert primary_response.content == _INVALID
    assert after_log[1]["output_schema"] is _SPEC
    final_response = after_log[1]["response"]
    assert isinstance(final_response, AIMessage)
    assert final_response.content == _VALID
    resend_prompt = after_log[1]["prompt_messages"]
    assert isinstance(resend_prompt, list)
    assert isinstance(resend_prompt[-1], HumanMessage)  # the correction
    assert after_log[1]["cache_hit"] is False

    # State persists only [task, final response].
    assert len(final["messages"]) == 2


@pytest.mark.asyncio
async def test_conforming_candidate_keeps_single_anchor_pass() -> None:
    """No resend → exactly one pass per anchor, no ``output_schema`` key —
    byte-identical payloads to pre-PR-3."""
    before_log: list[dict[str, object]] = []
    after_log: list[dict[str, object]] = []
    before = MiddlewareChain.from_middlewares(
        "before_llm_call",
        [_SpyMiddleware("b", "before_llm_call", before_log, snapshot_keys=("output_schema",))],
    )
    after = MiddlewareChain.from_middlewares(
        "after_llm_call",
        [_SpyMiddleware("a", "after_llm_call", after_log, snapshot_keys=("output_schema",))],
    )
    caller = _RecordingCaller(responses=[AIMessage(content=_VALID)])

    await _run(caller, output_schema=_SPEC, before_llm_chain=before, after_llm_chain=after)

    assert len(before_log) == 1
    assert len(after_log) == 1
    assert before_log[0]["output_schema"] is None
    assert after_log[0]["output_schema"] is None


# ---------------------------------------------------------------------------
# § 7.4 end-to-end — real E.13 middlewares: structured entries are written
# AND read back, and the key varies with the schema.
# ---------------------------------------------------------------------------


def _cache_chains(cache: LLMResponseCache) -> tuple[MiddlewareChain, MiddlewareChain]:
    lookup = LLMCacheLookupMiddleware(cache=cache, model="m", temperature=0.0, max_tokens=256)
    store = LLMCacheStoreMiddleware(cache=cache, model="m", temperature=0.0, max_tokens=256)
    return (
        MiddlewareChain.from_middlewares("before_llm_call", [lookup]),
        MiddlewareChain.from_middlewares("after_llm_call", [store]),
    )


@pytest.mark.asyncio
async def test_structured_cache_write_then_read_roundtrip() -> None:
    """Run 1 stores (unstructured candidate + structured resend); run 2 is
    served ENTIRELY from cache — zero upstream calls. Lookup and store
    must derive the same structured key or run 2 would call again."""
    cache = LLMResponseCache(redis=InMemoryRedisCache())
    tenant = str(uuid4())

    before, after = _cache_chains(cache)
    caller_1 = _RecordingCaller(responses=[AIMessage(content=_INVALID), AIMessage(content=_VALID)])
    final_1 = await _run(
        caller_1,
        output_schema=_SPEC,
        before_llm_chain=before,
        after_llm_chain=after,
        config=_config(tenant=tenant),
    )
    assert len(caller_1.calls) == 2
    assert final_1["messages"][-1].additional_kwargs["parsed"] == {"score": 4}

    # Fresh graph + caller, same cache, same tenant + prompt: the primary
    # lookup hits the stored candidate (unstructured key), the resend
    # lookup hits the stored structured entry (schema-fingerprint key).
    before, after = _cache_chains(cache)
    caller_2 = _RecordingCaller(responses=[AIMessage(content="never called")])
    final_2 = await _run(
        caller_2,
        output_schema=_SPEC,
        before_llm_chain=before,
        after_llm_chain=after,
        config=_config(tenant=tenant),
    )
    assert caller_2.calls == []
    last = final_2["messages"][-1]
    assert isinstance(last, AIMessage)
    assert last.content == _VALID
    assert last.additional_kwargs["parsed"] == {"score": 4}


@pytest.mark.asyncio
async def test_bypass_run_skips_the_structured_resend_lookup_too() -> None:
    """B-66 —— ``:regenerate`` 带 ``LLM_CACHE_BYPASS_KEY`` 时,结构化重发那一次查找也要跳过。
    确定性模型重跑出同一个不合规候选时,重发的 prompt 与原轮字节相同 —— 只绕开主调用
    的话,重发会命中结构化条目,把旧的结构化答案原样端回来。"""
    cache = LLMResponseCache(redis=InMemoryRedisCache())
    tenant = str(uuid4())

    before, after = _cache_chains(cache)
    caller_1 = _RecordingCaller(responses=[AIMessage(content=_INVALID), AIMessage(content=_VALID)])
    await _run(
        caller_1,
        output_schema=_SPEC,
        before_llm_chain=before,
        after_llm_chain=after,
        config=_config(tenant=tenant),
    )
    assert len(caller_1.calls) == 2

    before, after = _cache_chains(cache)
    caller_2 = _RecordingCaller(
        responses=[AIMessage(content=_INVALID), AIMessage(content='{"score": 5}')]
    )
    config = _config(tenant=tenant)
    config["configurable"][LLM_CACHE_BYPASS_KEY] = True
    final_2 = await _run(
        caller_2,
        output_schema=_SPEC,
        before_llm_chain=before,
        after_llm_chain=after,
        config=config,
    )
    assert len(caller_2.calls) == 2
    assert final_2["messages"][-1].additional_kwargs["parsed"] == {"score": 5}


@pytest.mark.asyncio
async def test_cache_key_varies_with_schema() -> None:
    """Same tenant + prompt, a DIFFERENT schema — the structured entry
    stored for schema A must not serve schema B (fingerprint keying)."""
    cache = LLMResponseCache(redis=InMemoryRedisCache())
    tenant = str(uuid4())

    before, after = _cache_chains(cache)
    caller_1 = _RecordingCaller(responses=[AIMessage(content=_INVALID), AIMessage(content=_VALID)])
    await _run(
        caller_1,
        output_schema=_SPEC,
        before_llm_chain=before,
        after_llm_chain=after,
        config=_config(tenant=tenant),
    )
    assert len(caller_1.calls) == 2

    spec_b = StructuredOutputSpec(
        schema={
            "type": "object",
            "properties": {"score": {"type": "integer"}, "reason": {"type": "string"}},
            "required": ["score", "reason"],
            "additionalProperties": False,
        },
        name="verdict",
    )
    before, after = _cache_chains(cache)
    caller_2 = _RecordingCaller(responses=[AIMessage(content='{"score": 4, "reason": "solid"}')])
    final_2 = await _run(
        caller_2,
        output_schema=spec_b,
        before_llm_chain=before,
        after_llm_chain=after,
        config=_config(tenant=tenant),
    )
    # The primary candidate is served from the unstructured entry (cache
    # hit on the invalid candidate), but the structured lookup MISSES —
    # schema B's fingerprint differs — so exactly one upstream resend runs.
    assert len(caller_2.calls) == 1
    assert caller_2.calls[0]["output_schema"] is spec_b
    last = final_2["messages"][-1]
    assert isinstance(last, AIMessage)
    assert last.additional_kwargs["parsed"] == {"score": 4, "reason": "solid"}


# ---------------------------------------------------------------------------
# Poisoning defence — a non-conforming entry under the structured key must
# degrade to a resend (lookup self-heal), and a resend rewritten by the
# output guards must never be stored under the structured key (store guard).
# ---------------------------------------------------------------------------


@dataclass
class _PoisonPlanter:
    """before_llm_call middleware that plants a NON-conforming cache hit on
    the structured pass only — simulating a poisoned structured entry."""

    poison: AIMessage
    name: str = "poison_planter"
    anchor: str = "before_llm_call"
    after: tuple[str, ...] = field(default_factory=tuple)
    before: tuple[str, ...] = field(default_factory=tuple)

    async def __call__(self, ctx: MiddlewareContext, call_next: CallNext) -> None:
        if ctx.payload.get("output_schema") is not None:
            ctx.payload["llm_cache_hit"] = self.poison
        await call_next(ctx)


@pytest.mark.asyncio
async def test_poisoned_structured_cache_hit_falls_back_to_resend() -> None:
    """Lookup self-heal: a structured cache hit that does NOT validate (e.g.
    a refusal stored before the store guard existed) is ignored — the turn
    issues the real resend instead of raising LLMOutputValidationError."""
    before = MiddlewareChain.from_middlewares(
        "before_llm_call",
        [_PoisonPlanter(poison=AIMessage(content="I can't help with that."))],
    )
    caller = _RecordingCaller(responses=[AIMessage(content=_INVALID), AIMessage(content=_VALID)])

    final = await _run(caller, output_schema=_SPEC, before_llm_chain=before)

    # The poisoned hit was ignored: the resend really went upstream.
    assert len(caller.calls) == 2
    assert caller.calls[1]["output_schema"] is _SPEC
    last = final["messages"][-1]
    assert isinstance(last, AIMessage)
    assert last.additional_kwargs["parsed"] == {"score": 4}


@pytest.mark.asyncio
async def test_guard_rewritten_resend_not_stored_under_structured_key() -> None:
    """Store guard, end-to-end with the real E.13 middlewares: a resend whose
    reply the 7.4 DLP rewrite broke (``parsed`` dropped) must not land under
    the structured key — the next identical turn resends instead of raising."""
    contact_schema = StructuredOutputSpec(
        schema={
            "type": "object",
            "properties": {"contact": {"type": "string", "pattern": "^[a-z]+@[a-z]+\\.com$"}},
            "required": ["contact"],
            "additionalProperties": False,
        },
        name="contact_card",
    )
    cache = LLMResponseCache(redis=InMemoryRedisCache())
    tenant = str(uuid4())

    # Run 1: invalid candidate → resend returns a conforming reply carrying
    # an email; DLP redacts it, the pattern no longer matches, ``parsed`` is
    # dropped — the rewritten reply must store WITHOUT the schema tag.
    before, after = _cache_chains(cache)
    caller_1 = _RecordingCaller(
        responses=[
            AIMessage(content=_INVALID),
            AIMessage(content='{"contact": "bob@corp.com"}'),
        ]
    )
    final_1 = await _run(
        caller_1,
        output_schema=contact_schema,
        before_llm_chain=before,
        after_llm_chain=after,
        config=_config(tenant=tenant),
        output_dlp=True,
    )
    assert len(caller_1.calls) == 2
    last_1 = final_1["messages"][-1]
    assert isinstance(last_1, AIMessage)
    assert "[redacted]" in str(last_1.content)
    assert "parsed" not in last_1.additional_kwargs  # dropped, not stale

    # Run 2, same tenant + prompt: the structured lookup must MISS (nothing
    # under the structured key) → one real resend, and the turn completes
    # instead of raising on a poisoned hit.
    before, after = _cache_chains(cache)
    caller_2 = _RecordingCaller(responses=[AIMessage(content='{"contact": "ann@corp.com"}')])
    final_2 = await _run(
        caller_2,
        output_schema=contact_schema,
        before_llm_chain=before,
        after_llm_chain=after,
        config=_config(tenant=tenant),
        output_dlp=True,
    )
    # The primary candidate is served from the unstructured entry (run 1's
    # invalid candidate), so the single upstream call IS the resend.
    assert len(caller_2.calls) == 1
    assert caller_2.calls[0]["output_schema"] is contact_schema
    last_2 = final_2["messages"][-1]
    assert isinstance(last_2, AIMessage)
    assert "[redacted]" in str(last_2.content)
