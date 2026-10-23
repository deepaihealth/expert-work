"""B-209 §5.3 / §5.5 / §5.6 —— 一份文档的扫描页识别。

每页:先查缓存(主模型与兜底模型的键都查,哪个识别过都不再付费)→ 主模型 → 清洗 + 校验 →
不合格换兜底模型再识别一次 → 仍不合格标「失败」(可补识别),不阻塞整份入库。

* 模型没配、没有密钥、账户不可用、重试用完仍限流 / 5xx:这一页标「待识别」——不是识别
  错了,是没能识别;配好后补识别只处理这些页(其余走缓存)。
* 每次拿到厂商回答的调用都按厂商返回的 ``usage`` 记一行 ``knowledge_ingest`` 用量,记在
  上传文档的租户名下(不合格的回答、形状不对的回包厂商也收了钱,一样记)。
* 缓存**按文档**(复核 M1:文档删了,它的识别文字 —— 可能是健康资料 —— 随之删除),键 =
  图片字节 sha256 + 模型 + 请求参数(提示词版本 / 温度 / 关思考字段 / 输出上限)+ 渲染参数;
  存的是厂商**原始回答**,读出时按当前规则重新清洗与校验 —— 清洗规则修了,缓存里的页跟着修。
* 页面与文档上给租户看的错误只有固定的错误类(:data:`ERROR_CLASSES`),厂商原文、凭据
  解析的报错只进服务端日志(复核 L2)。
* 清洗与校验在线程里跑,不占事件循环;单页并发出错不留下仍在花钱的兄弟调用。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol
from uuid import UUID

import httpx

from control_plane.knowledge.recognition.cleaning import (
    clean_reading,
    extract_image_regions,
    is_no_text_reply,
    reading_failure,
    reading_too_large,
)
from control_plane.knowledge.recognition.clients import (
    CHAT_COMPLETIONS_URLS,
    DEFAULT_OCR_MAX_TOKENS,
    LAYOUT_PARSING_URLS,
    OCR_PROMPT_VERSION,
    OCR_TEMPERATURE,
    CallErrorKind,
    ChatVlmClient,
    LayoutParsingClient,
    Reading,
    RecognitionCallError,
    RecognitionClient,
    read_with_retry,
)
from control_plane.knowledge.recognition.figure_images import image_size
from control_plane.knowledge.recognition.figure_markup import (
    region_placeholder,
    resolve_figures,
)
from control_plane.knowledge.recognition.pages import render_params
from expert_work.persistence.knowledge.base import KnowledgeStore, ParseCacheEntry, ParseCacheKind
from expert_work.protocol import OcrCaps, PageStatus, catalog_entry
from orchestrator.agent_factory import thinking_off_request_fields

logger = logging.getLogger(__name__)

#: Concurrent calls per model when the catalog declares no ``max_concurrency``.
DEFAULT_MODEL_CONCURRENCY = 4
#: Scanned pages rendered per parse-pool call: enough to keep the models busy
#: while the next batch renders, small enough that a batch of JPEGs (about
#: 0.3 MB each) is a cheap hand-over between processes.
RENDER_BATCH = 16
_CACHE_KIND: ParseCacheKind = "page_ocr"

#: The only reasons a tenant sees for a page that was not recognized.
ERROR_NOT_CONFIGURED = "not_configured"
ERROR_MODEL_UNSUPPORTED = "model_unsupported"
ERROR_CREDENTIAL_MISSING = "credential_missing"
ERROR_OVER_LIMIT = "over_limit"
ERROR_INTERNAL = "internal_error"
#: A page whose text did not fit under the document's text limit — read and
#: dropped, or (once the limit is reached) not read at all.
ERROR_DOCUMENT_TEXT_LIMIT = "document_text_limit"
_CALL_ERROR_CLASSES: dict[CallErrorKind, str] = {
    "auth": "vendor_auth",
    "quota": "vendor_quota",
    "busy": "vendor_unavailable",
    "rejected": "vendor_rejected",
    "bad_response": "vendor_bad_response",
}
ERROR_CLASSES = frozenset(
    {
        ERROR_NOT_CONFIGURED,
        ERROR_MODEL_UNSUPPORTED,
        ERROR_CREDENTIAL_MISSING,
        ERROR_OVER_LIMIT,
        ERROR_INTERNAL,
        ERROR_DOCUMENT_TEXT_LIMIT,
        "bad_output",
        *_CALL_ERROR_CLASSES.values(),
    }
)


@dataclass(frozen=True)
class ModelChoice:
    """One configured recognition model."""

    provider: str
    model: str

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass(frozen=True)
class RecognitionSettings:
    """The platform's 「文档识别」 settings (B-209 §5.7). ``None`` = switched off.
    ``figure`` describes the images inside documents (PR-2)."""

    primary: ModelChoice | None
    fallback: ModelChoice | None
    max_pages: int
    figure: ModelChoice | None = None


class RecognitionSettingsSource(Protocol):
    async def recognition_settings(self) -> RecognitionSettings:
        """The live settings (platform config, defaults when unset)."""


class ApiKeySource(Protocol):
    async def __call__(self, *, tenant_id: UUID, provider: str) -> str:
        """The provider's API key; raises when there is none."""


