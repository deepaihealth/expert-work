"""B-209 §5.3 / §5.5 / §5.6 —— 一份文档的扫描页识别:缓存、主模型 → 兜底 → 失败 / 待识别、
按模型限并发、记账、给租户看的错误类。识别客户端是假的(不联网),回答按 ``Reading`` 脚本给出。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge.recognition.cleaning import NO_TEXT_SENTENCE
from control_plane.knowledge.recognition.clients import (
    ChatVlmClient,
    LayoutParsingClient,
    Reading,
    RecognitionCallError,
    RecognitionClient,
)
from control_plane.knowledge.recognition.recognizer import (
    ERROR_CLASSES,
    DocumentRecognizer,
    ModelChoice,
    PageOutcome,
    RecognitionSettings,
    default_client,
)
from expert_work.persistence import InMemoryKnowledgeStore
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
        del delay

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
    await _recognize(recognizer, doc)
    stored = next(iter(doc.store._parse_cache.values()))
    assert stored.result["raw"] == raw.text
    (outcome,) = await _recognize(recognizer, doc)
    assert outcome.cached is True
    assert outcome.text == "## 第一章\n\n正文"


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

    def spy(reading: Reading) -> tuple[str, str | None]:
        threads.append(threading.current_thread() is threading.main_thread())
        return real(reading)

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
