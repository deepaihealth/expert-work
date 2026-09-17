"""Stream L.L7 — trajectory recording for J.13 eval gate.

Completed agent runs are serialised to an
:class:`~expert_work.runtime.storage.ObjectStore` as
ShareGPT-flavoured JSONL (one line, one record), keyed under

::

    {prefix}/{tenant_id}/{outcome}/{YYYY}/{MM}/{DD}/{thread_id}.jsonl

so the J.13 eval gate / future fine-tuning pipelines can ``list_prefix``
by outcome without an SQL join. Four outcomes — ``success`` /
``failed`` / ``max_steps`` / ``cancelled`` (Mini-ADR L-7) — let the
caller filter cleanly between good trajectories and the various
failure modes worth studying.

Hermes ``agent/trajectory.py:30-56`` splits success vs failure into
two files; we add a ``max_steps`` and ``cancelled`` split because our
durable-resume / max_steps paths are distinct events the eval gate
will want to weigh differently.

Mini-ADR L-7 highlights:

* **Plain ObjectStore, not WORM.** ``audit_log`` is the compliance
  source of truth (Stream D.1 sends it to S3 Object Lock); trajectory
  is LLM-trainable side data. A lost JSONL line is acceptable; a lost
  audit row is not.
* **Best-effort.** :meth:`TrajectoryRecorder.record` swallows
  ``ObjectStoreError`` after emitting a counter so it cannot stall
  the run's terminal path. Callers schedule it via
  ``asyncio.create_task`` with their own deadline.
* **Per-tenant prefix.** Per-tenant scan stays cheap; no
  cross-tenant trajectory mixing in the bucket layout.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from expert_work.common.conversation_channel import is_hidden
from expert_work.common.observability import expert_work_counter
from expert_work.runtime.storage import ObjectStore, ObjectStoreError

logger = logging.getLogger(__name__)

#: Default key prefix; can be overridden per-recorder. Bucket
#: configuration lives in :class:`expert_work.runtime.storage.S3CompatibleConfig`.
DEFAULT_PREFIX: str = "trajectories"

TrajectoryOutcome = Literal["success", "failed", "max_steps", "cancelled"]

_VALID_OUTCOMES: frozenset[str] = frozenset({"success", "failed", "max_steps", "cancelled"})

_trajectory_recorded_total = expert_work_counter(
    "expert_work_trajectory_recorded_total",
    "Trajectories successfully written to ObjectStore (Stream L.L7).",
    ("outcome",),
)

_trajectory_record_errors_total = expert_work_counter(
    "expert_work_trajectory_record_errors_total",
    "Trajectory write failures swallowed so the run's terminal path stays clean.",
    ("outcome", "reason"),
)


@dataclass(frozen=True)
class TrajectoryRecord:
    """One run's worth of conversation + metadata, ready for serialisation.

    ``messages`` is the canonical :class:`AgentState.messages` list at
    the run's terminal point; the recorder converts it to ShareGPT
    shape via :func:`serialize_messages_sharegpt`.
    """

    thread_id: UUID
    tenant_id: UUID
    outcome: TrajectoryOutcome
    messages: Sequence[BaseMessage]
    user_id: UUID | None = None
    run_id: UUID | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    step_count: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def serialize_messages_sharegpt(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    """Map LangChain ``BaseMessage`` instances to ShareGPT-flavoured dicts.

    The output shape — one ``{role, content, ...}`` dict per message —
    matches the format Hermes saves to ``trajectory_samples.jsonl``
    (``agent/trajectory.py``) and the loader our future J.13 eval gate
    will consume. ``tool_calls`` / ``tool_call_id`` carry through when
    set so the trajectory is a faithful replay of the run.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = _message_role(msg)
        entry: dict[str, Any] = {"role": role, "content": _message_content_text(msg)}
        if is_hidden(msg):
            # B-67 — hidden HumanMessages (inputs block, advisories) serialise as
            # ``user`` too; the flag lets readers skip them when they want the
            # user's actual request (``first_user_message``). Plain entries keep
            # their shape.
            entry["hidden"] = True
        if isinstance(msg, AIMessage):
            tool_calls = list(getattr(msg, "tool_calls", None) or [])
            if tool_calls:
                # Strip the LangChain-internal ``type`` key the recorder
                # cares about ShareGPT-shape, not LangChain's tagging.
                entry["tool_calls"] = [
                    {k: v for k, v in tc.items() if k != "type"} for tc in tool_calls
                ]
        elif isinstance(msg, ToolMessage):
            tool_call_id = getattr(msg, "tool_call_id", None)
            if tool_call_id:
                entry["tool_call_id"] = str(tool_call_id)
        out.append(entry)
    return out


def _message_role(msg: BaseMessage) -> str:
    if isinstance(msg, SystemMessage):
        return "system"
    if isinstance(msg, HumanMessage):
        return "user"
    if isinstance(msg, AIMessage):
        return "assistant"
    if isinstance(msg, ToolMessage):
        return "tool"
    # Fallback for unknown subclasses — log + best-effort. The recorder
    # is fire-and-forget, so an unknown message kind must not crash; it
    # lands with an underspecified role so the failure is visible in
    # the JSONL rather than silently dropped.
    logger.warning("trajectory.unknown_message_type type=%s", type(msg).__name__)
    return f"unknown:{type(msg).__name__}"


