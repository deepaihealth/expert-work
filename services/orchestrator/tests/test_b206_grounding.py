"""B-206 11-09 — grounding for knowledge-base answers.

A. A ``knowledge_read`` result that shows passages carries a grounding notice
   (platform text, facts not commands): the text shown is what the document
   states; names / titles / sitting in the same table are where text is, not a
   relation; a list leaving an item out is not permission; what is said of one
   item is not said of another. A read that showed nothing carries none.
   The answerability rule in the ``knowledge_search`` description gives the
   partial-answer form the user chose (style B, 11-07): "not directly stated" +
   the related facts with their documents + inference / general knowledge
   marked as such.
B. The answer check: on every step after this turn's knowledge results, one
   platform line in the prompt view (view-only, no extra model call). Absent on
   a turn without knowledge results, even when an earlier turn had some.

Prompt-text tests pin structure and gating, plus a token budget — not the
exact wording.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from expert_work.runtime.tokens import default_estimator
from orchestrator.graph_builder.answer_check import ANSWER_CHECK_NOTE, answer_check_note
from orchestrator.tools.knowledge import KNOWLEDGE_ANSWERABILITY_RULE
from orchestrator.tools.knowledge_read_reader import GROUNDING_NOTICE
from orchestrator.tools.registry import ToolContext, ToolRegistry, ToolResult, ToolSpec

from .test_figure_block_injection import _graph, _RecordingLLM
from .test_knowledge_read import _flat, _lib, _manual

#: Per-injection budgets (o200k tokens): the read notice rides on every read,
#: the answer check on every step after a knowledge result.
_READ_NOTICE_MAX_TOKENS = 90
_ANSWER_CHECK_MAX_TOKENS = 90


def _tokens(text: str) -> int:
    return default_estimator().count(text)


# ---------------------------------------------------------------------------
# A. knowledge_read grounding notice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        {"document": "D2"},
        {"document": "D2", "page": 2},
        {"document": "D2#4", "context": 1},
        {"document": "D2", "find": "P03"},
    ],
    ids=["page", "next-page", "around", "find"],
)
async def test_a_read_that_shows_passages_carries_the_grounding_notice(
    args: dict[str, Any],
) -> None:
    lib = await _lib()
    await _manual(lib)

    result = await lib.read(**args)

    assert result.meta["passages"], "the read showed passages"
    assert result.notice is not None
    assert result.notice.count(GROUNDING_NOTICE) == 1


async def test_a_section_read_carries_the_grounding_notice() -> None:
    lib = await _lib()
    await lib.add("first.md", _flat(1))
    await lib.add("guide.md", _flat(3, path=("Usage",)))

    result = await lib.read(document="D2", section="Usage")

    assert result.meta["passages"]
    assert result.notice is not None and GROUNDING_NOTICE in result.notice


@pytest.mark.parametrize(
    "args",
    [{"document": "D2", "find": "absent words"}, {"document": "D2", "section": "nothing"}],
    ids=["find-no-match", "no-section"],
)
async def test_a_read_that_showed_nothing_carries_no_grounding_notice(
    args: dict[str, Any],
) -> None:
    lib = await _lib()
    await _manual(lib)

    result = await lib.read(**args)

    assert result.meta["passages"] == []
    assert result.notice is not None and GROUNDING_NOTICE not in result.notice


def test_the_grounding_notice_names_the_four_traps_as_facts_within_budget() -> None:
    notice = GROUNDING_NOTICE
    assert notice.startswith("[knowledge_read: ") and notice.endswith("]")
    assert notice.isascii()
    assert _tokens(notice) <= _READ_NOTICE_MAX_TOKENS
    lowered = notice.lower()
    # Names / titles / same table: navigation, not a relation.
    assert "file name" in lowered and "same table" in lowered and "related" in lowered
    # Absence from a list is not permission.
    assert "leaves an item out" in lowered and "allowed" in lowered
    # One item's rule does not transfer to another.
    assert "one item" in lowered and "another" in lowered
    # Facts, not commands (B-163): no imperative opener, no "must" / "do not".
    for command in ("must", "do not", "don't", "never", "always"):
        assert command not in lowered, command
    # Not the reverted track-D framing (c2509964e).
    assert "not evidence" not in lowered and "reading more" not in lowered


# ---------------------------------------------------------------------------
# A. style-B partial answers in the knowledge_search rule
# ---------------------------------------------------------------------------


def test_the_answerability_rule_gives_the_partial_answer_form() -> None:
    rule = KNOWLEDGE_ANSWERABILITY_RULE
    # The measured opening (B-175 wave 2 PR3) stays.
    assert rule.startswith(
        "Results are the closest passages, not confirmed answers: rely only on what they "
        "explicitly state"
    )
    assert "does not directly state" in rule
    assert "related facts" in rule and "documents" in rule
    assert "inference" in rule and "general knowledge" in rule
    # Style A ("only say not stated") is gone: inference is labelled, not banned.
    assert "instead of inferring or guessing it" not in rule


# ---------------------------------------------------------------------------
# B. the answer check — unit
# ---------------------------------------------------------------------------


def _kb_result(name: str = "knowledge_search", call_id: str = "c1") -> ToolMessage:
    return ToolMessage(content="passages", tool_call_id=call_id, name=name)


def _call(name: str, call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": call_id}])


@pytest.mark.parametrize("tool", ["knowledge_search", "knowledge_read"])
def test_the_check_follows_a_knowledge_result_of_this_turn(tool: str) -> None:
    messages: list[BaseMessage] = [HumanMessage(content="q"), _call(tool), _kb_result(tool)]
    assert answer_check_note(messages) == ANSWER_CHECK_NOTE


def test_the_check_stays_after_later_tools_in_the_same_turn() -> None:
    messages: list[BaseMessage] = [
        HumanMessage(content="q"),
        _call("knowledge_search"),
        _kb_result(),
        _call("web_search", "c2"),
        ToolMessage(content="web", tool_call_id="c2", name="web_search"),
    ]
    assert answer_check_note(messages) == ANSWER_CHECK_NOTE


def test_no_check_on_a_turn_without_knowledge_results() -> None:
    assert answer_check_note([HumanMessage(content="q")]) is None
    messages: list[BaseMessage] = [
        HumanMessage(content="q"),
        _call("web_search"),
        ToolMessage(content="web", tool_call_id="c1", name="web_search"),
    ]
    assert answer_check_note(messages) is None


def test_an_earlier_turns_knowledge_results_do_not_count() -> None:
    messages: list[BaseMessage] = [
        HumanMessage(content="q1"),
        _call("knowledge_search"),
        _kb_result(),
        AIMessage(content="answer 1"),
        HumanMessage(content="q2"),
    ]
    assert answer_check_note(messages) is None


def test_a_hidden_platform_message_does_not_start_a_turn() -> None:
    """Platform scaffolding (hidden user-role messages) is not the user's turn."""
    messages: list[BaseMessage] = [
        HumanMessage(content="q"),
        _call("knowledge_search"),
        _kb_result(),
        HumanMessage(content="[notice]", additional_kwargs={"expert_work_hide_from_ui": True}),
    ]
    assert answer_check_note(messages) == ANSWER_CHECK_NOTE


