"""B-267 step 2 —— the base-profile refresh bookkeeping both stores share.

``knowledge_base.profile`` (:class:`~expert_work.protocol.knowledge_profile.KnowledgeProfileRecord`)
carries a pending ``refresh``; these pure transitions are the only code that
changes it, so the SQL and the in-memory store cannot drift.

Debounce: a change asks for a refresh :data:`PROFILE_DEBOUNCE` later, every
further change pushes it back; while documents of the base are still being
ingested the refresh waits, but never longer than :data:`PROFILE_MAX_WAIT`
after the first change. A claimed refresh is leased (``due_at`` moves to the
lease end) and fenced (``holder``): a worker that dies leaves it to be claimed
again, and only the claim holding it may finish or fail it. Finishing clears the
refresh only when no change came in meanwhile (``marks``); a failure keeps the
old profile and retries once; a refresh that used up its attempts — a crashed
or cancelled claim counts — is dropped until the next change asks again.
Deleting or switching off a document marks the profile stale at no cost
(:func:`staled`): it stops showing until a new one is generated.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

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
#: A failed refresh is tried once more, no more: a billed but unusable answer
#: is not paid for a third time.
PROFILE_MAX_ATTEMPTS: Final = 2
#: How much of a sampled document's first chunk a digest keeps.
PROFILE_OPENING_CHARS: Final = 100
#: Section headings are taken this many levels deep (``Chapter > Section``),
#: from the document's first chunks only.
PROFILE_HEADING_DEPTH: Final = 2
PROFILE_HEADING_SCAN: Final = 50


@dataclass(frozen=True)
class ProfileClaim:
    """A base whose profile refresh a worker now holds (``holder`` fences it)."""

    tenant_id: UUID
    kb_id: UUID
    marks: int
    attempts: int
    calibrate_only: bool
    holder: str = ""


@dataclass(frozen=True)
class DocumentDigest:
    """What a profile is generated from, per ready document: its title and —
    for the documents sampled across the base — its first distinct section
    headings and the start of its text (empty otherwise)."""

    title: str
    headings: tuple[str, ...] = ()
    opening: str = ""


@dataclass(frozen=True)
class ProfileDigest:
    """The base's ready documents, newest first (at most the asked number of
    titles; the sampled ones described), and how many it has in all."""

    documents: tuple[DocumentDigest, ...]
    ready_documents: int


def sample_positions(count: int, wanted: int) -> list[int]:
    """``wanted`` positions spread evenly over ``range(count)`` — the first
    (newest) and the last included; the same set for the same ``count``."""
    if count <= wanted:
        return list(range(count))
    if wanted <= 1:
        return [0][:wanted]
    step = (count - 1) / (wanted - 1)
    return sorted({round(index * step) for index in range(wanted)})


def exhausted(
    record: KnowledgeProfileRecord | None, *, max_attempts: int = PROFILE_MAX_ATTEMPTS
) -> bool:
    """A pending refresh whose attempts are used up (crashed claims count)."""
    refresh = record.refresh if record is not None else None
    return refresh is not None and refresh.attempts >= max_attempts


def marked(
    record: KnowledgeProfileRecord | None,
    *,
    now: datetime,
    debounce: timedelta = PROFILE_DEBOUNCE,
    calibrate_only: bool = False,
) -> KnowledgeProfileRecord:
    """``record`` with a refresh asked for ``debounce`` from ``now``. A pending
    refresh keeps its ``dirty_since`` and holder and counts one more mark; a
    full refresh asked for wins over a calibration-only one. A later
    ``due_at`` (a lease or a retry wait) is kept: a held refresh is not claimed
    twice."""
    current = record or KnowledgeProfileRecord()
    pending = current.refresh
    due_at = now + debounce
    refresh = ProfileRefresh(
        due_at=max(due_at, pending.due_at) if pending is not None else due_at,
        dirty_since=pending.dirty_since if pending is not None else now,
        marks=pending.marks + 1 if pending is not None else 1,
        attempts=0,
        calibrate_only=calibrate_only and (pending is None or pending.calibrate_only),
        holder=pending.holder if pending is not None else None,
    )
    return current.model_copy(update={"refresh": refresh})


def claimable(
    record: KnowledgeProfileRecord | None,
    *,
    now: datetime,
    ingesting: bool,
    max_wait: timedelta = PROFILE_MAX_WAIT,
) -> bool:
    """Whether the base's refresh is due: its time has come, its attempts are
    not used up, and no document is still being ingested — unless the first
    change is ``max_wait`` old."""
    refresh = record.refresh if record is not None else None
    if refresh is None or refresh.due_at > now or exhausted(record):
        return False
    return not ingesting or refresh.dirty_since + max_wait <= now


def claimed(
    record: KnowledgeProfileRecord, *, now: datetime, lease: timedelta = PROFILE_LEASE
) -> KnowledgeProfileRecord:
    """``record`` with its refresh leased until ``now + lease`` to a new holder."""
    assert record.refresh is not None  # noqa: S101 - only claimable records are claimed
    refresh = record.refresh.model_copy(
        update={
            "due_at": now + lease,
            "attempts": record.refresh.attempts + 1,
            "holder": uuid4().hex,
        }
    )
    return record.model_copy(update={"refresh": refresh})


def dropped(record: KnowledgeProfileRecord) -> KnowledgeProfileRecord:
    """``record`` without its (used-up) refresh."""
    return record.model_copy(update={"refresh": None})


def _holds(record: KnowledgeProfileRecord | None, holder: str) -> bool:
    refresh = record.refresh if record is not None else None
    return refresh is not None and refresh.holder == holder


def finished(
    record: KnowledgeProfileRecord | None,
    *,
    holder: str,
    marks: int,
    profile: KnowledgeProfile | None,
    calibration: ProbeCalibration | None,
    now: datetime,
    retry_calibration: bool = False,
    debounce: timedelta = PROFILE_DEBOUNCE,
) -> KnowledgeProfileRecord | None:
    """``record`` with the new profile / calibration (``None``: keep the
    stored one; a new profile also clears ``stale_since``) and its refresh
    cleared — unless more changes came in since the claim (``marks``): then it
    is asked for again. ``retry_calibration``: the profile is stored but the
    calibration failed — it alone is asked for again. A claim that no longer
    holds the refresh changes nothing (``record`` is returned as is)."""
    if not _holds(record, holder):
        return record
    assert record is not None and record.refresh is not None  # noqa: S101 - held
    pending = record.refresh
    refresh: ProfileRefresh | None = None
    if pending.marks != marks:
        refresh = pending.model_copy(
            update={"due_at": now + debounce, "attempts": 0, "holder": None}
        )
    elif retry_calibration:
        refresh = ProfileRefresh(
            due_at=now + PROFILE_RETRY_AFTER, dirty_since=now, calibrate_only=True
        )
    return KnowledgeProfileRecord(
        profile=profile if profile is not None else record.profile,
        calibration=calibration if calibration is not None else record.calibration,
        refresh=refresh,
        stale_since=None if profile is not None else record.stale_since,
    )


def failed(
    record: KnowledgeProfileRecord | None,
    *,
    holder: str,
    now: datetime,
    retry_after: timedelta = PROFILE_RETRY_AFTER,
    max_attempts: int = PROFILE_MAX_ATTEMPTS,
) -> KnowledgeProfileRecord | None:
    """``record`` after a failed refresh: the old profile stays; retried
    ``retry_after`` later, dropped after ``max_attempts`` (the next change asks
    again). A claim that no longer holds the refresh changes nothing."""
    if not _holds(record, holder):
        return record
    assert record is not None and record.refresh is not None  # noqa: S101 - held
    if record.refresh.attempts >= max_attempts:
        return dropped(record)
    refresh = record.refresh.model_copy(update={"due_at": now + retry_after, "holder": None})
    return record.model_copy(update={"refresh": refresh})


def staled(
    record: KnowledgeProfileRecord | None, *, now: datetime
) -> KnowledgeProfileRecord | None:
    """``record`` with its profile marked stale (a document it may describe was
    deleted or switched off); ``None`` when there is nothing to mark (no
    profile, or already stale) — the caller then writes nothing."""
    if record is None or record.profile is None or record.stale_since is not None:
        return None
    return record.model_copy(update={"stale_since": now})


def document_digest(
    title: str,
    heading_paths: Sequence[Sequence[str] | None],
    first_chunk: str | None,
    *,
    max_headings: int,
) -> DocumentDigest:
    """One sampled document's digest from its chunks' heading paths (in chunk
    order) and its first chunk — the same rule for both stores."""
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
