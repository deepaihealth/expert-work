"""B-222 —— what an ingest is estimated to cost before it starts, and what it cost.

Every ingest the tenant pays for is first estimated — a local parse and counts,
no model called — and only starts once the user confirms the estimate
(``awaiting_confirmation`` → ``pending``), whatever its amount. The estimate is
kept on the document (``knowledge_document.ingest_estimate``) and the spend of
the ingest that followed next to it (``ingest_actual``), so a row reads
「预估 ¥x · 实际 ¥y」.

Amounts are integer micro-yuan, priced the way the billing rollup prices a
usage row: ``(input * input rate + output * output rate) // 1_000_000`` with
the rate card's micro-yuan per million tokens. Nothing here names a vendor:
per-model averages come from the model catalog (``ModelEntry.estimate_caps``),
prices from ``model_rate_card``.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

#: §14 ① —— an estimate waits this long for its confirmation, then it is
#: withdrawn (a new upload deleted, anything else put back as it was).
CONFIRM_WINDOW: Final = timedelta(days=7)

#: §5 —— a confirmed estimate's parse snapshot is kept this much longer, for a
#: crashed ingest to retry from it.
SNAPSHOT_GRACE_AFTER_CONFIRM: Final = timedelta(days=1)

#: §14 ④ —— a confirmed estimate is asked again when the fresh total is over
#: this share above it (in percent) **and** at least this much higher.
RECONFIRM_RISE_PERCENT: Final = 20
RECONFIRM_MIN_RISE_MICROS: Final = 10_000  # ¥0.01

#: The only currency the rate card prices in.
ESTIMATE_CURRENCY: Final = "CNY"


class EstimateOrigin(StrEnum):
    """What started the estimate — decides what withdrawing it puts back."""

    #: A new document: withdrawing deletes it, file and all (§14 ②).
    UPLOAD = "upload"
    #: New bytes for an existing document (same name): withdrawing drops them,
    #: the serving version stays.
    REUPLOAD = "reupload"
    #: The same bytes again: withdrawing restores the document as it was.
    REINGEST = "reingest"
    RECOGNIZE = "recognize"
    REINGEST_STALE = "reingest_stale"
    BATCH_REINGEST = "batch_reingest"


class EstimateKind(StrEnum):
    """A line of an estimate — only what is billed to the tenant
    (``knowledge_ingest``); what the platform pays for is never listed."""

    EMBEDDING = "embedding"
    RECOGNITION = "recognition"
    FIGURE = "figure"


class EstimateUnit(StrEnum):
    CHUNKS = "chunks"
    PAGES = "pages"
    FIGURES = "figures"


class EstimateBasis(StrEnum):
    """How a line's tokens were arrived at."""

    #: Counted on the parsed text (embeddings).
    LOCAL_COUNT = "local_count"
    #: Per-page / per-figure averages the model catalog declares for the model.
    CATALOG_AVERAGE = "catalog_average"
    #: The platform's generic averages (「按通用均值估」): the catalog declares none.
    DEFAULT_AVERAGE = "default_average"


class EstimateNote(StrEnum):
    """Hint codes on an estimate; the console words them."""

    #: Figures on scanned pages are only found by recognition: not counted.
    SCANNED_FIGURES_NOT_COUNTED = "scanned_figures_not_counted"
    #: No usable recognition model: scanned pages wait, nothing billed.
    RECOGNITION_UNAVAILABLE = "recognition_unavailable"
    #: Figure description is off: the document's pictures are left out.
    FIGURES_OFF = "figures_off"
    #: A line uses the platform's generic averages.
    DEFAULT_AVERAGE = "default_average"
    #: Pages past the per-document recognition limit are not read.
    OVER_PAGE_LIMIT = "over_page_limit"
    #: A model has no rate card row: its line is not billed (amount 0).
    UNPRICED = "unpriced"
    #: Recognition stops once the document's text limit is reached — the
    #: estimate counts every page (an over-estimate).
    TEXT_LIMIT_MAY_STOP_EARLY = "text_limit_may_stop_early"
    #: A logo repeated within one upload batch is counted in each document.
    BATCH_REPEATS_COUNTED = "batch_repeats_counted"
    #: A figure is counted as one description call: a chart's data and a
    #: text-heavy figure's text take more calls — charts may cost more.
    FIGURE_EXTRA_CALLS_NOT_COUNTED = "figure_extra_calls_not_counted"


