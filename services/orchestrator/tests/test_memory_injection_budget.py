"""Stream RT-2 PR-2 (RT-ADR-10) — memory injection token budget.

The recall path caps COUNT only (``retrieve_top_k``): a single oversized
memory could blow up the injected block (STREAM-RT §8.1). ``_inject_memories``
now selects items greedily in rank order against a token budget (default
2000), truncating the boundary item with a visible marker, with a
guaranteed slice for user-corrected memories (``confidence == 1.0`` is
the M-4 correction API's EXCLUSIVE sentinel — there is no dedicated
``kind``, and the extraction write path caps its LLM-scored confidence
at 0.99 so nothing else can reach the sentinel), so ordinary memories
can never squeeze a user's explicit correction out.

The default path is pinned unchanged: ``retrieve_top_k=5`` ordinary
memories sit far below the budget, so the rendered block is
byte-identical to the pre-budget logic.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.runnables import RunnableConfig

from expert_work.common.spotlight import spotlight_untrusted
from expert_work.protocol import MemoryItem
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import GraphRunner, ToolRegistry, build_react_graph
from orchestrator.context.memory_render import (
    MEMORY_BLOCK_HEADING,
    MEMORY_LINE_LEGEND,
    MEMORY_TRUNCATION_MARKER,
)
from orchestrator.graph_builder.builder import (
    _inject_memories,
    _memory_injection_truncated_total,
)
from orchestrator.graph_builder.memory import parse_extracted_memories
from orchestrator.tools.registry import ToolSpec

#: The reminder section starts here; its lines follow the block's source marks
#: (pinned in ``test_memory_render.py``).
_REMINDER_HEADING = "## Before relying on a memory\n"

#: B-300 PR 2 —— every line now names its source. The contents below are
#: shortened by the prefix growth, so each LINE keeps its old length and every
#: token budget in this file is unchanged (ordinary rows have no source column
#: → "source not recorded"; confidence 1.0 → "corrected by the user").
_ORD = "- (fact · source not recorded) "
_COR = "- (fact · corrected by the user) "


def _memory(content: str, *, confidence: float = 0.5) -> MemoryItem:
    return MemoryItem(
        id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        kind="fact",
        content=content,
        embedding=(),
        confidence=confidence,
    )


def _base_messages() -> list[BaseMessage]:
    return [SystemMessage(content="system"), HumanMessage(content="task")]


def _strip_frame(text: str) -> str:
    """B-300 ③ —— the block is heading + legend line + items + blank line + the
    reminder section; these tests look at the heading and the items only (the
    frame itself is pinned in ``test_memory_render.py``)."""
    head = MEMORY_BLOCK_HEADING + "\n" + MEMORY_LINE_LEGEND + "\n"
    tail = "\n\n" + _REMINDER_HEADING
    assert text.startswith(head)
    assert tail in text
    return MEMORY_BLOCK_HEADING + "\n" + text[len(head) : text.index(tail)]


def _body(out: list[BaseMessage]) -> str:
    """The injected block's heading + items in per_session mode (position 1)."""
    return _strip_frame(str(out[1].content))


def _counter_value(outcome: str) -> float:
    child = _memory_injection_truncated_total.labels(outcome=outcome)
    return child._value.get()  # type: ignore[attr-defined,no-any-return]


# ---------------------------------------------------------------------------
# Default path — pinned byte-identical to the pre-budget logic
# ---------------------------------------------------------------------------


def test_default_path_five_ordinary_memories_unchanged() -> None:
    """RT-2 PR-2 invariant: the default configuration (top_k=5 ordinary
    memories, default 2000-token budget) renders exactly the pre-budget
    block — the budget only bites on oversized items."""
    contents = [f"user prefers option {i} for their workflow" for i in range(5)]
    expected = "## Relevant memories from past sessions\n" + "\n".join(
        f"{_ORD}{content}" for content in contents
    )
    out = _inject_memories(_base_messages(), [_memory(c) for c in contents], mode="per_session")
    assert _body(out) == expected


