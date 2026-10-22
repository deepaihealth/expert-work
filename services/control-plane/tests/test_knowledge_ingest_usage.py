"""B-209 §5.8 / review #6 —— 入库用量写库:失败先在当场重试一次,仍失败记计数器 + 带数字的
告警行(能据此补账),不让入库失败。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from control_plane.knowledge import usage as usage_module
from control_plane.knowledge.usage import IngestUsageRecorder
from expert_work.persistence.token_usage_store import (
    KNOWLEDGE_INGEST_USAGE_KIND,
    InMemoryTokenUsageStore,
    TokenUsageRecord,
)


class _FlakyStore(InMemoryTokenUsageStore):
    """Fails the first ``failures`` inserts."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.attempts = 0

    async def insert(self, record: TokenUsageRecord) -> TokenUsageRecord:
        self.attempts += 1
        if self.attempts <= self.failures:
            msg = "connection reset"
            raise ConnectionError(msg)
        return await super().insert(record)


def _failures() -> float:
    return float(usage_module._usage_write_failures._value.get())  # type: ignore[attr-defined]


async def test_a_failed_write_is_retried_once() -> None:
    store, tenant = _FlakyStore(failures=1), uuid4()
    before = _failures()
    await IngestUsageRecorder(store).record(
        tenant_id=tenant, provider="glm", model="glm-ocr", input_tokens=10, output_tokens=2
    )
    rows = await store.list_for_tenant(tenant_id=tenant)
    assert [(r.model, r.usage_kind, r.input_tokens) for r in rows] == [
        ("glm-ocr", KNOWLEDGE_INGEST_USAGE_KIND, 10)
    ]
    assert store.attempts == 2
    assert _failures() == before


async def test_a_write_failing_twice_is_counted_and_logged_with_the_amounts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, tenant = _FlakyStore(failures=5), uuid4()
    before = _failures()
    with caplog.at_level("WARNING"):
        await IngestUsageRecorder(store).record(
            tenant_id=tenant, provider="glm", model="glm-ocr", input_tokens=10, output_tokens=2
        )
    assert store.attempts == 2
    assert _failures() == before + 1
    messages = [record.getMessage() for record in caplog.records]
    (line,) = [message for message in messages if "ingest_usage_write_failed" in message]
    assert f"tenant={tenant}" in line
    assert "input_tokens=10 output_tokens=2" in line
