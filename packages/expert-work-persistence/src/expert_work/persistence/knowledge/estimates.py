"""Ingest estimates — the types and rules both knowledge stores share (B-222 PR-2).

A document's estimate lives in ``knowledge_document.ingest_estimate`` (JSONB):
the estimate itself (:class:`~expert_work.protocol.knowledge_estimate.IngestEstimate`
— absent while the document is still being estimated) plus three keys the
store keeps across estimates (:data:`META_KEYS`): what started it (``origin``),
whether the user asked to ingest a duplicate (``allow_duplicate``) and — never
shown — how to put the document back when the estimate is withdrawn
(``restore``). The pure rules here
— where a document goes when its estimate is withdrawn, which document of a
base is the duplicate, how a base's waiting documents add up, how retries of an
ingest add up — are used by both
:class:`~expert_work.persistence.knowledge.sql.SqlKnowledgeStore` and
:class:`~expert_work.persistence.knowledge.memory.InMemoryKnowledgeStore`, so
the two cannot drift.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from uuid import UUID, uuid4

from pydantic import ValidationError

from expert_work.protocol import DocumentStatus, IngestStage, KnowledgeDocument
from expert_work.protocol.knowledge_estimate import (
    ESTIMATE_CURRENCY,
    EstimateOrigin,
    IngestActual,
    IngestEstimate,
)

#: The statuses of a document whose estimate is under way or waits for its
#: confirmation — the previous version, if any, keeps serving.
WAITING_STATUSES: Final = frozenset(
    {DocumentStatus.ESTIMATING, DocumentStatus.AWAITING_CONFIRMATION}
)

#: Keys of ``ingest_estimate`` the store sets (whatever estimate is written).
META_KEYS: Final = ("origin", "allow_duplicate", "restore")
#: Of them, the ones never shown.
_STORE_ONLY: Final = ("restore", "first_confirm_by")
#: B2b review —— the deadline a released duplicate had before it went back
#: to ``estimating`` (ISO). Its estimate, when it lands, keeps that deadline
#: (:func:`kept_deadline`) — a released copy outlives its first one by at most
#: :data:`RELEASED_COPY_GRACE`, and only when it was released that close to it.
FIRST_CONFIRM_BY: Final = "first_confirm_by"
#: The least time a released copy's estimate is left to be confirmed in.
RELEASED_COPY_GRACE: Final = timedelta(hours=1)

#: A withdrawn estimate puts a document back in one of these as it was; any
#: other status it came from (an ingest under way) becomes ``cancelled``.
_RESTORABLE: Final = frozenset(
    {
        DocumentStatus.READY,
        DocumentStatus.FAILED,
        DocumentStatus.CANCELLED,
        DocumentStatus.BLOCKED,
        DocumentStatus.DISABLED,
    }
)

#: Statuses that keep a document out of retrieval whatever serves: a withdrawn
#: re-upload goes back to them, not to ``ready``.
_HELD_BACK: Final = frozenset({DocumentStatus.BLOCKED, DocumentStatus.DISABLED})

#: ``error`` texts — the same as the stores' own for the same states.
DISCARDED_ERROR: Final = "pending upload discarded; nothing is serving"
CANCELLED_ERROR: Final = "cancelled by user"

#: What an upload became (``busy``: the document is past the point of no
#: return — its new chunks are being swapped in — nothing was touched).
UploadOutcome = Literal["unchanged", "estimating", "duplicate", "busy"]
#: What confirming one document did.
ConfirmOutcome = Literal[
    "confirmed", "estimate_changed", "not_awaiting", "duplicate", "expired", "not_found"
]
#: What withdrawing an estimate did with the document.
WithdrawOutcome = Literal["withdrawn", "withdrawn_deleted"]

#: Origins that bring new bytes — the only ones checked for duplicates.
_NEW_BYTES: Final = frozenset({EstimateOrigin.UPLOAD.value, EstimateOrigin.REUPLOAD.value})


class EstimateRefused(Exception):  # noqa: N818 — a refusal, not a fault
    """:meth:`KnowledgeStore.begin_estimate` changed nothing: ``busy`` (the
    document is past the point of no return, :func:`writing_chunks`),
    ``not_awaiting`` / ``estimate_changed`` (it is no longer what the caller
    expected)."""

    def __init__(self, reason: Literal["busy", "not_awaiting", "estimate_changed"]) -> None:
        super().__init__(reason)
        self.reason = reason


def writing_chunks(status: str, stage: str | None) -> bool:
    """B-222 —— past the point of no return: ``processing`` at ``indexing``
    (the new chunks are being swapped in, READY follows). Cancel, discard, a
    re-upload and a re-estimate all leave such a document alone."""
    return status == DocumentStatus.PROCESSING.value and stage == IngestStage.INDEXING.value


def estimate_refusal(
    *,
    status: str,
    stage: str | None,
    raw: Mapping[str, Any] | None,
    expect_status: DocumentStatus | None,
    expect_estimate_id: UUID | None,
) -> Literal["busy", "not_awaiting", "estimate_changed"] | None:
    """Why :meth:`KnowledgeStore.begin_estimate` must leave the document as
    it is (``None``: it may go on)."""
    if writing_chunks(status, stage):
        return "busy"
    if expect_status is not None and status != expect_status.value:
        return "not_awaiting"
    if expect_estimate_id is not None and estimate_id_of(raw) != expect_estimate_id:
        return "estimate_changed"
    return None


def reindex_fence_statuses(status: DocumentStatus) -> frozenset[DocumentStatus]:
    """What a re-index write-back read in ``status`` is fenced on (with the
    generation): that status — except that ``estimating`` and
    ``awaiting_confirmation`` stand for each other: an estimate finishing
    keeps the generation, and the chunks serving meanwhile are the previous
    version's either way."""
    return WAITING_STATUSES if status in WAITING_STATUSES else frozenset({status})


