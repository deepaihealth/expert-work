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
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Final

from expert_work.common.observability.metrics import expert_work_counter
from orchestrator.graph_builder.retrieval_query import has_reference_marker
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
    "conversation. When the user's message refers to something without naming it, say in "
    "your answer what you took it to mean. If the conversation leaves two or more things of "
    "the same kind it could mean, ask which one, or answer for each by name."
)

#: Markers a user message may carry harmlessly (``retrieval_query`` leaves the
#: generic English pronouns out) but a standalone query should not: "it",
#: "they"; and Chinese "该" (the said ...) except inside "应该" (should).
_QUERY_MARKERS: Final = re.compile(r"\b(?:it|its|they|them|their)\b|(?<!应)该", re.IGNORECASE)

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
    return has_reference_marker(query) or bool(_QUERY_MARKERS.search(query))


def note_outgoing_query(tool_name: str, args: Mapping[str, Any]) -> None:
    """Count a :data:`QUERY_TOOL_NAMES` call whose ``query`` holds a reference marker."""
    if tool_name not in QUERY_TOOL_NAMES:
        return
    query = args.get("query")
    if isinstance(query, str) and query_has_reference(query):
        _outgoing_query_reference_total.labels(tool=tool_name).inc()
