"""B-209 PR-2 §5.4 / §5.5 / §5.8 —— 一份文档的插图:描述成文字,插回原位。

过了单文档过滤的图(解析进程里做完)→ 同租户 ≥ 3 份文档出现的去掉 → 每份最多 50 张(按面积)→
描述(缓存 → 图片描述模型 → 兜底模型)→ 文字大段的再走识别模型拿图中文字、图表再用描述模型
转写数据(送去的图短边 < 400 px 的都不抄字)→ 引用块。原图由调用方在确认文档仍归自己之后存。

* 描述模型是平台「文档识别」里配的图片描述模型;**没配就不描述**(:meth:`FigureDescriber.enabled`
  为假,调用方整条图片路径都不走);兜底 = 平台的兜底识别模型(要是能收提示词的看图模型),只排在
  图片描述模型后面。能用的模型都用不了(没密钥、账户不可用、限流到重试用完)→ 这张图「待描述」,
  补识别 / 重新摄取时再描述。回答不合格 → 换兜底;仍不合格 → 「失败」。一张图出错不影响别的图,
  不阻塞整份入库。
* 分三步走,每步一次性查缓存(不按张查):描述 → 图表转数据 → 图中文字(一次交给识别器)。
* 缓存按文档(``knowledge_parse_cache``,``figure_desc`` / ``figure_ocr``),键 = 送模型的图片字节
  sha256 + 模型 + 请求参数(提示词版本 / 语言 / 小图 / 温度 / 关思考字段 / 输出上限)+ 规整参数;
  存厂商原始回答,读出时按当前规则重新解析 / 清洗与校验;当前规则不认的会被新回答覆盖。
* 每次拿到厂商回答的调用都按厂商返回的 ``usage`` 记一行 ``knowledge_ingest``,记在知识库所属
  租户名下(兜底换模型记兜底模型;不合格的回答厂商也收了钱,一样记;回答里没有 ``usage`` 记警告)。
* 每个模型的并发名额与扫描页识别共用一份(:class:`~.recognizer.ModelLimits`)。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

import httpx

from control_plane.knowledge.recognition import figure_images
from control_plane.knowledge.recognition.cleaning import (
    clean_reading,
    reading_failure,
    reading_too_large,
)
from control_plane.knowledge.recognition.clients import (
    CHAT_COMPLETIONS_URLS,
    ChatVlmClient,
    Reading,
    RecognitionCallError,
    RecognitionClient,
    read_with_retry,
)
from control_plane.knowledge.recognition.figure_describe import (
    CHART_MAX_TOKENS,
    CHART_PROMPT,
    CHART_PROMPT_VERSION,
    CHART_USER_TEXT,
    DESCRIBE_MAX_TOKENS,
    DESCRIBE_TEMPERATURE,
    DESCRIBE_USER_TEXT,
    Description,
    Language,
    describe_prompt,
    describe_prompt_tag,
    description_failure,
    parse_description,
)
from control_plane.knowledge.recognition.figure_extract import FigureExtraction
from control_plane.knowledge.recognition.figure_filters import (
    TRANSCRIBE_MIN_SIDE_PX,
    FigureCandidate,
    select_figures,
)
from control_plane.knowledge.recognition.figure_markup import quote_block, resolve_figures
from control_plane.knowledge.recognition.figure_storage import FigureStorage, figure_id
from control_plane.knowledge.recognition.recognizer import (
    ERROR_CREDENTIAL_MISSING,
    ERROR_NOT_CONFIGURED,
    ApiKeySource,
    DocumentRecognizer,
    ModelChoice,
    ModelLimits,
    RecognitionPlan,
    RecognitionSettingsSource,
    UsageSink,
    cache_key,
    model_concurrency,
)
from expert_work.persistence.knowledge.base import (
    ClaimedIngestion,
    KnowledgeStore,
    ParseCacheKind,
)
from expert_work.protocol import DocumentStatus, FigureStats, PageStatus, catalog_entry
from orchestrator.agent_factory import thinking_off_request_fields

logger = logging.getLogger(__name__)

FigureStatus = Literal["described", "failed", "pending"]

#: Builds a vision-chat client for one fixed prompt: (model, API key, system
#: prompt, user text, output cap). Tests inject fakes.
FigureClientFactory = Callable[[ModelChoice, str, str, str, int], RecognitionClient]


def default_figure_client(http: httpx.AsyncClient | None = None) -> FigureClientFactory:
    """The OpenAI-compatible vision chat adapter (thinking off, output cap)."""

    def build(
        choice: ModelChoice, api_key: str, prompt: str, user_text: str, max_tokens: int
    ) -> RecognitionClient:
        url = CHAT_COMPLETIONS_URLS.get(choice.provider)
        if url is None:
            msg = f"no chat endpoint for provider {choice.provider!r}"
            raise ValueError(msg)
        return ChatVlmClient(
            url=url,
            api_key=api_key,
            model=choice.model,
            request_fields=_request_fields(choice, max_tokens),
            http=http,
            system_prompt=prompt,
            user_text=user_text,
            temperature=DESCRIBE_TEMPERATURE,
        )

    return build


def _request_fields(choice: ModelChoice, max_tokens: int) -> dict[str, Any]:
    return thinking_off_request_fields(choice.provider, choice.model, max_tokens=max_tokens)


def _can_describe(choice: ModelChoice) -> bool:
    """A vision chat model that takes a prompt (not a layout-parsing API)."""
    entry = catalog_entry(choice.provider, choice.model)
    return entry is not None and entry.vision and not entry.ocr_only


@dataclass(frozen=True)
class FigureOutcome:
    """What became of a document's figures: each described figure's quote
    block (by figure number), the counts, per page (1-based) how many figures
    were kept / described there, and the described originals to store (figure
    id → JPEG)."""

    blocks: Mapping[int, str]
    stats: FigureStats
    kept_pages: Mapping[int, int]
    described_pages: Mapping[int, int]
    originals: Mapping[str, bytes] = field(default_factory=dict)


@dataclass(frozen=True)
class _Engine:
    choice: ModelChoice
    api_key: str | None
    unavailable: str | None = None


@dataclass(frozen=True)
class _Document:
    tenant_id: UUID
    kb_id: UUID
    document_id: UUID
    language: Language


@dataclass(frozen=True)
class _Ask:
    """One fixed-prompt question to the describe engines."""

    kind: ParseCacheKind
    prompt: str
    user_text: str
    max_tokens: int
    tag: str
    #: The answer's failure code (``None``: keep it).
    check: Callable[[Reading], str | None]


@dataclass(frozen=True)
class _Answer:
    raw: str | None
    status: FigureStatus


class FigureDescriber:
    """Describes the figures of documents (one per process; per-model
    concurrency slots shared with the recognizer through ``limits``)."""

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        settings: RecognitionSettingsSource,
        api_keys: ApiKeySource,
        recognizer: DocumentRecognizer | None = None,
        storage: FigureStorage | None = None,
        usage: UsageSink | None = None,
        client_factory: FigureClientFactory | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        limits: ModelLimits | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._api_keys = api_keys
        self._recognizer = recognizer
        self._storage = storage
        self._usage = usage
        self._client_factory = client_factory or default_figure_client()
        self._sleep = sleep
        self._limits = limits or ModelLimits()

    @property
    def storage(self) -> FigureStorage | None:
        return self._storage

    async def tidy(self, claim: ClaimedIngestion, *, settled: bool) -> None:
        """After an ingest ends (review H5): settled READY → the document's
        originals of earlier generations go; not settled → what this
        generation wrote goes, unless the same generation is still being
        ingested (another worker took the lease) or served. Best effort: the
        reconciler removes whatever a failure here leaves."""
        if self._storage is None:
            return
        where = {
            "tenant_id": claim.tenant_id,
            "kb_id": claim.kb_id,
            "document_id": claim.document_id,
        }
        try:
            if settled:
                await self._storage.delete_older_generations(**where, generation=claim.generation)
                return
            document = await self._store.get_document(
                tenant_id=claim.tenant_id, document_id=claim.document_id
            )
            if document is None:
                await self._storage.delete_document(**where)
            elif (
                document.ingest_generation != claim.generation
                or document.status is DocumentStatus.FAILED
            ):
                await self._storage.delete_generation(**where, generation=claim.generation)
        except Exception:
            logger.warning(
                "knowledge.figure_tidy_failed document=%s generation=%s",
                claim.document_id,
                claim.generation,
                exc_info=True,
            )

    async def store_originals(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        document_id: UUID,
        generation: int | None,
        originals: Mapping[str, bytes],
    ) -> None:
        """Store described originals under the ingest's generation (after the
        caller checked the document is still its own). A failure costs only
        the preview thumbnail."""
        if self._storage is None:
            return
        for identifier, image in originals.items():
            try:
                await self._storage.put(
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                    document_id=document_id,
                    generation=generation,
                    figure=identifier,
                    image=image,
                )
            except Exception:
                logger.warning(
                    "knowledge.figure_store_failed document=%s", document_id, exc_info=True
                )

    async def enabled(self, *, tenant_id: UUID) -> bool:
        """A figure description model is configured, takes a prompt and has a
        key — only then are figures taken out of documents at all."""
        engines = await self._engines(tenant_id)
        return bool(engines) and engines[0].api_key is not None

    async def describe(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        document_id: UUID,
        extraction: FigureExtraction,
        language: Language,
    ) -> FigureOutcome:
        """Describe the document's figures (see module doc); never raises for
        one figure going wrong."""
        candidates = extraction.candidates
        hashes = tuple(c.pixel_hash for c in candidates)
        others = await self._other_documents(tenant_id, document_id, hashes)
        selection = select_figures(candidates, others)
        engines = await self._engines(tenant_id) if selection.kept else ()
        document = _Document(tenant_id, kb_id, document_id, language)
        described, statuses = await self._describe_all(document, selection.kept, engines)
        texts = await self._figure_texts(document, selection.kept, described, engines)
        blocks: dict[int, str] = {}
        originals: dict[str, bytes] = {}
        kept_pages: Counter[int] = Counter()
        described_pages: Counter[int] = Counter()
        for figure in selection.kept:
            kept_pages.update(figure.pages)
            description = described.get(figure.number)
            if description is None:
                continue
            identifier = figure_id(figure.image)
            blocks[figure.number] = quote_block(
                identifier,
                description.description,
                text=texts.get(figure.number, ""),
                chart=description.chart,
            )
            originals[identifier] = figure.image
            described_pages.update(figure.pages)
        counted = Counter(statuses.values())
        return FigureOutcome(
            blocks=blocks,
            stats=FigureStats(
                extracted=extraction.extracted,
                filtered=extraction.filtered + selection.tenant_repeated,
                capped=extraction.capped + selection.capped,
                unsupported=extraction.unsupported,
                described=counted["described"],
                failed=counted["failed"],
                pending=counted["pending"],
            ),
            kept_pages=dict(kept_pages),
            described_pages=dict(described_pages),
            originals=originals,
        )

    async def _describe_all(
        self, document: _Document, figures: Sequence[FigureCandidate], engines: Sequence[_Engine]
    ) -> tuple[dict[int, Description], dict[int, FigureStatus]]:
        """Step 1: every figure described (one cache read for all)."""
        if not engines:
            return {}, dict.fromkeys((f.number for f in figures), "pending")
        asks = {f.number: self._describe_ask(document, f) for f in figures}
        answers = await self._ask_all(document, figures, engines, asks)
        described: dict[int, Description] = {}
        statuses: dict[int, FigureStatus] = {}
        for figure in figures:
            answer = answers[figure.number]
            parsed = parse_description(answer.raw) if answer.raw is not None else None
            if parsed is not None:
                described[figure.number] = parsed
                statuses[figure.number] = "described"
            else:
                statuses[figure.number] = (
                    "failed" if answer.status == "described" else answer.status
                )
        return described, statuses

    def _describe_ask(self, document: _Document, figure: FigureCandidate) -> _Ask:
        small = _short_side(figure) < TRANSCRIBE_MIN_SIDE_PX
        return _Ask(
            kind="figure_desc",
            prompt=describe_prompt(document.language, small=small),
            user_text=DESCRIBE_USER_TEXT,
            max_tokens=DESCRIBE_MAX_TOKENS,
            tag=describe_prompt_tag(document.language, small=small),
            check=lambda reading: description_failure(
                reading.text, finish_reason=reading.finish_reason
            ),
        )

    async def _figure_texts(
        self,
        document: _Document,
        figures: Sequence[FigureCandidate],
        described: Mapping[int, Description],
        engines: Sequence[_Engine],
    ) -> dict[int, str]:
        """Steps 2 and 3: a chart's data (the describe models, chart prompt)
        and a text-heavy figure's text (the recognition models); never for a
        figure whose image sent is under :data:`TRANSCRIBE_MIN_SIDE_PX` on its
        short side. A figure whose text cannot be read keeps its description."""
        readable = [
            f for f in figures if f.number in described and _short_side(f) >= TRANSCRIBE_MIN_SIDE_PX
        ]
        charts = [f for f in readable if described[f.number].chart]
        texts: dict[int, str] = {}
        if charts:
            ask = _Ask(
                kind="figure_ocr",
                prompt=CHART_PROMPT,
                user_text=CHART_USER_TEXT,
                max_tokens=CHART_MAX_TOKENS,
                tag=CHART_PROMPT_VERSION,
                check=_transcription_failure,
            )
            answers = await self._ask_all(
                document, charts, engines, {f.number: ask for f in charts}
            )
            for figure in charts:
                raw = answers[figure.number].raw
                if raw is not None:
                    texts[figure.number] = clean_reading(raw)
        heavy = [
            f
            for f in readable
            if not described[f.number].chart and described[f.number].text_amount == "lots"
        ]
        if heavy:
            texts.update(await self._read_texts(document, heavy))
        return texts

    async def _read_texts(
        self, document: _Document, figures: Sequence[FigureCandidate]
    ) -> dict[int, str]:
        if self._recognizer is None:
            return {}
        try:
            plan: RecognitionPlan = await self._recognizer.plan(tenant_id=document.tenant_id)
            if not plan.usable:
                return {}
            outcomes = await self._recognizer.recognize(
                tenant_id=document.tenant_id,
                document_id=document.document_id,
                plan=plan,
                images={f.number: f.image for f in figures},
                render=figure_images.render_params(),
                kind="figure_ocr",
            )
        except Exception:
            logger.warning("knowledge.figure_text_failed", exc_info=True)
            return {}
        return {
            number: resolve_figures(outcome.text, {}).strip()
            for number, outcome in outcomes.items()
            if outcome.status is PageStatus.RECOGNIZED
        }

    async def _other_documents(
        self, tenant_id: UUID, document_id: UUID, hashes: Sequence[str]
    ) -> dict[str, int]:
        if not hashes:
            return {}
        try:
            return await self._store.count_figure_documents(
                tenant_id=tenant_id, pixel_hashes=hashes, exclude_document_id=document_id
            )
        except Exception:
            # Without the counts a tenant's logo may be described once more —
            # money, not correctness.
            logger.warning("knowledge.figure_repeat_count_failed", exc_info=True)
            return {}

    async def _engines(self, tenant_id: UUID) -> tuple[_Engine, ...]:
        """The figure model, then the fallback behind it — none at all when
        no figure model is configured (figure description is off)."""
        settings = await self._settings.recognition_settings()
        if settings.figure is None or not _can_describe(settings.figure):
            return ()
        choices = [settings.figure]
        fallback = settings.fallback
        if fallback is not None and _can_describe(fallback) and fallback != settings.figure:
            choices.append(fallback)
        engines = []
        for choice in choices:
            try:
                key = await self._api_keys(tenant_id=tenant_id, provider=choice.provider)
            except Exception as exc:
                logger.warning(
                    "knowledge.figure_credential_missing model=%s tenant=%s detail=%s",
                    choice.key,
                    tenant_id,
                    exc,
                )
                engines.append(_Engine(choice, None, ERROR_CREDENTIAL_MISSING))
                continue
            engines.append(_Engine(choice, key))
        return tuple(engines)

    async def _ask_all(
        self,
        document: _Document,
        figures: Sequence[FigureCandidate],
        engines: Sequence[_Engine],
        asks: Mapping[int, _Ask],
    ) -> dict[int, _Answer]:
        """Each figure's answer to its question: one cache read for all, then
        the uncached ones asked concurrently (within the models' slots)."""
        render = figure_images.render_params()
        keys = {
            f.number: [
                cache_key(f.image, e.choice.key, self._tag(e.choice, asks[f.number]), render)
                for e in engines
            ]
            for f in figures
        }
        kind = next(iter(asks.values())).kind
        cached = await self._cached(document, kind, [k for row in keys.values() for k in row])

        async def answer(figure: FigureCandidate) -> _Answer:
            try:
                return await self._ask(
                    document,
                    figure.image,
                    engines,
                    asks[figure.number],
                    keys[figure.number],
                    cached,
                )
            except Exception:
                logger.exception("knowledge.figure_failed document=%s", document.document_id)
                return _Answer(None, "failed")

        answers = await asyncio.gather(*(answer(figure) for figure in figures))
        return dict(zip((f.number for f in figures), answers, strict=True))

    async def _ask(
        self,
        document: _Document,
        image: bytes,
        engines: Sequence[_Engine],
        ask: _Ask,
        keys: Sequence[str],
        cached: Mapping[str, Mapping[str, Any]],
    ) -> _Answer:
        """The first acceptable raw answer — cached or fresh — and the
        status; ``(None, pending | failed)`` when there is none."""
        if not engines:
            return _Answer(None, "pending")
        for key in keys:
            hit = cached.get(key)
            if hit is not None:
                reading = Reading(
                    text=str(hit.get("raw") or ""),
                    finish_reason=_text_or_none(hit.get("finish_reason")),
                )
                if ask.check(reading) is None:
                    return _Answer(reading.text, "described")
        errors: list[str] = []
        unavailable_only = True
        for engine, key in zip(engines, keys, strict=True):
            if engine.api_key is None:
                errors.append(f"{engine.choice.key}: {engine.unavailable or ERROR_NOT_CONFIGURED}")
                continue
            client = self._client_factory(
                engine.choice, engine.api_key, ask.prompt, ask.user_text, ask.max_tokens
            )
            slot = self._limits.slot(engine.choice, model_concurrency(engine.choice))
            try:
                reading = await read_with_retry(client, image, sleep=self._sleep, limit=slot)
            except RecognitionCallError as exc:
                logger.warning(
                    "knowledge.figure_call_failed model=%s kind=%s detail=%s",
                    engine.choice.key,
                    exc.kind,
                    exc,
                )
                await self._bill(document, engine.choice, exc.input_tokens, exc.output_tokens, True)
                errors.append(f"{engine.choice.key}: {exc.kind}")
                unavailable_only = unavailable_only and (exc.unavailable or exc.retryable)
                continue
            await self._bill(
                document,
                engine.choice,
                reading.input_tokens,
                reading.output_tokens,
                reading.usage_reported,
            )
            failure = ask.check(reading)
            if failure is not None:
                errors.append(f"{engine.choice.key}: bad_output({failure})")
                unavailable_only = False
                continue
            await self._remember(document, ask.kind, key, engine.choice, reading)
            return _Answer(reading.text, "described")
        logger.info("knowledge.figure_not_described errors=%s", "; ".join(errors))
        return _Answer(None, "pending" if unavailable_only else "failed")

    def _tag(self, choice: ModelChoice, ask: _Ask) -> str:
        fields = json.dumps(_request_fields(choice, ask.max_tokens), sort_keys=True)
        return f"chat_vlm|{ask.tag}|temperature={DESCRIBE_TEMPERATURE}|{fields}"

    async def _cached(
        self, document: _Document, kind: ParseCacheKind, keys: Sequence[str]
    ) -> dict[str, Mapping[str, Any]]:
        try:
            entries = await self._store.get_parse_cache(
                tenant_id=document.tenant_id,
                document_id=document.document_id,
                kind=kind,
                keys=keys,
            )
        except Exception:
            logger.warning("knowledge.figure_cache_read_failed", exc_info=True)
            return {}
        return {key: entry.result for key, entry in entries.items()}

    async def _remember(
        self,
        document: _Document,
        kind: ParseCacheKind,
        key: str,
        choice: ModelChoice,
        reading: Reading,
    ) -> None:
        try:
            await self._store.put_parse_cache(
                tenant_id=document.tenant_id,
                document_id=document.document_id,
                kind=kind,
                key=key,
                model=choice.key,
                result={"raw": reading.text, "finish_reason": reading.finish_reason},
            )
        except Exception:
            logger.warning("knowledge.figure_cache_write_failed", exc_info=True)

    async def _bill(
        self,
        document: _Document,
        choice: ModelChoice,
        input_tokens: int,
        output_tokens: int,
        reported: bool,
    ) -> None:
        if not reported:
            logger.warning("knowledge.figure_usage_missing model=%s", choice.key)
        if self._usage is None or (input_tokens <= 0 and output_tokens <= 0):
            return
        await self._usage.record(
            tenant_id=document.tenant_id,
            provider=choice.provider,
            model=choice.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


def _short_side(figure: FigureCandidate) -> int:
    """The short side of the image actually sent (the JPEG may be scaled
    down from the source), never more than the source's."""
    sent = figure_images.image_size(figure.image)
    return min(sent) if sent is not None else figure.short_side


def _transcription_failure(reading: Reading) -> str | None:
    if reading_too_large(reading.text):
        return "oversized"
    return reading_failure(
        clean_reading(reading.text), finish_reason=reading.finish_reason, layout_chars=None
    )


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None
