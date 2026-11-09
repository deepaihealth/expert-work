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


#: What the tools node records in ``artifact`` for a knowledge result that showed
#: passages (``ToolResult.meta``): a search's hit count, a read's passages.
_SHOWN: dict[str, dict[str, Any]] = {
    "knowledge_search": {"status": "ok", "hits": 2, "refs": ["D1#0", "D1#1"]},
    "knowledge_read": {"status": "ok", "passages": [{"ref": "D1#0"}]},
}


def _kb_result(
    name: str = "knowledge_search",
    call_id: str = "c1",
    *,
    artifact: dict[str, Any] | None = None,
    status: str = "success",
) -> ToolMessage:
    return ToolMessage(
        content="passages",
        tool_call_id=call_id,
        name=name,
        status=status,
        artifact=_SHOWN[name] if artifact is None else artifact,
    )


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


@pytest.mark.parametrize(
    ("name", "artifact", "status"),
    [
        # A failed call (no bases, invalid args, a raised error): nothing was shown.
        ("knowledge_search", {}, "error"),
        ("knowledge_read", {}, "error"),
        ("knowledge_search", {"status": "ok", "hits": 2}, "error"),
        # A search that ran and found nothing, also when a retrieval path failed
        # (K-22: that says nothing about absence; its own notice already says so).
        ("knowledge_search", {"status": "no_hits", "hits": 0}, "success"),
        ("knowledge_search", {"status": "below_threshold", "hits": 0}, "success"),
        ("knowledge_search", {"status": "no_hits", "hits": 0, "keyword_degraded": True}, "success"),
        # A read that showed no passage (find without a match, no such section).
        ("knowledge_read", {"status": "no_match", "passages": []}, "success"),
        # No recorded metadata: cannot tell what was shown.
        ("knowledge_search", {}, "success"),
    ],
    ids=[
        "search-error",
        "read-error",
        "error-with-metadata",
        "no-hits",
        "below-threshold",
        "degraded-no-hits",
        "read-no-match",
        "no-artifact",
    ],
)
def test_no_check_when_no_knowledge_result_showed_a_passage(
    name: str, artifact: dict[str, Any], status: str
) -> None:
    messages: list[BaseMessage] = [
        HumanMessage(content="q"),
        _call(name),
        _kb_result(name, artifact=artifact, status=status),
    ]
    assert answer_check_note(messages) is None


def test_a_degraded_search_that_showed_passages_still_gets_the_check() -> None:
    """Its wording is about the results shown, so it claims nothing about the
    bases or paths that could not be searched."""
    artifact = {"status": "ok", "hits": 1, "unsearched": 1, "embedding_degraded": True}
    messages: list[BaseMessage] = [
        HumanMessage(content="q"),
        _call("knowledge_search"),
        _kb_result(artifact=artifact),
    ]
    assert answer_check_note(messages) == ANSWER_CHECK_NOTE


def test_the_check_is_short_platform_text_in_style_b() -> None:
    note = ANSWER_CHECK_NOTE
    assert note.isascii()
    assert _tokens(note) <= _ANSWER_CHECK_MAX_TOKENS
    lowered = note.lower()
    assert "final answer" in lowered
    # Scoped to what was shown: never a claim about the whole knowledge base
    # (K-22 / the no-bases advice forbid "not in the knowledge base").
    assert "the results shown do not directly state" in lowered
    assert "knowledge base does not" not in lowered
    assert "related facts" in lowered
    assert "inference" in lowered


# ---------------------------------------------------------------------------
# B. the answer check — wired into agent_node
# ---------------------------------------------------------------------------


@dataclass
class _Fake:
    name: str
    fails: bool = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.name, is_read_only=True)

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        if self.fails:
            msg = "none of this agent's knowledge bases exists"
            raise RuntimeError(msg)
        return ToolResult(content="ok", meta=_SHOWN.get(self.name, {}))


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


async def test_no_check_after_a_failed_knowledge_call() -> None:
    registry = ToolRegistry()
    registry.register(_Fake("knowledge_search", fails=True))
    llm = _RecordingLLM(responses=[_call("knowledge_search")])

    await _run(llm, registry, [HumanMessage(content="q")])

    assert len(llm.seen_prompts) == 2
    assert all(ANSWER_CHECK_NOTE not in _text(p) for p in llm.seen_prompts)


async def test_no_check_when_the_turn_used_no_knowledge_tool() -> None:
    registry = ToolRegistry()
    registry.register(_Fake("echo"))
    llm = _RecordingLLM(responses=[_call("echo")])

    await _run(llm, registry, [HumanMessage(content="q")])

    assert all(ANSWER_CHECK_NOTE not in _text(p) for p in llm.seen_prompts)