@dataclass(frozen=True)
class UploadStart:
    """What :meth:`KnowledgeStore.begin_upload` did with an upload."""

    document: KnowledgeDocument
    outcome: UploadOutcome


@dataclass(frozen=True)
class ParseSnapshot:
    """An estimate's local parse, encoded by the control plane (``format``
    names the shape and its version)."""

    format: str
    payload: bytes


@dataclass(frozen=True)
class EstimateSummary:
    """A base's documents being estimated or waiting for a confirmation:
    ``awaiting`` and ``total_micros`` / ``unpriced`` count the confirmable
    ones; duplicates, and estimates past their deadline not yet swept
    (``expired``), are counted apart and never in the total. ``fingerprint``
    names exactly which estimates are confirmable (「全部确认」 sends it back:
    a document the user never saw is never confirmed with them)."""

    estimating: int = 0
    awaiting: int = 0
    duplicates: int = 0
    expired: int = 0
    total_micros: int = 0
    unpriced: bool = False
    earliest_confirm_by: datetime | None = None
    fingerprint: str = ""


@dataclass(frozen=True)
class ConfirmAllResult:
    """:meth:`KnowledgeStore.confirm_all`: ``matched`` is whether the base's
    confirmable documents still added up to what the user saw (else nothing
    was changed and ``summary`` is what they add up to now). ``skipped``:
    estimates past their deadline, left for the expiry sweep."""

    matched: bool
    summary: EstimateSummary
    confirmed: tuple[UUID, ...] = ()
    withdrawn: Mapping[UUID, WithdrawOutcome] = field(default_factory=dict)
    skipped: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class ExpiredEstimate:
    """An estimate withdrawn because its confirmation deadline passed."""

    tenant_id: UUID
    kb_id: UUID
    document_id: UUID
    filename: str
    outcome: WithdrawOutcome


