"""B-267 step 2 —— the base-profile refresh bookkeeping both stores share.

``knowledge_base.profile`` (:class:`~expert_work.protocol.knowledge_profile.KnowledgeProfileRecord`)
carries a pending ``refresh``; these pure transitions are the only code that
changes it, so the SQL and the in-memory store cannot drift.

Debounce: a change asks for a refresh :data:`PROFILE_DEBOUNCE` later, every
further change pushes it back; while documents of the base are still being
ingested the refresh waits, but never longer than :data:`PROFILE_MAX_WAIT`
after the first change. A claimed refresh is leased (``due_at`` moves to the
lease end): a worker that dies leaves it to be claimed again. Finishing clears
the refresh only when no change came in meanwhile (``marks``); a failure keeps
the old profile and retries a few times, then gives up until the next change.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from expert_work.protocol.knowledge_profile import (
    KnowledgeProfile,
    KnowledgeProfileRecord,
    ProbeCalibration,
    ProfileRefresh,
)

PROFILE_DEBOUNCE: Final = timedelta(seconds=60)
PROFILE_MAX_WAIT: Final = timedelta(minutes=30)
PROFILE_LEASE: Final = timedelta(minutes=10)
PROFILE_RETRY_AFTER: Final = timedelta(minutes=10)
PROFILE_MAX_ATTEMPTS: Final = 3
#: How much of a document's first chunk a digest keeps.
PROFILE_OPENING_CHARS: Final = 300
#: Section headings are taken this many levels deep (``Chapter > Section``).
PROFILE_HEADING_DEPTH: Final = 2


@dataclass(frozen=True)
class ProfileClaim:
    """A base whose profile refresh a worker now holds."""

    tenant_id: UUID
    kb_id: UUID
    marks: int
    attempts: int
    calibrate_only: bool


@dataclass(frozen=True)
class DocumentDigest:
    """What a profile is generated from, per ready document: its title, its
    first distinct section headings and the start of its text."""

    title: str
    headings: tuple[str, ...]
    opening: str


@dataclass(frozen=True)
class ProfileDigest:
    """The digests (oldest documents first, at most the asked number) and how
    many ready documents the base has."""

    documents: tuple[DocumentDigest, ...]
    ready_documents: int


def marked(
    record: KnowledgeProfileRecord | None,
    *,
    now: datetime,
    debounce: timedelta = PROFILE_DEBOUNCE,
    calibrate_only: bool = False,
) -> KnowledgeProfileRecord:
    """``record`` with a refresh asked for ``debounce`` from ``now``. A pending
    refresh keeps its ``dirty_since`` and counts one more mark; a full refresh
    asked for wins over a calibration-only one. A later ``due_at`` (a lease or
    a retry wait) is kept: a held refresh is not claimed twice."""
    current = record or KnowledgeProfileRecord()
    pending = current.refresh
    due_at = now + debounce
    refresh = ProfileRefresh(
        due_at=max(due_at, pending.due_at) if pending is not None else due_at,
        dirty_since=pending.dirty_since if pending is not None else now,
        marks=pending.marks + 1 if pending is not None else 1,
        attempts=0,
        calibrate_only=calibrate_only and (pending is None or pending.calibrate_only),
    )
    return current.model_copy(update={"refresh": refresh})


def claimable(
    record: KnowledgeProfileRecord | None,
    *,
    now: datetime,
    ingesting: bool,
    max_wait: timedelta = PROFILE_MAX_WAIT,
) -> bool:
    """Whether the base's refresh is due: its time has come, and no document
    is still being ingested — unless the first change is ``max_wait`` old."""
    refresh = record.refresh if record is not None else None
    if refresh is None or refresh.due_at > now:
        return False
    return not ingesting or refresh.dirty_since + max_wait <= now


def claimed(
    record: KnowledgeProfileRecord, *, now: datetime, lease: timedelta = PROFILE_LEASE
) -> KnowledgeProfileRecord:
    """``record`` with its refresh leased until ``now + lease``."""
    assert record.refresh is not None  # noqa: S101 - only claimable records are claimed
    refresh = record.refresh.model_copy(
        update={"due_at": now + lease, "attempts": record.refresh.attempts + 1}
    )
    return record.model_copy(update={"refresh": refresh})


def finished(
    record: KnowledgeProfileRecord | None,
    *,
    marks: int,
    profile: KnowledgeProfile | None,
    calibration: ProbeCalibration | None,
) -> KnowledgeProfileRecord:
    """``record`` with the new profile / calibration (``None``: keep the
    stored one) and its refresh cleared — unless more changes came in since
    the claim (``marks``): then the refresh stays asked for."""
    current = record or KnowledgeProfileRecord()
    refresh = current.refresh
    if refresh is not None and refresh.marks == marks:
        refresh = None
    return KnowledgeProfileRecord(
        profile=profile if profile is not None else current.profile,
        calibration=calibration if calibration is not None else current.calibration,
        refresh=refresh,
    )


def failed(
    record: KnowledgeProfileRecord | None,
    *,
    now: datetime,
    retry_after: timedelta = PROFILE_RETRY_AFTER,
    max_attempts: int = PROFILE_MAX_ATTEMPTS,
) -> KnowledgeProfileRecord | None:
    """``record`` after a failed refresh: the old profile stays; retried
    ``retry_after`` later, dropped after ``max_attempts`` (the next change asks
    again)."""
    if record is None or record.refresh is None:
        return record
    if record.refresh.attempts >= max_attempts:
        return record.model_copy(update={"refresh": None})
    refresh = record.refresh.model_copy(update={"due_at": now + retry_after})
    return record.model_copy(update={"refresh": refresh})


def document_digest(
    title: str,
    heading_paths: Sequence[Sequence[str] | None],
    first_chunk: str | None,
    *,
    max_headings: int,
) -> DocumentDigest:
    """One document's digest from its chunks' heading paths (in chunk order)
    and its first chunk — the same rule for both stores."""
    headings: list[str] = []
    for path in heading_paths:
        if not path:
            continue
        heading = " > ".join(" ".join(part.split()) for part in path[:PROFILE_HEADING_DEPTH])
        if heading and heading not in headings:
            headings.append(heading)
            if len(headings) >= max_headings:
                break
    opening = " ".join((first_chunk or "").split())[:PROFILE_OPENING_CHARS]
    return DocumentDigest(title=title, headings=tuple(headings), opening=opening)
