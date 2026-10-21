"""B-209 §5.3 / §5.5 / §5.6 —— 一份文档的扫描页识别:缓存、主模型 → 兜底 → 失败 / 待识别、
按模型限并发、记账。识别客户端是假的(不联网),回答按 ``Reading`` 脚本给出。
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
            msg = f"no platform credential for provider {provider!r}"
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

    def __init__(self, *script: Reading | RecognitionCallError, delay: float = 0.0) -> None:
        self._script = list(script)
        self._delay = delay
        self.calls = 0
        self.in_flight = 0
        self.peak = 0

    async def read(self, image: bytes) -> Reading:
        del image
        self.calls += 1
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            step = self._script[min(self.calls, len(self._script)) - 1]
            if isinstance(step, RecognitionCallError):
                raise step
            return step
        finally:
            self.in_flight -= 1


def _recognizer(
    *,
    clients: dict[ModelChoice, _Fake],
    store: InMemoryKnowledgeStore | None = None,
    primary: ModelChoice | None = GLM_OCR,
    fallback: ModelChoice | None = QWEN_FLASH,
    keys: _Keys | None = None,
    usage: _Usage | None = None,
) -> DocumentRecognizer:
    async def no_sleep(delay: float) -> None:
        del delay

    def factory(choice: ModelChoice, api_key: str) -> RecognitionClient:
        assert api_key == f"key-{choice.provider}"
        return clients[choice]

    return DocumentRecognizer(
        store=store or InMemoryKnowledgeStore(),
        settings=_Settings(RecognitionSettings(primary=primary, fallback=fallback, max_pages=1000)),
        api_keys=keys or _Keys(),
        usage=usage,
        client_factory=factory,
        sleep=no_sleep,
    )


async def _recognize(
    recognizer: DocumentRecognizer,
    images: Sequence[bytes] = (b"page-1",),
    tenant: UUID | None = None,
    render: str = _RENDER,
) -> list[PageOutcome]:
    tenant = tenant or uuid4()
    plan = await recognizer.plan(tenant_id=tenant)
    outcomes = await recognizer.recognize(
        tenant_id=tenant, plan=plan, images=dict(enumerate(images)), render=render
    )
    return [outcomes[index] for index in range(len(images))]


async def test_primary_reading_is_kept_cleaned_and_billed() -> None:
    fenced = Reading(
        text=f"```\n{_PAGE_TEXT}\n```", input_tokens=9, output_tokens=3, usage_reported=True
    )
    primary, fallback, usage = _Fake(fenced), _Fake(), _Usage()
    tenant = uuid4()
    (outcome,) = await _recognize(
        _recognizer(clients={GLM_OCR: primary, QWEN_FLASH: fallback}, usage=usage), tenant=tenant
    )
    assert outcome.status is PageStatus.RECOGNIZED
    assert outcome.text == _PAGE_TEXT
    assert (outcome.provider, outcome.model, outcome.cached) == ("glm", "glm-ocr", False)
    assert fallback.calls == 0
    assert usage.rows == [(tenant, "glm", "glm-ocr", 9, 3)]


@pytest.mark.parametrize("bad", [_EMPTY, _TRUNCATED, Reading(text="血压正常。" * 100)])
async def test_a_bad_primary_reading_goes_to_the_fallback(bad: Reading) -> None:
    primary, fallback, usage = _Fake(bad), _Fake(_GOOD), _Usage()
    (outcome,) = await _recognize(
        _recognizer(clients={GLM_OCR: primary, QWEN_FLASH: fallback}, usage=usage)
    )
    assert outcome.status is PageStatus.RECOGNIZED
    assert outcome.model == "qwen3.7-flash"
    # The vendor charged for the bad reading too: both calls are billed.
    assert [row[2] for row in usage.rows] == ["glm-ocr", "qwen3.7-flash"]


async def test_both_bad_marks_the_page_failed_with_both_reasons() -> None:
    clients = {GLM_OCR: _Fake(_EMPTY), QWEN_FLASH: _Fake(_TRUNCATED)}
    (outcome,) = await _recognize(_recognizer(clients=clients))
    assert outcome.status is PageStatus.FAILED
    assert outcome.text == ""
    assert outcome.error == ("glm/glm-ocr: empty output; qwen/qwen3.7-flash: truncated output")


async def test_no_text_reply_is_a_valid_empty_page() -> None:
    clients = {GLM_OCR: _Fake(Reading(text=NO_TEXT_SENTENCE)), QWEN_FLASH: _Fake(_GOOD)}
    (outcome,) = await _recognize(_recognizer(clients=clients))
    assert outcome.status is PageStatus.RECOGNIZED
    assert outcome.text == ""
    assert clients[QWEN_FLASH].calls == 0


async def test_missing_primary_key_falls_back() -> None:
    clients = {GLM_OCR: _Fake(_GOOD), QWEN_FLASH: _Fake(_GOOD)}
    recognizer = _recognizer(clients=clients, keys=_Keys(frozenset({"qwen"})))
    (outcome,) = await _recognize(recognizer)
    assert outcome.model == "qwen3.7-flash"
    assert clients[GLM_OCR].calls == 0


async def test_no_usable_model_leaves_the_page_pending() -> None:
    clients = {GLM_OCR: _Fake(_GOOD), QWEN_FLASH: _Fake(_GOOD)}
    recognizer = _recognizer(clients=clients, keys=_Keys(frozenset()))
    plan = await recognizer.plan(tenant_id=uuid4())
    assert not plan.usable
    (outcome,) = await _recognize(recognizer)
    assert outcome.status is PageStatus.PENDING
    assert outcome.error is not None
    assert "no platform credential for provider 'glm'" in outcome.error
    assert clients[GLM_OCR].calls == clients[QWEN_FLASH].calls == 0


async def test_recognition_switched_off_leaves_the_page_pending() -> None:
    (outcome,) = await _recognize(_recognizer(clients={}, primary=None, fallback=None))
    assert outcome.status is PageStatus.PENDING
    assert outcome.error == "no recognition model configured"


async def test_a_vendor_out_of_service_leaves_the_page_pending() -> None:
    # Load errors past the retries + an account error: nothing was wrong with
    # the page — it waits for 「补识别」.
    busy = RecognitionCallError("503 overloaded", retryable=True)
    clients = {
        GLM_OCR: _Fake(busy),
        QWEN_FLASH: _Fake(RecognitionCallError("401 bad key", unavailable=True)),
    }
    (outcome,) = await _recognize(_recognizer(clients=clients))
    assert outcome.status is PageStatus.PENDING
    assert clients[GLM_OCR].calls == 4  # one call + three retries


async def test_a_rejected_request_fails_the_page() -> None:
    clients = {
        GLM_OCR: _Fake(RecognitionCallError("400 image too large")),
        QWEN_FLASH: _Fake(RecognitionCallError("401 bad key", unavailable=True)),
    }
    (outcome,) = await _recognize(_recognizer(clients=clients))
    assert outcome.status is PageStatus.FAILED


async def test_a_recognized_page_is_never_billed_twice() -> None:
    store, usage, tenant = InMemoryKnowledgeStore(), _Usage(), uuid4()
    clients = {GLM_OCR: _Fake(_EMPTY), QWEN_FLASH: _Fake(_GOOD)}
    recognizer = _recognizer(clients=clients, store=store, usage=usage)
    await _recognize(recognizer, tenant=tenant)
    assert len(usage.rows) == 2
    # Again (re-upload / 「补识别」 / re-chunk): the fallback's reading is in the
    # cache — the primary is not asked again either.
    (outcome,) = await _recognize(recognizer, tenant=tenant)
    assert outcome.cached is True
    assert outcome.model == "qwen3.7-flash"
    assert outcome.text == _PAGE_TEXT
    assert clients[GLM_OCR].calls == clients[QWEN_FLASH].calls == 1
    assert len(usage.rows) == 2


async def test_the_cache_is_per_tenant() -> None:
    store = InMemoryKnowledgeStore()
    clients = {GLM_OCR: _Fake(_GOOD), QWEN_FLASH: _Fake(_GOOD)}
    recognizer = _recognizer(clients=clients, store=store)
    await _recognize(recognizer, tenant=uuid4())
    await _recognize(recognizer, tenant=uuid4())
    assert clients[GLM_OCR].calls == 2


async def test_a_different_rendering_is_read_again() -> None:
    store, tenant = InMemoryKnowledgeStore(), uuid4()
    clients = {GLM_OCR: _Fake(_GOOD), QWEN_FLASH: _Fake(_GOOD)}
    recognizer = _recognizer(clients=clients, store=store)
    await _recognize(recognizer, tenant=tenant)
    await _recognize(recognizer, tenant=tenant, render=_RENDER + ";max_pixels=1000000")
    assert clients[GLM_OCR].calls == 2


async def test_failed_pages_are_not_cached() -> None:
    store, tenant = InMemoryKnowledgeStore(), uuid4()
    clients = {GLM_OCR: _Fake(_EMPTY), QWEN_FLASH: _Fake(_EMPTY, _GOOD)}
    recognizer = _recognizer(clients=clients, store=store)
    (first,) = await _recognize(recognizer, tenant=tenant)
    assert first.status is PageStatus.FAILED
    (second,) = await _recognize(recognizer, tenant=tenant)
    assert second.status is PageStatus.RECOGNIZED


async def test_calls_per_model_stay_under_its_concurrency() -> None:
    # glm-ocr declares max_concurrency=2 (concurrency 4 drew 429s in the model test).
    clients = {GLM_OCR: _Fake(_GOOD, delay=0.01), QWEN_FLASH: _Fake(_GOOD)}
    images = [f"page-{n}".encode() for n in range(8)]
    outcomes = await _recognize(_recognizer(clients=clients), images=images)
    assert all(o.status is PageStatus.RECOGNIZED for o in outcomes)
    assert clients[GLM_OCR].calls == 8
    assert clients[GLM_OCR].peak == 2


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


async def test_a_model_without_recognition_capability_is_unavailable() -> None:
    recognizer = DocumentRecognizer(
        store=InMemoryKnowledgeStore(),
        settings=_Settings(
            RecognitionSettings(
                primary=ModelChoice("qwen", "qwen3.8-max"), fallback=None, max_pages=10
            )
        ),
        api_keys=_Keys(),
    )
    plan = await recognizer.plan(tenant_id=uuid4())
    assert not plan.usable
    assert "not a recognition model" in plan.unavailable_reason
