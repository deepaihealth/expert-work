"""B-206 11-09 (B) — the answer check after knowledge results.

Once a turn has knowledge results (``knowledge_search`` / ``knowledge_read``),
every later step of that turn carries :data:`ANSWER_CHECK_NOTE` in its
``<platform-context>`` tail: check each conclusion against the text shown, and
give what no text states in the partial-answer form the user chose (11-07,
style B).

Why before the answer, not after it. A check after the final answer means one
more model call on almost every turn of an agent with a knowledge base (B-267
has it search first), the answer written twice, and the first version already
streamed to the user token by token. Here the note rides on the steps the turn
takes anyway: no extra call, no second answer. The trigger is structural (a
knowledge tool result in this turn), so it does not depend on the language of
the question or the answer.

View-only, like the other tail notes: never stored, one copy per prompt, gone
the next turn. Cost: about 70 tokens on each step after the first knowledge
result of the turn; the tail is already re-attached every step (workspace
snapshot), so it breaks no more of the prompt cache than before.

Where an independent "is the material enough" verifier would go (not built,
waits on eval data): a separate call with only the question and the shown
passages, made when the step that follows a knowledge result returns a final
answer and before ``agent`` routes to the end.
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage

from expert_work.common.conversation_channel import is_hidden
from orchestrator.context.knowledge_reference import (
    KNOWLEDGE_READ_TOOL_NAME,
    KNOWLEDGE_SEARCH_TOOL_NAME,
)

KNOWLEDGE_TOOL_NAMES = frozenset({KNOWLEDGE_SEARCH_TOOL_NAME, KNOWLEDGE_READ_TOOL_NAME})

ANSWER_CHECK_NOTE = (
    "[answer check] This turn used knowledge base results. Before the final answer, check "
    "each conclusion against text a result showed. For one that no shown text states, say "
    "the knowledge base does not directly state it, give the related facts it does state "
    "with their documents, and mark any inference of your own as inference."
)


def _current_turn(messages: Sequence[BaseMessage]) -> Sequence[BaseMessage]:
    """The messages after the latest user message (platform-hidden ones do not count)."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, HumanMessage) and not is_hidden(message):
            return messages[index + 1 :]
    return messages


def answer_check_note(messages: Sequence[BaseMessage]) -> str | None:
    """:data:`ANSWER_CHECK_NOTE` when this turn has a knowledge tool result, else ``None``."""
    used = any(
        isinstance(message, ToolMessage) and message.name in KNOWLEDGE_TOOL_NAMES
        for message in _current_turn(messages)
    )
    return ANSWER_CHECK_NOTE if used else None
