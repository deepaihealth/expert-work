"""B-206 K-2 — reference lines for ``knowledge_read`` and ``knowledge_search`` results.

``knowledge_read`` results are exempt from externalization
(:data:`~orchestrator.tools.overflow.EXEMPT_TOOLS`): the document is re-readable,
so a page the model asked for reaches it whole and nothing is written to the
workspace. That also leaves the pruners no workspace copy to point at, so an old
read collapses to this one-line reference instead — what was read and that the
same arguments read it again (the version in use then; a re-ingest may have
changed it). Same shape and rules as :mod:`orchestrator.context.skill_reference`:
only a successful read gets one, model-written arguments are ``repr``-escaped
and capped at what the tool accepts (so they are whole: "the same arguments"),
and there is no call syntax (the L2 summariser drops it).

The line is built from the ``ToolResult.meta`` the dispatch layer surfaced in
``ToolMessage.artifact`` (``args`` and the page position): platform-built, with
only the model's own section / find echoed — never document text. A document
without a number is "this document": its reference would be its file name, and
the line sits outside the spotlight fence.

``knowledge_search`` (B-206 track D) is exempt too: an old search collapses to
what was searched and the references of the passages it showed — each re-readable
with ``knowledge_read``, and the same arguments run the search again. Only the
query (the model's own words) is echoed; bases are named by handle (``K2``) and
passages by reference (``D12#7``), a hit without a document number is only
counted, so no base name, file name or document text leaves the fence.

Consumed by :mod:`orchestrator.context.tool_result_prune`: the in-turn collapse
(CM-12) and the cross-turn cleanup (B-126).
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import BaseMessage, ToolMessage

from orchestrator.tools.knowledge_read_reader import MAX_ARGUMENT_CHARS

KNOWLEDGE_READ_TOOL_NAME = "knowledge_read"
KNOWLEDGE_SEARCH_TOOL_NAME = "knowledge_search"
_DOCUMENT_REF = re.compile(r"D[0-9]+(?:#[0-9]+)?")
_PASSAGE_REF = re.compile(r"D[0-9]+#[0-9]+")
_BASE_HANDLE = re.compile(r"K[1-9][0-9]*")
#: A search that ran: found passages, or found none.
_SEARCH_STATUSES = frozenset({"ok", "no_hits", "below_threshold"})
#: Passage references listed in a search's line; the rest are counted.
MAX_LISTED_PASSAGES = 10


def knowledge_read_reference(message: BaseMessage) -> str | None:
    """One-line reference replacing a ``knowledge_read`` result's content, or
    ``None`` when ``message`` is not a successful read with recorded arguments."""
    if not isinstance(message, ToolMessage) or message.name != KNOWLEDGE_READ_TOOL_NAME:
        return None
    if getattr(message, "status", "success") == "error":
        return None
    artifact = message.artifact
    if not isinstance(artifact, dict) or artifact.get("status") != "ok":
        return None
    args = artifact.get("args")
    if not isinstance(args, dict) or not isinstance(args.get("document"), str):
        return None
    document = args["document"]
    parts = [document if _DOCUMENT_REF.fullmatch(document) else "this document"]
    for key in ("section", "find"):
        if isinstance(args.get(key), str):
            parts.append(f"{key} {_quoted(args[key])}")
    if _is_int(args.get("context")):
        parts.append(f"context {args['context']}")
    if _is_int(artifact.get("page")) and _is_int(artifact.get("pages")):
        parts.append(f"page {artifact['page']} of {artifact['pages']}")
    if _is_int(args.get("max_tokens")):
        parts.append(f"max_tokens {args['max_tokens']}")
    return (
        f"[knowledge_read of {', '.join(parts)} — content compressed; re-readable via the "
        "knowledge_read tool with the same arguments]"
    )


def knowledge_search_reference(message: BaseMessage) -> str | None:
    """One-line reference replacing a ``knowledge_search`` result's content, or
    ``None`` when ``message`` is not a search that ran with recorded arguments
    (a result checkpointed before they were recorded keeps the generic path)."""
    if not isinstance(message, ToolMessage) or message.name != KNOWLEDGE_SEARCH_TOOL_NAME:
        return None
    if getattr(message, "status", "success") == "error":
        return None
    artifact = message.artifact
    if not isinstance(artifact, dict) or artifact.get("status") not in _SEARCH_STATUSES:
        return None
    args = artifact.get("args")
    if not isinstance(args, dict) or not isinstance(args.get("query"), str):
        return None
    found = _found_passages(artifact)
    if found is None:
        return None
    query = args["query"]
    whole = len(query) <= MAX_ARGUMENT_CHARS
    searched = f"knowledge_search for {_quoted(query)}{'' if whole else '…'}"
    bases = args.get("bases")
    if isinstance(bases, list):
        handles = [b for b in bases if isinstance(b, str) and _BASE_HANDLE.fullmatch(b)]
        if handles:
            searched += f" in bases {', '.join(handles)}"
    if _is_int(args.get("limit")):
        searched += f", limit {args['limit']}"
    if _count(artifact.get("unsearched")):
        found += "; some knowledge bases could not be searched"
    rerun = "the search can be run again via the knowledge_search tool"
    if whole:
        rerun += " with the same arguments"
    if found.startswith("passages "):
        rerun = (
            "each passage is re-readable via the knowledge_read tool with its reference, and "
            + rerun
        )
    return f"[{searched}: {found} — content compressed; {rerun}]"


def _found_passages(artifact: dict[str, Any]) -> str | None:
    """What a search showed: its passages' references (the first
    :data:`MAX_LISTED_PASSAGES`, the rest counted), how many had no reference,
    or that it found none; ``None`` for a search with hits but no recorded list."""
    if artifact.get("status") != "ok":
        return "no passage found"
    refs = artifact.get("refs")
    if not isinstance(refs, list):
        return None
    listed = [r for r in refs if isinstance(r, str) and _PASSAGE_REF.fullmatch(r)]
    unnumbered = _count(artifact.get("unnumbered"))
    if not listed:
        if not unnumbered:
            return "no passage found"
        return f"{unnumbered} passage{'s' if unnumbered != 1 else ''} without a reference"
    found = f"passages {', '.join(listed[:MAX_LISTED_PASSAGES])}"
    if len(listed) > MAX_LISTED_PASSAGES:
        found += f" and {len(listed) - MAX_LISTED_PASSAGES} more"
    if unnumbered:
        found += f" and {unnumbered} without a reference"
    return found


def _quoted(text: str) -> str:
    return repr(text[:MAX_ARGUMENT_CHARS])


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _count(value: object) -> int:
    """``value`` when it is a positive count, else ``0``."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
