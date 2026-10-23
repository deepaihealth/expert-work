"""B-209 PR-2 §5.4 / §5.5 / §5.8 —— 一份文档的插图描述:先观察再描述、文字大段走识别模型、
图表转数据、小图不抄字、缓存(重新入库不再付费)、兜底 / 失败 / 待描述、记账、租户级重复与
每份上限、原图存储。看图模型与识别模型都是假的(不联网),回答按脚本给出。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge.recognition.clients import Reading, RecognitionCallError
from control_plane.knowledge.recognition.figure_describe import (
    CHART_USER_TEXT,
    DESCRIBE_USER_TEXT,
)
from control_plane.knowledge.recognition.figure_extract import FigureExtraction
from control_plane.knowledge.recognition.figure_filters import FigureCandidate
from control_plane.knowledge.recognition.figure_images import normalize
from control_plane.knowledge.recognition.figure_markup import (
    CHART_LABEL,
    FIGURE_ID_PATTERN,
    TEXT_LABEL,
)
from control_plane.knowledge.recognition.figure_pipeline import FigureDescriber
from control_plane.knowledge.recognition.figure_storage import FigureStorage, figure_id
from control_plane.knowledge.recognition.recognizer import (
    DocumentRecognizer,
    ModelChoice,
    ModelLimits,
    RecognitionSettings,
)
from control_plane.knowledge.usage import IngestUsageRecorder
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.knowledge.base import ClaimedIngestion
from expert_work.persistence.token_usage_store import (
    KNOWLEDGE_INGEST_USAGE_KIND,
    InMemoryTokenUsageStore,
)
from expert_work.protocol import FigureStats
from expert_work.runtime.storage import InMemoryObjectStore
from tests.figure_test_images import noise_png

FIGURE_MODEL = ModelChoice("qwen", "qwen3.8-flash")
FALLBACK = ModelChoice("qwen", "qwen3.7-flash")
OCR_MODEL = ModelChoice("glm", "glm-ocr")

_PHOTO = json.dumps({"text": "none", "chart": False, "description": "一台设备的外观照片。"})
_TEXT_SHOT = json.dumps({"text": "lots", "chart": False, "description": "一张操作说明的截图。"})
_CHART = json.dumps({"text": "little", "chart": True, "description": "四个季度销量的柱状图。"})
_CHART_DATA = "季度销量\n\n| 季度 | 销量 |\n| --- | --- |\n| 一季度 | 120 |"
_OCR_TEXT = "第一步 接通电源\n\n第二步 按下启动键"


def _figure(number: int = 1, *, side: int = 600, digest: str | None = None) -> FigureCandidate:
    return FigureCandidate(
        number=number,
        image=f"jpeg-{number}-{digest}".encode(),
        width=side,
        height=side,
        pixel_hash=digest or f"pixels-{number}",
        pages=(number,),
    )


def _extraction(*figures: FigureCandidate, extracted: int | None = None) -> FigureExtraction:
    return FigureExtraction(
        candidates=tuple(figures), extracted=len(figures) if extracted is None else extracted
    )


@dataclass
class _Vision:
    """Fake vision chat: answers by question (description / chart data)."""

    describe: list[str | Reading | Exception]
    chart: str = _CHART_DATA
    calls: list[tuple[str, str, str]] = field(default_factory=list)  # (model, question, prompt)
    #: An image whose description call blows up (not a vendor error).
    crash_on: bytes | None = None

    def factory(
        self, choice: ModelChoice, api_key: str, prompt: str, user_text: str, max_tokens: int
    ) -> _Client:
        del api_key, max_tokens
        return _Client(self, choice, prompt, user_text)


@dataclass
class _Client:
    vision: _Vision
    choice: ModelChoice
    prompt: str
    user_text: str

    async def read(self, image: bytes) -> Reading:
        if image == self.vision.crash_on:
            raise RuntimeError("unexpected")
        question = "describe" if self.user_text == DESCRIBE_USER_TEXT else "chart"
        assert self.user_text in (DESCRIBE_USER_TEXT, CHART_USER_TEXT)
        self.vision.calls.append((self.choice.model, question, self.prompt))
        if question == "chart":
            return Reading(
                text=self.vision.chart,
                finish_reason="stop",
                input_tokens=700,
                output_tokens=80,
                usage_reported=True,
            )
        answer = (
            self.vision.describe.pop(0)
            if len(self.vision.describe) > 1
            else self.vision.describe[0]
        )
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, Reading):
            return answer
        return Reading(
            text=answer,
            finish_reason="stop",
            input_tokens=750,
            output_tokens=40,
            usage_reported=True,
        )


@dataclass
class _Ocr:
    calls: int = 0

    async def read(self, image: bytes) -> Reading:
        del image
        self.calls += 1
        return Reading(text=_OCR_TEXT, input_tokens=500, output_tokens=60, usage_reported=True)


@dataclass
class _Settings:
    figure: ModelChoice | None = FIGURE_MODEL
    fallback: ModelChoice | None = FALLBACK

    async def recognition_settings(self) -> RecognitionSettings:
        return RecognitionSettings(
            primary=OCR_MODEL, fallback=self.fallback, max_pages=1000, figure=self.figure
        )


@dataclass
class _Keys:
    missing: frozenset[str] = frozenset()

    async def __call__(self, *, tenant_id: UUID, provider: str) -> str:
        del tenant_id
        if provider in self.missing:
            msg = f"no credential for {provider}"
            raise LookupError(msg)
        return f"key-{provider}"


@dataclass
class _Setup:
    store: InMemoryKnowledgeStore
    usage: InMemoryTokenUsageStore
    objects: InMemoryObjectStore
    tenant: UUID
    kb: UUID
    document: UUID
    ocr: _Ocr

    def describer(
        self,
        vision: _Vision,
        *,
        settings: _Settings | None = None,
        keys: _Keys | None = None,
        limits: ModelLimits | None = None,
        store: InMemoryKnowledgeStore | None = None,
    ) -> FigureDescriber:
        recorder = IngestUsageRecorder(self.usage)
        resolved_settings = settings or _Settings()
        resolved_keys = keys or _Keys()
        shared = limits or ModelLimits()
        recognizer = DocumentRecognizer(
            store=store or self.store,
            settings=resolved_settings,
            api_keys=resolved_keys,
            usage=recorder,
            client_factory=lambda choice, key: self.ocr,
            limits=shared,
        )
        return FigureDescriber(
            store=store or self.store,
            settings=resolved_settings,
            api_keys=resolved_keys,
            recognizer=recognizer,
            storage=FigureStorage(self.objects),
            usage=recorder,
            client_factory=vision.factory,
            sleep=_no_sleep,
            limits=shared,
        )

    async def describe(self, describer: FigureDescriber, *figures: FigureCandidate):  # type: ignore[no-untyped-def]
        return await describer.describe(
            tenant_id=self.tenant,
            kb_id=self.kb,
            document_id=self.document,
            extraction=_extraction(*figures),
            language="zh",
        )

    async def billed(self) -> list[tuple[str, str, int, int]]:
        rows = sorted(
            await self.usage.list_for_tenant(tenant_id=self.tenant), key=lambda row: row.id or 0
        )
        assert {row.usage_kind for row in rows} <= {KNOWLEDGE_INGEST_USAGE_KIND}
        return [
            (row.provider or "", row.model, row.input_tokens, row.output_tokens) for row in rows
        ]


async def _no_sleep(*_: object) -> None:
    return None


@pytest.fixture
async def setup() -> _Setup:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="manual.pdf", content=b"pdf"
    )
    return _Setup(
        store,
        InMemoryTokenUsageStore(),
        InMemoryObjectStore(),
        tenant,
        base.id,
        document.id,
        _Ocr(),
    )


def _block_lines(block: str) -> list[str]:
    return block.splitlines()


# ---------------------------------------------------------------------------
# observe, then describe
# ---------------------------------------------------------------------------


async def test_a_photo_is_described_in_one_call_and_billed(setup: _Setup) -> None:
    vision = _Vision([_PHOTO])
    outcome = await setup.describe(setup.describer(vision), _figure())
    block = outcome.blocks[1]
    assert "一台设备的外观照片。" in _block_lines(block)[0]
    assert TEXT_LABEL not in block and CHART_LABEL not in block
    assert [question for _, question, _ in vision.calls] == ["describe"]
    assert setup.ocr.calls == 0
    assert outcome.stats == FigureStats(extracted=1, described=1)
    assert outcome.kept_pages == {1: 1} and outcome.described_pages == {1: 1}
    assert await setup.billed() == [("qwen", "qwen3.8-flash", 750, 40)]


async def test_lots_of_text_is_read_by_the_recognition_model(setup: _Setup) -> None:
    vision = _Vision([_TEXT_SHOT])
    outcome = await setup.describe(setup.describer(vision), _figure())
    lines = _block_lines(outcome.blocks[1])
    assert lines[2] == f"> {TEXT_LABEL}"
    assert "> 第一步 接通电源" in lines and "> 第二步 按下启动键" in lines
    assert setup.ocr.calls == 1
    assert [question for _, question, _ in vision.calls] == ["describe"]
    billed = await setup.billed()
    assert ("glm", "glm-ocr", 500, 60) in billed and len(billed) == 2
    cached = {key[2] for key in setup.store._parse_cache}
    assert cached == {"figure_desc", "figure_ocr"}


async def test_a_chart_is_transcribed_as_data_by_the_describe_model(setup: _Setup) -> None:
    vision = _Vision([_CHART])
    outcome = await setup.describe(setup.describer(vision), _figure())
    block = outcome.blocks[1]
    assert f"> {CHART_LABEL}" in _block_lines(block)
    assert "> | 一季度 | 120 |" in _block_lines(block)
    assert [question for _, question, _ in vision.calls] == ["describe", "chart"]
    assert setup.ocr.calls == 0


@pytest.mark.parametrize("answer", [_TEXT_SHOT, _CHART])
async def test_a_small_image_is_described_but_its_text_never_transcribed(
    setup: _Setup, answer: str
) -> None:
    vision = _Vision([answer])
    outcome = await setup.describe(setup.describer(vision), _figure(side=399))
    block = outcome.blocks[1]
    assert TEXT_LABEL not in block and CHART_LABEL not in block
    assert [question for _, question, _ in vision.calls] == ["describe"]
    assert "不要写出任何具体的文字" in vision.calls[0][2]  # the small-image prompt
    assert setup.ocr.calls == 0


async def test_an_image_of_400_pixels_gets_its_text_read(setup: _Setup) -> None:
    vision = _Vision([_TEXT_SHOT])
    await setup.describe(setup.describer(vision), _figure(side=400))
    assert setup.ocr.calls == 1
    assert "不要写出任何具体的文字" not in vision.calls[0][2]


# ---------------------------------------------------------------------------
# cache: the same document ingested again pays nothing
# ---------------------------------------------------------------------------


async def test_re_ingesting_the_document_reuses_every_answer(setup: _Setup) -> None:
    vision = _Vision([_TEXT_SHOT])
    first = await setup.describe(setup.describer(vision), _figure(1), _figure(2, digest="b"))
    billed = await setup.billed()
    calls, ocr_calls = len(vision.calls), setup.ocr.calls
    again = _Vision([RecognitionCallError("must not be called", kind="rejected")])
    second = await setup.describe(setup.describer(again), _figure(1), _figure(2, digest="b"))
    assert second.blocks == first.blocks
    assert again.calls == [] and setup.ocr.calls == ocr_calls
    assert await setup.billed() == billed
    assert calls == 2 and ocr_calls == 2


async def test_a_cached_answer_the_rules_now_reject_is_asked_again(setup: _Setup) -> None:
    vision = _Vision([_PHOTO])
    await setup.describe(setup.describer(vision), _figure())
    for key, entry in list(setup.store._parse_cache.items()):
        setup.store._parse_cache[key] = type(entry)(model=entry.model, result={"raw": "?"})
    again = _Vision([_PHOTO])
    outcome = await setup.describe(setup.describer(again), _figure())
    assert len(again.calls) == 1 and outcome.stats.described == 1


# ---------------------------------------------------------------------------
# fallback, failure, waiting
# ---------------------------------------------------------------------------


async def test_a_bad_answer_falls_back_to_the_fallback_model(setup: _Setup) -> None:
    truncated = Reading(
        text=_PHOTO,
        finish_reason="length",
        input_tokens=750,
        output_tokens=1024,
        usage_reported=True,
    )
    vision = _Vision([truncated, _PHOTO])
    outcome = await setup.describe(setup.describer(vision), _figure())
    assert outcome.stats.described == 1
    assert [model for model, _, _ in vision.calls] == ["qwen3.8-flash", "qwen3.7-flash"]
    # The vendor charged for the bad answer too.
    assert await setup.billed() == [
        ("qwen", "qwen3.8-flash", 750, 1024),
        ("qwen", "qwen3.7-flash", 750, 40),
    ]


async def test_two_bad_answers_fail_the_figure_without_a_block(setup: _Setup) -> None:
    vision = _Vision(["看不清。"])
    outcome = await setup.describe(setup.describer(vision), _figure())
    assert outcome.blocks == {}
    assert outcome.stats.failed == 1 and outcome.stats.described == 0
    assert len(vision.calls) == 2


async def test_no_figure_model_leaves_figures_waiting(setup: _Setup) -> None:
    vision = _Vision([_PHOTO])
    settings = _Settings(figure=None, fallback=None)
    outcome = await setup.describe(setup.describer(vision, settings=settings), _figure())
    assert outcome.stats.pending == 1 and vision.calls == []


async def test_without_a_figure_model_the_fallback_is_never_asked(setup: _Setup) -> None:
    # Review H2: the figure model off means description off — the fallback
    # only ever stands behind a configured figure model.
    vision = _Vision([_PHOTO])
    describer = setup.describer(vision, settings=_Settings(figure=None, fallback=FALLBACK))
    outcome = await setup.describe(describer, _figure())
    assert vision.calls == [] and outcome.stats.pending == 1
    assert await describer.enabled(tenant_id=setup.tenant) is False
    assert await setup.describer(vision).enabled(tenant_id=setup.tenant) is True
    no_key = setup.describer(vision, keys=_Keys(missing=frozenset({"qwen"})))
    assert await no_key.enabled(tenant_id=setup.tenant) is False


async def test_a_layout_parsing_fallback_is_never_asked_to_describe(setup: _Setup) -> None:
    vision = _Vision(["看不清。"])
    settings = _Settings(fallback=OCR_MODEL)
    outcome = await setup.describe(setup.describer(vision, settings=settings), _figure())
    assert [model for model, _, _ in vision.calls] == ["qwen3.8-flash"]
    assert outcome.stats.failed == 1


async def test_no_key_leaves_figures_waiting(setup: _Setup) -> None:
    vision = _Vision([_PHOTO])
    outcome = await setup.describe(
        setup.describer(vision, keys=_Keys(missing=frozenset({"qwen"}))), _figure()
    )
    assert outcome.stats.pending == 1 and vision.calls == []


async def test_a_vendor_out_of_retries_leaves_the_figure_waiting(setup: _Setup) -> None:
    vision = _Vision([RecognitionCallError("429 busy", kind="busy")])
    outcome = await setup.describe(setup.describer(vision), _figure())
    assert outcome.stats.pending == 1
    assert len(vision.calls) == 2 * 4  # both models, 1 + 3 retries each


async def test_one_figure_going_wrong_never_stops_the_others(setup: _Setup) -> None:
    vision = _Vision([_PHOTO], crash_on=_figure(1).image)
    outcome = await setup.describe(setup.describer(vision), _figure(1), _figure(2, digest="b"))
    assert set(outcome.blocks) == {2}
    assert (outcome.stats.failed, outcome.stats.described) == (1, 1)


# ---------------------------------------------------------------------------
# tenant-wide repeats, the per-document cap, originals
# ---------------------------------------------------------------------------


async def test_an_image_in_three_of_the_tenants_documents_is_not_described(
    setup: _Setup,
) -> None:
    for name in ("a.pdf", "b.pdf"):
        other = await setup.store.upsert_document(
            tenant_id=setup.tenant, kb_id=setup.kb, filename=name, content=name.encode()
        )
        await setup.store.set_document_figures(
            tenant_id=setup.tenant,
            document_id=other.id,
            stats=FigureStats(),
            pixel_hashes=["letterhead"],
        )
    vision = _Vision([_PHOTO])
    outcome = await setup.describe(
        setup.describer(vision), _figure(1, digest="letterhead"), _figure(2, digest="photo")
    )
    assert set(outcome.blocks) == {2}
    assert outcome.stats.filtered == 1 and outcome.stats.described == 1
    assert len(vision.calls) == 1


async def test_at_most_fifty_figures_are_described(setup: _Setup) -> None:
    figures = [_figure(n, side=400 + n) for n in range(1, 53)]
    vision = _Vision([_PHOTO])
    outcome = await setup.describe(setup.describer(vision), *figures)
    assert outcome.stats.capped == 2 and outcome.stats.described == 50
    assert 1 not in outcome.blocks and 2 not in outcome.blocks


async def test_a_described_figure_hands_back_its_original_under_its_id(setup: _Setup) -> None:
    # Security review S-M2: the describer does not store; the caller stores
    # after checking the document is still its own.
    figure = _figure()
    outcome = await setup.describe(setup.describer(_Vision([_PHOTO])), figure)
    identifier = FIGURE_ID_PATTERN.search(outcome.blocks[1]).group(1)  # type: ignore[union-attr]
    assert identifier == figure_id(figure.image)
    assert outcome.originals == {identifier: figure.image}
    assert await setup.objects.list_prefix("") == []


async def test_a_failed_figure_has_no_original_to_store(setup: _Setup) -> None:
    outcome = await setup.describe(setup.describer(_Vision(["看不清。"])), _figure())
    assert outcome.originals == {}


# ---------------------------------------------------------------------------
# review follow-ups
# ---------------------------------------------------------------------------


async def test_a_rejected_cached_answer_is_replaced_so_a_third_run_pays_nothing(
    setup: _Setup,
) -> None:
    # Review M2: the fresh answer overwrites the entry the rules rejected.
    await setup.describe(setup.describer(_Vision([_PHOTO])), _figure())
    for key, entry in list(setup.store._parse_cache.items()):
        setup.store._parse_cache[key] = type(entry)(model=entry.model, result={"raw": "?"})
    second = _Vision([_PHOTO])
    await setup.describe(setup.describer(second), _figure())
    third = _Vision([RecognitionCallError("must not be called", kind="rejected")])
    outcome = await setup.describe(setup.describer(third), _figure())
    assert len(second.calls) == 1 and third.calls == []
    assert outcome.stats.described == 1


async def test_figures_and_pages_share_one_set_of_model_slots(setup: _Setup) -> None:
    # Review M4: one registry — the figure describer and the recognizer draw
    # from the same semaphore for a model.
    taken: list[tuple[str, int]] = []

    class _Recording(ModelLimits):
        def slot(self, choice: ModelChoice, size: int) -> asyncio.Semaphore:
            taken.append((choice.key, size))
            return super().slot(choice, size)

    limits = _Recording()
    await setup.describe(setup.describer(_Vision([_TEXT_SHOT]), limits=limits), _figure())
    assert ("qwen/qwen3.8-flash", 4) in taken and ("glm/glm-ocr", 2) in taken
    assert limits.slot(FIGURE_MODEL, 4) is limits.slot(FIGURE_MODEL, 99)


async def test_small_is_decided_by_the_image_sent_not_the_source(setup: _Setup) -> None:
    # Review L1: a 3000 x 500 picture is sent as 2000 x 333 — too small to
    # transcribe, though its source's short side is 500.
    image = normalize(noise_png(3000, 500))
    assert image is not None
    figure = FigureCandidate(
        number=1, image=image.jpeg, width=3000, height=500, pixel_hash=image.pixel_hash
    )
    vision = _Vision([_TEXT_SHOT])
    outcome = await setup.describe(setup.describer(vision), figure)
    assert "不要写出任何具体的文字" in vision.calls[0][2]
    assert setup.ocr.calls == 0 and TEXT_LABEL not in outcome.blocks[1]


async def test_an_answer_without_usage_is_logged(
    setup: _Setup, caplog: pytest.LogCaptureFixture
) -> None:
    # Review L2: the same warning the recognizer gives.
    silent = Reading(text=_PHOTO, finish_reason="stop")
    with caplog.at_level("WARNING"):
        await setup.describe(setup.describer(_Vision([silent])), _figure())
    assert "knowledge.figure_usage_missing model=qwen/qwen3.8-flash" in caplog.text


async def test_cache_reads_are_batched_per_step(setup: _Setup) -> None:
    # Review L6: describing five figures reads the cache once per step, not
    # once per figure.
    reads: list[str] = []

    class _Counting(InMemoryKnowledgeStore):
        async def get_parse_cache(self, **kwargs: Any) -> Any:
            reads.append(kwargs["kind"])
            return await super().get_parse_cache(**kwargs)

    store = _Counting()
    base = await store.create_base(tenant_id=setup.tenant, name="kb")
    document = await store.upsert_document(
        tenant_id=setup.tenant, kb_id=base.id, filename="m.pdf", content=b"x"
    )
    describer = setup.describer(_Vision([_TEXT_SHOT]), store=store)
    figures = [_figure(n, digest=str(n)) for n in range(1, 6)]
    await describer.describe(
        tenant_id=setup.tenant,
        kb_id=base.id,
        document_id=document.id,
        extraction=_extraction(*figures),
        language="zh",
    )
    assert reads == ["figure_desc", "figure_ocr"]


async def test_originals_are_stored_under_the_ingest_generation(setup: _Setup) -> None:
    describer = setup.describer(_Vision([_PHOTO]))
    await describer.store_originals(
        tenant_id=setup.tenant,
        kb_id=setup.kb,
        document_id=setup.document,
        generation=3,
        originals={"0123456789abcdef": b"jpeg"},
    )
    (key,) = await setup.objects.list_prefix("")
    assert key == (
        f"knowledge_figures/{setup.tenant}/{setup.kb}/{setup.document}/g3/0123456789abcdef.jpg"
    )


def _claim(setup: _Setup, generation: int) -> ClaimedIngestion:
    return ClaimedIngestion(
        tenant_id=setup.tenant,
        document_id=setup.document,
        kb_id=setup.kb,
        filename="manual.pdf",
        content=b"pdf",
        content_sha256=None,
        chunk_max_tokens=512,
        chunk_overlap_tokens=0,
        attempts=1,
        generation=generation,
    )


async def _store_generations(setup: _Setup, *generations: int) -> None:
    storage = FigureStorage(setup.objects)
    for generation in generations:
        await storage.put(
            tenant_id=setup.tenant,
            kb_id=setup.kb,
            document_id=setup.document,
            generation=generation,
            figure="0123456789abcdef",
            image=b"jpeg",
        )


async def _generations(setup: _Setup) -> list[str]:
    return [key.split("/")[-2] for key in await setup.objects.list_prefix("")]


async def test_a_settled_ingest_drops_older_generations_only(setup: _Setup) -> None:
    # Review H5 (c): re-ingested with other pictures — the old originals go;
    # a newer generation (a re-upload already ingesting) is left alone.
    await _store_generations(setup, 1, 2, 3)
    await setup.describer(_Vision([_PHOTO])).tidy(_claim(setup, 2), settled=True)
    assert await _generations(setup) == ["g2", "g3"]


async def test_an_abandoned_ingest_drops_what_it_wrote(setup: _Setup) -> None:
    # Review H5 (a): re-uploaded mid-ingest (the generation moved on).
    await _store_generations(setup, 0, 1)
    document = await setup.store.upsert_document(
        tenant_id=setup.tenant, kb_id=setup.kb, filename="manual.pdf", content=b"v2", force=True
    )
    assert document.ingest_generation == 1
    await setup.describer(_Vision([_PHOTO])).tidy(_claim(setup, 0), settled=False)
    assert await _generations(setup) == ["g1"]


async def test_an_ingest_still_running_elsewhere_keeps_its_originals(setup: _Setup) -> None:
    # Same generation, still processing (another worker took the lease over).
    await _store_generations(setup, 0)
    await setup.describer(_Vision([_PHOTO])).tidy(_claim(setup, 0), settled=False)
    assert await _generations(setup) == ["g0"]


async def test_a_deleted_document_loses_every_original(setup: _Setup) -> None:
    await _store_generations(setup, 0, 1)
    await setup.store.delete_document(tenant_id=setup.tenant, document_id=setup.document)
    await setup.describer(_Vision([_PHOTO])).tidy(_claim(setup, 1), settled=False)
    assert await setup.objects.list_prefix("") == []
