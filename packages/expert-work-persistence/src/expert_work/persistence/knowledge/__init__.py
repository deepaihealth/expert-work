"""Knowledge base / RAG repository — Stream J.5.

Tenant-scoped knowledge bases of uploaded, chunked, embedded documents,
retrieved by cosine similarity. See ``docs/streams/STREAM-J-DESIGN.md`` § 12.
"""

from expert_work.persistence.knowledge.base import (
    ALL_TENANTS_BASES_LIMIT as ALL_TENANTS_BASES_LIMIT,
)
from expert_work.persistence.knowledge.base import UNSET as UNSET
from expert_work.persistence.knowledge.base import (
    DuplicateKnowledgeBaseError as DuplicateKnowledgeBaseError,
)
from expert_work.persistence.knowledge.base import (
    KnowledgeRecallError as KnowledgeRecallError,
)
from expert_work.persistence.knowledge.base import KnowledgeStore as KnowledgeStore
from expert_work.persistence.knowledge.base import ParseCacheEntry as ParseCacheEntry
from expert_work.persistence.knowledge.base import ParseCacheKind as ParseCacheKind
from expert_work.persistence.knowledge.memory import (
    InMemoryKnowledgeStore as InMemoryKnowledgeStore,
)
from expert_work.persistence.knowledge.retrieval_log import (
    RETRIEVAL_LOG_ERROR as RETRIEVAL_LOG_ERROR,
)
from expert_work.persistence.knowledge.retrieval_log import (
    RETRIEVAL_LOG_QUERY_MAX_CHARS as RETRIEVAL_LOG_QUERY_MAX_CHARS,
)
from expert_work.persistence.knowledge.retrieval_log import (
    RETRIEVAL_LOG_SOURCES as RETRIEVAL_LOG_SOURCES,
)
from expert_work.persistence.knowledge.retrieval_log import (
    InMemoryKnowledgeRetrievalLogStore as InMemoryKnowledgeRetrievalLogStore,
)
from expert_work.persistence.knowledge.retrieval_log import (
    KnowledgeRetrievalLogStore as KnowledgeRetrievalLogStore,
)
from expert_work.persistence.knowledge.retrieval_log import (
    RetrievalLogEntry as RetrievalLogEntry,
)
from expert_work.persistence.knowledge.retrieval_log import (
    RetrievalLogSource as RetrievalLogSource,
)
from expert_work.persistence.knowledge.retrieval_log import (
    SqlKnowledgeRetrievalLogStore as SqlKnowledgeRetrievalLogStore,
)
from expert_work.persistence.knowledge.sql import SqlKnowledgeStore as SqlKnowledgeStore

__all__ = [
    "ALL_TENANTS_BASES_LIMIT",
    "RETRIEVAL_LOG_ERROR",
    "RETRIEVAL_LOG_QUERY_MAX_CHARS",
    "RETRIEVAL_LOG_SOURCES",
    "UNSET",
    "DuplicateKnowledgeBaseError",
    "InMemoryKnowledgeRetrievalLogStore",
    "InMemoryKnowledgeStore",
    "KnowledgeRecallError",
    "KnowledgeRetrievalLogStore",
    "KnowledgeStore",
    "ParseCacheEntry",
    "ParseCacheKind",
    "RetrievalLogEntry",
    "RetrievalLogSource",
    "SqlKnowledgeRetrievalLogStore",
    "SqlKnowledgeStore",
]