def test_default_path_per_turn_unchanged() -> None:
    contents = ["likes tea", "works remotely"]
    expected = "## Relevant memories from past sessions\n" + "\n".join(
        f"{_ORD}{content}" for content in contents
    )
    out = _inject_memories(_base_messages(), [_memory(c) for c in contents], mode="per_turn")
    assert _strip_frame(str(out[-1].content)) == expected


def test_default_path_spotlight_unchanged() -> None:
    """The budget selects on the RAW item lines; spotlighting (PI-1b)
    still wraps the selected block exactly as before."""
    contents = ["likes tea", "works remotely"]
    items = "\n".join(f"{_ORD}{content}" for content in contents)
    expected = "## Relevant memories from past sessions\n" + spotlight_untrusted(
        items, nonce="abc123def456"
    )
    out = _inject_memories(
        _base_messages(),
        [_memory(c) for c in contents],
        mode="per_session",
        spotlight_nonce="abc123def456",
    )
    assert _body(out) == expected


# ---------------------------------------------------------------------------
# Greedy budget — stop at the boundary, truncate the boundary item
# ---------------------------------------------------------------------------


def test_budget_stops_greedy_and_truncates_boundary_item() -> None:
    """Items accumulate in rank order; the first item that does not fit
    is truncated to the remaining budget (visible marker) and later
    items are dropped. Char heuristic: line = 31-char prefix + content,
    tokens = chars // 4."""
    mems = [
        _memory("a" * 69),  # line 100 chars → 25 tokens
        _memory("b" * 69),  # 25 tokens
        _memory("c" * 69),  # 25 tokens → only 10 left → truncated
        _memory("d" * 69),  # dropped
    ]
    out = _inject_memories(_base_messages(), mems, mode="per_session", token_budget=60)
    body = _body(out)
    lines = body.splitlines()
    # header + 3 selected lines (the 4th is dropped).
    assert len(lines) == 4
    assert lines[1] == _ORD + "a" * 69
    assert lines[2] == _ORD + "b" * 69
    assert lines[3].endswith(MEMORY_TRUNCATION_MARKER)
    # The truncated boundary item keeps a head slice of its content.
    assert lines[3].startswith(_ORD + "ccc")
    assert "dddd" not in body


def test_single_overlong_memory_is_truncated_not_dropped() -> None:
    """RT-ADR-10 headline case: one oversized memory (the recall path
    caps count only) can no longer blow up the injection block — it is
    cut to the budget with a visible marker."""
    out = _inject_memories(_base_messages(), [_memory("y" * 40_000)], mode="per_session")
    body = _body(out)
    assert MEMORY_TRUNCATION_MARKER in body
    # ~2000 tokens ≈ 8000 chars at the chars//4 heuristic (plus marker).
    assert len(body) < 9_000
    assert body.startswith("## Relevant memories from past sessions\n" + _ORD + "yyy")


def test_injected_estimator_drives_the_budget() -> None:
    """The estimator is injectable (the factory hands the shared
    tiktoken-backed one); a 1-token-per-char estimator makes the same
    line cross a budget the chars//4 heuristic stays 4x under."""

    class _OnePerChar:
        def count(self, text: str) -> int:
            return len(text)

    mems = [_memory("z" * 169)]  # line 200 chars → 50 heuristic / 200 injected
    untouched = _inject_memories(_base_messages(), mems, mode="per_session", token_budget=100)
    truncated = _inject_memories(
        _base_messages(),
        mems,
        mode="per_session",
        token_budget=100,
        estimator=_OnePerChar(),
    )
    assert MEMORY_TRUNCATION_MARKER not in _body(untouched)
    assert MEMORY_TRUNCATION_MARKER in _body(truncated)


# ---------------------------------------------------------------------------
# Correction guarantee (confidence == 1.0 — M-4 user corrections)
# ---------------------------------------------------------------------------