@dataclass(frozen=True)
class WithdrawPlan:
    """Where a document goes when its estimate is withdrawn: deleted (a new
    upload, original file and all), or put back — its new bytes dropped
    (``clear_pending``) and the given status / error / stage / estimate set."""

    delete: bool
    clear_pending: bool = False
    status: DocumentStatus | None = None
    error: str | None = None
    error_code: str | None = None
    stage: str | None = None
    estimate: dict[str, Any] | None = None


# -- the stored JSON --------------------------------------------------------------


def public_part(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """``raw`` without the store-only keys; ``None`` when it holds no estimate
    yet (a document still being estimated)."""
    if not raw or "id" not in raw:
        return None
    return {key: value for key, value in raw.items() if key not in _STORE_ONLY}


def public_estimate(raw: Mapping[str, Any] | None) -> IngestEstimate | None:
    """The shown estimate, or ``None`` (none yet, or a shape this version
    cannot read — never an error on a document list)."""
    shown = public_part(raw)
    if shown is None:
        return None
    try:
        return IngestEstimate.model_validate(shown)
    except ValidationError:
        return None


def read_actual(raw: Mapping[str, Any] | None) -> IngestActual | None:
    if not raw:
        return None
    try:
        return IngestActual.model_validate(raw)
    except ValidationError:
        return None


def origin_of(raw: Mapping[str, Any] | None) -> str | None:
    value = (raw or {}).get("origin")
    return value if isinstance(value, str) else None


def is_duplicate(raw: Mapping[str, Any] | None) -> bool:
    return bool((raw or {}).get("duplicate_of"))


def is_estimated(raw: Mapping[str, Any] | None) -> bool:
    """A real estimate (not a duplicate caught on upload, never parsed)."""
    return public_part(raw) is not None and bool((raw or {}).get("estimated", True))


def estimate_id_of(raw: Mapping[str, Any] | None) -> UUID | None:
    value = (raw or {}).get("id")
    try:
        return UUID(str(value)) if value is not None else None
    except ValueError:
        return None


def capture_restore(
    *,
    status: str,
    error: str | None,
    error_code: str | None,
    stage: str | None,
    raw_estimate: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """How to put a settled document back if the estimate it enters is withdrawn."""
    return {
        "status": status,
        "error": error,
        "error_code": error_code,
        "stage": stage,
        "estimate": public_part(raw_estimate),
    }


def _rank(origin: str | None) -> int:
    if origin == EstimateOrigin.UPLOAD.value:
        return 2
    if origin == EstimateOrigin.REUPLOAD.value:
        return 1
    return 0


def begin_meta(
    *,
    status: str | None,
    raw: Mapping[str, Any] | None,
    origin: EstimateOrigin,
    allow_duplicate: bool,
    restore: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The store-only keys of a document entering an estimate.

    A document already waiting keeps what it would be put back to, and the
    stronger origin (a new upload stays one; new bytes stay new bytes until
    withdrawn); any other status is captured now (``restore``, ``None`` for a
    row created by this upload)."""
    if status in {s.value for s in WAITING_STATUSES} and raw is not None:
        kept = origin_of(raw)
        chosen = kept if kept is not None and _rank(kept) > _rank(origin.value) else origin.value
        return {
            "origin": chosen,
            "allow_duplicate": allow_duplicate,
            "restore": raw.get("restore"),
        }
    return {"origin": origin.value, "allow_duplicate": allow_duplicate, "restore": restore}


def with_estimate(meta: Mapping[str, Any], estimate: Mapping[str, Any]) -> dict[str, Any]:
    """The stored JSON: an estimate and the store-only keys it was made under."""
    return {**dict(estimate), **{key: meta.get(key) for key in META_KEYS}}


def meta_of(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    return {key: (raw or {}).get(key) for key in META_KEYS}


def duplicate_estimate(
    *, meta: Mapping[str, Any], duplicate_of: Mapping[str, Any], now: datetime
) -> dict[str, Any]:
    """K-21 ① —— an upload whose bytes another document of the base already
    holds: not parsed, nothing to pay until the user asks for it anyway."""
    estimate: dict[str, Any] = {
        "id": str(uuid4()),
        "version": 1,
        "origin": meta.get("origin"),
        "estimated": False,
        "created_at": now.isoformat(),
        "duplicate_of": dict(duplicate_of),
        "allow_duplicate": False,
        "currency": ESTIMATE_CURRENCY,
        "total_micros": 0,
        "unpriced": False,
        "notes": [],
        "items": [],
    }
    return with_estimate(meta, estimate)


def checks_duplicates(meta: Mapping[str, Any]) -> bool:
    """Only new bytes are checked against the base, and never once the user
    asked to ingest them anyway."""
    return meta.get("origin") in _NEW_BYTES and not meta.get("allow_duplicate")


def duplicate_ref(*, document_id: UUID, doc_seq: int | None, filename: str) -> dict[str, Any]:
    return {"id": str(document_id), "doc_seq": doc_seq, "filename": filename}


def confirmed(raw: Mapping[str, Any], *, confirmed_by: str | None, now: datetime) -> dict[str, Any]:
    return {**dict(raw), "confirmed_at": now.isoformat(), "confirmed_by": confirmed_by}


# -- withdrawing -------------------------------------------------------------------


def withdraw_plan(raw: Mapping[str, Any] | None, *, chunk_count: int) -> WithdrawPlan:
    """§6.2 —— where a document goes when its estimate is withdrawn (cancelled
    while being estimated or awaiting confirmation, or expired):

    * a new upload is deleted, its file and snapshot with it (§14 ②);
    * new bytes for an existing document are dropped: it is ``ready`` again
      when a previous version serves, else ``failed`` (nothing serving);
    * a re-ingest, 「补识别」 or re-chunk of the same bytes puts it back exactly
      as it was — unless it came from an ingest under way (re-ingested while
      pending / processing), which cannot be resumed: then ``cancelled``.

    The previous estimate comes back with it."""
    origin = origin_of(raw)
    restore = (raw or {}).get("restore") or {}
    previous = restore.get("estimate")
    if origin == EstimateOrigin.UPLOAD.value:
        return WithdrawPlan(delete=True)
    if origin == EstimateOrigin.REUPLOAD.value:
        kept = _status(restore.get("status"))
        if kept in _HELD_BACK:
            # Held out of retrieval before the re-upload: it stays so.
            return WithdrawPlan(
                delete=False,
                clear_pending=True,
                status=kept,
                error=restore.get("error"),
                error_code=restore.get("error_code"),
                stage=restore.get("stage"),
                estimate=previous,
            )
        serving = chunk_count > 0
        return WithdrawPlan(
            delete=False,
            clear_pending=True,
            status=DocumentStatus.READY if serving else DocumentStatus.FAILED,
            error=None if serving else DISCARDED_ERROR,
            stage=IngestStage.READY.value if serving else restore.get("stage"),
            estimate=previous,
        )
    status = _status(restore.get("status"))
    if status is not None and status in _RESTORABLE:
        return WithdrawPlan(
            delete=False,
            status=status,
            error=restore.get("error"),
            error_code=restore.get("error_code"),
            stage=restore.get("stage"),
            estimate=previous,
        )
    return WithdrawPlan(
        delete=False,
        status=DocumentStatus.CANCELLED,
        error=CANCELLED_ERROR,
        stage=restore.get("stage"),
        estimate=previous,
    )


def discard_plan(raw: Mapping[str, Any] | None, *, chunk_count: int) -> WithdrawPlan:
    """``discard_pending`` on a document waiting with new bytes: those bytes
    go, and so does the estimate made for them. A new upload whose first
    bytes were never ingested is estimated again (``estimating``: they are
    still to be confirmed); anything else goes where a withdrawn re-upload
    goes — ``ready`` when a previous version serves (or the held-back status
    it had), else ``failed`` — with the previous estimate."""
    if origin_of(raw) == EstimateOrigin.UPLOAD.value:
        return WithdrawPlan(
            delete=False,
            clear_pending=True,
            status=DocumentStatus.ESTIMATING,
            stage=IngestStage.QUEUED.value,
            estimate=meta_of(raw),  # still a new upload: withdrawing deletes it
        )
    as_reupload = {**dict(raw or {}), "origin": EstimateOrigin.REUPLOAD.value}
    return withdraw_plan(as_reupload, chunk_count=chunk_count)


def duplicate_ref_id(raw: Mapping[str, Any] | None) -> UUID | None:
    """The document a duplicate mark names (``None``: no mark)."""
    ref = (raw or {}).get("duplicate_of")
    if not isinstance(ref, Mapping):
        return None
    try:
        return UUID(str(ref.get("id")))
    except ValueError:
        return None


def released_duplicate(
    raw: Mapping[str, Any], *, confirm_by: datetime | None
) -> tuple[DocumentStatus, dict[str, Any]]:
    """K-21 ①, §7 —— a duplicate whose original no longer holds its bytes
    (withdrawn, deleted, replaced): the mark goes. One with a real estimate
    (caught when its estimate finished) waits as a plain estimate; one caught
    on upload, never parsed, is estimated now (its store-only keys kept, and
    its deadline ``confirm_by``: see :func:`kept_deadline`)."""
    if is_estimated(raw):
        kept = {key: value for key, value in raw.items() if key != "duplicate_of"}
        return DocumentStatus.AWAITING_CONFIRMATION, kept
    meta = meta_of(raw)
    if confirm_by is not None:
        meta[FIRST_CONFIRM_BY] = confirm_by.isoformat()
    return DocumentStatus.ESTIMATING, meta


def kept_deadline(
    raw: Mapping[str, Any] | None, confirm_by: datetime, *, now: datetime
) -> datetime:
    """The deadline a finished estimate gets: ``confirm_by``, or the deadline
    the document had before it was released (:data:`FIRST_CONFIRM_BY`) when
    that is earlier — but never less than :data:`RELEASED_COPY_GRACE` from
    ``now``: a copy released with minutes left is still shown long enough to
    be confirmed, not withdrawn by the next sweep before anyone sees it."""
    first = (raw or {}).get(FIRST_CONFIRM_BY)
    if not isinstance(first, str):
        return confirm_by
    try:
        earlier = datetime.fromisoformat(first)
    except ValueError:
        return confirm_by
    return min(confirm_by, max(earlier, now + RELEASED_COPY_GRACE))


def _status(value: object) -> DocumentStatus | None:
    try:
        return DocumentStatus(str(value)) if value is not None else None
    except ValueError:
        return None


# -- a base's waiting documents ----------------------------------------------------


def summarize(
    documents: Iterable[tuple[UUID, str, Mapping[str, Any] | None, datetime | None]],
    *,
    now: datetime,
) -> EstimateSummary:
    """``(document id, status, raw estimate, confirm_by)`` of a base's
    documents → their summary (other statuses are ignored)."""
    estimating = awaiting = duplicates = expired = total = 0
    unpriced = False
    earliest: datetime | None = None
    confirmable: list[str] = []
    for document_id, status, raw, confirm_by in documents:
        if status == DocumentStatus.ESTIMATING.value:
            estimating += 1
            continue
        if status != DocumentStatus.AWAITING_CONFIRMATION.value:
            continue
        if is_expired(confirm_by, now):
            expired += 1
            continue
        if confirm_by is not None and (earliest is None or confirm_by < earliest):
            earliest = confirm_by
        if is_duplicate(raw) or not is_estimated(raw):
            duplicates += 1
            continue
        awaiting += 1
        total += int((raw or {}).get("total_micros") or 0)
        unpriced = unpriced or bool((raw or {}).get("unpriced"))
        confirmable.append(f"{document_id}:{estimate_id_of(raw)}")
    return EstimateSummary(
        estimating=estimating,
        awaiting=awaiting,
        duplicates=duplicates,
        expired=expired,
        total_micros=total,
        unpriced=unpriced,
        earliest_confirm_by=earliest,
        fingerprint=hashlib.sha256("\n".join(sorted(confirmable)).encode()).hexdigest(),
    )


def summary_matches(
    summary: EstimateSummary, *, count: int, total_micros: int, fingerprint: str
) -> bool:
    """What 「全部确认」 was shown is still what is confirmable: the same
    estimates (by id), the same count and total."""
    return (
        summary.awaiting == count
        and summary.total_micros == total_micros
        and summary.fingerprint == fingerprint
    )


def confirm_outcome(
    *,
    status: str,
    raw: Mapping[str, Any] | None,
    estimate_id: UUID,
    confirm_by: datetime | None,
    now: datetime,
) -> ConfirmOutcome | None:
    """Why one document cannot be confirmed (``None``: it can)."""
    if status != DocumentStatus.AWAITING_CONFIRMATION.value:
        return "not_awaiting"
    if not is_estimated(raw):
        return "duplicate"  # caught on upload: estimate it first (「仍入库」)
    if estimate_id_of(raw) != estimate_id:
        return "estimate_changed"
    if is_expired(confirm_by, now):
        return "expired"
    return None


def is_expired(confirm_by: datetime | None, now: datetime) -> bool:
    """Past its deadline: the estimate is withdrawn, never confirmed (§14 ①)."""
    return confirm_by is not None and confirm_by <= now


def withdraws_with_confirm_all(
    document_id: UUID, raw: Mapping[str, Any] | None, seen: Collection[UUID]
) -> bool:
    """A duplicate 「全部确认」 withdraws: one the user was shown (``seen``) —
    never one that arrived after."""
    return document_id in seen and (is_duplicate(raw) or not is_estimated(raw))


# -- what an ingest cost -----------------------------------------------------------


def actual_lands(
    *,
    row_generation: int,
    row_status: str,
    row_estimate: Mapping[str, Any] | None,
    generation: int,
    estimate_id: UUID | None,
) -> bool:
    """Whether an ingest's spend still belongs on the document: it is still
    that claim's (same generation), or it was cancelled out of it (the cancel
    bumps the generation once and nothing has started since); and it was
    confirmed under the document's current estimate (``None`` = an ingest
    with none, on a document that has none)."""
    ours = row_generation == generation or (
        row_generation == generation + 1 and row_status == DocumentStatus.CANCELLED.value
    )
    return ours and estimate_id_of(row_estimate) == estimate_id


def merge_actual(existing: Mapping[str, Any] | None, new: Mapping[str, Any]) -> dict[str, Any]:
    """The spend to keep: a retry of the same ingest (same generation and
    estimate) adds to what the earlier attempts were billed; anything else
    replaces it."""
    if (
        not existing
        or existing.get("generation") != new.get("generation")
        or existing.get("estimate_id") != new.get("estimate_id")
    ):
        return dict(new)
    by_model: dict[tuple[Any, Any], dict[str, Any]] = {}
    for item in [*existing.get("items", ()), *new.get("items", ())]:
        key = (item.get("provider"), item.get("model"))
        held = by_model.get(key)
        if held is None:
            by_model[key] = dict(item)
            continue
        for count in ("input_tokens", "output_tokens", "micros"):
            held[count] = int(held.get(count) or 0) + int(item.get(count) or 0)
        held["priced"] = bool(held.get("priced", True)) and bool(item.get("priced", True))
    return {
        **dict(new),
        "items": list(by_model.values()),
        "total_micros": int(existing.get("total_micros") or 0) + int(new.get("total_micros") or 0),
        "unpriced": bool(existing.get("unpriced")) or bool(new.get("unpriced")),
    }
