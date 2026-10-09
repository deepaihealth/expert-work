"""Long-term memory repository — Stream J.3.

Cross-session memory for the per-user persistent agent: ``fact`` /
``episodic`` rows with embeddings, retrieved by cosine similarity.
See ``docs/streams/STREAM-J-DESIGN.md`` § 8.
"""

from expert_work.persistence.memory.base import MemoryStore as MemoryStore
from expert_work.persistence.memory.dlq import (
    DLQRow as DLQRow,
)
from expert_work.persistence.memory.dlq import (
    InMemoryMemoryWritebackDLQ as InMemoryMemoryWritebackDLQ,
)
from expert_work.persistence.memory.dlq import (
    MemoryWritebackDLQ as MemoryWritebackDLQ,
)
from expert_work.persistence.memory.dlq import (
    SqlMemoryWritebackDLQ as SqlMemoryWritebackDLQ,
)
from expert_work.persistence.memory.hash import (
    hash_content as hash_content,
)
from expert_work.persistence.memory.hash import (
    normalise_content as normalise_content,
)
from expert_work.persistence.memory.memory import (
    InMemoryMemoryStore as InMemoryMemoryStore,
)
from expert_work.persistence.memory.sql import SqlMemoryStore as SqlMemoryStore
from expert_work.persistence.memory.writeback_job import (
    InMemoryMemoryWritebackJobStore as InMemoryMemoryWritebackJobStore,
)
from expert_work.persistence.memory.writeback_job import (
    MemoryWritebackJob as MemoryWritebackJob,
)
from expert_work.persistence.memory.writeback_job import (
    MemoryWritebackJobStore as MemoryWritebackJobStore,
)
from expert_work.persistence.memory.writeback_job import (
    SqlMemoryWritebackJobStore as SqlMemoryWritebackJobStore,
)

__all__ = [
    "DLQRow",
    "InMemoryMemoryStore",
    "InMemoryMemoryWritebackDLQ",
    "InMemoryMemoryWritebackJobStore",
    "MemoryStore",
    "MemoryWritebackDLQ",
    "MemoryWritebackJob",
    "MemoryWritebackJobStore",
    "SqlMemoryStore",
    "SqlMemoryWritebackDLQ",
    "SqlMemoryWritebackJobStore",
    "hash_content",
    "normalise_content",
]
