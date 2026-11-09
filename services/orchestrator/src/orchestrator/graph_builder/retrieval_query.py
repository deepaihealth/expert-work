"""The query of a turn's automatic look-ups: long-term memory recall and the KB probe.

Both run before the model has said anything this turn, so the only query they
have is what the user wrote. A follow-up often names nothing a retriever can
match -- a pronoun ("his blood sugar"), a demonstrative ("this one", "those
two"), a pointer into the last answer ("the second item above"), a time ("that
day") or plain ellipsis ("and the price?"). The referent is in the
conversation: in the previous user message or, often, only in the assistant's
previous answer.

:func:`build_retrieval_query` -- deterministic, no model call -- carries that
context along when the message is short (:data:`FOLLOW_UP_MAX_CHARS`) or holds a
reference / ellipsis marker and is not long (:data:`REFERENCE_MAX_CHARS`). It
carries nothing for small talk, an explicit topic switch, or a message that
names an identifier (an order number, a product code) and refers to nothing.

The context never replaces the message, and the callers keep the message's own
weight: keyword / full-text search puts the message first
(:attr:`RetrievalQuery.keyword_text`; the memory recall's full-text side uses
the message alone), and the memory recall searches the message and the
augmented query side by side.

Bounds (characters):

* previous user message: its last :data:`USER_CONTEXT_CHARS` -- the question of
  a long message is at its end (the bound the KB probe used since B-267);
* previous answer: its first :data:`ASSISTANT_HEAD_CHARS` and last
  :data:`ASSISTANT_TAIL_CHARS` -- an answer names its subject and opens its list
  first, and a closing line is often an offer or a clarifying question;
* the context is at most about 500 characters: enough to name the referent,
  small enough for the KB probe's time budget.

A long message is left alone even with a marker: a paste or a detailed request
names its own subject, and carrying the last turn along would only dilute it.

:func:`recent_history` is the bounded window the optional model rewrite of the
memory recall query (``memory.long_term.rewrite_reads``) reads.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from expert_work.common.conversation_channel import is_hidden, is_superseded, message_text
from expert_work.common.spotlight import unspotlight
from orchestrator.graph_builder.platform_context import strip_platform_context

logger = logging.getLogger(__name__)

#: A message this short carries the context along unless it is small talk, a
#: topic switch or an identifier.
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
_ZH_DIGITS: Final = "一二三四五六七八九十两"
_MEASURE_WORDS: Final = "个些两几款种位份条项家本篇段张"

#: Chinese markers. "这" / "那" (this / that) count at the start of a
#: sentence or bound to a measure word, not in "this week / month / year"; the
#: third-person pronouns count except inside "其他" (other); "以上"
#: (the above) not after a number or an age / price unit.
_ZH_MARKERS: Final = re.compile(
    rf"(?:^|(?<=[\s,.!?;:\u3002\uff01\uff1f\uff0c\uff1b\uff1a]))[这那](?![周月年天]|个月|个星期|个礼拜)"
    rf"|这(?:个(?!月|星期|礼拜)|[{_MEASURE_WORDS[1:]}])"
    rf"|那(?:[{_MEASURE_WORDS}]|里|边|天|次|时)"
    r"|(?<!其)[他她它]"
    r"|其中|上面|上述|上文|前面|前者|后者|刚才|刚刚|上次|上一|另一|当时|提到|你说的|您说的"
    r"|(?<![0-9一二三四五六七八九十百千万岁元斤克])以上"
    rf"|第[{_ZH_DIGITS}0-9]+[{_MEASURE_WORDS}点次行章]"
)
#: English markers: pointers into what was said, not the generic it / that / they.
_EN_MARKERS: Final = re.compile(
    r"\b(?:the other|this one|that one|these two|those two|which one|what about|how about|"
    r"the former|the latter|you mentioned|"
    r"the (?:first|second|third|fourth|fifth|last)|"
    r"(?:first|second|third|fourth|fifth|last|other) one|"
    r"he|him|his|she|her|hers)\b",
    re.IGNORECASE,
)
#: An explicit topic switch: the previous turn is not what this message is about.
_TOPIC_SWITCH: Final = re.compile(
    r"换个话题|换一个话题|换话题|说点别的|聊点别的|问个别的|另外我想|另外问|另外想|"
    r"另一个问题|新问题|"
    r"\b(?:by the way|btw|different question|another question|change the subject|new topic|"
    r"unrelated)\b",
    re.IGNORECASE,
)
#: An identifier: a run of letters / digits (with . _ / -) at least four long that
#: mixes letters and digits (a product or document code), or a long digit run
#: (:data:`_ID_MIN_DIGITS`, an order or record number). A year, a dose
#: ("500mg") or a reading ("128/82") is not one: it asks about the subject before.
_IDENTIFIER: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{3,}")
_QUANTITY: Final = re.compile(r"[0-9]+(?:\.[0-9]+)?[A-Za-z]{1,3}")
_ID_MIN_DIGITS: Final = 6

#: Greetings, thanks, acknowledgements (normalised: lower-case, letters / digits
#: / CJK only). Platform text, Chinese and English. The KB probe skips them too.
SMALL_TALK: Final = frozenset(
    {
        "你好",
        "您好",
        "你好啊",
        "嗨",
        "哈喽",
        "在吗",
        "在不在",
        "谢谢",
        "谢谢你",
        "多谢",
        "感谢",
        "好的",
        "好",
        "嗯",
        "嗯嗯",
        "收到",
        "明白",
        "知道了",
        "继续",
        "继续吧",
        "ok",
        "okay",
        "再见",
        "拜拜",
        "早上好",
        "晚上好",
        "下午好",
        "你是谁",
        "你叫什么",
        "你能做什么",
        "hi",
        "hello",
        "hey",
        "thanks",
        "thankyou",
        "thx",
        "bye",
        "goodbye",
        "goodmorning",
        "whoareyou",
        "whatcanyoudo",
        "continue",
        "goon",
    }
)

#: Closers that only round off small talk ("ok, that's it then").
_SMALL_TALK_TAILS: Final = frozenset(
    {"那就这样吧", "那就这样", "就这样吧", "就这样", "吧", "啊", "呀"}
)
#: Longer than this (normalised) is never only small talk.
_SMALL_TALK_MAX_CHARS: Final = 24

Trigger = Literal["none", "short", "reference"]


@dataclass(frozen=True)
class RetrievalQuery:
    """What the user wrote this turn (:attr:`current`), why context was
    considered (:attr:`trigger`) and the context carried along (:attr:`context`,
    empty: none)."""

    current: str
    trigger: Trigger
    context: str = ""

    @property
    def augmented(self) -> bool:
        return bool(self.context)

    @property
    def context_chars(self) -> int:
        return len(self.context)

    @property
    def text(self) -> str:
        """Context, then the message: the augmented query (vector side)."""
        return f"{self.context}\n{self.current}" if self.context else self.current

    @property
    def keyword_text(self) -> str:
        """The message, then the context: keyword queries keep their first terms."""
        return f"{self.current}\n{self.context}" if self.context else self.current


def normalised(text: str) -> str:
    """Lower-case, letters / digits / CJK only (spaces and punctuation gone)."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).lower() if ch.isalnum())


