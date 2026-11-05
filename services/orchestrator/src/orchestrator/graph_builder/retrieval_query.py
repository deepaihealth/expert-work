"""The query of a turn's automatic look-ups: long-term memory recall and the KB probe.

Both run before the model has said anything this turn, so the only query they
have is what the user wrote. A follow-up often names nothing a retriever can
match -- a pronoun ("his blood sugar", "is it waterproof"), a demonstrative
("this one", "those two"), a pointer into the last answer ("the second item
above"), a time ("that day") or plain ellipsis ("and the price?"). The referent
is in the conversation: in the previous user message or, often, only in the
assistant's previous answer.

:func:`build_retrieval_query` -- deterministic, no model call -- carries that
context along when the message is short (:data:`FOLLOW_UP_MAX_CHARS`) or holds a
reference / ellipsis marker and is not long (:data:`REFERENCE_MAX_CHARS`):

    <previous user message, its end> \\n <previous answer, head and tail> \\n <this message>

Bounds (characters):

* previous user message: its last :data:`USER_CONTEXT_CHARS` -- the question of
  a long message is at its end (the same bound the KB probe used since B-267);
* previous answer: its first :data:`ASSISTANT_HEAD_CHARS` and last
  :data:`ASSISTANT_TAIL_CHARS` -- an answer names its subject and opens its list
  first, and a closing line is often an offer or a clarifying question;
* the context is at most about 500 characters, a few times a typical follow-up:
  enough to name the referent, small enough that the KB probe stays inside its
  time budget and the query still leans on what was just asked.

A long message is left alone even with a marker: a paste or a detailed request
names its own subject, and carrying the last turn along would only dilute it.

:func:`recent_history` is the bounded window the optional model rewrite of the
memory recall query (``memory.long_term.rewrite_reads``) reads.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from expert_work.common.conversation_channel import is_hidden, is_superseded, message_text
from expert_work.common.spotlight import unspotlight
from orchestrator.graph_builder.platform_context import strip_platform_context

logger = logging.getLogger(__name__)

#: A message this short carries the context along whatever it says.
FOLLOW_UP_MAX_CHARS: Final = 20
#: A message with a reference marker carries it along up to this length.
REFERENCE_MAX_CHARS: Final = 200
USER_CONTEXT_CHARS: Final = 200
ASSISTANT_HEAD_CHARS: Final = 200
ASSISTANT_TAIL_CHARS: Final = 100
#: The model rewrite reads the last few turns, each message cut to head + tail.
HISTORY_TURNS: Final = 3
HISTORY_MESSAGE_HEAD_CHARS: Final = 400
HISTORY_MESSAGE_TAIL_CHARS: Final = 200

_ELLIPSIS: Final = "…"

#: Chinese demonstratives, pronouns, pointers into what was said before and
#: ordinals of a list item. "该" ("that / the said") is not counted inside
#: the common words for "should" and similar.
_ZH_MARKERS: Final = re.compile(
    "这|那|它|他|她|(?<![应活本])该|其中|上面|上述|上文|以上|前面|前者|后者|刚才|刚刚|之前|"
    "上次|上一|另一|另外|当时|哪个|哪些|哪一|哪种|哪款|哪位|提到|"
    "第[一二三四五六七八九十两0-9]+[条个项点种款位家本篇段行张次]"
)
_EN_MARKERS: Final = re.compile(
    r"\b(?:it|its|this|that|these|those|they|them|their|he|him|his|she|her|another|"
    r"above|previous|earlier|former|latter|same|what about|how about|which one|"
    r"the (?:first|second|third|fourth|fifth|last|other)|"
    r"(?:first|second|third|fourth|fifth|last|other) one)\b",
    re.IGNORECASE,
)

Trigger = Literal["none", "short", "reference"]


@dataclass(frozen=True)
class RetrievalQuery:
    """The query (:attr:`text`), what the user wrote this turn (:attr:`current`),
    why context was considered (:attr:`trigger`) and how much was carried along
    (:attr:`context_chars`, 0: none)."""

    text: str
    current: str
    trigger: Trigger
    context_chars: int = 0

    @property
    def augmented(self) -> bool:
        return self.context_chars > 0


def user_text(message: BaseMessage) -> str:
    """What the user wrote: the platform section and the spotlight fence off."""
    return unspotlight(strip_platform_context(message_text(message.content))).strip()


def _answer_text(message: BaseMessage) -> str:
    text = message_text(message.content).strip()
    # A placeholder ("...") names nothing.
    return text if any(ch.isalnum() for ch in text) else ""


def _live(message: BaseMessage) -> bool:
    return not is_hidden(message) and not is_superseded(message)


def _user_positions(messages: Sequence[BaseMessage]) -> list[int]:
    return [i for i, m in enumerate(messages) if isinstance(m, HumanMessage) and _live(m)]


def _excerpt(text: str, head: int, tail: int) -> str:
    if len(text) <= head + tail:
        return text
    return f"{text[:head]}{_ELLIPSIS}{text[-tail:]}"


def trigger_of(text: str) -> Trigger:
    """Whether a message needs the conversation to be understood on its own."""
    if len(text) <= FOLLOW_UP_MAX_CHARS:
        return "short"
    if len(text) <= REFERENCE_MAX_CHARS and (_ZH_MARKERS.search(text) or _EN_MARKERS.search(text)):
        return "reference"
    return "none"


def _last_answer(messages: Sequence[BaseMessage], start: int, end: int) -> str:
    for message in reversed(messages[start:end]):
        if isinstance(message, AIMessage) and _live(message):
            text = _answer_text(message)
            if text:
                return text
    return ""


def build_retrieval_query(messages: Sequence[BaseMessage]) -> RetrievalQuery | None:
    """The query for the latest user message in ``messages`` -- ``None`` when
    there is none (or it holds no text)."""
    users = _user_positions(messages)
    if not users:
        return None
    current = user_text(messages[users[-1]])
    if not current:
        return None
    trigger = trigger_of(current)
    if trigger == "none":
        return RetrievalQuery(text=current, current=current, trigger=trigger)
    previous_at = users[-2] if len(users) > 1 else -1
    parts: list[str] = []
    if previous_at >= 0:
        asked = user_text(messages[previous_at])[-USER_CONTEXT_CHARS:]
        if asked:
            parts.append(asked)
    answer = _last_answer(messages, previous_at + 1, users[-1])
    if answer:
        parts.append(_excerpt(answer, ASSISTANT_HEAD_CHARS, ASSISTANT_TAIL_CHARS))
    if not parts:
        return RetrievalQuery(text=current, current=current, trigger=trigger)
    context = "\n".join(parts)
    return RetrievalQuery(
        text=f"{context}\n{current}",
        current=current,
        trigger=trigger,
        context_chars=len(context),
    )


def recent_history(messages: Sequence[BaseMessage], *, turns: int = HISTORY_TURNS) -> str:
    """The last ``turns`` turns before the latest user message, oldest first, one
    line per message (``[user] ...`` / ``[assistant] ...``), each cut to head +
    tail. Empty when the latest user message is the first."""
    users = _user_positions(messages)
    if len(users) < 2:
        return ""
    window = users[:-1][-turns:]
    lines: list[str] = []
    for message in messages[window[0] : users[-1]]:
        if not _live(message):
            continue
        if isinstance(message, HumanMessage):
            role, text = "user", user_text(message)
        elif isinstance(message, AIMessage):
            role, text = "assistant", _answer_text(message)
        else:
            continue
        if text:
            flat = " ".join(text.split())
            excerpt = _excerpt(flat, HISTORY_MESSAGE_HEAD_CHARS, HISTORY_MESSAGE_TAIL_CHARS)
            lines.append(f"[{role}] {excerpt}")
    return "\n".join(lines)


def log_retrieval_query(site: str, query: RetrievalQuery) -> None:
    """One line per built query: flags and lengths only, never the text."""
    logger.info(
        "retrieval_query site=%s trigger=%s augmented=%s current_chars=%d context_chars=%d",
        site,
        query.trigger,
        str(query.augmented).lower(),
        len(query.current),
        query.context_chars,
    )
