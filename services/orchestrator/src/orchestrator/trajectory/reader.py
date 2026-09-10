"""Stream J.12 — read L7 trajectories back from the ObjectStore.

The L7 :class:`~orchestrator.trajectory.recorder.TrajectoryRecorder` is
write-only; J.12's curation worker needs to enumerate and parse stored
trajectories. :class:`TrajectoryReader` is the read counterpart over
the same ``ObjectStore`` + key scheme (STREAM-J-DESIGN § 17.3).

A malformed / vanished object is skipped (logged), never raised — the
curation worker is best-effort and must not stall on one bad line.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, get_args
from uuid import UUID

from expert_work.runtime.storage import ObjectNotFoundError, ObjectStore, ObjectStoreError
from orchestrator.trajectory.recorder import DEFAULT_PREFIX, TrajectoryOutcome

logger = logging.getLogger(__name__)

_VALID_OUTCOMES: frozenset[str] = frozenset(get_args(TrajectoryOutcome))


@dataclass(frozen=True)
class StoredTrajectory:
    """One trajectory object read back from the ObjectStore.

    ``messages`` is the ShareGPT-shaped list the recorder serialised;
    the rest mirror the :class:`~orchestrator.trajectory.recorder.TrajectoryRecord`
    envelope.
    """

    key: str
    thread_id: UUID
    tenant_id: UUID
    outcome: TrajectoryOutcome
    messages: list[dict[str, Any]]
    user_id: UUID | None = None
    run_id: UUID | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    step_count: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TrajectoryReader:
    """Enumerate + parse L7 trajectory objects for the curation worker."""

    object_store: ObjectStore
    prefix: str = DEFAULT_PREFIX

    def __post_init__(self) -> None:
        if not self.prefix:
            msg = "TrajectoryReader.prefix must be non-empty"
            raise ValueError(msg)
        object.__setattr__(self, "prefix", self.prefix.rstrip("/"))

    async def list_keys(
        self,
        *,
        tenant_id: UUID | None = None,
        outcome: TrajectoryOutcome | None = None,
    ) -> list[str]:
        """List trajectory object keys under an optional tenant / outcome scope.

        ``outcome`` can only be applied with ``tenant_id`` — the key
        scheme is ``{prefix}/{tenant}/{outcome}/...`` so a prefix scan
        cannot filter outcome without the tenant segment.
        """
        if outcome is not None and tenant_id is None:
            msg = "outcome filter requires tenant_id (key scheme is tenant/outcome)"
            raise ValueError(msg)
        scan = self.prefix
        if tenant_id is not None:
            scan = f"{scan}/{tenant_id}"
        if outcome is not None:
            scan = f"{scan}/{outcome}"
        return await self.object_store.list_prefix(f"{scan}/")

    async def find_by_thread(self, *, tenant_id: UUID, thread_id: UUID) -> StoredTrajectory | None:
        """The newest stored trajectory for ``thread_id`` under ``tenant_id``, or ``None``.

        P-2 — the synchronous 👎 → candidate path needs "is this thread's
        trajectory on disk yet" without waiting for the curation worker's
        300 s sweep. Keys are ``{prefix}/{tenant}/{outcome}/{YYYY}/{MM}/{DD}/
        {thread}.jsonl`` — the OUTCOME segment comes BEFORE the date, so key
        order is NOT time order (``…/success/2026/09/01/…`` sorts after
        ``…/failed/2026/09/09/…``). A thread matches at most one key per
        ``(outcome, day)`` partition, i.e. a handful, so every match is read
        and the one with the greatest ``finished_at`` wins; ties (same
        instant, or no ``finished_at`` on a legacy envelope) fall back to the
        greater key so the choice is deterministic. Same list-prefix call the
        worker already issues per sweep, scoped to one tenant.
        """
        suffix = f"/{thread_id}.jsonl"
        keys = [k for k in await self.list_keys(tenant_id=tenant_id) if k.endswith(suffix)]
        newest: StoredTrajectory | None = None
        for key in keys:
            stored = await self.read(key)
            if stored is None:
                continue
            if newest is None or _recency(stored) > _recency(newest):
                newest = stored
        return newest

    async def read(self, key: str) -> StoredTrajectory | None:
        """Read + parse one trajectory object.

        Returns ``None`` when the object vanished (a listed-then-deleted
        race) or its JSONL line is malformed.
        """
        try:
            raw = await self.object_store.get(key)
        except ObjectNotFoundError:
            return None
        except ObjectStoreError as exc:
            logger.warning("trajectory_reader.store_error key=%s err=%s", key, type(exc).__name__)
            return None
        return _parse(key, raw)


def _parse(key: str, raw: bytes) -> StoredTrajectory | None:
    try:
        envelope = json.loads(raw.decode("utf-8"))
        if not isinstance(envelope, dict):
            raise ValueError("trajectory envelope is not a JSON object")
        outcome = envelope["outcome"]
        if outcome not in _VALID_OUTCOMES:
            raise ValueError(f"unknown outcome {outcome!r}")
        messages = envelope["messages"]
        if not isinstance(messages, list):
            raise ValueError("trajectory 'messages' is not a list")
        return StoredTrajectory(
            key=key,
            thread_id=UUID(str(envelope["thread_id"])),
            tenant_id=UUID(str(envelope["tenant_id"])),
            outcome=outcome,
            messages=messages,
            user_id=_opt_uuid(envelope.get("user_id")),
            run_id=_opt_uuid(envelope.get("run_id")),
            started_at=_opt_dt(envelope.get("started_at")),
            finished_at=_opt_dt(envelope.get("finished_at")),
            step_count=envelope.get("step_count"),
            metadata=dict(envelope.get("metadata") or {}),
        )
    except (ValueError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("trajectory_reader.malformed key=%s err=%s", key, exc)
        return None


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _recency(stored: StoredTrajectory) -> tuple[datetime, str]:
    """Sort key for :meth:`TrajectoryReader.find_by_thread` — ``finished_at``
    first (legacy envelopes without one sort oldest), key string second."""
    return (stored.finished_at or _EPOCH, stored.key)


def _opt_uuid(value: object) -> UUID | None:
    return UUID(str(value)) if value is not None else None


def _opt_dt(value: object) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value is not None else None