def _message_content_text(msg: BaseMessage) -> str:
    """Stringify ``msg.content`` regardless of whether it is str or block list.

    Mirrors :func:`orchestrator.llm.providers.anthropic._message_text`
    — flatten any block list by concatenating block ``text`` values.
    """
    content = msg.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


@dataclass
class TrajectoryRecorder:
    """Write completed runs to ObjectStore for the J.13 eval gate.

    Single dependency: an :class:`ObjectStore`. The orchestrator runner
    constructs one at startup using the same store the audit-backup
    worker uses (different prefix — trajectories live under
    ``trajectories/`` rather than ``audit_logs/``).
    """

    object_store: ObjectStore
    prefix: str = DEFAULT_PREFIX

    def __post_init__(self) -> None:
        if not self.prefix:
            msg = "TrajectoryRecorder.prefix must be non-empty"
            raise ValueError(msg)
        if self.prefix.endswith("/"):
            # Normalise — ``key_for`` joins with ``/``.
            object.__setattr__(self, "prefix", self.prefix.rstrip("/"))

    def key_for(self, record: TrajectoryRecord) -> str:
        """Build the ObjectStore key for ``record``.

        ``finished_at`` (if set) anchors the YYYY/MM/DD partition; falls
        back to "now" UTC otherwise. The thread_id-named file is unique
        because run termination only happens once per thread per run;
        multiple runs on the same thread land in distinct timestamps.
        """
        if record.outcome not in _VALID_OUTCOMES:
            msg = f"invalid outcome {record.outcome!r}; expected one of {sorted(_VALID_OUTCOMES)}"
            raise ValueError(msg)
        ts = record.finished_at or datetime.now(UTC)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        else:
            ts = ts.astimezone(UTC)
        return (
            f"{self.prefix}/{record.tenant_id}/{record.outcome}/"
            f"{ts:%Y}/{ts:%m}/{ts:%d}/{record.thread_id}.jsonl"
        )

    async def record(self, record: TrajectoryRecord) -> None:
        """Best-effort write of ``record`` to the configured ObjectStore.

        Failures are logged and counted but never re-raised — the
        caller schedules this as a fire-and-forget task and must not
        be impacted by ObjectStore outages. The audit_log row remains
        the source of truth for "did this run finish?"; trajectory is
        eval-side training data.
        """
        try:
            key = self.key_for(record)
            payload = self._serialise(record)
            await self.object_store.put(
                key,
                payload,
                content_type="application/jsonl",
            )
        except ValueError as exc:
            # ``key_for`` rejected an invalid outcome — record the
            # programmer error and move on. Don't let a config error
            # take down the run's terminal path.
            logger.warning("trajectory.invalid_record err=%s", exc)
            _trajectory_record_errors_total.labels(
                outcome=str(record.outcome), reason="invalid_record"
            ).inc()
        except ObjectStoreError as exc:
            logger.warning(
                "trajectory.store_error outcome=%s err=%s",
                record.outcome,
                type(exc).__name__,
            )
            _trajectory_record_errors_total.labels(
                outcome=str(record.outcome), reason="store_error"
            ).inc()
        except Exception as exc:
            # Final defensive catch — anything unexpected (a bug in
            # serialisation, a misbehaving custom ObjectStore) must
            # not crash the run.
            logger.exception(
                "trajectory.unexpected_error outcome=%s err=%s",
                record.outcome,
                type(exc).__name__,
            )
            _trajectory_record_errors_total.labels(
                outcome=str(record.outcome), reason="unexpected"
            ).inc()
        else:
            _trajectory_recorded_total.labels(outcome=str(record.outcome)).inc()
            logger.info(
                "trajectory.recorded outcome=%s thread_id=%s",
                record.outcome,
                record.thread_id,
            )

    def _serialise(self, record: TrajectoryRecord) -> bytes:
        """Build the one-line JSONL payload for ``record``."""
        envelope: dict[str, Any] = {
            "thread_id": str(record.thread_id),
            "tenant_id": str(record.tenant_id),
            "outcome": record.outcome,
            "messages": serialize_messages_sharegpt(record.messages),
        }
        if record.user_id is not None:
            envelope["user_id"] = str(record.user_id)
        if record.run_id is not None:
            envelope["run_id"] = str(record.run_id)
        if record.started_at is not None:
            envelope["started_at"] = _isoformat(record.started_at)
        if record.finished_at is not None:
            envelope["finished_at"] = _isoformat(record.finished_at)
        if record.step_count is not None:
            envelope["step_count"] = record.step_count
        if record.metadata:
            envelope["metadata"] = record.metadata
        # JSONL — one record per line; \n keeps loaders happy even on
        # the single-record file case (Hermes ``trajectory_samples.jsonl``
        # uses the same shape).
        return (json.dumps(envelope, ensure_ascii=False) + "\n").encode("utf-8")


def _isoformat(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).isoformat()
