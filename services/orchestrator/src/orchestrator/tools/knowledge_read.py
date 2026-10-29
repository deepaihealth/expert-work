"""The ``knowledge_read`` tool — read a document of the agent's knowledge bases (B-206 K-2).

``knowledge_search`` returns the closest passages; the answer is often in the
passage next to one, or a find-all question needs every candidate document
checked. ``knowledge_read`` lets the model decide how much to read:

* ``D12`` — the document a page at a time, in reading order;
* ``D12#7`` — the passage holding chunk 7, with ``context`` passages each side;
* ``section`` — the passages under a heading;
* ``find`` — the passages that contain every word given, one passage each side.

``document`` is a ``knowledge_search`` reference, the document's number, or its
file name / title. A page is filled to 80% of ``max_tokens`` (counted with the
platform's token estimator); the document's details and the marks take the
rest. Only documents of the agent's own bases are readable, and only what recall
could return: switched-off passages and documents are not. While a document is
ingested again, the version in use is read.

This module resolves the call — its arguments, the document, its readable
passages — and :mod:`orchestrator.tools.knowledge_read_reader` lays out what was
asked for. Tenant text (file names, headings, bodies, base names) goes in the
content, which the tools node fences; the notice and errors reach the model
unfenced and carry only platform text, numbers and ``D`` references. ``meta``
records what was read (the trajectory panel's "pages read", and the arguments
that read it again, :mod:`orchestrator.context.knowledge_reference`).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from expert_work.persistence import KnowledgeStore
from expert_work.persistence.knowledge.passages import Passage
from expert_work.protocol import (
    ChunkType,
    DocumentStatus,
    KnowledgeBase,
    KnowledgeDocument,
    chunk_retrieval_text,
)
from expert_work.runtime.tokens import TokenEstimator, default_estimator
from orchestrator.tools.error_classifier import GuidedToolError
from orchestrator.tools.knowledge_read_reader import (
    MAX_ARGUMENT_CHARS,
    MAX_MAX_TOKENS,
    DocumentReader,
    OpenDocument,
    PassageTokens,
)
from orchestrator.tools.knowledge_read_view import ReadPassage
from orchestrator.tools.registry import ToolBlockedError, ToolContext, ToolResult, ToolSpec

logger = logging.getLogger(__name__)

TOOL_NAME = "knowledge_read"
DEFAULT_MAX_TOKENS = 4_000
MIN_MAX_TOKENS = 4_000
DEFAULT_CONTEXT = 1
MAX_CONTEXT = 5
#: Documents listed when a name matches several, or only partly.
MAX_CANDIDATES = 10

_DESCRIPTION = (
    "Read a document of the knowledge bases knowledge_search searches: the passages around "
    "a search result, a page of the document, the passages under a heading, or the passages "
    "that contain given words. Use it when a search result is cut short or may continue in the "
    "passages around it, to check what comes before or after a result, and to check each candidate "
    "document against every condition of a question that asks for all / every / how many. "
    "document is a reference from a knowledge_search result: D12#7 reads the passage holding "
    "chunk 7 with context passages on each side, D12 reads the document page by page; a file "
    "name or title also works. page pages through the document, the section or the find "
    "results in reading order, max_tokens per page. section reads the passages under a "
    "heading (one heading, or a path like A > B). find returns the passages that contain "
    "every word given, in any order, ignoring case, with one passage on each side. A result "
    "starts with the document's details. Passages already returned in full in this task do "
    "not need to be read again, unless they now show only as a compressed reference."
)
_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "document": {
            "type": "string",
            "description": (
                "D12 or D12#7 from a knowledge_search result, or a document's file name or title."
            ),
        },
        "page": {
            "type": "integer",
            "minimum": 1,
            "description": "Page to read, from 1 (default 1).",
        },
        "section": {
            "type": "string",
            "description": "Read the passages under this heading (A > B for a heading path).",
        },
        "find": {
            "type": "string",
            "description": "Words that must all appear in a passage, in any order.",
        },
        "context": {
            "type": "integer",
            "minimum": 0,
            "maximum": MAX_CONTEXT,
            "description": f"Passages on each side of D12#7 (default {DEFAULT_CONTEXT}).",
        },
        "max_tokens": {
            "type": "integer",
            "minimum": MIN_MAX_TOKENS,
            "maximum": MAX_MAX_TOKENS,
            "description": f"Size of a page in tokens (default {DEFAULT_MAX_TOKENS}).",
        },
    },
    "required": ["document"],
}

_NOT_FOUND_ADVICE = (
    "Use a reference from this agent's knowledge_search results (D12 or D12#7), or search "
    "first; only documents in this agent's knowledge bases can be read."
)
_NO_BASES_ADVICE = (
    "Nothing was read; the same call fails the same way until the agent's knowledge base "
    "setting is fixed."
)
_UNREADABLE_ADVICE = "Search again for other documents."
_CHOOSE_NOTICE = (
    "[knowledge_read: nothing was read; pass one of the listed references as document.]"
)

_BRACKETED = re.compile(r"\[([^\[\]]+)\]")
#: A number, optionally ``D``-prefixed, with an optional ``#`` chunk part. The
#: tool's own labels list a passage's chunks as runs (``D12#7-9,41``): the first
#: number is the reference, the rest is read past.
_NUMBERED = re.compile(r"([Dd])?\s*([0-9]{1,18})\s*(?:#\s*(?:([0-9]{1,9})[0-9,\-\s]*)?)?")
#: The ``· match`` mark a label carries in find / around results.
_MATCH_MARK = re.compile(r"\s*·\s*match\s*$")
_CHUNK_SUFFIX = re.compile(r"(.+)#([0-9]{1,9})")
_NO_TEXT_YET = {
    DocumentStatus.PENDING: "has no readable text yet: its first ingest has not finished",
    DocumentStatus.PROCESSING: "has no readable text yet: its first ingest has not finished",
    DocumentStatus.FAILED: "has no readable text: its ingest failed",
    DocumentStatus.CANCELLED: "has no readable text: its ingest was cancelled",
}


@dataclass(frozen=True)
class _Target:
    """What ``document`` names: a number, or else a name (``alternative``: the
    name without a trailing ``#<chunk>``, tried when the whole name matches
    nothing); and a chunk number, which makes it an around read. A number
    written without ``D`` (``2024``) that finds nothing is tried as ``name``."""

    doc_seq: int | None = None
    name: str = ""
    chunk: int | None = None
    alternative: tuple[str, int] | None = None


@dataclass(frozen=True)
class KnowledgeReadTool:
    """The ``knowledge_read`` tool over ``knowledge_base_refs`` — registered with
    ``knowledge_search`` for a manifest's ``knowledge:`` block, never without it.
    ``estimator`` counts page sizes (the platform's ``o200k_base`` estimator),
    through ``tokens``, which keeps this tool's passage counts out of the
    estimator's shared cache."""

    store: KnowledgeStore
    knowledge_base_refs: tuple[str, ...]
    estimator: TokenEstimator = field(default_factory=default_estimator)
    tokens: PassageTokens = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tokens", PassageTokens(self.estimator))

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=TOOL_NAME,
            description=_DESCRIPTION,
            parameters=_PARAMETERS,
            is_read_only=True,
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        if ctx.tenant_id is None:
            msg = "knowledge_read requires a tenant binding"
            raise ToolBlockedError(msg)
        target = _parse_document(args.get("document"))
        section = _text_arg(args, "section")
        find = _text_arg(args, "find")
        if section is not None and find is not None:
            msg = "knowledge_read takes section or find, not both"
            raise ValueError(msg)
        page = _page_arg(args.get("page"))
        max_tokens = _clamp(
            args.get("max_tokens"), DEFAULT_MAX_TOKENS, MIN_MAX_TOKENS, MAX_MAX_TOKENS
        )
        bases = await self._bound_bases(ctx.tenant_id)
        found = await self._resolve(ctx.tenant_id, target, bases)
        if isinstance(found, ToolResult):
            return found
        doc, chunk = found
        passages = await self._passages(ctx.tenant_id, doc)
        reader = DocumentReader(self.tokens, doc, passages, max_tokens, len(bases) > 1)
        if find is not None:
            return await reader.find(find, page)
        if section is not None:
            return await reader.section(section, page)
        if chunk is not None:
            if page > 1:
                msg = (
                    "page reads a whole document, a section or find results; a reference with # "
                    "reads one passage and its neighbours — pass the document without # to read "
                    "it by page"
                )
                raise ValueError(msg)
            context = _clamp(args.get("context"), DEFAULT_CONTEXT, 0, MAX_CONTEXT)
            return await reader.around(chunk, context)
        return await reader.page(page)

    async def _bound_bases(self, tenant_id: UUID) -> dict[UUID, KnowledgeBase]:
        """The agent's bases that exist, by id."""
        by_name = {base.name: base for base in await self.store.list_bases(tenant_id=tenant_id)}
        bound = {
            by_name[ref].id: by_name[ref] for ref in self.knowledge_base_refs if ref in by_name
        }
        if not bound:
            msg = "none of this agent's knowledge bases exists, so nothing can be read"
            raise GuidedToolError(msg, advice=_NO_BASES_ADVICE)
        return bound

    async def _resolve(
        self, tenant_id: UUID, target: _Target, bases: Mapping[UUID, KnowledgeBase]
    ) -> tuple[OpenDocument, int | None] | ToolResult:
        """The document ``target`` names in ``bases`` (and its chunk), or a
        result listing candidates. Errors say the same for a document that does
        not exist and one outside the agent's bases."""
        if target.doc_seq is not None:
            row = await self.store.get_document_by_seq(tenant_id=tenant_id, doc_seq=target.doc_seq)
            if row is not None and row.kb_id in bases:
                return _opened(row, bases), target.chunk
            if not target.name:  # D-prefixed: a number, never a name
                msg = f"no document D{target.doc_seq} in this agent's knowledge bases"
                raise GuidedToolError(msg, advice=_NOT_FOUND_ADVICE)
        found = await self._named(tenant_id, target, bases)
        if found is not None:
            return found
        msg = "no document with that file name or title in this agent's knowledge bases"
        if target.doc_seq is not None:
            msg = (
                f"no document D{target.doc_seq} in this agent's knowledge bases, and none with "
                "that file name or title"
            )
        raise GuidedToolError(msg, advice=_NOT_FOUND_ADVICE)

    async def _named(
        self, tenant_id: UUID, target: _Target, bases: Mapping[UUID, KnowledgeBase]
    ) -> tuple[OpenDocument, int | None] | ToolResult | None:
        """The document whose file name or title is the name (or, failing that,
        the name without its ``#<chunk>``), the candidates when several are or
        none is but some contain it, else ``None``. Switched-off documents are
        never listed (the store leaves them out)."""
        tries = [(target.name, target.chunk)]
        if target.alternative is not None:
            tries.append(target.alternative)
        for name, chunk in tries:
            exact = await self._find(tenant_id, name, bases, exact=True)
            if len(exact) == 1:
                return _opened(exact[0], bases), chunk
            if exact:
                return _candidates("ambiguous", target.name, exact, bases)
        partial = await self._find(tenant_id, tries[-1][0], bases, exact=False)
        if partial:
            return _candidates("no_match_name", target.name, partial, bases)
        return None

    async def _find(
        self, tenant_id: UUID, name: str, bases: Mapping[UUID, KnowledgeBase], *, exact: bool
    ) -> list[KnowledgeDocument]:
        return await self.store.find_documents_by_name(
            tenant_id=tenant_id,
            kb_ids=list(bases),
            name=name,
            limit=MAX_CANDIDATES + 1,
            exact=exact,
        )

    async def _passages(self, tenant_id: UUID, doc: OpenDocument) -> list[ReadPassage]:
        """The document's readable passages. Reading them takes two statements;
        one that straddled a chunk swap (a re-ingest) returns a parent row
        without its chunks, and is read once more."""
        stored = await self.store.list_passages(
            tenant_id=tenant_id, document_id=doc.row.id, enabled_only=True
        )
        if any(p.row.chunk_type is ChunkType.PARENT and not p.children for p in stored):
            logger.info("knowledge.read_reread — a passage came back without its chunks")
            stored = await self.store.list_passages(
                tenant_id=tenant_id, document_id=doc.row.id, enabled_only=True
            )
        if not stored:
            reason = _NO_TEXT_YET.get(
                doc.row.status,
                "has no readable passages: its text is empty or every passage is switched off",
            )
            raise GuidedToolError(f"{doc.spoken} {reason}", advice=_UNREADABLE_ADVICE)
        return [_view(p) for p in stored]


def _parse_document(raw: object) -> _Target:
    """``document`` as a number (``D12``, ``12``, ``[D12#7]``, a JSON integer, a
    copied header ``[D12#7] name``, one of the tool's own labels ``[D12#7-9] ·
    match``) or a name (``name#7`` too)."""
    if isinstance(raw, int) and not isinstance(raw, bool):
        return _Target(doc_seq=raw, name=str(raw))
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_ARGUMENT_CHARS * 2:
        msg = "knowledge_read requires 'document': a reference like D12 or D12#7, or a file name"
        raise ValueError(msg)
    text = raw.strip()
    bare = _MATCH_MARK.sub("", text)
    if (bracketed := _BRACKETED.match(bare)) is not None:
        if (reference := _reference(bracketed.group(1).strip())) is not None:
            return reference
        if bracketed.end() == len(text):
            text = bracketed.group(1).strip()
    elif (reference := _reference(bare)) is not None:
        return reference
    return _Target(name=text, alternative=_without_chunk(text))


def _reference(text: str) -> _Target | None:
    """``text`` as a number reference, or ``None``. Written without ``D`` it
    keeps ``text`` as the name to try when the number finds nothing."""
    match = _NUMBERED.fullmatch(text)
    if match is None:
        return None
    prefixed, number, chunk = match.groups()
    return _Target(
        doc_seq=int(number),
        chunk=int(chunk) if chunk is not None else None,
        name="" if prefixed else text,
        alternative=None if prefixed else _without_chunk(text),
    )


def _without_chunk(text: str) -> tuple[str, int] | None:
    """``name#7`` as ``(name, 7)``."""
    suffixed = _CHUNK_SUFFIX.fullmatch(text)
    if suffixed is None or not suffixed.group(1).strip():
        return None
    return suffixed.group(1).strip(), int(suffixed.group(2))


def _text_arg(args: Mapping[str, Any], key: str) -> str | None:
    raw = args.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        msg = f"knowledge_read '{key}' must be a string"
        raise ValueError(msg)
    text = raw.strip()
    if len(text) > MAX_ARGUMENT_CHARS:
        msg = f"knowledge_read '{key}' takes at most {MAX_ARGUMENT_CHARS} characters"
        raise ValueError(msg)
    return text or None


def _page_arg(raw: object) -> int:
    if raw is None:
        return 1
    if isinstance(raw, str) and raw.strip().isascii() and raw.strip().isdigit():
        return int(raw.strip())
    if not isinstance(raw, int) or isinstance(raw, bool):
        msg = "knowledge_read 'page' must be an integer"
        raise ValueError(msg)
    return raw


def _clamp(raw: object, default: int, low: int, high: int) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool):
        return default
    return max(low, min(raw, high))