def test_the_check_is_short_platform_text_in_style_b() -> None:
    note = ANSWER_CHECK_NOTE
    assert note.isascii()
    assert _tokens(note) <= _ANSWER_CHECK_MAX_TOKENS
    lowered = note.lower()
    assert "final answer" in lowered
    assert "does not directly state" in lowered
    assert "related facts" in lowered
    assert "inference" in lowered


# ---------------------------------------------------------------------------
# B. the answer check — wired into agent_node
# ---------------------------------------------------------------------------


@dataclass
class _Fake:
    name: str

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.name, is_read_only=True)

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        return ToolResult(content="ok")


def _text(messages: list[BaseMessage]) -> str:
    return "\n".join(str(m.content) for m in messages)


async def _run(llm: _RecordingLLM, registry: ToolRegistry, history: list[BaseMessage]) -> None:
    config: RunnableConfig = {
        "configurable": {"thread_id": str(uuid4()), "tenant_id": str(uuid4())}
    }
    async with _graph(llm, registry=registry) as compiled:
        await compiled.ainvoke(
            {"messages": history, "step_count": 0, "max_steps": 6}, config=config
        )


async def test_the_check_reaches_every_prompt_after_a_knowledge_result_once() -> None:
    registry = ToolRegistry()
    registry.register(_Fake("knowledge_search"))
    registry.register(_Fake("echo"))
    llm = _RecordingLLM(
        responses=[_call("echo"), _call("knowledge_search", "c2"), _call("echo", "c3")]
    )

    await _run(llm, registry, [HumanMessage(content="q")])

    counts = [_text(prompt).count(ANSWER_CHECK_NOTE) for prompt in llm.seen_prompts]
    # before any tool / after echo / after knowledge_search / after the later echo
    assert counts == [0, 0, 1, 1]
    assert "<platform-context>" in str(llm.seen_prompts[2][-1].content)
    # One model call per step: the check adds none.
    assert llm.calls == 4


async def test_the_check_is_not_in_the_checkpoint() -> None:
    """View-only: the stored history never carries it, so it cannot pile up."""
    registry = ToolRegistry()
    registry.register(_Fake("knowledge_search"))
    llm = _RecordingLLM(responses=[_call("knowledge_search")])
    config: RunnableConfig = {
        "configurable": {"thread_id": str(uuid4()), "tenant_id": str(uuid4())}
    }
    async with _graph(llm, registry=registry) as compiled:
        final = await compiled.ainvoke(
            {"messages": [HumanMessage(content="q")], "step_count": 0, "max_steps": 5},
            config=config,
        )
    assert ANSWER_CHECK_NOTE not in _text(final["messages"])
    assert ANSWER_CHECK_NOTE in _text(llm.seen_prompts[-1])


async def test_no_check_when_the_turn_used_no_knowledge_tool() -> None:
    registry = ToolRegistry()
    registry.register(_Fake("echo"))
    llm = _RecordingLLM(responses=[_call("echo")])

    await _run(llm, registry, [HumanMessage(content="q")])

    assert all(ANSWER_CHECK_NOTE not in _text(p) for p in llm.seen_prompts)