class EstimateItem(BaseModel):
    """One line: ``units`` to process (``reused_units`` of them come from an
    earlier ingest and are not billed again), the tokens they are expected to
    take on ``model`` and what that costs."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: EstimateKind
    units: int = Field(ge=0)
    unit: EstimateUnit
    reused_units: int = Field(default=0, ge=0)
    model: str | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    micros: int = Field(default=0, ge=0)
    basis: EstimateBasis = EstimateBasis.LOCAL_COUNT
    #: ``False``: the model has no rate card row — ``micros`` is 0.
    priced: bool = True


class EstimateCounts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    chunks: int = Field(default=0, ge=0)
    scanned_pages: int = Field(default=0, ge=0)
    figures: int = Field(default=0, ge=0)
    pages_over_limit: int = Field(default=0, ge=0)


class EstimateModels(BaseModel):
    """The models the estimate was made for (``provider/model`` keys; the
    embedding one as the ingest pins it). Any of them different when the
    ingest starts → the estimate is asked again (§14 ④)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    embedding: str | None = None
    recognition: str | None = None
    fallback: str | None = None
    figure: str | None = None

    def fingerprint(self) -> str:
        models = (self.embedding, self.recognition, self.fallback, self.figure)
        joined = "|".join(model or "" for model in models)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


class DuplicateOf(BaseModel):
    """K-21 ① —— the document of the same base already holding these bytes."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: UUID
    doc_seq: int | None = None
    filename: str


class IngestEstimate(BaseModel):
    """``knowledge_document.ingest_estimate`` as shown (what the store keeps
    to put a document back when the estimate is withdrawn is not part of it).

    ``estimated`` is ``False`` for a duplicate caught on upload: it was never
    parsed — 「仍入库」 estimates it first."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: UUID
    version: int = 1
    origin: EstimateOrigin
    estimated: bool = True
    created_at: datetime
    duplicate_of: DuplicateOf | None = None
    #: The user asked to ingest it although it is a duplicate.
    allow_duplicate: bool = False
    currency: str = ESTIMATE_CURRENCY
    total_micros: int = Field(default=0, ge=0)
    #: Some line has no rate card row (its amount is 0, not billed).
    unpriced: bool = False
    notes: tuple[str, ...] = ()
    counts: EstimateCounts = Field(default_factory=EstimateCounts)
    models: EstimateModels = Field(default_factory=EstimateModels)
    models_fingerprint: str = ""
    items: tuple[EstimateItem, ...] = ()
    confirmed_at: datetime | None = None
    confirmed_by: str | None = None


class ActualItem(BaseModel):
    """What one model was billed for during the ingest."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    provider: str | None = None
    model: str
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    micros: int = Field(default=0, ge=0)
    priced: bool = True


IngestOutcome = Literal["ready", "failed", "stopped"]


class IngestActual(BaseModel):
    """``knowledge_document.ingest_actual`` —— what the latest paid ingest of
    the document was billed (priced like the bill). ``estimate_id`` is the
    estimate it was confirmed under (``None`` for an ingest that had none);
    ``generation`` the claim's: retries of the same ingest add up."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    estimate_id: UUID | None = None
    generation: int = Field(ge=0)
    currency: str = ESTIMATE_CURRENCY
    total_micros: int = Field(default=0, ge=0)
    unpriced: bool = False
    items: tuple[ActualItem, ...] = ()
    #: ``stopped``: cancelled, or lost to another claim, before it settled.
    outcome: IngestOutcome
    finished_at: datetime


def token_micros(input_tokens: int, output_tokens: int, input_rate: int, output_rate: int) -> int:
    """The billed micro-yuan for one usage row: rates are micro-yuan per
    million tokens, divided once and floored — the rollup's arithmetic."""
    return (input_tokens * input_rate + output_tokens * output_rate) // 1_000_000


def needs_reconfirmation(confirmed: IngestEstimate, fresh: IngestEstimate) -> bool:
    """§6.3 / §14 ④ —— whether an ingest about to start must be confirmed
    again: a model changed, or the total rose more than
    :data:`RECONFIRM_RISE_PERCENT` percent **and** by at least
    :data:`RECONFIRM_MIN_RISE_MICROS`. Integers only."""
    if confirmed.models_fingerprint != fresh.models_fingerprint:
        return True
    rise = fresh.total_micros - confirmed.total_micros
    over_share = fresh.total_micros * 100 > confirmed.total_micros * (100 + RECONFIRM_RISE_PERCENT)
    return over_share and rise >= RECONFIRM_MIN_RISE_MICROS
