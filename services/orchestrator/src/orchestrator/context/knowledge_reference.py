"""B-206 K-2 — reference lines for ``knowledge_read`` results.

``knowledge_read`` results are exempt from externalization
(:data:`~orchestrator.tools.overflow.EXEMPT_TOOLS`): the document is re-readable,
so a page the model asked for reaches it whole and nothing is written to the
workspace. That also leaves the pruners no workspace copy to point at, so an old
read collapses to this one-line reference instead — what was read and that the
same arguments read it again (the version in use then; a re-ingest may have
changed it). Same shape and rules as :mod:`orchestrator.context.skill_reference`:
only a successful read gets one, model-written arguments are ``repr``-escaped
and capped, and there is no call syntax (the L2 summariser drops it).

The line is built from the ``ToolResult.meta`` the dispatch layer surfaced in
``ToolMessage.artifact`` (``args`` and the page position): platform-built, with
only the model's own arguments echoed — never document text.

Consumed by :mod:`orchestrator.context.tool_result_prune`: the in-turn collapse
(CM-12) and the cross-turn cleanup (B-126).
"""

from __future__ import annotations

import re

from langchain_core.messages import BaseMessage, ToolMessage

KNOWLEDGE_READ_TOOL_NAME = "knowledge_read"
#: Bound on a model-written argument echoed into the reference.
_ARGUMENT_CHAR_CAP = 80
_DOCUMENT_REF = re.compile(r"D[0-9]+(?:#[0-9]+)?")


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
    parts = [document if _DOCUMENT_REF.fullmatch(document) else _quoted(document)]
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


def _quoted(text: str) -> str:
    return repr(text[:_ARGUMENT_CHAR_CAP])


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