def is_small_talk_text(text: str) -> bool:
    """Only greetings, thanks and acknowledgements -- one of :data:`SMALL_TALK`
    or several run together ("ok thanks", "fine, that's it then")."""
    flat = normalised(text)
    if not flat or len(flat) > _SMALL_TALK_MAX_CHARS:
        return False
    pieces = SMALL_TALK | _SMALL_TALK_TAILS
    # covered[i]: flat[:i] splits into pieces.
    covered = [True] + [False] * len(flat)
    for end in range(1, len(flat) + 1):
        covered[end] = any(covered[start] and flat[start:end] in pieces for start in range(end))
    return covered[-1]


def strip_reference_markers(text: str) -> str:
    """``text`` without its reference / ordinal markers: the words it names itself."""
    return _EN_MARKERS.sub(" ", _ZH_MARKERS.sub(" ", text))


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


def _has_marker(text: str) -> bool:
    return bool(_ZH_MARKERS.search(text) or _EN_MARKERS.search(text))


def _is_identifier(token: str) -> bool:
    digits = sum(ch.isdigit() for ch in token)
    letters = any(ch.isalpha() for ch in token)
    if letters:
        return digits > 0 and not _QUANTITY.fullmatch(token)
    return digits >= _ID_MIN_DIGITS


def _names_identifier(text: str) -> bool:
    return any(_is_identifier(token) for token in _IDENTIFIER.findall(text))


def trigger_of(text: str) -> Trigger:
    """Whether a message needs the conversation to be understood on its own."""
    if is_small_talk_text(text) or _TOPIC_SWITCH.search(text):
        return "none"
    marker = _has_marker(text)
    if not marker and _names_identifier(text):
        return "none"
    if len(text) <= FOLLOW_UP_MAX_CHARS:
        return "short"
    if len(text) <= REFERENCE_MAX_CHARS and marker:
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
        return RetrievalQuery(current=current, trigger=trigger)
    previous_at = users[-2] if len(users) > 1 else -1
    parts: list[str] = []
    if previous_at >= 0:
        asked = user_text(messages[previous_at])[-USER_CONTEXT_CHARS:]
        if asked:
            parts.append(asked)
    answer = _last_answer(messages, previous_at + 1, users[-1])
    if answer:
        parts.append(_excerpt(answer, ASSISTANT_HEAD_CHARS, ASSISTANT_TAIL_CHARS))
    return RetrievalQuery(current=current, trigger=trigger, context="\n".join(parts))


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
