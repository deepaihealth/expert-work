"""Agent-artifact registry — Stream J.9.

Records the named files an agent explicitly produces (``save_artifact``)
with versioning. Content lives in the J.15 persistent workspace volume;
the store holds only metadata. See ``docs/streams/STREAM-J-DESIGN.md``
§ 10.
"""

from expert_work.persistence.artifact.base import ArtifactStore as ArtifactStore
from expert_work.persistence.artifact.base import (
    ExpiredArtifactVersion as ExpiredArtifactVersion,
)
from expert_work.persistence.artifact.memory import (
    InMemoryArtifactStore as InMemoryArtifactStore,
)
from expert_work.persistence.artifact.sql import SqlArtifactStore as SqlArtifactStore

__all__ = ["ArtifactStore", "ExpiredArtifactVersion", "InMemoryArtifactStore", "SqlArtifactStore"]
