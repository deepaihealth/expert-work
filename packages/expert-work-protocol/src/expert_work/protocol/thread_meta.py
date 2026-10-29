"""thread_meta row shape — vendor-aligned (see 06-OPEN-SOURCE-DEPS §P0)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ThreadStatus(StrEnum):
    """LangGraph thread lifecycle."""

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: Soft-deleted from the session-history list (reversible). Hidden from
    #: the default listing; the checkpoint/runs/artifacts are untouched.
    ARCHIVED = "archived"


class ThreadMeta(BaseModel):
    """One row of ``thread_meta`` — keyed by LangGraph thread_id; multiple
    runs / sessions can share the same thread.

    DeerFlow vendor pattern, extended with ``tenant_id`` per ADR-0002.
    """

    model_config = ConfigDict(frozen=True)

    thread_id: UUID
    tenant_id: UUID
    user_id: UUID | None = Field(
        default=None,
        description="owning tenant_user.id (Stream J.14); None for pre-J.14 threads",
    )
    created_by: str = Field(description="actor_id of session creator (user / sa)")
    status: ThreadStatus = ThreadStatus.ACTIVE
    title: str | None = Field(
        default=None,
        description="human label — auto-set from the first user message, "
        "manually overridable (session-history list)",
    )
    agent_name: str | None = Field(default=None, description="manifest name")
    agent_version: str | None = Field(default=None, description="manifest version (semver)")
    message_count: int | None = Field(
        default=None, description="第三方可见口径的消息条数;None=尚未算过"
    )
    workspace_layout: Literal["conversation", "agent_root"] = Field(
        default="conversation",
        description="B-236 沙箱 /workspace 的视图:conversation = 本对话自己的目录;"
        "agent_root = 发布前已开的对话,沿用 agent 根",
    )
    created_at: datetime | None = None
    updated_at: datetime | None = None
