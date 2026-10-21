"""B-209 §5.3 / §5.5 / §5.6 —— 一份文档的扫描页识别。

每页:先查缓存(主模型与兜底模型的键都查,哪个识别过都不再付费)→ 主模型 → 清洗 + 校验 →
不合格换兜底模型再识别一次 → 仍不合格标「失败」(可补识别),不阻塞整份入库。

* 模型没配、没有密钥、账户不可用、重试用完仍限流 / 5xx:这一页标「待识别」——不是识别
  错了,是没能识别;配好后补识别只处理这些页(其余走缓存)。
* 每次拿到厂商回答的调用都按厂商返回的 ``usage`` 记一行 ``knowledge_ingest`` 用量,记在
  上传文档的租户名下(不合格的回答厂商也收了钱,一样记)。
* 缓存按租户隔离(设计 ⑩),键 = 图片字节 sha256 + 模型 + 提示词版本 + 渲染参数。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol
from uuid import UUID

import httpx

from control_plane.knowledge.recognition.cleaning import (
    clean_reading,
    is_no_text_reply,
    reading_failure,
)
from control_plane.knowledge.recognition.clients import (
    CHAT_COMPLETIONS_URLS,
    DEFAULT_OCR_MAX_TOKENS,
    LAYOUT_PARSING_URLS,
    OCR_PROMPT_VERSION,
    ChatVlmClient,
    LayoutParsingClient,
    Reading,
    RecognitionCallError,
    RecognitionClient,
    read_with_retry,
)
from control_plane.knowledge.recognition.pages import render_params
from expert_work.persistence.knowledge.base import KnowledgeStore, ParseCacheEntry
from expert_work.protocol import PageStatus, catalog_entry
from orchestrator.agent_factory import thinking_off_request_fields

logger = logging.getLogger(__name__)

#: Concurrent calls per model when the catalog declares no ``max_concurrency``.
DEFAULT_MODEL_CONCURRENCY = 4
#: Scanned pages rendered per parse-pool call: enough to keep the models busy
#: while the next batch renders, small enough that a batch of JPEGs (about
#: 0.3 MB each) is a cheap hand-over between processes.
RENDER_BATCH = 16
#: A page's ``error`` is cut to this many characters.
_ERROR_CAP = 300
_CACHE_KIND: Literal["page_ocr"] = "page_ocr"


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
    """The platform's 「文档识别」 settings (B-209 §5.7). ``None`` = switched off."""

    primary: ModelChoice | None
    fallback: ModelChoice | None
    max_pages: int


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

#: Builds a client for a model from its API key (tests inject fakes).
ClientFactory = Callable[[ModelChoice, str], RecognitionClient]


@dataclass(frozen=True)
class PageOutcome:
    """What recognition made of one page."""

    status: PageStatus
    text: str = ""
    provider: str | None = None
    model: str | None = None
    cached: bool = False
    error: str | None = None


@dataclass(frozen=True)
class _Engine:
    """One model, resolved for one document: its client (``None`` with the
    reason when it cannot be used), cache tag and limits."""

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
        return "; ".join(reasons) or "no recognition model configured"


def default_client(
    http: httpx.AsyncClient | None = None,
) -> ClientFactory:
    """The HTTP adapter for a model, by its catalog ``ocr_caps.api``."""

    def build(choice: ModelChoice, api_key: str) -> RecognitionClient:
        entry = catalog_entry(choice.provider, choice.model)
        caps = entry.ocr_caps if entry is not None else None
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
        fields = thinking_off_request_fields(
            choice.provider,
            choice.model,
            max_tokens=caps.max_output_tokens or DEFAULT_OCR_MAX_TOKENS,
        )
        return ChatVlmClient(
            url=url, api_key=api_key, model=choice.model, request_fields=fields, http=http
        )

    return build


def cache_key(image: bytes, engine_key: str, cache_tag: str, render: str) -> str:
    """``knowledge_parse_cache.cache_key`` (B-209 §5.5)."""
    digest = hashlib.sha256(image).hexdigest()
    return hashlib.sha256(f"{digest}|{engine_key}|{cache_tag}|{render}".encode()).hexdigest()