class UsageSink(Protocol):
    async def record(
        self,
        *,
        tenant_id: UUID,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Bill one model call to ``tenant_id`` (``knowledge_ingest``)."""


#: Renders pages (0-based indexes) of the document being recognized as JPEG,
#: at most ``max_pixels`` each when given.
PageRenderer = Callable[[list[int], int | None], Coroutine[Any, Any, list[bytes]]]

#: Called before each batch of pages; raises to stop (the document is no
#: longer this worker's — re-uploaded or settled elsewhere).
BatchGuard = Callable[[], Awaitable[None]]

#: Builds a client for a model from its API key (tests inject fakes).
ClientFactory = Callable[[ModelChoice, str], RecognitionClient]


@dataclass(frozen=True)
class PageOutcome:
    """What recognition made of one page. ``error`` holds error classes only.

    ``regions``: the image regions the vendor's layout model found, as
    boxes relative to the image sent (0..1: independent of the render size),
    each left in ``text`` as a region placeholder at its place (B-209 PR-2 ——
    figures on scanned pages); whoever uses the text resolves them
    (:func:`~.figure_markup.resolve_figures`)."""

    status: PageStatus
    text: str = ""
    provider: str | None = None
    model: str | None = None
    cached: bool = False
    error: str | None = None
    regions: tuple[tuple[float, float, float, float], ...] = ()


_TEXT_LIMIT = PageOutcome(status=PageStatus.FAILED, error=ERROR_DOCUMENT_TEXT_LIMIT)


@dataclass(frozen=True)
class _Engine:
    """One model, resolved for one document: its client (``None`` with the
    error class when it cannot be used), cache tag and limits."""

    choice: ModelChoice
    client: RecognitionClient | None
    unavailable: str | None
    cache_tag: str
    max_pixels: int | None
    max_concurrency: int


@dataclass(frozen=True)
class RecognitionPlan:
    """The models one document is recognized with, primary first."""

    engines: tuple[_Engine, ...]
    max_pages: int

    @property
    def usable(self) -> bool:
        return any(engine.client is not None for engine in self.engines)

    @property
    def max_pixels(self) -> int | None:
        """The tightest pixel cap any model declares — one rendering serves both."""
        caps = [engine.max_pixels for engine in self.engines if engine.max_pixels]
        return min(caps) if caps else None

    @property
    def unavailable_reason(self) -> str:
        reasons = [f"{e.choice.key}: {e.unavailable}" for e in self.engines if e.unavailable]
        return "; ".join(reasons) or ERROR_NOT_CONFIGURED


def default_client(
    http: httpx.AsyncClient | None = None,
) -> ClientFactory:
    """The HTTP adapter for a model, by its catalog ``ocr_caps.api``."""

    def build(choice: ModelChoice, api_key: str) -> RecognitionClient:
        caps = _ocr_caps(choice)
        if caps is None:
            msg = f"{choice.key} is not a recognition model"
            raise ValueError(msg)
        if caps.api == "layout_parsing":
            url = LAYOUT_PARSING_URLS.get(choice.provider)
            if url is None:
                msg = f"no layout-parsing endpoint for provider {choice.provider!r}"
                raise ValueError(msg)
            return LayoutParsingClient(url=url, api_key=api_key, model=choice.model, http=http)
        url = CHAT_COMPLETIONS_URLS.get(choice.provider)
        if url is None:
            msg = f"no chat endpoint for provider {choice.provider!r}"
            raise ValueError(msg)
        return ChatVlmClient(
            url=url,
            api_key=api_key,
            model=choice.model,
            request_fields=_chat_fields(choice, caps),
            http=http,
        )

    return build


def _ocr_caps(choice: ModelChoice) -> OcrCaps | None:
    entry = catalog_entry(choice.provider, choice.model)
    return entry.ocr_caps if entry is not None else None


def _chat_fields(choice: ModelChoice, caps: OcrCaps) -> dict[str, Any]:
    return thinking_off_request_fields(
        choice.provider,
        choice.model,
        max_tokens=caps.max_output_tokens or DEFAULT_OCR_MAX_TOKENS,
    )


def _cache_tag(choice: ModelChoice, caps: OcrCaps | None) -> str:
    """Everything besides the image that changes what the model reads: for a
    chat model the prompt version, temperature and vendor fields (thinking
    off, output cap)."""
    if caps is None:
        return "unsupported"
    if caps.api == "layout_parsing":
        return "layout_parsing"
    fields = json.dumps(_chat_fields(choice, caps), sort_keys=True)
    return f"chat_vlm|{OCR_PROMPT_VERSION}|temperature={OCR_TEMPERATURE}|{fields}"


def cache_key(image: bytes, engine_key: str, cache_tag: str, render: str) -> str:
    """``knowledge_parse_cache.cache_key`` (B-209 §5.5), within one document."""
    digest = hashlib.sha256(image).hexdigest()
    return hashlib.sha256(f"{digest}|{engine_key}|{cache_tag}|{render}".encode()).hexdigest()


class ModelLimits:
    """Per-model concurrency slots, one registry per process: every caller of
    a model (scanned pages, figure descriptions, figure text) draws from the
    same slots, so a model used for both never sees more than its limit."""

    def __init__(self) -> None:
        self._held: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Semaphore]] = {}

    def slot(self, choice: ModelChoice, size: int) -> asyncio.Semaphore:
        """The model's semaphore (``size`` slots when first made; one per
        event loop — tests run several)."""
        loop = asyncio.get_running_loop()
        held = self._held.get(choice.key)
        if held is None or held[0] is not loop:
            held = (loop, asyncio.Semaphore(size))
            self._held[choice.key] = held
        return held[1]


def model_concurrency(choice: ModelChoice) -> int:
    """The catalog's ``ocr_caps.max_concurrency`` for the model, else the default."""
    caps = _ocr_caps(choice)
    return (caps.max_concurrency if caps is not None else None) or DEFAULT_MODEL_CONCURRENCY


