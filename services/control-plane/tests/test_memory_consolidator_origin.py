"""B-300 PR 2 —— 整理与来源(设计 §6.2、§13.1 假簇标记那一行、§13.3)。

* 一簇里既有「来自助手」、又有用户消息里说的 / 用户更正的 → 取候选时直接滤掉,不调模型;
  滤掉的簇的成员照样记进「已见」,同一片里的下一个种子不会取出一组略有不同的成员再问;
  下一轮同样 0 次调用(不用记标记:是程序判断,每轮结果一样);
* 来源未记录的不算哪一边:「来自助手 + 未记录」照常汇总,汇总行来源为空;
* 全是来自助手的簇 → 汇总行来源「来自助手」、置信度 ≤ 0.6;
* 汇总行带这次算向量的模型名(嵌入器报得出来时)。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from control_plane.memory_consolidator import MemoryConsolidator, make_consolidator_embedder
from control_plane.tenancy import TenantConfigService
from expert_work.persistence import InMemoryMemoryStore
from expert_work.persistence.memory.hash import hash_content
from expert_work.persistence.tenant_config import InMemoryTenantConfigStore
from expert_work.protocol import MemoryItem, TenantConfigPatch
from expert_work.protocol.memory_item import MemoryOrigin
from tests.test_memory_consolidator import _build_logger, _FakeEmbedder, _ScriptedAuxModel

_TENANT = UUID("eeeeeeee-cccc-cccc-cccc-cccccccccccc")
_USER = UUID("ffffffff-dddd-dddd-dddd-dddddddddddd")
_KEEP = '{"keep": true, "summary": "%s", "reject_reason": null}'
_MODEL = "vendor-y/embed-2"


def _angle(degrees: float) -> tuple[float, float]:
    rad = math.radians(degrees)
    return (math.cos(rad), math.sin(rad))


def _item(
    content: str,
    embedding: tuple[float, ...],
    *,
    origin: MemoryOrigin | None,
    confidence: float = 0.5,
    minutes_ago: int = 0,
) -> MemoryItem:
    created = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return MemoryItem(
        id=uuid4(),
        tenant_id=_TENANT,
        user_id=_USER,
        kind="fact",
        content=content,
        embedding=embedding,
        content_hash=hash_content(content),
        created_at=created,
        last_used_at=created,
        origin=origin,
        confidence=confidence,
    )


class _ReportingOrchestratorEmbedder:
    """``DynamicResolvingEmbedder`` 的形状:批量 ``embed`` + 报出模型的那一个。"""

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        return [(0.5, 0.5) for _ in texts]

    async def embed_reporting_model(
        self, texts: Sequence[str], *, tenant_id: UUID
    ) -> tuple[list[tuple[float, ...]], str]:
        return await self.embed(texts, tenant_id=tenant_id), _MODEL


async def _worker(
    store: InMemoryMemoryStore, aux: object, embedder: object | None = None
) -> MemoryConsolidator:
    audit_logger, _audit_store = _build_logger()
    config_service = TenantConfigService(
        store=InMemoryTenantConfigStore(), audit_logger=audit_logger
    )
    await config_service.upsert(
        tenant_id=_TENANT, patch=TenantConfigPatch(display_name="Test Tenant"), actor_id="seed"
    )
    return MemoryConsolidator(
        memory_store=store,
        tenant_config_service=config_service,
        audit_logger=audit_logger,
        aux_model=aux,  # type: ignore[arg-type]
        embedder=embedder or _FakeEmbedder(),  # type: ignore[arg-type]
    )


def _consolidated(store: InMemoryMemoryStore) -> list[MemoryItem]:
    return [r for r in store._rows if r.status == "consolidated"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_origin", "user_confidence"),
    [("user_stated", 0.9), (None, 1.0)],
    ids=["stated", "corrected"],
)
async def test_mixed_source_cluster_costs_no_model_call(
    user_origin: MemoryOrigin | None, user_confidence: float
) -> None:
    store = InMemoryMemoryStore()
    store._rows.extend(
        [
            _item("a", (1.0, 0.0), origin=user_origin, confidence=user_confidence, minutes_ago=3),
            _item("b", (1.0, 0.0), origin="assistant_derived", minutes_ago=2),
            _item("c", (1.0, 0.0), origin="assistant_derived", minutes_ago=1),
        ]
    )
    aux = _ScriptedAuxModel([])
    worker = await _worker(store, aux)

    first = await worker.run_once()
    second = await worker.run_once()

    assert aux.calls == []
    assert (first.cluster_candidates, second.cluster_candidates) == (0, 0)
    assert _consolidated(store) == []


@pytest.mark.asyncio
async def test_filtered_neighbourhood_members_are_seen() -> None:
    """相似度 0.85 ≈ 31.8°。``a`` 的邻域 {a, b, c} 是混合来源、被滤掉;它的成员若不记进
    「已见」,``d`` 会取出全是来自助手的 {b, c, d} 再问一次模型。"""
    store = InMemoryMemoryStore()
    store._rows.extend(
        [
            _item("a", _angle(0), origin="user_stated", minutes_ago=4),
            _item("b", _angle(20), origin="assistant_derived", minutes_ago=3),
            _item("c", _angle(25), origin="assistant_derived", minutes_ago=2),
            _item("d", _angle(45), origin="assistant_derived", minutes_ago=1),
        ]
    )
    aux = _ScriptedAuxModel([_KEEP % "summary", _KEEP % "summary"])
    worker = await _worker(store, aux)

    await worker.run_once()

    assert aux.calls == []


@pytest.mark.asyncio
async def test_assistant_plus_unrecorded_is_summarized_without_a_source() -> None:
    store = InMemoryMemoryStore()
    store._rows.extend(
        [
            _item("a", (1.0, 0.0), origin="assistant_derived", minutes_ago=3),
            _item("b", (1.0, 0.0), origin=None, minutes_ago=2),
            _item("c", (1.0, 0.0), origin="assistant_derived", minutes_ago=1),
        ]
    )
    aux = _ScriptedAuxModel([_KEEP % "summary"])
    worker = await _worker(store, aux)

    await worker.run_once()

    [parent] = _consolidated(store)
    assert parent.origin is None


@pytest.mark.asyncio
async def test_all_assistant_cluster_summary_is_from_the_assistant_and_names_the_model() -> None:
    store = InMemoryMemoryStore()
    store._rows.extend(
        [
            _item(f"m{i}", (1.0, 0.0), origin="assistant_derived", confidence=0.6, minutes_ago=i)
            for i in range(3)
        ]
    )
    aux = _ScriptedAuxModel([_KEEP % "summary"])
    worker = await _worker(store, aux, make_consolidator_embedder(_ReportingOrchestratorEmbedder()))

    await worker.run_once()

    [parent] = _consolidated(store)
    assert parent.origin == "assistant_derived"
    assert parent.confidence <= 0.6
    assert parent.embedding_model == _MODEL


@pytest.mark.asyncio
async def test_summary_model_is_none_when_the_embedder_cannot_tell() -> None:
    store = InMemoryMemoryStore()
    store._rows.extend([_item(f"m{i}", (1.0, 0.0), origin=None, minutes_ago=i) for i in range(3)])
    aux = _ScriptedAuxModel([_KEEP % "summary"])
    worker = await _worker(store, aux)  # _FakeEmbedder: only embed_one

    await worker.run_once()

    [parent] = _consolidated(store)
    assert parent.embedding_model is None