class DocumentRecognizer:
    """Recognizes the scanned pages of documents (one per process; the
    per-model concurrency limits are shared by every document in flight)."""

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        settings: RecognitionSettingsSource,
        api_keys: ApiKeySource,
        usage: UsageSink | None = None,
        client_factory: ClientFactory | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._settings = settings
        self._api_keys = api_keys
        self._usage = usage
        self._client_factory = client_factory or default_client()
        self._sleep = sleep
        self._limits: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Semaphore]] = {}

    async def plan(self, *, tenant_id: UUID) -> RecognitionPlan:
        """Resolve the configured models (and their keys) once per document."""
        settings = await self._settings.recognition_settings()
        engines: list[_Engine] = []
        for choice in (settings.primary, settings.fallback):
            if choice is not None and choice not in (engine.choice for engine in engines):
                engines.append(await self._engine(choice, tenant_id))
        return RecognitionPlan(engines=tuple(engines), max_pages=settings.max_pages)

    async def _engine(self, choice: ModelChoice, tenant_id: UUID) -> _Engine:
        entry = catalog_entry(choice.provider, choice.model)
        caps = entry.ocr_caps if entry is not None else None
        tag = OCR_PROMPT_VERSION if caps is None or caps.api == "chat_vlm" else caps.api
        engine = _Engine(
            choice=choice,
            client=None,
            unavailable=None,
            cache_tag=tag,
            max_pixels=caps.max_pixels if caps is not None else None,
            max_concurrency=(caps.max_concurrency if caps is not None else None)
            or DEFAULT_MODEL_CONCURRENCY,
        )
        try:
            api_key = await self._api_keys(tenant_id=tenant_id, provider=choice.provider)
            client = self._client_factory(choice, api_key)
        except Exception as exc:
            logger.info("knowledge.recognition_model_unavailable model=%s: %s", choice.key, exc)
            return replace(engine, unavailable=f"unavailable ({type(exc).__name__}: {exc})")
        return replace(engine, client=client)

    async def recognize(
        self,
        *,
        tenant_id: UUID,
        plan: RecognitionPlan,
        images: Mapping[int, bytes],
        render: str,
    ) -> dict[int, PageOutcome]:
        """Recognize the given pages (index → JPEG bytes)."""
        keys = {
            index: [
                cache_key(image, engine.choice.key, engine.cache_tag, render)
                for engine in plan.engines
            ]
            for index, image in images.items()
        }
        cached = await self._cached(tenant_id, [key for row in keys.values() for key in row])
        results = await asyncio.gather(
            *(
                self._page(tenant_id, plan, image, keys[index], cached)
                for index, image in images.items()
            )
        )
        return dict(zip(images, results, strict=True))

    async def recognize_document(
        self, *, tenant_id: UUID, pages: Sequence[int], render: PageRenderer
    ) -> dict[int, PageOutcome]:
        """Recognize one document's scanned pages (0-based indexes, in page
        order) up to the per-document cap — the rest are ``over_limit``.
        ``render`` draws pages as JPEG (in the parse pool) a batch at a time;
        the next batch renders while this one is read. No usable model: every
        page is ``pending``, nothing is rendered."""
        plan = await self.plan(tenant_id=tenant_id)
        within, over = list(pages[: plan.max_pages]), pages[plan.max_pages :]
        over_cap = PageOutcome(
            status=PageStatus.OVER_LIMIT,
            error=f"past the {plan.max_pages}-page recognition limit per document",
        )
        outcomes = dict.fromkeys(over, over_cap)
        if not plan.usable:
            pending = PageOutcome(status=PageStatus.PENDING, error=plan.unavailable_reason)
            outcomes.update(dict.fromkeys(within, pending))
            return outcomes
        params = render_params(plan.max_pixels)
        batches = [
            within[start : start + RENDER_BATCH] for start in range(0, len(within), RENDER_BATCH)
        ]

        def rendering(indexes: list[int]) -> asyncio.Task[list[bytes]]:
            return asyncio.create_task(render(indexes, plan.max_pixels))

        upcoming = rendering(batches[0]) if batches else None
        try:
            for position, indexes in enumerate(batches):
                assert upcoming is not None  # noqa: S101 — one task per batch
                images = await upcoming
                upcoming = rendering(batches[position + 1]) if position + 1 < len(batches) else None
                outcomes.update(
                    await self.recognize(
                        tenant_id=tenant_id,
                        plan=plan,
                        images=dict(zip(indexes, images, strict=True)),
                        render=params,
                    )
                )
        finally:
            if upcoming is not None:
                upcoming.cancel()
                await asyncio.gather(upcoming, return_exceptions=True)
        return outcomes

    async def _cached(self, tenant_id: UUID, keys: Sequence[str]) -> dict[str, ParseCacheEntry]:
        try:
            return await self._store.get_parse_cache(
                tenant_id=tenant_id, kind=_CACHE_KIND, keys=keys
            )
        except Exception:
            # A cache outage costs money (pages are read again), not correctness.
            logger.warning("knowledge.recognition_cache_read_failed", exc_info=True)
            return {}

    async def _page(
        self,
        tenant_id: UUID,
        plan: RecognitionPlan,
        image: bytes,
        keys: Sequence[str],
        cached: Mapping[str, ParseCacheEntry],
    ) -> PageOutcome:
        for engine, key in zip(plan.engines, keys, strict=True):
            hit = cached.get(key)
            if hit is not None:
                return PageOutcome(
                    status=PageStatus.RECOGNIZED,
                    text=str(hit.result.get("text") or ""),
                    provider=engine.choice.provider,
                    model=engine.choice.model,
                    cached=True,
                )
        errors: list[str] = []
        unavailable_only = True
        for engine, key in zip(plan.engines, keys, strict=True):
            if engine.client is None:
                errors.append(f"{engine.choice.key}: {engine.unavailable}")
                continue
            try:
                async with self._limit(engine):
                    reading = await read_with_retry(engine.client, image, sleep=self._sleep)
            except RecognitionCallError as exc:
                errors.append(f"{engine.choice.key}: {exc}")
                # Out of retries on load / 5xx, or no usable account: the vendor
                # is unavailable — the page waits. A rejected request is a failure.
                unavailable_only = unavailable_only and (exc.unavailable or exc.retryable)
                continue
            await self._bill(tenant_id, engine.choice, reading)
            text, failure = _accept(reading)
            if failure is not None:
                errors.append(f"{engine.choice.key}: {failure}")
                unavailable_only = False
                continue
            await self._remember(tenant_id, key, engine.choice, text)
            return PageOutcome(
                status=PageStatus.RECOGNIZED,
                text=text,
                provider=engine.choice.provider,
                model=engine.choice.model,
            )
        if not plan.engines:
            errors.append("no recognition model configured")
        return PageOutcome(
            status=PageStatus.PENDING if unavailable_only else PageStatus.FAILED,
            error="; ".join(errors)[:_ERROR_CAP],
        )

    def _limit(self, engine: _Engine) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        held = self._limits.get(engine.choice.key)
        if held is None or held[0] is not loop:
            held = (loop, asyncio.Semaphore(engine.max_concurrency))
            self._limits[engine.choice.key] = held
        return held[1]

    async def _bill(self, tenant_id: UUID, choice: ModelChoice, reading: Reading) -> None:
        if not reading.usage_reported:
            logger.warning("knowledge.recognition_usage_missing model=%s", choice.key)
        if self._usage is None:
            return
        await self._usage.record(
            tenant_id=tenant_id,
            provider=choice.provider,
            model=choice.model,
            input_tokens=reading.input_tokens,
            output_tokens=reading.output_tokens,
        )

    async def _remember(self, tenant_id: UUID, key: str, choice: ModelChoice, text: str) -> None:
        try:
            await self._store.put_parse_cache(
                tenant_id=tenant_id,
                kind=_CACHE_KIND,
                key=key,
                model=choice.key,
                result={"text": text},
            )
        except Exception:
            logger.warning("knowledge.recognition_cache_write_failed", exc_info=True)


def _accept(reading: Reading) -> tuple[str, str | None]:
    """The cleaned text and why it cannot be kept (``None``: it can). The
    prompt's fixed no-text sentence is a valid, empty reading."""
    if reading.finish_reason != "length" and is_no_text_reply(reading.text):
        return "", None
    text = clean_reading(reading.text)
    return text, reading_failure(
        text, finish_reason=reading.finish_reason, layout_blocks=reading.layout_blocks
    )
