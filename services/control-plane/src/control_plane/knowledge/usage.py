"""B-209 §5.8 —— 知识库入库的用量:记在上传文档的租户名下,``usage_kind = "knowledge_ingest"``。

两类调用:扫描页识别(每次调用一行,按厂商返回的 ``usage``;兜底换模型时记兜底模型)与
入库的嵌入(切块时的语义嵌入 + 分段嵌入;按模型合成一行,入库结束时写)。``agent_name``
留空 —— 这不是哪个智能体的花费。``knowledge_ingest`` 计入账单(不在
``NON_BILLABLE_USAGE_KINDS`` 里)。写库失败只记日志,不让入库失败。

B-222 PR-2:一次付费入库期间写的用量行同时记进它的 :class:`IngestSpend`(``spend_scope``),
入库结束按价目表算成文档的「实际花费」(``ingest_actual``)。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from expert_work.common.observability import expert_work_counter
from expert_work.persistence.token_usage_store import (
    KNOWLEDGE_INGEST_USAGE_KIND,
    TokenUsageRecord,
    TokenUsageStore,
)
from expert_work.protocol.billing import provider_for_model
from orchestrator.llm import embedding_usage_sink

logger = logging.getLogger(__name__)

#: A usage row is written at most this many times (one in-line retry).
_WRITE_ATTEMPTS = 2
_usage_write_failures = expert_work_counter(
    "expert_work_knowledge_ingest_usage_write_failures_total",
    "knowledge_ingest usage rows that could not be written (spend left unbilled).",
)


@dataclass
class IngestSpend:
    """B-222 PR-2 —— the usage rows one paid ingest wrote (``provider``,
    ``model``, input, output), as the vendor billed them — priced into the
    document's ``ingest_actual`` once the ingest ends."""

    rows: list[tuple[str | None, str, int, int]] = field(default_factory=list)


#: The paid ingest the current task works for (copied into the tasks it starts).
_spend: ContextVar[IngestSpend | None] = ContextVar("knowledge_ingest_spend", default=None)


@contextmanager
def spend_scope(spend: IngestSpend) -> Iterator[IngestSpend]:
    """Every usage row recorded inside (and in tasks started inside) also
    lands in ``spend``."""
    token = _spend.set(spend)
    try:
        yield spend
    finally:
        _spend.reset(token)


@dataclass(frozen=True)
class IngestUsageRecorder:
    """Writes ``knowledge_ingest`` usage rows for the uploading tenant."""

    store: TokenUsageStore

    async def record(
        self,
        *,
        tenant_id: UUID,
        provider: str | None,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        if input_tokens <= 0 and output_tokens <= 0:
            return
        spend = _spend.get()
        if spend is not None:
            # The vendor billed it, whether or not the row below gets written.
            spend.rows.append((provider, model, input_tokens, output_tokens))
        record = TokenUsageRecord(
            tenant_id=tenant_id,
            agent_name="",
            agent_version="",
            model=model,
            provider=provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usage_kind=KNOWLEDGE_INGEST_USAGE_KIND,
        )
        for attempt in range(1, _WRITE_ATTEMPTS + 1):
            try:
                await self.store.insert(record)
                return
            except Exception:
                if attempt < _WRITE_ATTEMPTS:
                    continue
                # Unbilled spend: the counter alerts, the line carries what to back-fill.
                _usage_write_failures.inc()
                logger.warning(
                    "knowledge.ingest_usage_write_failed tenant=%s provider=%s model=%s "
                    "input_tokens=%d output_tokens=%d attempts=%d",
                    tenant_id,
                    provider,
                    model,
                    input_tokens,
                    output_tokens,
                    attempt,
                    exc_info=True,
                )


@dataclass
class _EmbeddingTally:
    tokens: dict[str, int] = field(default_factory=dict)

    def __call__(self, model: str, usage: Mapping[str, Any]) -> None:
        spent = int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0)
        self.tokens[model] = self.tokens.get(model, 0) + spent


@asynccontextmanager
async def billed_embeddings(
    recorder: IngestUsageRecorder | None, *, tenant_id: UUID, model_key: str | None
) -> AsyncIterator[None]:
    """Bill the embedding calls made inside the block to ``tenant_id`` — also
    when the block fails half-way (the vendor charged for what it embedded).

    ``model_key`` is the pinned ``provider/model``; it names the provider of
    the model the vendor answered for (else the catalog's single owner)."""
    if recorder is None:
        yield
        return
    tally = _EmbeddingTally()
    pinned_provider, _, pinned_model = (model_key or "").partition("/")
    try:
        with embedding_usage_sink(tally):
            yield
    finally:
        for model, tokens in tally.tokens.items():
            provider = pinned_provider if model == pinned_model else provider_for_model(model)
            await recorder.record(
                tenant_id=tenant_id,
                provider=provider or None,
                model=model,
                input_tokens=tokens,
                output_tokens=0,
            )