class DocumentRecognizer:
    """Recognizes the scanned pages of documents (one per process; the
    per-model concurrency limits are shared by every document in flight,
    and with the figure describer when it is given the same ``limits``)."""

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        settings: RecognitionSettingsSource,
        api_keys: ApiKeySource,
        usage: UsageSink | None = None,
        client_factory: ClientFactory | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        limits: ModelLimits | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._api_keys = api_keys
        self._usage = usage
        self._client_factory = client_factory or default_client()
        self._sleep = sleep
        self._limits = limits or ModelLimits()

    async def plan(self, *, tenant_id: UUID) -> RecognitionPlan:
        """Resolve the configured models (and their keys) once per document."""
        settings = await self._settings.recognition_settings()
        engines: list[_Engine] = []
        for choice in (settings.primary, settings.fallback):
            if choice is not None and choice not in (engine.choice for engine in engines):
                engines.append(await self._engine(choice, tenant_id))
        return RecognitionPlan(engines=tuple(engines), max_pages=settings.max_pages)

    async def _engine(self, choice: ModelChoice, tenant_id: UUID) -> _Engine:
        caps = _ocr_caps(choice)
        engine = _Engine(
            choice=choice,
            client=None,
            unavailable=None,
            cache_tag=_cache_tag(choice, caps),
            max_pixels=caps.max_pixels if caps is not None else None,
            max_concurrency=(caps.max_concurrency if caps is not None else None)
            or DEFAULT_MODEL_CONCURRENCY,
        )
        try:
            api_key = await self._api_keys(tenant_id=tenant_id, provider=choice.provider)
        except Exception as exc:
            logger.warning(
                "knowledge.recognition_credential_missing model=%s tenant=%s detail=%s",
                choice.key,
                tenant_id,
                exc,
            )
            return replace(engine, unavailable=ERROR_CREDENTIAL_MISSING)
        try:
            client = self._client_factory(choice, api_key)
        except Exception as exc:
            logger.warning(
                "knowledge.recognition_model_unsupported model=%s detail=%s", choice.key, exc
            )
            return replace(engine, unavailable=ERROR_MODEL_UNSUPPORTED)
        return replace(engine, client=client)

    async def recognize(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        plan: RecognitionPlan,
        images: Mapping[int, bytes],
        render: str,
        kind: ParseCacheKind = _CACHE_KIND,
    ) -> dict[int, PageOutcome]:
        """Recognize the given pages (index → JPEG bytes). One page going
        wrong never leaves the others' paid calls running unattended: an
        unexpected error is that page's ``internal_error``. ``kind`` is the
        cache row kind (``figure_ocr``: the text inside a document's figure)."""
        keys = {
            index: [
                cache_key(image, engine.choice.key, engine.cache_tag, render)
                for engine in plan.engines
            ]
            for index, image in images.items()
        }
        cached = await self._cached(
            tenant_id, document_id, kind, [key for row in keys.values() for key in row]
        )
        results = await asyncio.gather(
            *(
                self._guarded_page(tenant_id, document_id, plan, image, keys[index], cached, kind)
                for index, image in images.items()
            )
        )
        return dict(zip(images, results, strict=True))

    async def recognize_document(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        pages: Sequence[int],
        render: PageRenderer,
        before_batch: BatchGuard | None = None,
        max_chars: int | None = None,
    ) -> dict[int, PageOutcome]:
        """Recognize one document's scanned pages (0-based indexes, in page
        order) up to the per-document cap — the rest are ``over_limit``.
        ``render`` draws pages as JPEG (in the parse pool) a batch at a time;
        the next batch renders while this one is read. ``before_batch`` runs
        before each batch and raises to stop a worker that lost the document.
        Once the pages read hold ``max_chars`` characters, the remaining pages
        are not read (nor paid for): they are ``document_text_limit``.

        No model configured at all: every page is ``pending``, nothing is
        rendered. Models configured but none usable (no key, account down):
        pages are still rendered so readings already in the document's cache
        are kept; the rest are ``pending``."""
        plan = await self.plan(tenant_id=tenant_id)
        within, over = list(pages[: plan.max_pages]), pages[plan.max_pages :]
        over_cap = PageOutcome(status=PageStatus.OVER_LIMIT, error=ERROR_OVER_LIMIT)
        outcomes = dict.fromkeys(over, over_cap)
        if not plan.engines:
            pending = PageOutcome(status=PageStatus.PENDING, error=ERROR_NOT_CONFIGURED)
            outcomes.update(dict.fromkeys(within, pending))
            return outcomes
        params = render_params(plan.max_pixels)
        batches = [
            within[start : start + RENDER_BATCH] for start in range(0, len(within), RENDER_BATCH)
        ]

        def rendering(indexes: list[int]) -> asyncio.Task[list[bytes]]:
            return asyncio.create_task(render(indexes, plan.max_pixels))

        upcoming = rendering(batches[0]) if batches else None
        read = 0
        try:
            for position, indexes in enumerate(batches):
                if max_chars is not None and read >= max_chars:
                    unread = [index for rest in batches[position:] for index in rest]
                    outcomes.update(dict.fromkeys(unread, _TEXT_LIMIT))
                    break
                if before_batch is not None:
                    await before_batch()
                assert upcoming is not None  # noqa: S101 — one task per batch
                images = await upcoming
                upcoming = rendering(batches[position + 1]) if position + 1 < len(batches) else None
                batch = await self.recognize(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    plan=plan,
                    images=dict(zip(indexes, images, strict=True)),
                    render=params,
                )
                outcomes.update(batch)
                read += sum(
                    len(o.text) for o in batch.values() if o.status is PageStatus.RECOGNIZED
                )
        finally:
            if upcoming is not None:
                upcoming.cancel()
                await asyncio.gather(upcoming, return_exceptions=True)
        return outcomes

    async def _cached(
        self, tenant_id: UUID, document_id: UUID, kind: ParseCacheKind, keys: Sequence[str]
    ) -> dict[str, ParseCacheEntry]:
        try:
            return await self._store.get_parse_cache(
                tenant_id=tenant_id, document_id=document_id, kind=kind, keys=keys
            )
        except Exception:
            # A cache outage costs money (pages are read again), not correctness.
            logger.warning("knowledge.recognition_cache_read_failed", exc_info=True)
            return {}

    async def _guarded_page(
        self,
        tenant_id: UUID,
        document_id: UUID,
        plan: RecognitionPlan,
        image: bytes,
        keys: Sequence[str],
        cached: Mapping[str, ParseCacheEntry],
        kind: ParseCacheKind,
    ) -> PageOutcome:
        try:
            return await self._page(tenant_id, document_id, plan, image, keys, cached, kind)
        except Exception:
            logger.exception("knowledge.recognition_page_failed document=%s", document_id)
            return PageOutcome(status=PageStatus.FAILED, error=ERROR_INTERNAL)

    async def _page(
        self,
        tenant_id: UUID,
        document_id: UUID,
        plan: RecognitionPlan,
        image: bytes,
        keys: Sequence[str],
        cached: Mapping[str, ParseCacheEntry],
        kind: ParseCacheKind,
    ) -> PageOutcome:
        for engine, key in zip(plan.engines, keys, strict=True):
            hit = cached.get(key)
            if hit is None:
                continue
            # Cached raw answers are cleaned and checked again under the current
            # rules; one the rules now reject is read afresh.
            text, failure, regions = await asyncio.to_thread(
                _accept, _reading_from_cache(hit), image
            )
            if failure is None:
                return PageOutcome(
                    status=PageStatus.RECOGNIZED,
                    text=text,
                    provider=engine.choice.provider,
                    model=engine.choice.model,
                    cached=True,
                    regions=regions,
                )
        errors: list[str] = []
        unavailable_only = True
        for engine, key in zip(plan.engines, keys, strict=True):
            if engine.client is None:
                errors.append(f"{engine.choice.key}: {engine.unavailable}")
                continue
            try:
                reading = await read_with_retry(
                    engine.client, image, sleep=self._sleep, limit=self._limit(engine)
                )
            except RecognitionCallError as exc:
                logger.warning(
                    "knowledge.recognition_call_failed model=%s kind=%s detail=%s",
                    engine.choice.key,
                    exc.kind,
                    exc,
                )
                await self._bill(
                    tenant_id, engine.choice, exc.input_tokens, exc.output_tokens, reported=True
                )
                errors.append(f"{engine.choice.key}: {_CALL_ERROR_CLASSES[exc.kind]}")
                # Out of retries on load / 5xx, or no usable account: the vendor
                # is unavailable — the page waits. A rejected request is a failure.
                unavailable_only = unavailable_only and (exc.unavailable or exc.retryable)
                continue
            await self._bill(
                tenant_id,
                engine.choice,
                reading.input_tokens,
                reading.output_tokens,
                reported=reading.usage_reported,
            )
            text, failure, regions = await asyncio.to_thread(_accept, reading, image)
            if failure is not None:
                errors.append(f"{engine.choice.key}: bad_output({failure})")
                unavailable_only = False
                continue
            await self._remember(tenant_id, document_id, kind, key, engine.choice, reading)
            return PageOutcome(
                status=PageStatus.RECOGNIZED,
                text=text,
                provider=engine.choice.provider,
                model=engine.choice.model,
                regions=regions,
            )
        return PageOutcome(
            status=PageStatus.PENDING if unavailable_only else PageStatus.FAILED,
            error="; ".join(errors),
        )

    def _limit(self, engine: _Engine) -> asyncio.Semaphore:
        return self._limits.slot(engine.choice, engine.max_concurrency)

    async def _bill(
        self,
        tenant_id: UUID,
        choice: ModelChoice,
        input_tokens: int,
        output_tokens: int,
        *,
        reported: bool,
    ) -> None:
        if not reported:
            logger.warning("knowledge.recognition_usage_missing model=%s", choice.key)
        if self._usage is None or (input_tokens <= 0 and output_tokens <= 0):
            return
        await self._usage.record(
            tenant_id=tenant_id,
            provider=choice.provider,
            model=choice.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    async def _remember(
        self,
        tenant_id: UUID,
        document_id: UUID,
        kind: ParseCacheKind,
        key: str,
        choice: ModelChoice,
        reading: Reading,
    ) -> None:
        try:
            await self._store.put_parse_cache(
                tenant_id=tenant_id,
                document_id=document_id,
                kind=kind,
                key=key,
                model=choice.key,
                result={
                    "raw": reading.text,
                    "finish_reason": reading.finish_reason,
                    "layout_chars": reading.layout_chars,
                    "layout_size": list(reading.layout_size) if reading.layout_size else None,
                },
            )
        except Exception:
            logger.warning("knowledge.recognition_cache_write_failed", exc_info=True)


def _reading_from_cache(entry: ParseCacheEntry) -> Reading:
    result = entry.result
    finish = result.get("finish_reason")
    layout = result.get("layout_chars")
    size = result.get("layout_size")
    layout_size = None
    if isinstance(size, list) and len(size) == 2:
        width, height = size
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            layout_size = (width, height)
    return Reading(
        text=str(result.get("raw") or ""),
        finish_reason=finish if isinstance(finish, str) else None,
        layout_chars=layout if isinstance(layout, int) else None,
        layout_size=layout_size,
    )


Regions = tuple[tuple[float, float, float, float], ...]


def _accept(reading: Reading, image: bytes) -> tuple[str, str | None, Regions]:
    """The cleaned text, why it cannot be kept (a ``reading_failure`` code;
    ``None``: it can) and the image regions left in it as placeholders, as
    boxes relative to the page (0..1). The vendor's boxes are in the frame of
    the page size it reports (``layout_size``); without one, in the pixels of
    ``image`` (the image read — what glm-ocr does, B-209 PR-2 recorded
    response). The prompt's fixed no-text sentence is a valid, empty reading.
    A raw answer past the size cap is never cleaned. The checks see the text
    without the placeholders."""
    if reading_too_large(reading.text):
        return "", "oversized", ()
    if reading.finish_reason != "length" and is_no_text_reply(reading.text):
        return "", None, ()
    marked, boxes = extract_image_regions(reading.text, region_placeholder)
    size = (reading.layout_size or image_size(image)) if boxes else None
    if boxes and size is None:  # an image that cannot be measured: drop its regions
        marked, _ = extract_image_regions(reading.text, lambda _: "")
        boxes = []
    text = clean_reading(marked)
    failure = reading_failure(
        resolve_figures(text, {}),
        finish_reason=reading.finish_reason,
        layout_chars=reading.layout_chars,
    )
    width, height = size or (1, 1)
    regions = tuple((x0 / width, y0 / height, x1 / width, y1 / height) for x0, y0, x1, y1 in boxes)
    return text, failure, regions
