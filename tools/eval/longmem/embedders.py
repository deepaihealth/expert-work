"""Embedder construction for runner modes — Stream CM-N5.

``KeywordEmbedder`` is the deterministic no-network arm (same blake2b
keyword-bucket mechanism as ``run_baseline._FakeKeywordEmbedder`` — a
local copy by the same precedent that keeps run_baseline independent of
test code). Real runs build the orchestrator's OpenAI-compatible
embedder from ``EXPERT_WORK_EVAL_EMBED_*`` env, mirroring how the platform
itself talks to the embedding endpoint — so baseline numbers measure
the production embedding space.

:class:`CachedEmbedder` wraps the real embedder with a sqlite
content-hash cache: the 5-arm ablation matrix re-embeds the identical
corpus per arm, which without a cache multiplies a ~350k-doc
LongMemEval_S pass by 5 in both wall-clock and yuan. Cache misses are
fetched in small concurrent batches (DashScope's compatible-mode
embedding endpoint caps batch size at 10 inputs).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import struct
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from longmem.transient import with_retries


class KeywordEmbedder:
    """Deterministic keyword-overlap embedder (CJK bigrams + ASCII words)."""

    DIM = 256

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id  # eval double has no per-tenant key
        return [self._encode(t) for t in texts]

    def _encode(self, text: str) -> tuple[float, ...]:
        vec = [0.0] * self.DIM
        for token in _tokenise(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
            vec[int.from_bytes(digest, "big") % self.DIM] += 1.0
        return tuple(vec)


def _tokenise(text: str) -> list[str]:
    cleaned = text.lower().strip()
    if not cleaned:
        return []
    ascii_words = [w for w in cleaned.replace(",", " ").split() if w]
    cjk_chars = [c for c in cleaned if "一" <= c <= "鿿"]
    cjk_bigrams = [cjk_chars[i] + cjk_chars[i + 1] for i in range(len(cjk_chars) - 1)]
    return ascii_words + cjk_chars + cjk_bigrams


class CachedEmbedder:
    """Content-hash sqlite cache over any ``Embedder``-shaped backend.

    Keys are ``(model, sha256(text))`` so switching embedding models
    never serves stale vectors. Misses go to the backend in
    ``backend_batch``-sized requests, ``concurrency`` in flight —
    DashScope compatible-mode caps embedding batches at 10 inputs and
    rate-limits per-second, so both knobs matter for a 350k-doc pass.
    sqlite access is synchronous-but-cheap (single-process eval tool).
    """

    def __init__(
        self,
        backend: object,
        *,
        model_key: str,
        db_path: Path,
        backend_batch: int = 10,
        concurrency: int = 4,
        max_text_chars: int = 8000,
    ) -> None:
        self._backend = backend
        self._model = model_key
        self._batch = max(1, backend_batch)
        self._max_text_chars = max(1, max_text_chars)
        self._sem = asyncio.Semaphore(max(1, concurrency))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS embeddings ("
            "model TEXT NOT NULL, hash TEXT NOT NULL, vec BLOB NOT NULL, "
            "PRIMARY KEY (model, hash))"
        )
        self._db.commit()

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        # DashScope text-embedding-v4 caps a single input at 8192 tokens —
        # LongMemEval_S has individual turns above that (a 400 in the wild,
        # 2026-06-10 baseline run). 8000 chars is safe even for pure-CJK
        # text (1 char ≈ 1 token worst case); the head of a turn carries
        # its retrieval semantics. The cache key hashes the truncated text
        # so cached vectors always match what was actually embedded.
        texts = [t[: self._max_text_chars] for t in texts]
        hashes = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts]
        out: list[tuple[float, ...] | None] = [self._get(h) for h in hashes]

        missing = [i for i, vec in enumerate(out) if vec is None]
        if missing:
            batches = [missing[s : s + self._batch] for s in range(0, len(missing), self._batch)]

            async def _fetch(batch: list[int]) -> list[tuple[float, ...]]:
                # Throttle-shaped 400s (DashScope quirk, round 2-3) and
                # transport drops (round 4 ReadTimeout) self-heal on the
                # shared policy; genuine content 400s re-raise at once.
                async with self._sem:
                    return await with_retries(
                        lambda: self._backend.embed(  # type: ignore[attr-defined]
                            [texts[i] for i in batch], tenant_id=tenant_id
                        )
                    )

            fetched = await asyncio.gather(*[_fetch(b) for b in batches])
            for batch, vectors in zip(batches, fetched, strict=True):
                for idx, vector in zip(batch, vectors, strict=True):
                    out[idx] = tuple(vector)
                    self._put(hashes[idx], tuple(vector))
            self._db.commit()
        return [vec for vec in out if vec is not None]

    def _get(self, content_hash: str) -> tuple[float, ...] | None:
        row = self._db.execute(
            "SELECT vec FROM embeddings WHERE model = ? AND hash = ?",
            (self._model, content_hash),
        ).fetchone()
        if row is None:
            return None
        blob = row[0]
        return struct.unpack(f"{len(blob) // 4}f", blob)

    def _put(self, content_hash: str, vector: tuple[float, ...]) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO embeddings (model, hash, vec) VALUES (?, ?, ?)",
            (self._model, content_hash, struct.pack(f"{len(vector)}f", *vector)),
        )

    def close(self) -> None:
        self._db.close()


_EMBED_ENV = ("EXPERT_WORK_EVAL_EMBED_API_KEY", "EXPERT_WORK_EVAL_EMBED_MODEL")


def build_real_embedder(
    *, cache_db: Path | None = None, concurrency: int = 4, max_text_chars: int = 8000
) -> object:
    """OpenAI-compatible embedder from ``EXPERT_WORK_EVAL_EMBED_*`` env.

    Required: ``EXPERT_WORK_EVAL_EMBED_API_KEY`` + ``EXPERT_WORK_EVAL_EMBED_MODEL``;
    optional ``EXPERT_WORK_EVAL_EMBED_BASE_URL`` (defaults to the orchestrator
    default endpoint). Imported lazily so the fake arm never pays the
    orchestrator import. With ``cache_db`` set the backend is wrapped in
    :class:`CachedEmbedder` (multi-arm runs embed each corpus once);
    ``max_text_chars`` is its truncation cap (the kb eval raises it so the
    vendor reports over-long inputs instead of silently embedding a head).
    """
    missing = [name for name in _EMBED_ENV if not os.environ.get(name)]
    if missing:
        raise SystemExit(
            f"--embedder real requires env {', '.join(missing)} "
            "(plus optional EXPERT_WORK_EVAL_EMBED_BASE_URL)"
        )
    from orchestrator.llm.embedder import HTTPEmbeddingClient, OpenAICompatibleEmbedder

    base_url = os.environ.get("EXPERT_WORK_EVAL_EMBED_BASE_URL")
    client = (
        HTTPEmbeddingClient(api_key=os.environ["EXPERT_WORK_EVAL_EMBED_API_KEY"], base_url=base_url)
        if base_url
        else HTTPEmbeddingClient(api_key=os.environ["EXPERT_WORK_EVAL_EMBED_API_KEY"])
    )
    model = os.environ["EXPERT_WORK_EVAL_EMBED_MODEL"]
    backend = OpenAICompatibleEmbedder(client=client, model=model)
    if cache_db is None:
        return backend
    return CachedEmbedder(
        backend,
        model_key=model,
        db_path=cache_db,
        concurrency=concurrency,
        max_text_chars=max_text_chars,
    )