def _opened(row: KnowledgeDocument, bases: Mapping[UUID, KnowledgeBase]) -> OpenDocument:
    """``row`` to read — refused when it is switched off (recall leaves it out)."""
    ref = f"D{row.doc_seq}" if row.doc_seq is not None else row.filename
    doc = OpenDocument(row=row, ref=ref, base=bases[row.kb_id])
    if not row.enabled:
        msg = f"{doc.spoken} is switched off in its knowledge base and cannot be read"
        raise GuidedToolError(msg, advice=_UNREADABLE_ADVICE)
    return doc


def _candidates(
    status: str,
    name: str,
    docs: Sequence[KnowledgeDocument],
    bases: Mapping[UUID, KnowledgeBase],
) -> ToolResult:
    """Documents a name matched several of, or only partly: listed, none read."""
    lead = (
        f'[several documents are named "{name}" in this agent\'s knowledge bases; pass one of '
        "these references as document:]"
        if status == "ambiguous"
        else f'[no document is named "{name}" in this agent\'s knowledge bases; these file '
        "names contain it — pass one of their references as document:]"
    )
    lines = [lead]
    for doc in docs[:MAX_CANDIDATES]:
        uploaded = f" · uploaded {doc.created_at.date().isoformat()}" if doc.created_at else ""
        ref = f"D{doc.doc_seq}" if doc.doc_seq is not None else doc.filename
        lines.append(f"{ref} · {doc.filename} · {bases[doc.kb_id].name}{uploaded}")
    if len(docs) > MAX_CANDIDATES:
        lines.append("… and more")
    meta = {
        "status": status,
        "passages": [],
        "candidates": [
            {"ref": f"D{d.doc_seq}" if d.doc_seq is not None else None, "kb_id": str(d.kb_id)}
            for d in docs[:MAX_CANDIDATES]
        ],
    }
    return ToolResult(content="\n".join(lines), meta=meta, notice=_CHOOSE_NOTICE)


def _view(passage: Passage) -> ReadPassage:
    children = passage.children
    paths = tuple(child.heading_path for child in children) or (passage.row.heading_path,)
    return ReadPassage(
        id=passage.id,
        chunk_indexes=tuple(sorted(child.chunk_index for child in children)),
        text=chunk_retrieval_text(passage.row.heading_path, passage.row.content),
        heading_paths=paths,
    )
