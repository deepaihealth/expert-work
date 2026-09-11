"""Agent artifacts — Stream J.9.

An :class:`Artifact` is a *named file* an agent explicitly produced — a
document, code, or data file. Its content lives in the user's J.15
persistent workspace volume; these models carry only the metadata
(name, kind, version, volume-relative path). Scoped to
``(tenant_id, user_id)``; re-saving a name appends a new
:class:`ArtifactVersion`. See ``docs/streams/STREAM-J-DESIGN.md`` § 10.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

#: An artifact's content category — declared by the agent at save time.
ArtifactKind = Literal["document", "code", "data", "other"]


class ArtifactVersion(BaseModel):
    """One row of ``artifact_version`` — a single saved revision."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    artifact_id: UUID
    tenant_id: UUID
    user_id: UUID
    version: int = Field(ge=1, description="1-based revision number")
    path_in_workspace: str = Field(
        description="path of the file inside the user's persistent workspace volume"
    )
    size_bytes: int | None = Field(
        default=None, ge=0, description="filled lazily on first content read"
    )
    sha256: str | None = Field(default=None, description="filled lazily on first content read")
    created_in_thread: str = Field(description="the thread/run that produced this revision")
    created_at: datetime | None = None


class Artifact(BaseModel):
    """One row of ``artifact`` — a logical named file with versions."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    user_id: UUID
    #: B-50 工作区分层 —— 这条产物属于哪个 agent(``sanitize_agent_key(spec.metadata.name)``,
    #: 与沙箱的 ``/opt/skills/<agent_key>`` 同一个值)。空串 = 归属不明的历史产物:
    #: 迁移 ``0154`` 回填时反推链断了的那批,以及没绑 agent 的调用方存的。
    agent_key: str = ""
    name: str = Field(description="logical name, unique per (tenant, user, agent_key)")
    kind: ArtifactKind
    latest_version: int = Field(ge=1, description="version number of the newest revision")
    created_at: datetime | None = None
    updated_at: datetime | None = None
    #: Soft-delete timestamp (Mini-ADR J-25). NULL = active.
    deleted_at: datetime | None = None
    #: ObjectStore key when the workspace files have been archived;
    #: NULL while the row is active or pending archive.
    archived_object_key: str | None = None