def test_correction_is_not_squeezed_out_by_earlier_ordinary_items() -> None:
    """deer-flow guaranteed_categories shape: the correction at the END
    of the ranking still lands because it gets first claim on the
    guarantee slice; without it the ordinary items would exhaust the
    budget first. ``confidence=1.0`` here is the real M-4 shape — the
    correction endpoint is the sentinel's only writer."""
    mems = [
        _memory("n" * 129),  # line 160 chars → 40 tokens
        _memory("o" * 129),  # 40 tokens
        _memory("p" * 129),  # 40 tokens — dropped (budget spent)
        _memory("q" * 47, confidence=1.0),  # 20 tokens — guaranteed
    ]
    out = _inject_memories(
        _base_messages(),
        mems,
        mode="per_session",
        token_budget=100,
        correction_token_budget=25,
    )
    body = _body(out)
    lines = body.splitlines()
    # Selected: n, o (general pass) + q (guarantee) — rendered in the
    # original rank order, so the default path stays byte-stable.
    assert len(lines) == 4
    assert lines[1] == _ORD + "n" * 129
    assert lines[2] == _ORD + "o" * 129
    assert lines[3] == _COR + "q" * 47
    assert "ppp" not in body


def test_correction_larger_than_reserve_competes_in_general_pass() -> None:
    """The guarantee slice is a floor, not a cap: a correction that
    overflows the reserve falls through to the general pass and can
    use the full remaining budget untruncated."""
    mems = [_memory("r" * 3_167, confidence=1.0)]  # line 3200 chars → 800 tokens
    out = _inject_memories(
        _base_messages(),
        mems,
        mode="per_session",
        token_budget=2_000,
        correction_token_budget=500,
    )
    body = _body(out)
    assert MEMORY_TRUNCATION_MARKER not in body
    assert _COR + "r" * 3_167 in body


def test_oversized_correction_is_skipped_without_blocking_later_corrections() -> None:
    """Review MEDIUM-1: within the reserve, corrections are packed
    greedily in rank order and one that overflows the REMAINING reserve
    is SKIPPED — a later, smaller correction still gets its guarantee.
    Corrections [300, 400, 50] against a 500 reserve: 300 in, 400
    skipped (falls to the general pass), 50 in."""
    mems = [
        _memory("n" * 2_569),  # 650 tokens — fills the general budget
        _memory("a" * 1_167, confidence=1.0),  # correction, 300 tokens
        _memory("b" * 1_567, confidence=1.0),  # correction, 400 tokens
        _memory("c" * 167, confidence=1.0),  # correction, 50 tokens
    ]
    out = _inject_memories(
        _base_messages(),
        mems,
        mode="per_session",
        token_budget=1_000,
        correction_token_budget=500,
    )
    body = _body(out)
    lines = body.splitlines()
    # Guarantee: 300 + 50 (the 400 overflows the remaining 200 reserve and
    # falls through); general: the 650 ordinary item exactly fills the rest,
    # so the skipped 400 correction is dropped. Rank order preserved.
    assert len(lines) == 4
    assert lines[1] == _ORD + "n" * 2_569
    assert lines[2] == _COR + "a" * 1_167
    assert lines[3] == _COR + "c" * 167
    assert "bbb" not in body
    assert MEMORY_TRUNCATION_MARKER not in body


