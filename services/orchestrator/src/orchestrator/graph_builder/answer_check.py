"""B-206 11-09 (B) — the answer check after knowledge results.

Once a knowledge result of this turn has shown passages (a ``knowledge_search``
with hits, a ``knowledge_read`` that showed a passage), every later step of the
turn carries :data:`ANSWER_CHECK_NOTE` in its ``<platform-context>`` tail: check
each conclusion against the text shown, and give what no shown text states in
the partial-answer form the user chose (11-07, style B).

Only results that showed something count. A failed call (no bases, invalid
arguments, a raised error), a search that found nothing and a read that showed
no passage do not: their own notices already say what that means, and for an
incomplete search finding nothing says nothing about absence (K-22). The note
speaks of "the results shown", never of the whole knowledge base, so a search
that showed passages while a base or a retrieval path failed is covered too.
The test: the result's ``status`` is not ``error`` (as in
:mod:`orchestrator.context.knowledge_reference`) and its recorded ``artifact``
(``ToolResult.meta``) has hits or passages — every other search / read status
carries none. It reads ``state["messages"]``, whose results keep their
``artifact``; the in-view copies a prune collapses drop it.

Why before the answer, not after it. A check after the final answer means one
more model call on almost every turn of an agent with a knowledge base (B-267
has it search first), the answer written twice, and the first version already
streamed to the user token by token. Here the note rides on the steps the turn
takes anyway: no extra call, no second answer. The trigger is structural (a
knowledge tool result in this turn), so it does not depend on the language of
the question or the answer.

View-only, like the other tail notes: never stored, one copy per prompt, gone
the next turn. Cost: about 70 tokens on each step after the first knowledge
result that showed passages. When the step already has a tail section (the
workspace snapshot whenever the workspace lists files, a plan, recalled memory)
the note joins it; when it is the only one, it adds the tail itself, with the
prompt-cache cost B-162 describes (the previous step's last message is sent
again without it).

A delegated child (``spawn_worker``) runs its own graph: knowledge results it
reads put the note in the child's prompts, not in the parent's final answer.

Where an independent "is the material enough" verifier would go (not built,
waits on eval data): a separate call with only the question and the shown
passages, made when the step that follows a knowledge result returns a final
answer and before ``agent`` routes to the end.
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import BaseMessage, ToolMessage

from orchestrator.context.knowledge_reference import (
    KNOWLEDGE_READ_TOOL_NAME,
    KNOWLEDGE_SEARCH_TOOL_NAME,
)
from orchestrator.context.tool_result_prune import current_turn_start

KNOWLEDGE_TOOL_NAMES = frozenset({KNOWLEDGE_SEARCH_TOOL_NAME, KNOWLEDGE_READ_TOOL_NAME})

ANSWER_CHECK_NOTE = (
    "[answer check] Knowledge base results in this turn showed passages. Before the final "
    "answer, check each conclusion against the text they showed. For one that no shown text "
    "states, say the results shown do not directly state it, give the related facts they do "
    "state with their documents, and mark any inference of your own as inference."
)


def _showed_passages(message: BaseMessage) -> bool:
    """A knowledge result that showed passages: a search with hits, a read with passages."""
    if not isinstance(message, ToolMessage) or message.name not in KNOWLEDGE_TOOL_NAMES:
        return False
    if getattr(message, "status", "success") == "error":
        return False
    artifact = message.artifact
    if not isinstance(artifact, dict):
        return False
    hits = artifact.get("hits")
    passages = artifact.get("passages")
    return (isinstance(hits, int) and not isinstance(hits, bool) and hits > 0) or (
        isinstance(passages, list) and bool(passages)
    )


def answer_check_note(messages: Sequence[BaseMessage]) -> str | None:
    """:data:`ANSWER_CHECK_NOTE` when a knowledge result of this turn showed passages."""
    turn = messages[current_turn_start(messages) :]
    return ANSWER_CHECK_NOTE if any(_showed_passages(m) for m in turn) else None
