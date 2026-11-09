"""B-206 11-09 (C) — multi-turn references in arguments that leave the conversation.

A knowledge base, a search engine, an HTTP API or an MCP server sees only the
arguments the model passes, never the conversation. A follow-up such as "and
its price?" resolved in the model's head but sent as "its price" retrieves the
wrong thing or nothing; resolved to the wrong referent it returns a confident
answer about the wrong one, which no grounding check catches. The platform has
no separate rewrite step (the model writing the query from the whole history is
the industry default), so the fix is a rule on the arguments themselves:

* :data:`OUTGOING_ARGUMENTS_CLAUSE` — in the system prompt of a build that has
  such a tool (:func:`sends_arguments_out`; an agent without one never reads it).
  Generic wording: any referent (a person, an organisation, a thing, a file, an
  item of an earlier answer, a time), no tenant or domain examples, and it does
  not rely on an integrator passing the "current object".
* ``query`` of ``knowledge_search`` / ``web_search`` says the same in one line.
* :func:`note_outgoing_query` — a counter of outgoing queries that still carry a
  reference marker, labelled by tool name only (never content). A heuristic
  signal for dashboards, not a behaviour change: the call runs as sent. Only the
  two query tools are counted — HTTP / MCP arguments are structured fields or
  message bodies where a pronoun is often legitimate, and their tool names are
  unbounded.

The markers are a list of their own (:func:`query_has_reference`), not the
user-message list of :mod:`~orchestrator.graph_builder.retrieval_query`: that
one is tuned to decide whether a short follow-up carries the last turn along,
and a search query is a different text — keyword strings, terms of art. Its bare
他 / 她 / 它 hit "阿托伐他汀" (a drug name) and "吉他"; its "该" would hit "该吃"
(should eat); "第三章" and "提到" are ordinary document words. Here a pronoun
counts only as a word (他们, 它的, or standing alone), "该" only before a noun,
and English pronouns only in lower case (or capitalised at the start), so "IT"
is not "it".
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Final

from expert_work.common.observability.metrics import expert_work_counter
from orchestrator.tools.mcp import MCP_TOOL_NAME_PREFIX

#: Builtin tools whose arguments leave the conversation; any ``mcp__`` tool too.
OUTGOING_TOOL_NAMES: Final = frozenset({"knowledge_search", "web_search", "http"})
#: Tools whose ``query`` the counter reads (bounded label set).
QUERY_TOOL_NAMES: Final = frozenset({"knowledge_search", "web_search"})

OUTGOING_ARGUMENTS_CLAUSE: Final = (
    "# Tool arguments\n"
    "Searches and calls to outside services receive only the arguments you pass, not this "
    "conversation. Make each query or argument you send self-contained: write out the name, "
    'identifier, date or title that a pronoun, a word like "this" or "the other one", a '
    'pointer such as "the second item above", or an omitted subject stands for in the '
    "conversation. When the thing referred to decides what you search for or send, say in "
    "your answer what you took it to mean; if the conversation leaves two or more things of "
    "the same kind it could mean, ask which one, or answer for each by name."
)

#: Where a standalone Chinese word starts / ends in a keyword string.
_EDGE: Final = r"\s,.!?;:\u3001\u3002\uff01\uff1f\uff0c\uff1b\uff1a"
_MEASURE: Final = "个些两几款种位份项家本篇张"
#: Chinese markers, as words: 他们 / 它的 (not inside 其他 / 其它 / 吉他), a
#: pronoun standing alone, 这 / 那 + a measure word (not 这个月 / 这个星期), a
#: pointer into what was said, the N-th one, and 该 + a noun (not 应该, and not
#: 该 + a verb or 如何 / 怎么: "should").
_ZH_QUERY_MARKERS: Final = re.compile(
    r"(?<![其吉])[他她它](?:们|的)"
    rf"|(?:^|(?<=[{_EDGE}]))[他她它](?=$|[{_EDGE}])"
    rf"|[这那](?:个(?!月|星期|礼拜)|[{_MEASURE[1:]}])"
    r"|上述|前者|后者|刚才|你说的|您说的|另一[个款种位家]|上面(?:的|那|这|第|说|提)"
    rf"|第[一二三四五六七八九十0-9]+[个款种位家项]"
    r"|(?<!应)该(?=[\u4e00-\u9fff])(?![怎如吃用不去做是有可能会要选买喝看找])"
)
#: English pronouns: lower case anywhere, capitalised only at the start ("IT").
_EN_QUERY_PRONOUNS: Final = re.compile(
    r"\b(?:it|its|they|them|their|he|him|his|she|her)\b"
    r"|^(?:It|Its|They|Them|Their|He|His|She|Her)\b"
)
_EN_QUERY_PHRASES: Final = re.compile(
    r"\b(?:the other one|this one|that one|which one|these two|those two|what about|how about|"
    r"the former|the latter|you mentioned)\b",
    re.IGNORECASE,
)

_outgoing_query_reference_total = expert_work_counter(
    "expert_work_outgoing_query_reference_total",
    "Outgoing search queries that still hold a pronoun / reference marker (heuristic; B-206).",
    ("tool",),
)


def sends_arguments_out(tool_names: Iterable[str]) -> bool:
    """Some tool of the build sends a query or arguments out of the conversation."""
    return any(
        name in OUTGOING_TOOL_NAMES or name.startswith(MCP_TOOL_NAME_PREFIX) for name in tool_names
    )


def query_has_reference(query: str) -> bool:
    """``query`` still holds a pronoun / reference marker (a heuristic)."""
    return any(
        pattern.search(query)
        for pattern in (_ZH_QUERY_MARKERS, _EN_QUERY_PRONOUNS, _EN_QUERY_PHRASES)
    )


def note_outgoing_query(tool_name: str, args: Mapping[str, Any]) -> None:
    """Count a :data:`QUERY_TOOL_NAMES` call whose ``query`` holds a reference marker."""
    if tool_name not in QUERY_TOOL_NAMES:
        return
    query = args.get("query")
    if isinstance(query, str) and query_has_reference(query):
        _outgoing_query_reference_total.labels(tool=tool_name).inc()