def test_extraction_scored_confidence_cannot_eat_the_correction_reserve() -> None:
    """Review HIGH regression: the extraction prompt encourages high
    confidence and the score clamp used to pin >=1 values to exactly
    1.0 — an ordinary extracted memory would then impersonate a
    correction, eat the reserve, and squeeze the real (later-ranked)
    M-4 correction out under a tight budget. The extraction path now
    caps at 0.99, so the sentinel stays M-4-exclusive."""
    extracted = parse_extracted_memories(
        '{"memories": [{"kind": "fact", "content": "'
        + "e" * 49
        + '", "importance": 0.9, "confidence": 1.0}]}'
    )
    assert len(extracted) == 1
    clamped = extracted[0].confidence
    assert clamped == 0.99
    mems = [
        # The extracted item, exactly as the writeback would persist it
        # (kind / content / confidence carried over).
        _memory(extracted[0].content, confidence=clamped),  # 20 tokens
        _memory("n" * 129),  # ordinary, 40 tokens
        _memory("q" * 47, confidence=1.0),  # the REAL correction, ranked last
    ]
    out = _inject_memories(
        _base_messages(),
        mems,
        mode="per_session",
        token_budget=60,
        correction_token_budget=25,
    )
    body = _body(out)
    # The real correction wins the reserve despite being ranked last; had
    # the extracted item reached 1.0 it would have claimed the reserve
    # first (earlier rank) and the correction would have been dropped.
    assert _COR + "q" * 47 in body
    assert _ORD + "e" * 49 in body


def test_zero_correction_budget_disables_the_guarantee() -> None:
    mems = [
        _memory("s" * 69),  # 25 tokens
        _memory("t" * 69),  # 25 tokens
        _memory("u" * 47, confidence=1.0),  # correction, but no reserve
    ]
    out = _inject_memories(
        _base_messages(),
        mems,
        mode="per_session",
        token_budget=50,
        correction_token_budget=0,
    )
    body = _body(out)
    assert "sss" in body
    assert "ttt" in body
    assert "uuu" not in body


# ---------------------------------------------------------------------------
# Metric
# ---------------------------------------------------------------------------


def test_truncation_metric_counts_truncated_and_dropped_items() -> None:
    truncated_before = _counter_value("truncated")
    dropped_before = _counter_value("dropped")
    mems = [
        _memory("a" * 91),  # 25 tokens — fits
        _memory("b" * 91),  # 25 tokens — truncated at the boundary
        _memory("c" * 91),  # dropped
        _memory("d" * 91),  # dropped
    ]
    _inject_memories(_base_messages(), mems, mode="per_session", token_budget=40)
    truncated_after = _counter_value("truncated")
    dropped_after = _counter_value("dropped")
    assert truncated_after == truncated_before + 1
    assert dropped_after == dropped_before + 2


def test_default_path_does_not_touch_the_metric() -> None:
    truncated_before = _counter_value("truncated")
    dropped_before = _counter_value("dropped")
    _inject_memories(_base_messages(), [_memory("likes tea")], mode="per_session")
    truncated_after = _counter_value("truncated")
    dropped_after = _counter_value("dropped")
    assert truncated_after == truncated_before
    assert dropped_after == dropped_before


# ---------------------------------------------------------------------------
# Wiring — build_react_graph threads the budget into agent_node
# ---------------------------------------------------------------------------


@dataclass
class _RecordingLLM:
    calls: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del tools
        self.calls.append(list(messages))
        return AIMessage(content="done")


@pytest.mark.asyncio
async def test_graph_threads_injection_budget_to_the_prompt() -> None:
    """``build_react_graph(memory_injection_token_budget=...)`` reaches
    the prompt the LLM sees — the oversized recalled memory arrives
    truncated."""
    llm = _RecordingLLM()
    graph = build_react_graph(
        llm_caller=llm,
        tool_registry=ToolRegistry(),
        memory_recall_mode="per_session",
        memory_injection_token_budget=100,
    )
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        await compiled.ainvoke(
            {
                "messages": [
                    SystemMessage(content="you are helpful"),
                    HumanMessage(content="start"),
                ],
                "step_count": 0,
                "max_steps": 3,
                "recalled_memories": [_memory("w" * 2_000)],
            },
            config=cfg,
        )
    assert len(llm.calls) == 1
    block = llm.calls[0][1]
    assert isinstance(block, HumanMessage)
    content = str(block.content)
    assert "Relevant memories" in content
    assert MEMORY_TRUNCATION_MARKER in content
    assert len(_strip_frame(content)) < 1_000
