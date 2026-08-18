"""event_log row shape — see ADR-0002 §event_log."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EventType(StrEnum):
    """Categorical type of a single event_log row.

    Stream A scope: the engine-emitted types. Vendor adaptation (Stream A.2)
    may extend with DeerFlow's internal types (e.g. ``stream_token``).
    """

    SESSION_START = "session_start"
    SESSION_END = "session_end"
    LLM_CALL = "llm_call"
    LLM_RESULT = "llm_result"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    STATE = "state"
    ERROR = "error"
    CHECKPOINT = "checkpoint"
    #: Stream RT-2 PR-4 — context compaction fired during a run. Emitted as a
    #: free-string ``"compaction"`` SSE frame + durable RunEventStore mirror
    #: (see ``orchestrator.sse.run_agent``); this taxonomy member keeps the
    #: canonical event-type name aligned with that wire value.
    COMPACTION = "compaction"
    #: Debug-console redesign PR1 (spec 2026-08-17 D6) — whole-snapshot plan
    #: (task list). Same shape as COMPACTION: free-string "plan" SSE frame +
    #: RunEventStore mirror; this member only pins the canonical event name
    #: (orchestrator.sse.run_agent references it).
    PLAN = "plan"


class EventRecord(BaseModel):
    """One row of ``event_log`` (post-redactor).

    The payload is opaque JSON; consumers should treat it as already PII-redacted
    by the orchestrator middleware chain (see [subsystems/20-observability §8] and
    Stream D.2 PII redactor).
    """

    model_config = ConfigDict(frozen=True)

    id: int | None = Field(default=None, description="DB autoincrement; None pre-insert")
    thread_id: UUID
    session_id: UUID | None = Field(default=None, description="Null for thread-level events")
    tenant_id: UUID
    seq: int = Field(ge=0, description="Strictly monotonic per (thread_id)")
    event_type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = Field(default=None, description="W3C trace_id propagated via OTel")
    created_at: datetime | None = Field(default=None, description="DB default now() at insert")
