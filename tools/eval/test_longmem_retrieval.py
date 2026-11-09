"""P0 harness end-to-end against the synthetic fixtures — Stream CM-N5.

Deterministic by construction: the keyword-overlap embedder (same
mechanism as ``run_baseline._FakeKeywordEmbedder``, blake2b-bucketed) +
in-memory store + fixture corpora make every number reproducible, so
the assertions pin behavior, not trends.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from longmem.adapter import load_locomo, load_longmemeval
from longmem.download import DatasetIntegrityError, DatasetSpec, cache_dir, file_sha256, verify
from longmem.retrieval import AblationConfig, evaluate_retrieval

FIXTURES = Path(__file__).parent / "datasets" / "longmem_fixture"

#: ``now`` for every test — the CM-K4 shift must use the same wall clock
#: ``InMemoryMemoryStore.retrieve`` decays against (``datetime.now``);
#: a fixed past date ages every shifted doc past the decay floor and
#: erases the freshness tie-break as the calendar moves on.
_NOW = datetime.now(UTC)


class _KeywordEmbedder:
    """Keyword-overlap embedder (run_baseline parity, blake2b buckets)."""

    DIM = 256

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        return [self._encode(t) for t in texts]

    def _encode(self, text: str) -> tuple[float, ...]:
        vec = [0.0] * self.DIM
        for token in text.lower().replace(",", " ").replace("?", " ").split():
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
            vec[int.from_bytes(digest, "big") % self.DIM] += 1.0
        return tuple(vec)


class _ReverseReranker:
    """Deterministic reranker double — reverses the candidate order."""

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        del query, tenant_id
        return list(range(len(documents)))[::-1][:top_k]


@pytest.mark.asyncio
async def test_default_arm_finds_the_evidence() -> None:
    instances = load_longmemeval(FIXTURES / "longmemeval_mini.json")
    report = await evaluate_retrieval(instances, embedder=_KeywordEmbedder(), now=_NOW)
    assert report.n_instances == 2
    assert report.blocked_writes == 0
    assert report.deduped_writes == 0
    # The fixture is calibrated so keyword overlap finds every evidence
    # doc within the top-5.
    assert report.mean_turn_recall == 1.0
    assert report.mean_session_recall == 1.0
    assert 0.0 < report.mean_ndcg <= 1.0
    assert set(report.by_question_type) == {"single-session-user", "knowledge-update"}
    assert report.by_question_type["knowledge-update"]["n"] == 1.0


@pytest.mark.asyncio
async def test_decay_breaks_the_knowledge_update_tie() -> None:
    """Two near-identical editor facts tie on keyword overlap; temporal
    decay (CM-6) must rank the fresher one first. MMR is off so the
    final order is the store's decayed order."""
    instances = [
        i
        for i in load_longmemeval(FIXTURES / "longmemeval_mini.json")
        if i.question_id == "fixture-editor-2"
    ]
    config = AblationConfig(mmr=False)
    report = await evaluate_retrieval(
        instances, embedder=_KeywordEmbedder(), config=config, now=_NOW
    )
    (result,) = report.per_instance
    # The newer (answer) doc ranks first -> reciprocal rank 1.0.
    assert result.mrr == 1.0
    assert result.turn_recall == 1.0


@pytest.mark.asyncio
async def test_decay_off_arm_still_recalls() -> None:
    instances = load_longmemeval(FIXTURES / "longmemeval_mini.json")
    config = AblationConfig(decay=False, mmr=False)
    report = await evaluate_retrieval(
        instances, embedder=_KeywordEmbedder(), config=config, now=_NOW
    )
    # Without timestamps nothing decays — recall (set membership) holds
    # even though the knowledge-update ordering is no longer guaranteed.
    assert report.mean_turn_recall == 1.0


@pytest.mark.asyncio
async def test_pure_vector_arm() -> None:
    instances = load_longmemeval(FIXTURES / "longmemeval_mini.json")
    config = AblationConfig(hybrid=False)
    report = await evaluate_retrieval(
        instances, embedder=_KeywordEmbedder(), config=config, now=_NOW
    )
    assert report.n_instances == 2
    assert report.mean_turn_recall == 1.0


@pytest.mark.asyncio
async def test_rerank_arm_applies_the_reranker_order() -> None:
    instances = [
        i
        for i in load_longmemeval(FIXTURES / "longmemeval_mini.json")
        if i.question_id == "fixture-editor-2"
    ]
    base = AblationConfig(mmr=False)
    flipped = AblationConfig(mmr=False, rerank=True)
    baseline = await evaluate_retrieval(
        instances, embedder=_KeywordEmbedder(), config=base, now=_NOW
    )
    reversed_run = await evaluate_retrieval(
        instances,
        embedder=_KeywordEmbedder(),
        config=flipped,
        reranker=_ReverseReranker(),
        now=_NOW,
    )
    # Reversing the wide order demotes the answer doc from rank 1.
    assert baseline.per_instance[0].mrr == 1.0
    assert reversed_run.per_instance[0].mrr < 1.0


@pytest.mark.asyncio
async def test_rerank_requires_an_instance() -> None:
    with pytest.raises(ValueError):
        await evaluate_retrieval(
            [], embedder=_KeywordEmbedder(), config=AblationConfig(rerank=True)
        )


@pytest.mark.asyncio
async def test_locomo_pipeline_shares_store_builds() -> None:
    instances = load_locomo(FIXTURES / "locomo_mini.json")

    class _CountingEmbedder(_KeywordEmbedder):
        corpus_batches = 0

        async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
            if len(texts) > 1:
                type(self).corpus_batches += 1
            return await super().embed(texts, tenant_id=tenant_id)

    embedder = _CountingEmbedder()
    report = await evaluate_retrieval(instances, embedder=embedder, now=_NOW)
    assert report.n_instances == 4
    # One shared corpus -> one corpus embedding pass for all four QAs.
    assert _CountingEmbedder.corpus_batches == 1
    assert report.mean_turn_recall > 0.5
    single_hop = [r for r in report.per_instance if r.question_type == "locomo-single-hop"]
    assert single_hop[0].turn_recall == 1.0


# ---------------------------------------------------------------------------
# download — integrity logic only (no network in CI)
# ---------------------------------------------------------------------------


def test_verify_rejects_tampered_file(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    target.write_text("[]", encoding="utf-8")
    spec = DatasetSpec(
        key="t", url="http://example.invalid/x", sha256="0" * 64, size_bytes=2, filename="data.json"
    )
    with pytest.raises(DatasetIntegrityError):
        verify(target, spec)
    good = DatasetSpec(
        key="t",
        url="http://example.invalid/x",
        sha256=file_sha256(target),
        size_bytes=2,
        filename="data.json",
    )
    verify(target, good)  # no raise


def test_cache_dir_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXPERT_WORK_LONGMEM_CACHE", str(tmp_path / "mirror"))
    assert cache_dir() == tmp_path / "mirror"
    monkeypatch.delenv("EXPERT_WORK_LONGMEM_CACHE")
    assert cache_dir().name == ".longmem_cache"
