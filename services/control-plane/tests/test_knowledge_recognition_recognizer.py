"""B-209 §5.3 / §5.5 / §5.6 —— 一份文档的扫描页识别:缓存、主模型 → 兜底 → 失败 / 待识别、
按模型限并发、记账、给租户看的错误类。识别客户端是假的(不联网),回答按 ``Reading`` 脚本给出。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from prometheus_client import REGISTRY

from control_plane.knowledge.recognition.cleaning import NO_TEXT_SENTENCE
from control_plane.knowledge.recognition.clients import (
    ChatVlmClient,
    LayoutParsingClient,
    Reading,
    RecognitionCallError,
    RecognitionClient,
)
from control_plane.knowledge.recognition.figure_markup import resolve_figures
from control_plane.knowledge.recognition.recognizer import (
    ERROR_CLASSES,
    DocumentRecognizer,
    ModelChoice,
    PageOutcome,
    RecognitionSettings,
    default_client,
    reader_digest,
)
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.knowledge.base import PAGE_OCR_PAGE, PAGE_OCR_READER, PAGE_OCR_SOURCE
from expert_work.protocol import PageStatus

GLM_OCR = ModelChoice("glm", "glm-ocr")
QWEN_FLASH = ModelChoice("qwen", "qwen3.7-flash")
_RENDER = "dpi=200;edge=2000;jpeg=85"

_PAGE_TEXT = "## 第一章 总则\n\n本制度适用于全体员工,自发布之日起施行。"
_GOOD = Reading(text=_PAGE_TEXT, input_tokens=1200, output_tokens=200, usage_reported=True)
_EMPTY = Reading(text="", input_tokens=1100, output_tokens=0, usage_reported=True)
_TRUNCATED = Reading(
    text=_PAGE_TEXT,
    finish_reason="length",
    input_tokens=1400,
    output_tokens=8192,
    usage_reported=True,
)
_VENDOR_SECRET_TEXT = "403 invalid api key sk-live-abc for account acme-health"


@dataclass
class _Settings:
    value: RecognitionSettings

    async def recognition_settings(self) -> RecognitionSettings:
        return self.value


@dataclass
class _Keys:
    known: frozenset[str] = frozenset({"glm", "qwen"})

    async def __call__(self, *, tenant_id: UUID, provider: str) -> str:
        del tenant_id
        if provider not in self.known:
            msg = (
                f"no platform credential for provider {provider!r} at secret://platform/{provider}"
            )
            raise LookupError(msg)
        return f"key-{provider}"


@dataclass
class _Usage:
    rows: list[tuple[UUID, str, str, int, int]] = field(default_factory=list)

    async def record(
        self,
        *,
        tenant_id: UUID,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        self.rows.append((tenant_id, provider, model, input_tokens, output_tokens))


class _Fake:
    """Answers each call with the next scripted reading / error (the last
    one repeats)."""

    def __init__(
        self, *script: Reading | BaseException | RecognitionCallError, delay: float = 0.0
    ) -> None:
        self._script = list(script)
        self._delay = delay
        self.calls = 0
        self.in_flight = 0
        self.peak = 0
        self.finished = 0

    async def read(self, image: bytes) -> Reading:
        del image
        self.calls += 1
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            step = self._script[min(self.calls, len(self._script)) - 1]
            if isinstance(step, BaseException):
                raise step
            self.finished += 1
            return step
        finally:
            self.in_flight -= 1


@dataclass
class _Doc:
    store: InMemoryKnowledgeStore
    tenant: UUID
    document: UUID


async def _doc(store: InMemoryKnowledgeStore | None = None, tenant: UUID | None = None) -> _Doc:
    store = store or InMemoryKnowledgeStore()
    tenant = tenant or uuid4()
    base = await store.get_base(tenant_id=tenant, name="kb") or await store.create_base(
        tenant_id=tenant, name="kb"
    )
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename=f"{uuid4().hex}.pdf", content=b"%PDF"
    )
    return _Doc(store, tenant, document.id)


def _recognizer(
    doc: _Doc,
    *,
    clients: dict[ModelChoice, _Fake],
    primary: ModelChoice | None = GLM_OCR,
    fallback: ModelChoice | None = QWEN_FLASH,
    keys: _Keys | None = None,
    usage: _Usage | None = None,
    max_pages: int = 1000,
) -> DocumentRecognizer:
    async def no_sleep(delay: float) -> None:
        """Skip the backoff wait."""

    def factory(choice: ModelChoice, api_key: str) -> RecognitionClient:
        assert api_key == f"key-{choice.provider}"
        return clients[choice]

    return DocumentRecognizer(
        store=doc.store,
        settings=_Settings(
            RecognitionSettings(primary=primary, fallback=fallback, max_pages=max_pages)
        ),
        api_keys=keys or _Keys(),
        usage=usage,
        client_factory=factory,
        sleep=no_sleep,
    )


async def _recognize(
    recognizer: DocumentRecognizer,
    doc: _Doc,
    images: Sequence[bytes] = (b"page-1",),
    render: str = _RENDER,
) -> list[PageOutcome]:
    plan = await recognizer.plan(tenant_id=doc.tenant)
    outcomes = await recognizer.recognize(
        tenant_id=doc.tenant,
        document_id=doc.document,
        plan=plan,
        images=dict(enumerate(images)),
        render=render,
    )
    return [outcomes[index] for index in range(len(images))]


def _assert_classes_only(error: str | None) -> None:
    """Review L2: ``model: class`` pairs, nothing a vendor or resolver wrote."""
    assert error is not None
    for part in error.split("; "):
        _, _, cls = part.rpartition(": ")
        assert cls.split("(")[0] in ERROR_CLASSES, part


async def test_primary_reading_is_kept_cleaned_and_billed() -> None:
    fenced = Reading(
        text=f"```\n{_PAGE_TEXT}\n```", input_tokens=9, output_tokens=3, usage_reported=True
    )
    primary, fallback, usage, doc = _Fake(fenced), _Fake(), _Usage(), await _doc()
    (outcome,) = await _recognize(
        _recognizer(doc, clients={GLM_OCR: primary, QWEN_FLASH: fallback}, usage=usage), doc
    )
    assert outcome.status is PageStatus.RECOGNIZED
    assert outcome.text == _PAGE_TEXT
    assert (outcome.provider, outcome.model, outcome.cached) == ("glm", "glm-ocr", False)
    assert fallback.calls == 0
    assert usage.rows == [(doc.tenant, "glm", "glm-ocr", 9, 3)]


@pytest.mark.parametrize("bad", [_EMPTY, _TRUNCATED, Reading(text="血压正常。" * 100)])
async def test_a_bad_primary_reading_goes_to_the_fallback(bad: Reading) -> None:
    primary, fallback, usage, doc = _Fake(bad), _Fake(_GOOD), _Usage(), await _doc()
    (outcome,) = await _recognize(
        _recognizer(doc, clients={GLM_OCR: primary, QWEN_FLASH: fallback}, usage=usage), doc
    )
    assert outcome.status is PageStatus.RECOGNIZED
    assert outcome.model == "qwen3.7-flash"
    if bad.input_tokens:
        # The vendor charged for the bad reading too: both calls are billed.
        assert [row[2] for row in usage.rows] == ["glm-ocr", "qwen3.7-flash"]


async def test_both_bad_marks_the_page_failed_with_both_reasons() -> None:
    doc = await _doc()
    clients = {GLM_OCR: _Fake(_EMPTY), QWEN_FLASH: _Fake(_TRUNCATED)}
    (outcome,) = await _recognize(_recognizer(doc, clients=clients), doc)
    assert outcome.status is PageStatus.FAILED
    assert outcome.text == ""
    assert outcome.error == (
        "glm/glm-ocr: bad_output(empty); qwen/qwen3.7-flash: bad_output(truncated)"
    )


async def test_an_oversized_raw_reading_is_failed_without_cleaning() -> None:
    doc = await _doc()
    huge = Reading(text="<table>" + "<td colspan=20>x</td>" * 4000 + "</table>")
    clients = {GLM_OCR: _Fake(huge), QWEN_FLASH: _Fake(huge)}
    (outcome,) = await _recognize(_recognizer(doc, clients=clients), doc)
    assert outcome.status is PageStatus.FAILED
    assert outcome.error == (
        "glm/glm-ocr: bad_output(oversized); qwen/qwen3.7-flash: bad_output(oversized)"
    )


async def test_no_text_reply_is_a_valid_empty_page() -> None:
    doc = await _doc()
    clients = {GLM_OCR: _Fake(Reading(text=NO_TEXT_SENTENCE)), QWEN_FLASH: _Fake(_GOOD)}
    (outcome,) = await _recognize(_recognizer(doc, clients=clients), doc)
    assert outcome.status is PageStatus.RECOGNIZED
    assert outcome.text == ""
    assert clients[QWEN_FLASH].calls == 0


async def test_missing_primary_key_falls_back() -> None:
    doc = await _doc()
    clients = {GLM_OCR: _Fake(_GOOD), QWEN_FLASH: _Fake(_GOOD)}
    recognizer = _recognizer(doc, clients=clients, keys=_Keys(frozenset({"qwen"})))
    (outcome,) = await _recognize(recognizer, doc)
    assert outcome.model == "qwen3.7-flash"
    assert clients[GLM_OCR].calls == 0


async def test_no_usable_model_leaves_the_page_pending_with_classes_only() -> None:
    doc = await _doc()
    clients = {GLM_OCR: _Fake(_GOOD), QWEN_FLASH: _Fake(_GOOD)}
    recognizer = _recognizer(doc, clients=clients, keys=_Keys(frozenset()))
    plan = await recognizer.plan(tenant_id=doc.tenant)
    assert not plan.usable
    (outcome,) = await _recognize(recognizer, doc)
    assert outcome.status is PageStatus.PENDING
    assert outcome.error == (
        "glm/glm-ocr: credential_missing; qwen/qwen3.7-flash: credential_missing"
    )
    assert "secret://" not in outcome.error
    assert clients[GLM_OCR].calls == clients[QWEN_FLASH].calls == 0


async def test_a_vendor_out_of_service_leaves_the_page_pending() -> None:
    # Load errors past the retries + an account error: nothing was wrong with
    # the page — it waits for 「补识别」.
    doc = await _doc()
    busy = RecognitionCallError("503 overloaded", kind="busy")
    clients = {
        GLM_OCR: _Fake(busy),
        QWEN_FLASH: _Fake(RecognitionCallError(_VENDOR_SECRET_TEXT, kind="auth")),
    }
    (outcome,) = await _recognize(_recognizer(doc, clients=clients), doc)
    assert outcome.status is PageStatus.PENDING
    assert clients[GLM_OCR].calls == 4  # one call + three retries
    assert outcome.error == "glm/glm-ocr: vendor_unavailable; qwen/qwen3.7-flash: vendor_auth"


async def test_a_rejected_request_fails_the_page_and_vendor_text_stays_in_logs() -> None:
    doc = await _doc()
    clients = {
        GLM_OCR: _Fake(RecognitionCallError(_VENDOR_SECRET_TEXT, kind="rejected")),
        QWEN_FLASH: _Fake(RecognitionCallError(_VENDOR_SECRET_TEXT, kind="quota")),
    }
    (outcome,) = await _recognize(_recognizer(doc, clients=clients), doc)
    assert outcome.status is PageStatus.FAILED
    assert outcome.error == "glm/glm-ocr: vendor_rejected; qwen/qwen3.7-flash: vendor_quota"
    _assert_classes_only(outcome.error)


async def test_a_malformed_answer_is_billed_and_falls_back() -> None:
    # Review #6: the usage read before the shape check is billed.
    doc, usage = await _doc(), _Usage()
    malformed = RecognitionCallError(
        "no md_results", kind="bad_response", input_tokens=700, output_tokens=30
    )
    clients = {GLM_OCR: _Fake(malformed), QWEN_FLASH: _Fake(_GOOD)}
    (outcome,) = await _recognize(_recognizer(doc, clients=clients, usage=usage), doc)
    assert outcome.model == "qwen3.7-flash"
    assert usage.rows[0] == (doc.tenant, "glm", "glm-ocr", 700, 30)


# -- B-237: why a page fell back -------------------------------------------------------


def _fallback_counts() -> dict[str, float]:
    return {
        sample.labels["reason"]: sample.value
        for metric in REGISTRY.collect()
        if metric.name == "expert_work_knowledge_recognition_fallback"
        for sample in metric.samples
        if sample.name.endswith("_total")
    }


def _fallback_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("knowledge.recognition_fallback")
    ]


@pytest.mark.parametrize(
    ("primary", "keys", "reason"),
    [
        (
            RecognitionCallError("429 slow down", kind="busy", cause="rate_limited"),
            None,
            "vendor_unavailable_rate_limited",
        ),
        (RecognitionCallError("400 bad image", kind="rejected"), None, "vendor_rejected"),
        (_TRUNCATED, None, "bad_output_truncated"),
        (_GOOD, _Keys(frozenset({"qwen"})), "credential_missing"),
    ],
)
async def test_a_fallback_is_logged_and_counted_with_why_the_primary_was_not_used(
    caplog: pytest.LogCaptureFixture,
    primary: Reading | RecognitionCallError,
    keys: _Keys | None,
    reason: str,
) -> None:
    caplog.set_level(logging.INFO)
    doc = await _doc()
    # Page 1 is read by the primary; page 2 falls back.
    clients = {GLM_OCR: _Fake(_GOOD, primary), QWEN_FLASH: _Fake(_GOOD)}
    if keys is not None:
        clients[GLM_OCR] = _Fake(_GOOD)
    before = _fallback_counts()
    outcomes = await _recognize(
        _recognizer(doc, clients=clients, keys=keys), doc, images=(b"page-1", b"page-2")
    )
    after = _fallback_counts()
    assert [o.model for o in outcomes][-1] == "qwen3.7-flash"
    expected_pages = 2 if keys is not None else 1  # no primary key: every page falls back
    assert after.get(reason, 0.0) - before.get(reason, 0.0) == expected_pages
    assert sum(after.values()) - sum(before.values()) == expected_pages
    lines = _fallback_lines(caplog)
    assert len(lines) == expected_pages
    assert lines[-1] == (
        f"knowledge.recognition_fallback document={doc.document} reading=page_ocr index=1 "
        f"model=qwen/qwen3.7-flash reasons=glm/glm-ocr: {reason}"
    )
    assert _PAGE_TEXT not in caplog.text


async def test_a_failed_call_is_logged_with_its_cause_and_the_page(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # B-237: the warning matches the fallback line (document, page).
    doc = await _doc()
    timeout = RecognitionCallError("ReadTimeout: timed out", kind="busy", cause="timeout")
    clients = {GLM_OCR: _Fake(_GOOD, timeout), QWEN_FLASH: _Fake(_GOOD)}
    await _recognize(_recognizer(doc, clients=clients), doc, images=(b"page-1", b"page-2"))
    failed = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("knowledge.recognition_call_failed")
    ]
    assert failed == [
        "knowledge.recognition_call_failed model=glm/glm-ocr kind=busy cause=timeout "
        f"document={doc.document} reading=page_ocr index=1 detail=ReadTimeout: timed out"
    ]


async def test_no_fallback_no_log_and_no_count(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    doc = await _doc()
    clients = {GLM_OCR: _Fake(_GOOD), QWEN_FLASH: _Fake(_GOOD)}
    recognizer = _recognizer(doc, clients=clients)
    before = _fallback_counts()
    (outcome,) = await _recognize(recognizer, doc)
    assert outcome.model == "glm-ocr"
    assert _fallback_lines(caplog) == []
    assert _fallback_counts() == before


async def test_a_fallback_reading_from_the_cache_is_not_logged_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The fallback was logged when it happened; a re-ingest reads its cached
    # reading without asking the primary — nothing fell back this time.
    caplog.set_level(logging.INFO)
    doc = await _doc()
    clients = {GLM_OCR: _Fake(_EMPTY), QWEN_FLASH: _Fake(_GOOD)}
    recognizer = _recognizer(doc, clients=clients)
    await _recognize(recognizer, doc)
    assert len(_fallback_lines(caplog)) == 1
    before = _fallback_counts()
    (outcome,) = await _recognize(recognizer, doc)
    assert outcome.cached and outcome.model == "qwen3.7-flash"
    assert len(_fallback_lines(caplog)) == 1
    assert _fallback_counts() == before


async def test_a_recognized_page_is_never_billed_twice() -> None:
    doc, usage = await _doc(), _Usage()
    clients = {GLM_OCR: _Fake(_EMPTY), QWEN_FLASH: _Fake(_GOOD)}
    recognizer = _recognizer(doc, clients=clients, usage=usage)
    await _recognize(recognizer, doc)
    assert len(usage.rows) == 2
    # Again (re-ingest / 「补识别」 / re-chunk of the same document): the
    # fallback's reading is in the cache — the primary is not asked again.
    (outcome,) = await _recognize(recognizer, doc)
    assert outcome.cached is True
    assert outcome.model == "qwen3.7-flash"
    assert outcome.text == _PAGE_TEXT
    assert clients[GLM_OCR].calls == clients[QWEN_FLASH].calls == 1
    assert len(usage.rows) == 2


async def test_the_cache_is_per_document() -> None:
    # Review M1: the same page in another document (same tenant or not) is
    # read again — deleting a document must be able to take its text with it.
    first = await _doc()
    second = await _doc(first.store, first.tenant)
    clients = {GLM_OCR: _Fake(_GOOD), QWEN_FLASH: _Fake(_GOOD)}
    recognizer = _recognizer(first, clients=clients)
    await _recognize(recognizer, first)
    await _recognize(recognizer, second)
    assert clients[GLM_OCR].calls == 2
    await first.store.delete_document(tenant_id=first.tenant, document_id=first.document)
    third = await _doc(first.store, first.tenant)
    await _recognize(recognizer, third)
    assert clients[GLM_OCR].calls == 3


async def test_a_cached_reading_is_cleaned_under_the_current_rules() -> None:
    # The cache holds the raw answer: a cleaning-rule fix reaches cached pages.
    doc = await _doc()
    raw = Reading(text="## 第一章\n\n![](page=0,bbox=[1, 2, 3, 4])\n\n正文")
    clients = {GLM_OCR: _Fake(raw), QWEN_FLASH: _Fake(_GOOD)}
    recognizer = _recognizer(doc, clients=clients)
    page = _jpeg(10, 20)
    await _recognize(recognizer, doc, images=(page,))
    stored = next(iter(doc.store._parse_cache.values()))
    assert stored.result["raw"] == raw.text
    (outcome,) = await _recognize(recognizer, doc, images=(page,))
    assert outcome.cached is True
    # B-209 PR-2: the image region stays in place as a placeholder (its box,
    # relative to the page image, alongside) for the figure step; without
    # figures it resolves to nothing.
    assert resolve_figures(outcome.text, {}) == "## 第一章\n\n正文"
    assert outcome.regions == ((0.1, 0.1, 0.3, 0.2),)


def _jpeg(width: int, height: int) -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), (255, 255, 255)).save(out, format="JPEG")
    return out.getvalue()


async def test_regions_of_an_image_that_cannot_be_measured_are_dropped() -> None:
    # B-209 PR-2: boxes are only meaningful against the image read; without
    # its size they are left out (and leave no placeholder behind).
    doc = await _doc()
    raw = Reading(text="## 第一章\n\n![](page=0,bbox=[1, 2, 3, 4])\n\n正文")
    recognizer = _recognizer(doc, clients={GLM_OCR: _Fake(raw), QWEN_FLASH: _Fake(_GOOD)})
    (outcome,) = await _recognize(recognizer, doc, images=(b"not-an-image",))
    assert outcome.regions == ()
    assert outcome.text == "## 第一章\n\n正文"


async def test_regions_are_measured_in_the_frame_the_vendor_reports() -> None:
    # Review H4: a box is relative to the page size the answer reports, also
    # when it comes back from the cache.
    doc = await _doc()
    raw = Reading(
        text="正文\n\n![](page=0,bbox=[100, 50, 300, 150])\n\n更多正文", layout_size=(400, 200)
    )
    recognizer = _recognizer(doc, clients={GLM_OCR: _Fake(raw), QWEN_FLASH: _Fake(_GOOD)})
    page = _jpeg(800, 400)
    (fresh,) = await _recognize(recognizer, doc, images=(page,))
    (cached,) = await _recognize(recognizer, doc, images=(page,))
    assert cached.cached is True
    assert fresh.regions == cached.regions == ((0.25, 0.25, 0.75, 0.75),)


async def test_a_cached_reading_the_rules_now_reject_is_read_again() -> None:
    doc = await _doc()
    clients = {GLM_OCR: _Fake(_GOOD), QWEN_FLASH: _Fake(_GOOD)}
    recognizer = _recognizer(doc, clients=clients)
    await _recognize(recognizer, doc)
    key = next(iter(doc.store._parse_cache))
    entry = doc.store._parse_cache[key]
    doc.store._parse_cache[key] = type(entry)(model=entry.model, result={"raw": ""})
    (outcome,) = await _recognize(recognizer, doc)
    assert outcome.cached is False
    assert clients[GLM_OCR].calls == 2
    # B-209 PR-2 (review M2): the fresh reading replaced the rejected one —
    # a third run reads the cache.
    (third,) = await _recognize(recognizer, doc)
    assert third.cached is True
    assert clients[GLM_OCR].calls == 2


async def test_a_different_rendering_is_read_again() -> None:
    doc = await _doc()
    clients = {GLM_OCR: _Fake(_GOOD), QWEN_FLASH: _Fake(_GOOD)}
    recognizer = _recognizer(doc, clients=clients)
    await _recognize(recognizer, doc)
    await _recognize(recognizer, doc, render=_RENDER + ";max_pixels=1000000")
    assert clients[GLM_OCR].calls == 2


async def test_failed_pages_are_not_cached() -> None:
    doc = await _doc()
    clients = {GLM_OCR: _Fake(_EMPTY), QWEN_FLASH: _Fake(_EMPTY, _GOOD)}
    recognizer = _recognizer(doc, clients=clients)
    (first,) = await _recognize(recognizer, doc)
    assert first.status is PageStatus.FAILED
    (second,) = await _recognize(recognizer, doc)
    assert second.status is PageStatus.RECOGNIZED


async def test_calls_per_model_stay_under_its_concurrency() -> None:
    # glm-ocr declares max_concurrency=2 (concurrency 4 drew 429s in the model test).
    doc = await _doc()
    clients = {GLM_OCR: _Fake(_GOOD, delay=0.01), QWEN_FLASH: _Fake(_GOOD)}
    images = [f"page-{n}".encode() for n in range(8)]
    outcomes = await _recognize(_recognizer(doc, clients=clients), doc, images=images)
    assert all(o.status is PageStatus.RECOGNIZED for o in outcomes)
    assert clients[GLM_OCR].calls == 8
    assert clients[GLM_OCR].peak == 2


async def test_one_page_blowing_up_fails_only_that_page() -> None:
    # Review #7: an unexpected error must not abandon the sibling pages' paid calls.
    doc = await _doc()

    class _Picky:
        calls = 0

        async def read(self, image: bytes) -> Reading:
            _Picky.calls += 1
            await asyncio.sleep(0.01)
            if image == b"bad":
                raise RuntimeError("client bug")
            return _GOOD

    picky = _Picky()

    def factory(choice: ModelChoice, api_key: str) -> RecognitionClient:
        del choice, api_key
        return picky

    recognizer = DocumentRecognizer(
        store=doc.store,
        settings=_Settings(RecognitionSettings(primary=GLM_OCR, fallback=None, max_pages=10)),
        api_keys=_Keys(),
        client_factory=factory,
    )
    outcomes = await _recognize(recognizer, doc, images=[b"ok-1", b"bad", b"ok-2"])
    assert [o.status for o in outcomes] == [
        PageStatus.RECOGNIZED,
        PageStatus.FAILED,
        PageStatus.RECOGNIZED,
    ]
    assert outcomes[1].error == "internal_error"


def test_default_client_follows_the_catalog() -> None:
    build = default_client()
    layout = build(GLM_OCR, "k")
    assert isinstance(layout, LayoutParsingClient)
    assert layout.url == "https://open.bigmodel.cn/api/paas/v4/layout_parsing"
    chat = build(QWEN_FLASH, "k")
    assert isinstance(chat, ChatVlmClient)
    assert chat.request_fields == {"enable_thinking": False, "max_completion_tokens": 8192}
    with pytest.raises(ValueError, match="not a recognition model"):
        build(ModelChoice("qwen", "qwen3.8-max"), "k")


async def test_a_model_without_recognition_capability_is_unsupported() -> None:
    doc = await _doc()
    recognizer = DocumentRecognizer(
        store=doc.store,
        settings=_Settings(
            RecognitionSettings(
                primary=ModelChoice("qwen", "qwen3.8-max"), fallback=None, max_pages=10
            )
        ),
        api_keys=_Keys(),
    )
    plan = await recognizer.plan(tenant_id=doc.tenant)
    assert not plan.usable
    assert plan.unavailable_reason == "qwen/qwen3.8-max: model_unsupported"


# ---------------------------------------------------------------------------
# recognize_document —— 分批、上限、补识别、换了主人就停
# ---------------------------------------------------------------------------


async def _render(indexes: list[int], max_pixels: int | None) -> list[bytes]:
    del max_pixels
    return [f"page-{index}".encode() for index in indexes]


async def test_switched_off_recognition_renders_nothing() -> None:
    doc = await _doc()
    rendered: list[int] = []

    async def render(indexes: list[int], max_pixels: int | None) -> list[bytes]:
        rendered.extend(indexes)
        return await _render(indexes, max_pixels)

    recognizer = _recognizer(doc, clients={}, primary=None, fallback=None)
    outcomes = await recognizer.recognize_document(
        tenant_id=doc.tenant, document_id=doc.document, pages=[0, 1], render=render
    )
    assert {o.status for o in outcomes.values()} == {PageStatus.PENDING}
    assert {o.error for o in outcomes.values()} == {"not_configured"}
    assert rendered == []


async def test_a_reingest_without_keys_keeps_recognized_pages_from_the_cache() -> None:
    # Review #7: the cache is read before giving up on unusable models.
    doc = await _doc()
    clients = {GLM_OCR: _Fake(_GOOD), QWEN_FLASH: _Fake(_GOOD)}
    with_keys = _recognizer(doc, clients=clients)
    first = await with_keys.recognize_document(
        tenant_id=doc.tenant, document_id=doc.document, pages=[0, 1], render=_render
    )
    assert {o.status for o in first.values()} == {PageStatus.RECOGNIZED}
    without_keys = _recognizer(doc, clients=clients, keys=_Keys(frozenset()))
    again = await without_keys.recognize_document(
        tenant_id=doc.tenant, document_id=doc.document, pages=[0, 1, 2], render=_render
    )
    assert [again[i].status for i in range(3)] == [
        PageStatus.RECOGNIZED,
        PageStatus.RECOGNIZED,
        PageStatus.PENDING,
    ]
    assert again[0].cached and again[1].cached
    assert clients[GLM_OCR].calls == 2


async def test_a_scanned_page_reading_records_its_page_source_and_reader() -> None:
    # B-238: the estimate counts pages read before (a cancelled ingest) by these.
    doc = await _doc()
    clients = {GLM_OCR: _Fake(_GOOD, _EMPTY), QWEN_FLASH: _Fake(_GOOD)}
    recognizer = _recognizer(doc, clients=clients)
    outcomes = await recognizer.recognize_document(
        tenant_id=doc.tenant,
        document_id=doc.document,
        pages=[0, 1],
        render=_render,
        source_sha256="src",
    )
    assert [outcomes[0].model, outcomes[1].model] == ["glm-ocr", "qwen3.7-flash"]
    primary, fallback = (await recognizer.plan(tenant_id=doc.tenant)).page_readers()

    async def pages(source: str, *readers: str) -> set[int]:
        return await doc.store.cached_page_indexes(
            tenant_id=doc.tenant, document_id=doc.document, source_sha256=source, readers=readers
        )

    assert await pages("src", primary, fallback) == {0, 1}
    assert await pages("src", primary) == {0}
    assert await pages("src", fallback) == {1}
    assert await pages("other", primary, fallback) == set()


async def test_readings_without_a_source_or_of_figures_record_no_page() -> None:
    doc = await _doc()
    recognizer = _recognizer(doc, clients={GLM_OCR: _Fake(_GOOD), QWEN_FLASH: _Fake(_GOOD)})
    plan = await recognizer.plan(tenant_id=doc.tenant)
    await recognizer.recognize(
        tenant_id=doc.tenant,
        document_id=doc.document,
        plan=plan,
        images={0: b"figure-0"},
        render=_RENDER,
        kind="figure_ocr",
        source_sha256="src",
    )
    await recognizer.recognize_document(
        tenant_id=doc.tenant, document_id=doc.document, pages=[1], render=_render
    )
    results = [entry.result for entry in doc.store._parse_cache.values()]
    assert len(results) == 2
    assert all(
        set(result) == {"raw", "finish_reason", "layout_chars", "layout_size"} for result in results
    )


# B-256 —— a page taken from a reading cached before B-238 (no page / bytes /
# reader recorded) is tagged on the way: the estimate counts it from then on.

_TAGS = {PAGE_OCR_PAGE, PAGE_OCR_SOURCE, PAGE_OCR_READER}


class _WriteCountingStore(InMemoryKnowledgeStore):
    def __init__(self) -> None:
        super().__init__()
        self.writes = 0
        self.fail_writes = False

    async def put_parse_cache(self, **kwargs: Any) -> None:
        self.writes += 1
        if self.fail_writes:
            raise RuntimeError("cache down")
        await super().put_parse_cache(**kwargs)


async def _read_untagged(store: _WriteCountingStore) -> tuple[_Doc, DocumentRecognizer, _Fake]:
    """Two pages read with no source: as a reading cached before B-238 looks."""
    doc = await _doc(store)
    clients = {GLM_OCR: _Fake(_GOOD), QWEN_FLASH: _Fake(_GOOD)}
    recognizer = _recognizer(doc, clients=clients)
    await recognizer.recognize_document(
        tenant_id=doc.tenant, document_id=doc.document, pages=[0, 1], render=_render
    )
    assert all(not _TAGS & set(e.result) for e in store._parse_cache.values())
    store.writes = 0
    return doc, recognizer, clients[GLM_OCR]


async def _read_with_source(doc: _Doc, recognizer: DocumentRecognizer) -> dict[int, PageOutcome]:
    return await recognizer.recognize_document(
        tenant_id=doc.tenant,
        document_id=doc.document,
        pages=[0, 1],
        render=_render,
        source_sha256="src",
    )


async def test_a_page_reused_from_an_untagged_reading_is_tagged() -> None:
    store = _WriteCountingStore()
    doc, recognizer, model = await _read_untagged(store)
    again = await _read_with_source(doc, recognizer)
    assert all(o.status is PageStatus.RECOGNIZED and o.cached for o in again.values())
    assert model.calls == 2  # nothing read again
    (primary, _) = (await recognizer.plan(tenant_id=doc.tenant)).page_readers()
    assert await doc.store.cached_page_indexes(
        tenant_id=doc.tenant, document_id=doc.document, source_sha256="src", readers=[primary]
    ) == {0, 1}
    # The reading itself is kept as it was.
    assert {e.result["raw"] for e in store._parse_cache.values()} == {_PAGE_TEXT}
    # Tagged once: the next ingest writes nothing.
    store.writes = 0
    await _read_with_source(doc, recognizer)
    assert store.writes == 0


async def test_a_reading_tagged_with_another_reader_is_tagged_again() -> None:
    # e.g. read before the reader digest took in the renderer's version.
    store = _WriteCountingStore()
    doc = await _doc(store)
    clients = {GLM_OCR: _Fake(_GOOD), QWEN_FLASH: _Fake(_GOOD)}
    recognizer = _recognizer(doc, clients=clients)
    await recognizer.recognize_document(
        tenant_id=doc.tenant,
        document_id=doc.document,
        pages=[0],
        render=_render,
        source_sha256="src",
    )
    for entry in store._parse_cache.values():
        entry.result[PAGE_OCR_READER] = "an-older-digest"
    store.writes = 0
    await recognizer.recognize_document(
        tenant_id=doc.tenant,
        document_id=doc.document,
        pages=[0],
        render=_render,
        source_sha256="src",
    )
    assert (store.writes, clients[GLM_OCR].calls) == (1, 1)
    (primary, _) = (await recognizer.plan(tenant_id=doc.tenant)).page_readers()
    assert {e.result[PAGE_OCR_READER] for e in store._parse_cache.values()} == {primary}


async def test_a_failed_tag_write_does_not_fail_the_page(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _WriteCountingStore()
    doc, recognizer, model = await _read_untagged(store)
    store.fail_writes = True
    with caplog.at_level(logging.WARNING):
        again = await _read_with_source(doc, recognizer)
    assert all(o.status is PageStatus.RECOGNIZED and o.cached for o in again.values())
    assert (model.calls, store.writes) == (2, 2)
    assert sum("knowledge.recognition_cache_tag_failed" in r.message for r in caplog.records) == 2


def test_the_reader_covers_the_renderer_version(monkeypatch: pytest.MonkeyPatch) -> None:
    # B-256: a new renderer may draw other bytes — every cache key misses — so a
    # reading by the old one must not count as read the way pages are read now.
    from control_plane.knowledge.recognition import recognizer as recognizer_module

    before = reader_digest("glm/glm-ocr", "layout_parsing", _RENDER)
    monkeypatch.setattr(recognizer_module, "RENDERER_VERSION", "pymupdf=9.9.9;mupdf=9.9.9")
    assert reader_digest("glm/glm-ocr", "layout_parsing", _RENDER) != before


async def test_pages_past_the_cap_are_over_limit() -> None:
    doc = await _doc()
    clients = {GLM_OCR: _Fake(_GOOD), QWEN_FLASH: _Fake(_GOOD)}
    recognizer = _recognizer(doc, clients=clients, max_pages=2)
    outcomes = await recognizer.recognize_document(
        tenant_id=doc.tenant, document_id=doc.document, pages=[0, 1, 2], render=_render
    )
    assert outcomes[2] == PageOutcome(status=PageStatus.OVER_LIMIT, error="over_limit")
    assert clients[GLM_OCR].calls == 2


class _LostDocumentError(Exception):
    pass


async def test_a_worker_that_lost_the_document_stops_before_the_next_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from control_plane.knowledge.recognition import recognizer as recognizer_module

    monkeypatch.setattr(recognizer_module, "RENDER_BATCH", 2)
    doc = await _doc()
    clients = {GLM_OCR: _Fake(_GOOD), QWEN_FLASH: _Fake(_GOOD)}
    checks = 0

    async def still_ours() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise _LostDocumentError

    recognizer = _recognizer(doc, clients=clients)
    with pytest.raises(_LostDocumentError):
        await recognizer.recognize_document(
            tenant_id=doc.tenant,
            document_id=doc.document,
            pages=[0, 1, 2, 3, 4, 5],
            render=_render,
            before_batch=still_ours,
        )
    assert clients[GLM_OCR].calls == 2  # the first batch only


async def test_cleaning_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review H1: a heavy reading must not stall every other request on the loop.
    import threading

    from control_plane.knowledge.recognition import recognizer as recognizer_module

    threads: list[bool] = []
    real = recognizer_module._accept

    def spy(reading: Reading, image: bytes) -> tuple[str, str | None, Any]:
        threads.append(threading.current_thread() is threading.main_thread())
        return real(reading, image)

    monkeypatch.setattr(recognizer_module, "_accept", spy)
    doc = await _doc()
    clients = {GLM_OCR: _Fake(_GOOD), QWEN_FLASH: _Fake(_GOOD)}
    recognizer = _recognizer(doc, clients=clients)
    await _recognize(recognizer, doc)  # fresh reading
    await _recognize(recognizer, doc)  # cached reading
    assert threads == [False, False]


def test_the_cache_key_covers_the_request_parameters() -> None:
    # Review #4: a different output cap / vendor field set reads differently.
    from control_plane.knowledge.recognition.recognizer import _cache_tag
    from expert_work.protocol import OcrCaps

    base = _cache_tag(QWEN_FLASH, OcrCaps(api="chat_vlm", max_output_tokens=8192))
    assert "max_completion_tokens" in base and "enable_thinking" in base
    assert base != _cache_tag(QWEN_FLASH, OcrCaps(api="chat_vlm", max_output_tokens=4096))
    assert _cache_tag(GLM_OCR, OcrCaps(api="layout_parsing")) == "layout_parsing"
