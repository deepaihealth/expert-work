"""B-267 step 2 / 3 —— what ``knowledge_base.profile`` (migration 0182) holds.

One JSON object per base, three independent parts:

* ``profile`` —— the base's generated profile: one LLM call per refresh over
  document-level information (titles, headings, each document's opening) —
  a summary, topics, typical questions and document types. Tenant data: it is
  only ever shown to that tenant's agents, in the knowledge catalog.
* ``calibration`` —— the per-base threshold of the per-turn probe: the 90th
  percentile of the top vector score a fixed list of generic, content-free
  queries reaches in this base, under the embedding model that produced it.
  Vector scores of different vendors and corpora are not comparable, so there
  is no global constant.
* ``refresh`` —— the debounced refresh the recovery worker runs.

Every part is optional; a stored object this version cannot read is treated as
absent (:func:`parse_profile_record`), never as an error on the read path.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

#: The summary is asked for in at most 60 Chinese characters (or ~25 English
#: words); anything longer is cut here.
PROFILE_SUMMARY_MAX_CHARS: Final = 120
PROFILE_TOPICS_MAX: Final = 10
PROFILE_TOPIC_MAX_CHARS: Final = 40
PROFILE_QUESTIONS_MAX: Final = 5
PROFILE_QUESTION_MAX_CHARS: Final = 80
PROFILE_DOC_TYPES_MAX: Final = 5
PROFILE_DOC_TYPE_MAX_CHARS: Final = 30


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _clip_all(values: object, *, most: int, limit: int) -> tuple[str, ...]:
    """Strings of ``values``, one line each, cut to ``limit``, blanks and
    repeats dropped, at most ``most`` of them."""
    if not isinstance(values, list | tuple):
        return ()
    out: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        clipped = _clip(value, limit)
        if clipped and clipped not in out:
            out.append(clipped)
    return tuple(out[:most])


class GeneratedProfile(BaseModel):
    """What the model returns — validated, then cut to the limits (a long
    answer is shortened, not refused)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    summary: str = Field(min_length=1)
    topics: tuple[str, ...] = ()
    typical_questions: tuple[str, ...] = ()
    doc_types: tuple[str, ...] = ()

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, value: object) -> object:
        return _clip(value, PROFILE_SUMMARY_MAX_CHARS) if isinstance(value, str) else value

    @field_validator("topics", mode="before")
    @classmethod
    def _topics(cls, value: object) -> tuple[str, ...]:
        return _clip_all(value, most=PROFILE_TOPICS_MAX, limit=PROFILE_TOPIC_MAX_CHARS)

    @field_validator("typical_questions", mode="before")
    @classmethod
    def _questions(cls, value: object) -> tuple[str, ...]:
        return _clip_all(value, most=PROFILE_QUESTIONS_MAX, limit=PROFILE_QUESTION_MAX_CHARS)

    @field_validator("doc_types", mode="before")
    @classmethod
    def _doc_types(cls, value: object) -> tuple[str, ...]:
        return _clip_all(value, most=PROFILE_DOC_TYPES_MAX, limit=PROFILE_DOC_TYPE_MAX_CHARS)


class KnowledgeProfile(GeneratedProfile):
    """A stored profile: the generated fields plus when, from how many
    documents and by which model (``provider/model``) it was made."""

    generated_at: datetime
    source_doc_count: int = Field(ge=0)
    model: str


class ProbeCalibration(BaseModel):
    """The per-turn probe's vector threshold for one base."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    #: The base's ``embedding_provider/embedding_model`` when it was computed —
    #: under any other model the threshold is not used.
    embedding_model: str
    vector_threshold: float
    #: How many generic queries it was computed from.
    queries: int = Field(ge=1)
    computed_at: datetime


class ProfileRefresh(BaseModel):
    """A pending refresh. ``marks`` counts the changes that asked for it: a
    refresh finishing under fewer marks than the base now has leaves the newer
    request standing. ``calibrate_only``: only the probe calibration is redone
    (the embedding model changed, the documents did not)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    due_at: datetime
    dirty_since: datetime
    marks: int = Field(default=1, ge=1)
    attempts: int = Field(default=0, ge=0)
    calibrate_only: bool = False


class KnowledgeProfileRecord(BaseModel):
    """The whole ``knowledge_base.profile`` object."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    profile: KnowledgeProfile | None = None
    calibration: ProbeCalibration | None = None
    refresh: ProfileRefresh | None = None

    def is_empty(self) -> bool:
        return self.profile is None and self.calibration is None and self.refresh is None


def parse_profile_record(raw: Any) -> KnowledgeProfileRecord | None:
    """The stored object, or ``None`` (none stored, or one this version cannot
    read — logged, never raised: the catalog and the probe just do without)."""
    if raw is None:
        return None
    try:
        return KnowledgeProfileRecord.model_validate(raw)
    except ValidationError:
        logger.warning("knowledge.profile_unreadable — treating the base as having no profile")
        return None


def dump_profile_record(record: KnowledgeProfileRecord | None) -> dict[str, Any] | None:
    """What the column stores (``None`` for an empty record)."""
    if record is None or record.is_empty():
        return None
    return record.model_dump(mode="json", exclude_none=True)
