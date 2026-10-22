"""kb_rerank.py:评测里的生产同款重排(``qwen3-vl-rerank``)+ 结果缓存(B-175 波 2 PR0)。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from kb_rerank import (  # type: ignore[import-not-found]
    DEFAULT_RERANK_MODEL,
    OFFLINE_KEY,
    CachedRerankClient,
    RerankCacheMissError,
    build_dashscope_reranker,
)

from orchestrator.llm.rerank import DashScopeReranker


class _FakeClient:
    def __init__(self, tokens: int = 1000) -> None:
        self.calls: list[tuple[str, str, tuple[str, ...], int]] = []
        self.tokens = tokens

    async def rerank(
        self, *, model: str, query: str, documents: Sequence[str], top_n: int
    ) -> Mapping[str, Any]:
        self.calls.append((model, query, tuple(documents), top_n))
        order = list(reversed(range(len(documents))))
        return {
            "output": {
                "results": [
                    {"index": i, "relevance_score": 0.9 - 0.1 * r} for r, i in enumerate(order)
                ]
            },
            "usage": {"total_tokens": self.tokens},
        }


async def test_second_identical_call_is_served_from_the_cache(tmp_path: Path) -> None:
    inner = _FakeClient()
    client = CachedRerankClient(inner=inner, path=tmp_path / "c.sqlite")
    first = await client.rerank(model="m", query="q", documents=["a", "b"], top_n=2)
    second = await client.rerank(model="m", query="q", documents=["a", "b"], top_n=2)
    assert first == second
    assert len(inner.calls) == 1
    assert client.stats() == {"calls": 2, "cache_hits": 1, "tokens": 1000, "cost_cny": 0.0005}


async def test_any_input_change_is_a_new_key(tmp_path: Path) -> None:
    inner = _FakeClient()
    client = CachedRerankClient(inner=inner, path=tmp_path / "c.sqlite")
    await client.rerank(model="m", query="q", documents=["a", "b"], top_n=2)
    await client.rerank(model="m", query="q", documents=["b", "a"], top_n=2)
    await client.rerank(model="m", query="q2", documents=["a", "b"], top_n=2)
    await client.rerank(model="m2", query="q", documents=["a", "b"], top_n=2)
    await client.rerank(model="m", query="q", documents=["a", "b"], top_n=1)
    assert len(inner.calls) == 5


class _InstructedFakeClient(_FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.instructions: list[str | None] = []

    async def rerank(
        self,
        *,
        model: str,
        query: str,
        documents: Sequence[str],
        top_n: int,
        instruction: str | None = None,
    ) -> Mapping[str, Any]:
        self.instructions.append(instruction)
        return await super().rerank(model=model, query=query, documents=documents, top_n=top_n)


async def test_the_instruction_is_part_of_the_key_and_reaches_the_client(tmp_path: Path) -> None:
    # K-14 A/B arms differ only by the instruction: they must not share scores.
    inner = _InstructedFakeClient()
    client = CachedRerankClient(inner=inner, path=tmp_path / "c.sqlite")
    await client.rerank(model="m", query="q", documents=["a"], top_n=1)
    await client.rerank(model="m", query="q", documents=["a"], top_n=1, instruction="judge")
    await client.rerank(model="m", query="q", documents=["a"], top_n=1, instruction="other")
    await client.rerank(model="m", query="q", documents=["a"], top_n=1, instruction="judge")
    assert inner.instructions == [None, "judge", "other"]


async def test_a_request_without_instruction_keeps_its_pre_k14_key(tmp_path: Path) -> None:
    """Rows cached before the instruction existed still answer the same request
    (the dual-view baseline replays for free)."""
    path = tmp_path / "c.sqlite"
    payload = json.dumps(
        {"model": "m", "query": "q", "documents": ["a", "b"], "top_n": 2},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    old_key = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE rerank_cache (key TEXT PRIMARY KEY, body TEXT)")
        conn.execute(
            "INSERT INTO rerank_cache VALUES (?, ?)",
            (old_key, json.dumps({"output": {"results": [{"index": 1}]}})),
        )
    client = CachedRerankClient(inner=None, path=path)
    body = await client.rerank(model="m", query="q", documents=["a", "b"], top_n=2)
    assert body == {"output": {"results": [{"index": 1}]}}


async def test_cache_survives_a_new_client(tmp_path: Path) -> None:
    path = tmp_path / "c.sqlite"
    await CachedRerankClient(inner=_FakeClient(), path=path).rerank(
        model="m", query="q", documents=["a"], top_n=1
    )
    later = _FakeClient()
    await CachedRerankClient(inner=later, path=path).rerank(
        model="m", query="q", documents=["a"], top_n=1
    )
    assert later.calls == []


async def test_offline_miss_raises_without_calling_out(tmp_path: Path) -> None:
    client = CachedRerankClient(inner=None, path=tmp_path / "c.sqlite")
    with pytest.raises(RerankCacheMissError):
        await client.rerank(model="m", query="q", documents=["a"], top_n=1)


def test_build_dashscope_reranker_defaults_to_the_production_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXPERT_WORK_EVAL_EMBED_API_KEY", "sk-test")
    monkeypatch.delenv("EXPERT_WORK_EVAL_RERANK_MODEL", raising=False)
    reranker, client = build_dashscope_reranker(cache_path=tmp_path / "c.sqlite")
    assert isinstance(reranker, DashScopeReranker)
    assert reranker.model == DEFAULT_RERANK_MODEL == "qwen3-vl-rerank"
    assert client.inner is not None
    assert reranker.client is client


def test_build_dashscope_reranker_offline_key_has_no_network_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXPERT_WORK_EVAL_EMBED_API_KEY", OFFLINE_KEY)
    monkeypatch.setenv("EXPERT_WORK_EVAL_RERANK_MODEL", "qwen3-rerank")
    reranker, client = build_dashscope_reranker(cache_path=tmp_path / "c.sqlite")
    assert reranker.model == "qwen3-rerank"
    assert client.inner is None


def test_build_dashscope_reranker_needs_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXPERT_WORK_EVAL_EMBED_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="EXPERT_WORK_EVAL_EMBED_API_KEY"):
        build_dashscope_reranker(cache_path=tmp_path / "c.sqlite")
