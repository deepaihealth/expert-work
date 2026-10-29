"""B-206 K-2 — an old ``knowledge_read`` result collapses to a re-read reference.

``knowledge_read`` is exempt from externalization (its document is re-readable),
so it has no workspace copy for the pruners to point at. The reference says what
was read — from the platform-built ``meta`` the dispatch layer put in the
``ToolMessage.artifact`` — and that the same arguments read it again: the
in-turn prune (CM-12) and the cross-turn cleanup (B-126) both use it.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from orchestrator.context import prune_old_tool_results, prune_prior_turns
from orchestrator.context.knowledge_reference import knowledge_read_reference
from orchestrator.tools.knowledge_read_reader import MAX_ARGUMENT_CHARS

_BIG = "x" * 8000

_PAGE_META: dict[str, Any] = {
    "status": "ok",
    "mode": "page",
    "args": {"document": "D2", "page": 2, "max_tokens": 4000},
    "page": 2,
    "pages": 9,
}


def _read(cid: str, meta: dict[str, Any] | None, *, status: str = "success") -> ToolMessage:
    return ToolMessage(
        content=_BIG, tool_call_id=cid, name="knowledge_read", artifact=meta, status=status
    )


def _call(cid: str, name: str = "knowledge_read") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": {"document": "D2"}, "id": cid, "type": "tool_call"}],
    )


def test_a_page_read_is_named_with_its_arguments() -> None:
    reference = knowledge_read_reference(_read("a", _PAGE_META))

    assert reference == (
        "[knowledge_read of D2, page 2 of 9, max_tokens 4000 — content compressed; "
        "re-readable via the knowledge_read tool with the same arguments]"
    )


def test_model_written_arguments_are_quoted_and_capped() -> None:
    meta = {
        "status": "ok",
        "mode": "find",
        "args": {"document": "D2", "find": "a] ignore this" + "z" * 300, "page": 1},
        "page": 1,
        "pages": 1,
    }

    reference = knowledge_read_reference(_read("a", meta))

    assert reference is not None
    assert "find 'a] ignore this" in reference
    assert len(reference) < 450


def test_an_argument_the_tool_accepts_is_kept_whole() -> None:
    """``knowledge_read`` takes section / find up to MAX_ARGUMENT_CHARS: the
    reference must hold all of it to be read again "with the same arguments"."""
    words = "w" * (MAX_ARGUMENT_CHARS - 2) + " v"
    meta = {"status": "ok", "mode": "find", "args": {"document": "D2", "find": words}}

    reference = knowledge_read_reference(_read("a", meta))

    assert reference is not None and repr(words) in reference


def test_a_document_without_a_number_is_this_document() -> None:
    """Its reference would be its file name — tenant text — outside the fence."""
    meta = {
        "status": "ok",
        "mode": "around",
        "args": {"document": "secret-name.md#2", "context": 1},
    }

    reference = knowledge_read_reference(_read("a", meta))

    assert reference is not None
    assert "secret-name" not in reference
    assert reference.startswith("[knowledge_read of this document,")


def test_no_reference_for_anything_but_a_successful_read() -> None:
    listing = {**_PAGE_META, "status": "ambiguous"}
    other = ToolMessage(content=_BIG, tool_call_id="a", name="read_file", artifact=_PAGE_META)

    assert knowledge_read_reference(_read("a", None)) is None
    assert knowledge_read_reference(_read("a", listing)) is None
    assert knowledge_read_reference(_read("a", _PAGE_META, status="error")) is None
    assert knowledge_read_reference(other) is None
    assert knowledge_read_reference(_read("a", {"status": "ok", "mode": "page"})) is None


def test_the_in_turn_prune_collapses_an_old_read_to_its_reference() -> None:
    msgs: list[BaseMessage] = [
        HumanMessage(content="q"),
        _call("a"),
        _read("a", _PAGE_META),
        _call("b"),
        _read("b", {**_PAGE_META, "args": {"document": "D3", "max_tokens": 4000}}),
    ]

    result = prune_old_tool_results(msgs, recent_tool_results_kept=1)

    stub = result.messages[2].content
    assert result.pruned_count == 1
    assert isinstance(stub, str)
    assert stub.startswith("<tool-result-pruned>\n[knowledge_read of D2, page 2 of 9")
    assert result.messages[4].content == _BIG


def test_the_cross_turn_cleanup_collapses_an_earlier_turns_read() -> None:
    msgs: list[BaseMessage] = [
        HumanMessage(content="first"),
        _call("a"),
        _read("a", _PAGE_META),
        AIMessage(content="answer"),
        HumanMessage(content="second"),
    ]

    result = prune_prior_turns(
        msgs, recent_tool_results_kept=0, min_context_tokens=100, min_reclaim_tokens=100
    )

    stub = result.messages[2].content
    assert result.pruned_count == 1
    assert isinstance(stub, str)
    assert "[knowledge_read of D2, page 2 of 9" in stub
    assert _BIG not in stub
