"""B-215 —— topic-shift decisions kept per document between ingests.

The semantic chunker embeds every block of a document to find topic shifts.
The chunks' own vectors were already reused by content hash, but those block
embeddings were not: every re-ingest and 「补识别」 paid for them again — 10-23
test, a 2.5M-character PDF re-recognized with nothing new to read was billed
683,103 embedding tokens again (44% of its first ingest).

A decision only depends on the two blocks' texts, the embedding model and the
threshold (:class:`~control_plane.knowledge.chunking.BreakMemo`), so it is kept
in the document's recognition cache (``knowledge_parse_cache``, kind
``semantic_break``, one row per document and embedding model): per document on
purpose, like the recognition readings — deleting the document deletes it.
The cache is an optimisation: a failed read or write only means the blocks are
embedded again.
"""

from __future__ import annotations

import logging
from uuid import UUID

from control_plane.knowledge.chunking import BreakMemo
from expert_work.persistence import KnowledgeStore

logger = logging.getLogger(__name__)

#: Bumped when what a decision depends on changes in a way the key does not
#: cover (the key already covers the texts, the model and the threshold).
_FORMAT = "v1"


def _cache_key(model_key: str) -> str:
    return f"{_FORMAT}:{model_key}"


async def load_break_memo(
    store: KnowledgeStore, *, tenant_id: UUID, document_id: UUID, model_key: str | None
) -> BreakMemo | None:
    """The document's earlier decisions for this embedding model (none known
    yet: an empty memo). ``None`` — no memo at all — when the model is unknown."""
    if model_key is None:
        return None
    key = _cache_key(model_key)
    try:
        entries = await store.get_parse_cache(
            tenant_id=tenant_id, document_id=document_id, kind="semantic_break", keys=[key]
        )
    except Exception:
        logger.warning("knowledge.break_cache_read_failed document=%s", document_id, exc_info=True)
        entries = {}
    entry = entries.get(key)
    known: dict[str, bool] = {}
    if entry is not None:
        known.update(dict.fromkeys(entry.result.get("breaks", ()), True))
        known.update(dict.fromkeys(entry.result.get("joins", ()), False))
    return BreakMemo(namespace=model_key, known=known)


async def store_break_memo(
    store: KnowledgeStore,
    *,
    tenant_id: UUID,
    document_id: UUID,
    model_key: str | None,
    memo: BreakMemo | None,
) -> None:
    """Keep this ingest's decisions (only the pairs the text has now); nothing
    is written when they are the ones already kept."""
    if memo is None or model_key is None or memo.decided == memo.known:
        return
    result = {
        "breaks": sorted(key for key, shift in memo.decided.items() if shift),
        "joins": sorted(key for key, shift in memo.decided.items() if not shift),
    }
    try:
        await store.put_parse_cache(
            tenant_id=tenant_id,
            document_id=document_id,
            kind="semantic_break",
            key=_cache_key(model_key),
            model=model_key,
            result=result,
        )
    except Exception:
        logger.warning("knowledge.break_cache_write_failed document=%s", document_id, exc_info=True)
