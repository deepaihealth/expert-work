"""B-206 K-7c — the dual view's weighted combination for a logit-scale rerank
model happens in logit space: averaging sigmoids lets one saturated view
outweigh real evidence in the other."""

from __future__ import annotations

import math
from collections.abc import Sequence
from uuid import UUID, uuid4

import pytest

from expert_work.protocol import KnowledgeChunk
from orchestrator.tools.knowledge import _TITLED_VIEW_WEIGHT, _two_view_order
from orchestrator.tools.knowledge_rerank import RerankRanking, sigmoid, weighted_views

#: The reviewer's case: A's title is a strong match (logit 16) but its body is
#: neutral (0); B is a moderate match either way (4 and 4).
_A_TITLED, _A_BODY, _B_TITLED, _B_BODY = 16.0, 0.0, 4.0, 4.0


def _logit_ranking(logits: dict[int, float]) -> RerankRanking:
    order = sorted(logits, key=lambda i: -logits[i])
    return RerankRanking(((i, sigmoid(logits[i])) for i in order), logits=logits)


def test_logit_views_combine_as_logits() -> None:
    titled = _logit_ranking({0: _A_TITLED, 1: _B_TITLED})
    body = _logit_ranking({0: _A_BODY, 1: _B_BODY})  # body rows: positions in ``top``
    ranked = weighted_views([0, 1], titled, body, 0.7, dropped=0)
    assert [index for index, _ in ranked] == [0, 1]  # A: 11.2 beats B: 4.0
    assert ranked.logits == pytest.approx({0: 11.2, 1: 4.0})
    assert [score for _, score in ranked] == pytest.approx([sigmoid(11.2), sigmoid(4.0)])
    # Averaging the sigmoids instead would have ranked B first.
    naive_a = 0.7 * sigmoid(_A_TITLED) + 0.3 * sigmoid(_A_BODY)
    naive_b = 0.7 * sigmoid(_B_TITLED) + 0.3 * sigmoid(_B_BODY)
    assert naive_b > naive_a


def test_other_scales_keep_the_weighted_mean_of_scores() -> None:
    ranked = weighted_views([1, 0], [(1, 0.9), (0, 0.8)], [(0, 0.1), (1, 0.9)], 0.7, dropped=2)
    # body row 0 is top[0] = candidate 1, row 1 is candidate 0.
    assert [index for index, _ in ranked] == [0, 1]
    assert [score for _, score in ranked] == pytest.approx([0.83, 0.66])
    assert isinstance(ranked, RerankRanking)
    assert (ranked.dropped, ranked.logits) == (2, None)


def test_one_view_without_logits_falls_back_to_the_scores() -> None:
    titled = _logit_ranking({0: _A_TITLED, 1: _B_TITLED})
    body = [(0, sigmoid(_A_BODY)), (1, sigmoid(_B_BODY))]
    ranked = weighted_views([0, 1], titled, body, 0.7, dropped=0)
    assert ranked.logits is None
    assert [index for index, _ in ranked] == [1, 0]


class _LogitReranker:
    """A logit-scale scoring reranker: ``logits[text]`` per document."""

    def __init__(self, logits: dict[str, float]) -> None:
        self.logits = logits

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        ranked = await self.rerank_scored(
            query=query, documents=documents, top_k=top_k, tenant_id=tenant_id
        )
        return [index for index, _ in ranked]

    async def rerank_scored(
        self,
        *,
        query: str,
        documents: Sequence[str],
        top_k: int,
        tenant_id: UUID,
        instruction: str | None = None,
    ) -> list[tuple[int, float | None]]:
        del query, tenant_id, instruction
        return _logit_ranking({i: self.logits[d] for i, d in enumerate(documents)})


@pytest.mark.asyncio
async def test_the_dual_view_ranks_a_logit_model_in_logit_space() -> None:
    tenant, kb, doc = uuid4(), uuid4(), uuid4()
    chunks = [
        KnowledgeChunk(
            id=uuid4(),
            tenant_id=tenant,
            kb_id=kb,
            document_id=doc,
            chunk_index=i,
            content=body,
            embedding=(1.0,),
            doc_title="guide",
        )
        for i, body in enumerate(["passage A", "passage B"])
    ]
    titled_a, titled_b = (chunk.retrieval_text for chunk in chunks)
    reranker = _LogitReranker(
        {
            titled_a: _A_TITLED,
            titled_b: _B_TITLED,
            "passage A": _A_BODY,
            "passage B": _B_BODY,
        }
    )
    ranked = await _two_view_order(reranker, "q", chunks, 5, tenant_id=tenant)
    expected = _TITLED_VIEW_WEIGHT * _A_TITLED + (1 - _TITLED_VIEW_WEIGHT) * _A_BODY
    assert [index for index, _ in ranked] == [0, 1]
    assert ranked[0][1] == pytest.approx(1 / (1 + math.exp(-expected)))
