"""kb_eval.py 运行器:内存库 + 假嵌入 + ``tmp_path`` 里现造的小语料(B-175 L1-T3)。

CI 可跑的部分只有这里:真 SQL 库 / 真嵌入 / 真语料的那次跑在仓库外手工做。
"""

from __future__ import annotations

import asyncio
import csv
import json
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import yaml
from kb_corpus_inventory import MANIFEST_FIELDS  # type: ignore[import-not-found]
from kb_dataset import KbCase, KbDataset, KbExpected, load_dataset  # type: ignore[import-not-found]
from kb_eval import (  # type: ignore[import-not-found]
    EMBED_OVERLONG_CHARS,
    EVAL_TENANT_ID,
    CaseRow,
    CountingEmbedder,
    DupCopy,
    EmbeddingDegradedError,
    EmbeddingDimensionError,
    EvalConfig,
    KeywordDegradedError,
    RerankDegradedError,
    _build_parser,
    _mean_p90,
    _ReplayRetriever,
    _size_spread,
    baseline_benchmark,
    baseline_section,
    build_corpus,
    build_embedder,
    build_fake_embedder,
    build_retriever,
    case_to_json,
    check_anchors,
    corpus_titles,
    doc_titles_for,
    fingerprint,
    format_summary_table,
    main,
    resolve_anchor_documents,
    rls_tenant,
    run_eval,
    search_log_json,
    select_rows,
    shown_parts,
    summarise,
    tool_message_text,
)
from kb_metrics import anchor_in, score_case  # type: ignore[import-not-found]
from kb_rerank import RerankCacheMissError  # type: ignore[import-not-found]
from longmem.embedders import CachedEmbedder  # type: ignore[import-not-found]

from control_plane.knowledge.chunking import CHUNKER_VERSION, count_tokens
from expert_work.common.spotlight import unspotlight
from expert_work.persistence import InMemoryKnowledgeStore, KnowledgeRecallError
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.persistence.rls import current_tenant_id_var
from expert_work.protocol import ChunkType, DocumentStatus, RetrievalMethod, chunk_retrieval_text
from orchestrator.tools import knowledge
from orchestrator.tools.knowledge import (
    KnowledgeRetriever,
    KnowledgeSearchOutcome,
    KnowledgeSearchTool,
    RerankRanking,
    RetrievedChunk,
)
from orchestrator.tools.knowledge_rerank import RerankError

_ANCHOR_A = "常温密封保存即可"
_ANCHOR_B = "专利号 ZL2019-1234567"
_DOC_A = (
    "# 产品说明书\n\n## 保存方式\n\n益生菌常温密封保存即可,不需要冷藏。开封后请尽快食用。\n\n"
    "## 成分\n\n每袋含活菌 100 亿 CFU,辅料为麦芽糊精。\n"
)
_DOC_B = "本品专利号 ZL2019-1234567,菌株编号 BL-99。规格 2g x 30 袋,零售价 198 元。\n"
_DOC_B_OTHER = "另一份同名文件:维生素 C 咀嚼片,每片 100 毫克,饭后服用。\n"


def _row(path: str, tier: str = "A", dup_group: str = "", size: int = 100) -> dict[str, str]:
    row = dict.fromkeys(MANIFEST_FIELDS, "")
    row.update(
        path=path,
        top_dir=path.split("/")[0],
        ext=path.rsplit(".", 1)[-1],
        size=str(size),
        tier=tier,
        pages="0",
        scanned_pages="0",
        text_chars="0",
        dup_group=dup_group,
    )
    return row


@pytest.fixture
def corpus(tmp_path: Path) -> tuple[Path, Path]:
    """返回 ``(manifest.csv, 语料根目录)``:2 份可入库 + 1 份重复 + 1 份坏文件 + 1 份 B 层
    + 1 份同名不同内容(不同目录、无 dup_group)。"""
    root = tmp_path / "corpus"
    docs = root / "docs"
    docs.mkdir(parents=True)
    other = root / "other"
    other.mkdir()
    (docs / "manual_a.md").write_text(_DOC_A, encoding="utf-8")
    (docs / "manual_b.txt").write_text(_DOC_B, encoding="utf-8")
    (docs / "manual_a_copy.md").write_text(_DOC_A, encoding="utf-8")
    (docs / "broken.xyz").write_bytes(b"\x00\x01 not a document")
    (docs / "scan.pdf").write_bytes(b"%PDF-1.4 fake")
    (other / "manual_b.txt").write_text(_DOC_B_OTHER, encoding="utf-8")
    rows = [
        _row("docs/manual_a.md", dup_group="g1"),
        _row("docs/manual_b.txt"),
        _row("docs/manual_a_copy.md", dup_group="g1"),
        _row("docs/broken.xyz"),
        _row("docs/scan.pdf", tier="B"),
        _row("other/manual_b.txt"),
    ]
    out = tmp_path / "inventory"
    out.mkdir()
    manifest = out / "manifest.csv"
    with manifest.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(MANIFEST_FIELDS))
        writer.writeheader()
        writer.writerows(rows)
    return manifest, root


def _cases() -> list[dict[str, Any]]:
    return [
        {
            "id": "q-storage",
            "query": "益生菌需要放冰箱冷藏吗",
            "source": "faq_xlsx",
            "tags": ["通用", "保存"],
            "expected": [{"document": "manual_a.md", "anchor": _ANCHOR_A, "page": None}],
            "answer": "常温密封保存即可。",
        },
        {
            "id": "q-patent",
            "query": "这个产品的专利号是多少",
            "source": "hard_exact",
            "tags": ["专利"],
            "expected": [{"document": "manual_b.txt", "anchor": _ANCHOR_B, "page": None}],
            "answer": None,
        },
        {
            "id": "q-ghost",
            "query": "维生素 D 每日推荐摄入量",
            "source": "manual_qa",
            "tags": ["通用"],
            "expected": [{"document": "ghost.md", "anchor": "这个文档不在语料里", "page": None}],
            "answer": None,
        },
        {
            "id": "q-dead-anchor",
            "query": "菌株编号是什么",
            "source": "hard_exact",
            "tags": ["专利"],
            "expected": [
                {
                    "document": "manual_b.txt",
                    "anchor": "这段锚文本不在文档的任何一段里",
                    "page": None,
                }
            ],
            "answer": None,
        },
        {
            "id": "q-none",
            "query": "这款产品含有多少咖啡因",
            "source": "no_answer",
            "tags": ["通用"],
            "expected": [],
            "answer": None,
        },
    ]


def _write_dataset(path: Path, manifest: str, cases: list[dict[str, Any]] | None = None) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "name": "unit_kb",
                "corpus": {"manifest": manifest, "tiers": ["A", "D-A"]},
                "cases": cases if cases is not None else _cases(),
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def dataset_path(tmp_path: Path, corpus: tuple[Path, Path]) -> Path:
    manifest, _root = corpus
    return _write_dataset(tmp_path / "unit_kb.yaml", str(manifest))


def _manifest_rows(manifest: Path) -> list[dict[str, str]]:
    with manifest.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------------------
# 选文档
# ---------------------------------------------------------------------------


def test_select_rows_filters_tier_dedupes_first_wins_and_limits(
    corpus: tuple[Path, Path],
) -> None:
    manifest, _root = corpus
    rows = _manifest_rows(manifest)
    selection = select_rows(rows, tiers=("A", "D-A"), limit_docs=0)
    assert [r["path"] for r in selection.rows] == [
        "docs/manual_a.md",
        "docs/manual_b.txt",
        "docs/broken.xyz",
        "other/manual_b.txt",
    ]
    assert selection.skipped_dup == 1
    assert selection.skipped_tier == 1
    assert selection.skipped_limit == 0
    assert selection.skipped_empty == 0
    # 0 字节的文件(清单 size=0)不入库也不算失败 —— 它解析不了是语料问题,不是检索问题。
    with_empty = [*rows, {**rows[0], "path": "docs/empty.pdf", "size": "0", "dup_group": ""}]
    empties = select_rows(with_empty, tiers=("A", "D-A"), limit_docs=0)
    assert "docs/empty.pdf" not in [r["path"] for r in empties.rows]
    assert empties.skipped_empty == 1
    # #17:limit 在去重之后数 —— 重复的那份不占名额,被 limit 砍掉的另计。
    limited = select_rows(rows, tiers=("A",), limit_docs=2)
    assert [r["path"] for r in limited.rows] == ["docs/manual_a.md", "docs/manual_b.txt"]
    assert limited.skipped_dup == 1
    assert limited.skipped_limit == 2


# ---------------------------------------------------------------------------
# 建库
# ---------------------------------------------------------------------------


def test_doc_titles_real_none_and_shuffled() -> None:
    """``real`` = 生产入库时的文档名;``none`` = 不带(改前);``shuffled`` = 对照组:同一批文档名
    在文档间打乱(长度与格式分布不变),每份都拿到别人的名字,固定种子可复现。"""
    rows = [_row(f"docs/doc_{i}.md") for i in range(6)]
    paths = [r["path"] for r in rows]
    real = doc_titles_for(rows, "real")
    assert real == {path: f"doc {i}" for i, path in enumerate(paths)}
    assert doc_titles_for(rows, "none") == dict.fromkeys(paths)
    shuffled = doc_titles_for(rows, "shuffled")
    assert sorted(shuffled.values()) == sorted(real.values())  # type: ignore[type-var]
    assert all(shuffled[path] != real[path] for path in paths)
    assert doc_titles_for(rows, "shuffled") == shuffled


async def _build(
    corpus: tuple[Path, Path],
    *,
    store: InMemoryKnowledgeStore,
    tenant: UUID,
    embedder: Any = None,
    tiers: Sequence[str] = ("A", "D-A"),
    **kwargs: Any,
) -> Any:
    manifest, root = corpus
    selection = select_rows(_manifest_rows(manifest), tiers=tiers, limit_docs=0)
    return await build_corpus(
        store=store,
        embedder=embedder if embedder is not None else build_fake_embedder(),
        tenant_id=tenant,
        base_name="unit_kb",
        corpus_root=root,
        selection=selection,
        concurrency=2,
        **kwargs,
    )


async def _chunk_texts(store: InMemoryKnowledgeStore, tenant: UUID, document_id: UUID) -> list[str]:
    page, _total = await store.list_chunks(
        tenant_id=tenant, document_id=document_id, offset=0, limit=200
    )
    return [c.content for c in page]


@pytest.mark.asyncio
async def test_build_corpus_ingests_once_per_dup_group_and_records_failures(
    corpus: tuple[Path, Path],
) -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    report = await _build(corpus, store=store, tenant=tenant)
    assert report.docs_ingested == 3  # manual_a, manual_b, other/manual_b (renamed)
    assert report.docs_skipped_dup == 1
    assert report.docs_failed == 1
    assert report.chunk_total >= 2
    spread = report.chunk_tokens
    assert 0 < spread["p10"] <= spread["p50"] <= spread["p90"] <= spread["max"]
    assert report.elapsed_s >= 0.0
    # #7:失败按阶段分类,解析失败是 parse。
    assert [(f.filename, f.stage, f.error_type) for f in report.ingest_failures] == [
        ("broken.xyz", "parse", "DocumentParseError")
    ]
    assert report.failed_by_stage == {"parse": 1}
    docs = await store.list_documents(tenant_id=tenant, kb_id=report.base_id)
    status = {d.filename: d.status for d in docs}
    assert status == {
        "manual_a.md": DocumentStatus.READY,
        "manual_b.txt": DocumentStatus.READY,
        "other/manual_b.txt": DocumentStatus.READY,  # same basename, ingested under its dir
        "broken.xyz": DocumentStatus.FAILED,
    }
    assert "manual_a_copy.md" not in status
    ready = [d for d in docs if d.status is DocumentStatus.READY]
    assert all(d.chunk_count >= 1 for d in ready)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [("real", {"manual a", "manual b"}), ("none", {None})],
)
async def test_build_corpus_ingests_chunks_with_the_chosen_titles(
    corpus: tuple[Path, Path], mode: str, expected: set[str | None]
) -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    await _build(corpus, store=store, tenant=tenant, doc_titles=mode)
    assert {c.doc_title for c in store._chunks} == expected


@pytest.mark.asyncio
async def test_build_corpus_reads_txt_as_plain_text_md_as_markdown(tmp_path: Path) -> None:
    # Same chunking as production ingest (B-199): a .txt file is plain text.
    root = tmp_path / "corpus"
    root.mkdir()
    text = "# 保存方式\n常温密封保存即可。\n"
    (root / "plain.txt").write_text(text, encoding="utf-8")
    (root / "marked.md").write_text(text, encoding="utf-8")
    manifest = tmp_path / "manifest.csv"
    with manifest.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(MANIFEST_FIELDS))
        writer.writeheader()
        writer.writerows([_row("plain.txt"), _row("marked.md")])
    store = InMemoryKnowledgeStore()
    await _build((manifest, root), store=store, tenant=uuid4())
    assert {c.doc_title: tuple(c.heading_path) for c in store._chunks} == {
        "plain": (),
        "marked": ("保存方式",),
    }


@pytest.mark.asyncio
async def test_build_corpus_same_basename_different_content_ingests_both_under_qualified_name(
    corpus: tuple[Path, Path],
) -> None:
    # #10a:``other/manual_b.txt`` 与 ``docs/manual_b.txt`` 同名不同内容、不是同一 dup_group。
    # 库内 (kb_id, filename) 唯一,直接放进去会把先入的那份重置掉 —— 后到的改以
    # 「上级目录/文件名」入库:两份都在,评测集按文件名指向的仍是先到的那份。
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    report = await _build(corpus, store=store, tenant=tenant)
    assert report.docs_name_collision == 1
    assert [(c.filename, c.path, c.kept_path, c.ingested_as) for c in report.name_collisions] == [
        ("manual_b.txt", "other/manual_b.txt", "docs/manual_b.txt", "other/manual_b.txt")
    ]
    assert "manual_b.txt" not in {f.filename for f in report.ingest_failures}
    docs = {
        d.filename: d for d in await store.list_documents(tenant_id=tenant, kb_id=report.base_id)
    }
    texts = await _chunk_texts(store, tenant, docs["manual_b.txt"].id)
    assert any(_ANCHOR_B in t for t in texts)  # 先入的那份原样还在
    assert not any("维生素 C 咀嚼片" in t for t in texts)
    later = await _chunk_texts(store, tenant, docs["other/manual_b.txt"].id)
    assert any("维生素 C 咀嚼片" in t for t in later)  # 后到的那份也在,只是换了名


def _first_is_slowest_parser(filename: str, raw: bytes) -> str:
    # The manifest's first document finishes parsing last.
    time.sleep(0.3 if filename == "manual_a.md" else 0.0)
    return raw.decode("utf-8")


@pytest.mark.asyncio
async def test_build_corpus_writes_in_manifest_order_whatever_parse_order(
    corpus: tuple[Path, Path],
) -> None:
    """Parsing runs concurrently but documents land in manifest order — the
    physical row order (which breaks retrieval ties) must not depend on which
    parse happened to finish first."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    manifest, _root = corpus
    expected = [
        r["path"]
        for r in select_rows(_manifest_rows(manifest), tiers=("A", "D-A"), limit_docs=0).rows
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        report = await _build(
            corpus, store=store, tenant=tenant, executor=pool, parser=_first_is_slowest_parser
        )
    docs = await store.list_documents(tenant_id=tenant, kb_id=report.base_id)
    names = {d.id: d.filename for d in docs}
    # Chunk write order (what breaks retrieval ties) follows the manifest.
    written: list[str] = []
    for chunk in store._chunks:
        name = names[chunk.document_id]
        if name not in written:
            written.append(name)
    assert [Path(p).name for p in expected] == [Path(f).name for f in written]


def test_fingerprint_records_vector_search_mode(tmp_path: Path) -> None:
    dataset = tmp_path / "d.yaml"
    manifest = tmp_path / "m.csv"
    dataset.write_text("x", encoding="utf-8")
    manifest.write_text("y", encoding="utf-8")
    common = {
        "dataset_path": dataset,
        "manifest_path": manifest,
        "corpus_root": tmp_path,
        "embedder": "fake",
        "embed_base_url": None,
        "query_marker": "none",
        "store": "sql",
        "tiers": ("A",),
        "limit_docs": 0,
        "cfg": EvalConfig(),
    }
    assert fingerprint(**common)["vector_search"] == "exact"
    assert fingerprint(**common, ann=True)["vector_search"] == "ann"
    assert fingerprint(**common)["chunker"] == CHUNKER_VERSION  # B-206 K-1: "k3k1"


def _slow_parser(filename: str, raw: bytes) -> str:
    time.sleep(0.5)
    return raw.decode("utf-8")


@pytest.mark.asyncio
async def test_build_corpus_parse_timeout_is_a_parse_failure(corpus: tuple[Path, Path]) -> None:
    # #8:每份文档解析有超时;超时记 parse 阶段失败,文档标 FAILED,整轮继续。
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    with ThreadPoolExecutor(max_workers=2) as pool:
        report = await _build(
            corpus,
            store=store,
            tenant=tenant,
            executor=pool,
            parser=_slow_parser,
            parse_timeout_s=0.05,
        )
    assert report.docs_ingested == 0
    assert report.failed_by_stage == {"parse": 4}  # incl. the renamed collision
    assert {f.error_type for f in report.ingest_failures} == {"TimeoutError"}
    docs = await store.list_documents(tenant_id=tenant, kb_id=report.base_id)
    assert {d.status for d in docs} == {DocumentStatus.FAILED}
    assert all(d.error and "TimeoutError" in d.error for d in docs)


def _somewhat_slow_parser(filename: str, raw: bytes) -> str:
    time.sleep(0.12)
    return raw.decode("utf-8")


@pytest.mark.asyncio
async def test_build_corpus_waits_out_a_parse_slower_than_the_timeout(
    corpus: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    # 10-15:两条评测并行时一份大 PDF 解析超过时限被跳过,语料静默少了 4,130 段。
    # 超时后接着等同一个解析(不重提交),总共最多等 SLOW_PARSE_GRACE 倍时限。
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    with ThreadPoolExecutor(max_workers=4) as pool:
        report = await _build(
            corpus,
            store=store,
            tenant=tenant,
            executor=pool,
            parser=_somewhat_slow_parser,
            parse_timeout_s=0.05,
        )
    assert report.docs_failed == 0
    assert report.docs_ingested == 4
    assert "slower than" in capsys.readouterr().err


def _summary_with_corpus(failures: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "overall": {},
        "by_source": {},
        "by_tag": {},
        "corpus": {
            "docs_ingested": 3,
            "docs_skipped_dup": 0,
            "docs_skipped_limit": 0,
            "docs_skipped_empty": 0,
            "docs_name_collision": 0,
            "docs_failed": len(failures),
            "failed_by_stage": {"parse": len(failures)} if failures else {},
            "chunk_total": 30,
            "elapsed_s": 1.0,
            "embedder": "fake",
            "ingest_failures": failures,
        },
    }


def test_summary_table_warns_when_the_corpus_is_incomplete() -> None:
    failed = [{"filename": "big.pdf", "path": "a/big.pdf", "stage": "parse"}]
    table = format_summary_table(_summary_with_corpus(failed))
    assert "WARNING corpus incomplete: 1 document(s) failed (big.pdf)" in table
    assert "WARNING" not in format_summary_table(_summary_with_corpus([]))


def test_chunk_sizes_are_reported_as_nearest_rank_percentiles() -> None:
    assert _size_spread(range(10, 0, -1)) == {"p10": 1, "p50": 5, "p90": 9, "max": 10}
    assert _size_spread([]) == {"p10": 0, "p50": 0, "p90": 0, "max": 0}
    summary = _summary_with_corpus([])
    summary["corpus"]["chunk_tokens"] = {"p10": 120, "p50": 310, "p90": 398, "max": 400}
    assert "chunk tokens: p10=120 p50=310 p90=398 max=400" in format_summary_table(summary)


class _ExplodingEmbedder:
    DIM = EMBEDDING_DIM

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        raise ConnectionError("embedding endpoint down")


@pytest.mark.asyncio
async def test_build_corpus_embed_failure_is_embed_stage(corpus: tuple[Path, Path]) -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    embedder = CountingEmbedder(_ExplodingEmbedder(), expected_dim=EMBEDDING_DIM)
    report = await _build(corpus, store=store, tenant=tenant, embedder=embedder)
    assert report.docs_ingested == 0
    assert report.failed_by_stage == {"parse": 1, "embed": 3}  # incl. the renamed collision
    assert report.chunk_tokens == {"p10": 0, "p50": 0, "p90": 0, "max": 0}  # failed docs: no chunks
    embed_failures = [f for f in report.ingest_failures if f.stage == "embed"]
    assert {f.error_type for f in embed_failures} == {"ConnectionError"}
    docs = await store.list_documents(tenant_id=tenant, kb_id=report.base_id)
    assert {d.status for d in docs} == {DocumentStatus.FAILED}


class _NarrowEmbedder:
    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        return [(1.0, 0.0, 0.0) for _ in texts]


@pytest.mark.asyncio
async def test_build_corpus_aborts_on_embedding_dimension_mismatch(
    corpus: tuple[Path, Path],
) -> None:
    # #14:嵌入维度 ≠ 向量列维度不是某份文档的失败,是配置错 —— 整轮明确报错退出。
    store = InMemoryKnowledgeStore()
    embedder = CountingEmbedder(_NarrowEmbedder(), expected_dim=EMBEDDING_DIM)
    with pytest.raises(EmbeddingDimensionError, match=rf"3 .*{EMBEDDING_DIM}"):
        await _build(corpus, store=store, tenant=uuid4(), embedder=embedder)


def test_fake_embedder_dimension_matches_store_dim() -> None:
    vectors = asyncio.run(build_fake_embedder().embed(["益生菌"], tenant_id=uuid4()))
    assert len(vectors[0]) == EMBEDDING_DIM


# ---------------------------------------------------------------------------
# 嵌入计数(#6 / #9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_counting_embedder_counts_texts_overlong_and_tokens() -> None:
    inner = build_fake_embedder()
    long_text = "益" * (EMBED_OVERLONG_CHARS + 1)
    unpriced = CountingEmbedder(inner, expected_dim=EMBEDDING_DIM)
    await unpriced.embed(["短文本", long_text], tenant_id=uuid4())
    await unpriced.embed(["再来一条"], tenant_id=uuid4())
    stats = unpriced.stats()
    assert stats.texts == 3
    assert stats.overlong == 1
    assert stats.chars == len("短文本") + len(long_text) + len("再来一条")
    assert stats.tokens == 0  # 假嵌入不计 token、不计费
    assert stats.cost_cny_nominal == 0.0
    assert stats.cost_cny_actual == 0.0

    priced = CountingEmbedder(
        inner, expected_dim=EMBEDDING_DIM, count_tokens=True, price_cny_per_1k_tokens=0.5
    )
    await priced.embed(["益生菌需要放冰箱冷藏吗"], tenant_id=uuid4())
    stats = priced.stats()
    assert stats.tokens > 0
    assert stats.cost_cny_nominal == pytest.approx(stats.tokens / 1000 * 0.5)
    # No cache in front: every text went to the embedder, so actual = nominal.
    assert (stats.fetched, stats.fetched_tokens) == (stats.texts, stats.tokens)
    assert stats.cost_cny_actual == stats.cost_cny_nominal


@pytest.mark.asyncio
async def test_embed_cost_counts_only_cache_misses_as_actual_spend(tmp_path: Path) -> None:
    # B-206 —— a cache hit costs nothing: the old single cost_cny counted every
    # text handed to the embedder, an upper bound reported as if it were spent.
    cached = CachedEmbedder(
        build_fake_embedder(),
        model_key="m",
        db_path=tmp_path / "emb.sqlite",
        count_tokens=count_tokens,
    )
    embedder = CountingEmbedder(
        cached, expected_dim=EMBEDDING_DIM, count_tokens=True, price_cny_per_1k_tokens=0.5
    )
    await embedder.embed(["益生菌需要放冰箱冷藏吗"], tenant_id=uuid4())
    await embedder.embed(["益生菌需要放冰箱冷藏吗", "开封后怎么保存"], tenant_id=uuid4())
    stats = embedder.stats()
    assert stats.texts == 3
    assert stats.fetched == 2  # the repeated text came from the cache
    assert stats.fetched_tokens == count_tokens("益生菌需要放冰箱冷藏吗") + count_tokens(
        "开封后怎么保存"
    )
    assert stats.cost_cny_actual == pytest.approx(stats.fetched_tokens / 1000 * 0.5)
    assert stats.cost_cny_nominal == pytest.approx(stats.tokens / 1000 * 0.5)
    assert stats.cost_cny_actual < stats.cost_cny_nominal
    cached.close()


_TASK = "Given a search query, retrieve relevant passages that answer the query"


@pytest.mark.asyncio
async def test_query_marker_arms_do_not_share_cached_query_vectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-206 K-5 A/B: the marker changes the query vector, so the cache key must
    change with it — else the second arm is served the first arm's vectors and
    the comparison measures nothing. Documents (no document marker) stay
    shared, so the corpus is not embedded twice."""
    from expert_work.protocol.model_catalog import EmbeddingCaps
    from orchestrator.llm.embedder import HTTPEmbeddingClient

    sent: list[dict[str, str]] = []

    async def _fake_embeddings(
        self: Any, *, model: str, texts: Sequence[str], fields: Any = None
    ) -> dict[str, Any]:
        sent.append(dict(fields or {}))
        value = 2.0 if fields else 1.0
        return {
            "data": [{"index": i, "embedding": [value] * EMBEDDING_DIM} for i in range(len(texts))]
        }

    monkeypatch.setattr(HTTPEmbeddingClient, "embeddings", _fake_embeddings)
    monkeypatch.setattr("kb_eval.EMBED_CACHE_DB", tmp_path / "embed_cache.sqlite")
    monkeypatch.setenv("EXPERT_WORK_EVAL_EMBED_API_KEY", "unit-test-key")
    monkeypatch.setenv("EXPERT_WORK_EVAL_EMBED_MODEL", "text-embedding-v4")
    tenant = uuid4()
    # The real catalog entry enables no marker, so "on" sends nothing extra.
    assert build_embedder("real", concurrency=1, query_marker=True).query_marker == "none"
    caps = EmbeddingCaps(asymmetry="query_field", query_marker="instruct", instruction=_TASK)
    monkeypatch.setattr("kb_eval.catalog_embedding_caps", lambda model: caps)

    off = build_embedder("real", concurrency=1, query_marker=False)
    on = build_embedder("real", concurrency=1, query_marker=True)
    assert off.query_marker == "off"
    assert on.query_marker == f"instruct={_TASK}"

    plain = await off.embedder.embed_query("开封后怎么保存", tenant_id=tenant)
    marked = await on.embedder.embed_query("开封后怎么保存", tenant_id=tenant)
    assert sent == [{}, {"instruct": _TASK}]
    assert plain != marked
    # Each arm keeps reading its own cached vector.
    assert await off.embedder.embed_query("开封后怎么保存", tenant_id=tenant) == plain
    assert await on.embedder.embed_query("开封后怎么保存", tenant_id=tenant) == marked
    assert len(sent) == 2

    await off.embedder.embed(["开封后请尽快食用"], tenant_id=tenant)
    await on.embedder.embed(["开封后请尽快食用"], tenant_id=tenant)
    assert sent[2:] == [{}]  # one document request for both arms
    assert on.embedder.stats().fetched == 1  # the on arm's document came from the cache


class _FailingQueryEmbedder:
    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        raise ConnectionError("embedding endpoint down")


@pytest.mark.asyncio
async def test_run_eval_aborts_when_query_embedding_degrades(
    corpus: tuple[Path, Path], dataset_path: Path
) -> None:
    """The retriever degrades a failed query embedding to keyword search
    (B-206 K-8); the eval must not score that as the hybrid result. Through the
    eval's own CountingEmbedder: it must hand the vendor error to the retriever
    unwrapped, or the retriever cannot tell it from a bug."""
    dataset, plain, _store, (tenant, _report) = await _built(corpus, dataset_path)
    embedder = CountingEmbedder(_FailingQueryEmbedder(), expected_dim=EMBEDDING_DIM)
    retriever = replace(plain, embedder=embedder)

    with pytest.raises(EmbeddingDegradedError, match="keyword"):
        await run_eval(
            dataset=dataset,
            retriever=retriever,
            tenant_id=tenant,
            cfg=EvalConfig(top_k=20),
            concurrency=1,
        )


@pytest.mark.asyncio
async def test_build_corpus_reports_embed_stats(corpus: tuple[Path, Path]) -> None:
    store = InMemoryKnowledgeStore()
    embedder = CountingEmbedder(build_fake_embedder(), expected_dim=EMBEDDING_DIM)
    report = await _build(corpus, store=store, tenant=uuid4(), embedder=embedder)
    assert report.embed is not None
    assert report.embed.texts >= report.chunk_total
    assert report.embed.overlong == 0
    assert report.embed.tokens == 0


# ---------------------------------------------------------------------------
# 锚文本存活(#11)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_anchors_separates_dead_anchor_from_missing_document(
    corpus: tuple[Path, Path], dataset_path: Path
) -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    report = await _build(corpus, store=store, tenant=tenant)
    anchors = await check_anchors(
        store=store, tenant_id=tenant, kb_id=report.base_id, dataset=load_dataset(dataset_path)
    )
    assert anchors.checked == 4  # q-storage / q-patent / q-ghost / q-dead-anchor 各一条 expected
    assert anchors.alive_cases == frozenset({"q-storage", "q-patent"})
    assert [(d.case_id, d.document, d.reason) for d in anchors.dead] == [
        ("q-ghost", "ghost.md", "document_missing"),
        ("q-dead-anchor", "manual_b.txt", "anchor_not_in_any_chunk"),
    ]


# ---------------------------------------------------------------------------
# 打分
# ---------------------------------------------------------------------------


async def _built(
    corpus: tuple[Path, Path], dataset_path: Path
) -> tuple[Any, Any, InMemoryKnowledgeStore, Any]:
    manifest, root = corpus
    dataset = load_dataset(dataset_path)
    store = InMemoryKnowledgeStore()
    embedder = CountingEmbedder(build_fake_embedder(), expected_dim=EMBEDDING_DIM)
    tenant = uuid4()
    selection = select_rows(_manifest_rows(manifest), tiers=("A", "D-A"), limit_docs=0)
    report = await build_corpus(
        store=store,
        embedder=embedder,
        tenant_id=tenant,
        base_name=dataset.name,
        corpus_root=root,
        selection=selection,
        concurrency=1,
    )
    retriever = build_retriever(store=store, embedder=embedder, reranker=None)
    return dataset, retriever, store, (tenant, report)


def test_build_retriever_uses_production_recall_depth() -> None:
    # B-175 W2 PR2:评测的召回深度与生产同一个默认(两路各 clamp(2*top_k, 50, 100)),
    # 不另设宽度;只有 top_k 超过生产深度时才抬到 top_k(#10b,否则前 k 名凑不满)。
    store = InMemoryKnowledgeStore()
    embedder = build_fake_embedder()
    production = KnowledgeRetriever(store=store, embedder=embedder)
    for top_k in (5, 20, 50, 100):
        retriever = build_retriever(store=store, embedder=embedder, reranker=None, top_k=top_k)
        assert retriever.recall_limit is None
        assert retriever.recall_depth(top_k) == production.recall_depth(top_k)
    wide = build_retriever(store=store, embedder=embedder, reranker=None, top_k=130)
    assert wide.recall_depth(130) == 130


@pytest.mark.asyncio
async def test_run_eval_document_and_chunk_hits(
    corpus: tuple[Path, Path], dataset_path: Path
) -> None:
    dataset, retriever, _store, (tenant, _report) = await _built(corpus, dataset_path)
    cfg = EvalConfig(method=RetrievalMethod.HYBRID, top_k=20, threshold=None, rerank=False)
    rows = await run_eval(
        dataset=dataset, retriever=retriever, tenant_id=tenant, cfg=cfg, concurrency=2
    )
    by_id = {r.case_id: r for r in rows}
    assert set(by_id) == {"q-storage", "q-patent", "q-ghost", "q-dead-anchor", "q-none"}

    storage = by_id["q-storage"]
    assert storage.score.doc_hit_20 == 1.0
    assert storage.score.chunk_hit_20 == 1.0
    assert storage.score.chunk_coverage_20 == 1.0
    # manual_a.md 按标题切成两段,只有「保存方式」那段含锚文本:段级命中必须**恰好一处**,
    # 且就是 manual_a.md 的那条结果(把锚文本判定去掉 → 两段都算命中 → 这里红)。
    assert len(storage.score.chunk_hit_ranks) == 1
    hit = storage.results[storage.score.chunk_hit_ranks[0] - 1]
    assert hit.filename == "manual_a.md"
    assert storage.score.doc_hit_ranks
    assert all(r.recall_source for r in storage.results)
    assert storage.score.best_score is not None  # #5:向量路最佳分数随 case 落盘

    # manual_b.txt 只有一段且含锚文本:锚文本判定反了 → 段级 0 命中 → 这里红。
    patent = by_id["q-patent"]
    assert patent.score.doc_hit_20 == 1.0
    assert patent.score.chunk_hit_20 == 1.0
    assert len(patent.score.chunk_hit_ranks) == 1

    ghost = by_id["q-ghost"]
    assert ghost.score.doc_hit_20 == 0.0
    assert ghost.score.chunk_hit_20 == 0.0
    assert ghost.score.zero_hit is False  # 有返回,只是没命中


@pytest.mark.asyncio
async def test_run_eval_passes_match_mode_to_scoring(
    tmp_path: Path, corpus: tuple[Path, Path]
) -> None:
    # #1:match=all 的 case 两份文档都要进前 k;只找到一份 → hit 0、coverage 0.5。
    manifest, _root = corpus
    cases = [
        {
            "id": "q-cross",
            "query": "益生菌需要放冰箱冷藏吗",
            "source": "hard_cross",
            "tags": [],
            "match": "all",
            "expected": [
                {"document": "manual_a.md", "anchor": _ANCHOR_A, "page": None},
                {"document": "ghost.md", "anchor": "这个文档不在语料里", "page": None},
            ],
            "answer": None,
        },
        {
            "id": "q-either",
            "query": "益生菌需要放冰箱冷藏吗",
            "source": "faq_xlsx",
            "tags": [],
            "expected": [
                {"document": "manual_a.md", "anchor": _ANCHOR_A, "page": None},
                {"document": "ghost.md", "anchor": "这个文档不在语料里", "page": None},
            ],
            "answer": None,
        },
    ]
    path = _write_dataset(tmp_path / "match_kb.yaml", str(manifest), cases)
    dataset, retriever, _store, (tenant, _report) = await _built(corpus, path)
    cfg = EvalConfig(method=RetrievalMethod.HYBRID, top_k=20, threshold=None, rerank=False)
    rows = await run_eval(
        dataset=dataset, retriever=retriever, tenant_id=tenant, cfg=cfg, concurrency=1
    )
    by_id = {r.case_id: r for r in rows}
    assert by_id["q-cross"].score.match == "all"
    assert by_id["q-cross"].score.doc_hit_20 == 0.0
    assert by_id["q-cross"].score.doc_coverage_20 == 0.5
    assert by_id["q-either"].score.match == "any"
    assert by_id["q-either"].score.doc_hit_20 == 1.0
    assert by_id["q-either"].score.doc_coverage_20 == 0.5


@pytest.mark.asyncio
async def test_run_eval_no_answer_refusal_and_false_refusal(
    corpus: tuple[Path, Path], dataset_path: Path
) -> None:
    dataset, retriever, _store, (tenant, _report) = await _built(corpus, dataset_path)
    lenient = EvalConfig(method=RetrievalMethod.HYBRID, top_k=20, threshold=None, rerank=False)
    rows = await run_eval(
        dataset=dataset, retriever=retriever, tenant_id=tenant, cfg=lenient, concurrency=1
    )
    none_row = next(r for r in rows if r.case_id == "q-none")
    assert none_row.score.answerable is False
    assert none_row.score.refused is None  # #5:没给阈值就没有拒答这回事
    assert none_row.score.doc_hit_10 is None
    summary = summarise(rows, corpus=None)
    assert summary["overall"]["no_answer_refusal_rate"] is None
    assert summary["overall"]["false_refusal_rate"] is None

    strict = EvalConfig(method=RetrievalMethod.VECTOR, top_k=20, threshold=0.999, rerank=False)
    rows = await run_eval(
        dataset=dataset, retriever=retriever, tenant_id=tenant, cfg=strict, concurrency=1
    )
    by_id = {r.case_id: r for r in rows}
    # #10c:阈值透传到检索器 —— 向量路在库里就被过滤,返回必须是空,不只是「分数都低」。
    assert all(row.results == () for row in rows)
    assert by_id["q-none"].score.refused is True
    assert by_id["q-storage"].score.refused is True  # 阈值太高 → 有答案也被拒 = 误拒
    summary = summarise(rows, corpus=None)
    assert summary["overall"]["no_answer_refusal_rate"] == 1.0
    assert summary["overall"]["false_refusal_rate"] == 1.0


class _FlatScoreReranker:
    """Scores every candidate 0.3 (a scoring reranker, like DashScope's)."""

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        return list(range(min(len(documents), top_k)))

    async def rerank_scored(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[tuple[int, float | None]]:
        return [(i, 0.3) for i in range(min(len(documents), top_k))]


@pytest.mark.asyncio
async def test_run_eval_rerank_threshold_refuses_and_records_rerank_scores(
    corpus: tuple[Path, Path], dataset_path: Path
) -> None:
    dataset, plain, _store, (tenant, _report) = await _built(corpus, dataset_path)
    retriever = replace(plain, reranker=_FlatScoreReranker())

    strict = EvalConfig(top_k=20, rerank=True, rerank_threshold=0.5)
    rows = await run_eval(
        dataset=dataset, retriever=retriever, tenant_id=tenant, cfg=strict, concurrency=1
    )
    assert all(row.results == () and row.score.refused is True for row in rows)

    loose = EvalConfig(top_k=20, rerank=True, rerank_threshold=0.1)
    rows = await run_eval(
        dataset=dataset, retriever=retriever, tenant_id=tenant, cfg=loose, concurrency=1
    )
    storage = next(r for r in rows if r.case_id == "q-storage")
    assert storage.score.refused is False
    assert {r.rerank_score for r in storage.results} == {0.3}
    assert case_to_json(storage)["best_rerank_score"] == 0.3


class _CacheMissReranker(_FlatScoreReranker):
    """An offline rerun whose request is not in the rerank cache."""

    async def rerank_scored(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[tuple[int, float | None]]:
        raise RerankCacheMissError("rerank cache miss (offline key)")


@pytest.mark.asyncio
async def test_run_eval_aborts_when_rerank_degrades(
    corpus: tuple[Path, Path], dataset_path: Path
) -> None:
    """The retriever degrades a failed rerank to the fused order; the eval must
    not score that silently (B-175 wave 2 PR3)."""
    dataset, plain, _store, (tenant, _report) = await _built(corpus, dataset_path)
    retriever = replace(plain, reranker=_CacheMissReranker())

    with pytest.raises(RerankDegradedError, match="fused order"):
        await run_eval(
            dataset=dataset,
            retriever=retriever,
            tenant_id=tenant,
            cfg=EvalConfig(top_k=20, rerank=True),
            concurrency=1,
        )


@pytest.mark.asyncio
async def test_summary_groups_by_source_and_tag(
    corpus: tuple[Path, Path], dataset_path: Path
) -> None:
    dataset, retriever, _store, (tenant, report) = await _built(corpus, dataset_path)
    cfg = EvalConfig(method=RetrievalMethod.HYBRID, top_k=20, threshold=None, rerank=False)
    rows = await run_eval(
        dataset=dataset, retriever=retriever, tenant_id=tenant, cfg=cfg, concurrency=1
    )
    summary = summarise(rows, corpus=report)
    assert summary["overall"]["n"] == 5
    assert summary["overall"]["n_answerable"] == 4
    assert set(summary["by_source"]) == {"faq_xlsx", "hard_exact", "manual_qa", "no_answer"}
    assert summary["by_source"]["faq_xlsx"]["doc_hit@20"] == 1.0
    assert summary["by_source"]["manual_qa"]["doc_hit@20"] == 0.0
    assert set(summary["by_tag"]) == {"通用", "保存", "专利"}
    assert summary["by_tag"]["通用"]["n"] == 3  # q-storage + q-ghost + q-none
    assert summary["by_tag"]["通用"]["doc_hit@20"] == 0.5  # 两条有答案的一中一不中
    assert summary["corpus"]["docs_ingested"] == 3  # incl. the renamed collision
    assert summary["corpus"]["docs_name_collision"] == 1
    assert summary["corpus"]["docs_skipped_empty"] == 0
    assert summary["corpus"]["ingest_failures"] == [
        {
            "filename": "broken.xyz",
            "path": "docs/broken.xyz",
            "stage": "parse",
            "error_type": "DocumentParseError",
            "error": summary["corpus"]["ingest_failures"][0]["error"],
        }
    ]
    assert summary["corpus"]["embed"]["texts"] > 0


def test_summary_counts_a_repeated_tag_once_per_case() -> None:
    # #16:同一 case 写了两遍同一个 tag,分组里只算一次。
    score = score_case([], [], threshold=None)
    row = CaseRow(
        case_id="q", source="no_answer", tags=("通用", "通用", "保存"), results=(), score=score
    )
    summary = summarise([row], corpus=None)
    assert summary["by_tag"]["通用"]["n"] == 1
    assert summary["by_tag"]["保存"]["n"] == 1


def test_rls_tenant_sets_and_resets_context_var() -> None:
    # #13:SQL 库那条路要像应用层一样带租户上下文(after_begin 才会 set_config app.tenant_id)。
    tenant = uuid4()
    assert current_tenant_id_var.get() is None
    with rls_tenant(tenant):
        assert current_tenant_id_var.get() == tenant
    assert current_tenant_id_var.get() is None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(dataset_path: Path, root: Path, out: Path, baseline: Path, *extra: str) -> list[str]:
    # 清单在 inventory/ 下、文档在 corpus/ 下(清点脚本 <根目录> <输出目录> 两参数的真实布局),
    # 所以每次都要给 --corpus-root。
    return [
        "--dataset",
        str(dataset_path),
        "--corpus-root",
        str(root),
        "--store",
        "memory",
        "--embedder",
        "fake",
        "--label",
        "unit",
        "--out",
        str(out),
        "--baseline-out",
        str(baseline),
        *extra,
    ]


def test_main_refuses_to_overwrite_out(
    tmp_path: Path, dataset_path: Path, corpus: tuple[Path, Path]
) -> None:
    _manifest, root = corpus
    out = tmp_path / "unit.jsonl"
    out.write_text("old\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        main(_cli(dataset_path, root, out, tmp_path / "baseline.yaml"))
    assert out.read_text(encoding="utf-8") == "old\n"


def test_main_exits_when_nothing_ingested(
    tmp_path: Path, dataset_path: Path, corpus: tuple[Path, Path]
) -> None:
    # 根目录给错 → 每份都 FileNotFoundError;零文档入库的跑不能产出一份全零的「基线」。
    wrong_root = tmp_path / "nowhere"
    out = tmp_path / "unit.jsonl"
    with pytest.raises(SystemExit, match="no document ingested"):
        main(_cli(dataset_path, wrong_root, out, tmp_path / "baseline.yaml"))
    assert not out.exists()


def test_main_writes_jsonl_and_baseline_fingerprint(
    tmp_path: Path,
    dataset_path: Path,
    corpus: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _manifest, root = corpus
    out = tmp_path / "out" / "unit.jsonl"
    baseline = tmp_path / "baseline.yaml"
    # --limit-docs 2:只入 manual_a / manual_b,零失败零同名冲突,基线才允许写。
    argv = _cli(
        dataset_path,
        root,
        out,
        baseline,
        "--update-baseline",
        "--concurrency",
        "2",
        "--tiers",
        "A",
        "--limit-docs",
        "2",
    )
    assert main(argv) == 0

    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 6  # 5 case + 1 summary
    case_line = next(line for line in lines if line.get("id") == "q-storage")
    assert case_line["source"] == "faq_xlsx"
    assert case_line["tags"] == ["通用", "保存"]
    assert case_line["match"] == "any"
    assert case_line["metrics"]["doc_hit@20"] == 1.0
    assert case_line["metrics"]["doc_coverage@20"] == 1.0
    assert case_line["hits"]["doc_ranks"]
    assert isinstance(case_line["best_score"], float)
    assert case_line["anchors_alive"] is True
    dead_line = next(line for line in lines if line.get("id") == "q-dead-anchor")
    assert dead_line["anchors_alive"] is False
    assert {"filename", "chunk_index", "score", "recall_source"} <= set(case_line["results"][0])
    summary_line = lines[-1]["summary"]
    assert summary_line["overall"]["n"] == 5
    assert summary_line["corpus"]["docs_ingested"] == 2
    assert summary_line["corpus"]["docs_skipped_dup"] == 1
    assert summary_line["corpus"]["docs_skipped_limit"] == 2
    assert summary_line["corpus"]["docs_failed"] == 0
    assert summary_line["corpus"]["embedder"] == "fake"
    assert summary_line["anchors"]["checked"] == 4
    assert summary_line["anchors"]["dead_cases"] == 2
    assert summary_line["config"]["method"] == "hybrid"
    assert summary_line["config"]["label"] == "unit"
    # The fake embedder has no catalog entry → no marker, whatever the switch says.
    assert summary_line["config"]["query_marker"] == "none"

    data = yaml.safe_load(baseline.read_text(encoding="utf-8"))
    section = data["kb"]["unit_kb"]
    assert section["overall"]["n"] == 5
    # #12:会抖的字段不进基线。
    assert "base_id" not in section["corpus"]
    assert "elapsed_s" not in section["corpus"]
    assert "label" not in section["config"]
    fingerprint = data["meta"]["fingerprints"]["kb/unit_kb"]
    for key in (
        "dataset_sha256",
        "manifest_sha256",
        "embedder",
        "embed_base_url",
        "query_marker",
        "embedding_dim",
        "store",
        "commit",
        "git_dirty",
        "corpus_root",
        "tiers",
        "limit_docs",
        "method",
        "top_k",
        "threshold",
        "rerank",
    ):
        assert key in fingerprint, key
    assert fingerprint["tiers"] == ["A"]  # #10d:来自 --tiers,不是默认值
    assert fingerprint["limit_docs"] == 2
    assert fingerprint["top_k"] == 20
    assert fingerprint["embedder"] == "fake"
    assert fingerprint["embed_base_url"] is None
    assert fingerprint["embedding_dim"] == EMBEDDING_DIM
    assert isinstance(fingerprint["git_dirty"], bool)
    assert fingerprint["corpus_root"] == root.resolve().name  # no machine-local path
    assert len(fingerprint["dataset_sha256"]) == 64
    assert len(fingerprint["manifest_sha256"]) == 64

    printed = capsys.readouterr().out
    assert "overall" in printed
    assert "doc_hit@20" in printed
    assert "anchors:" in printed


def test_main_refuses_baseline_when_any_document_failed(
    tmp_path: Path, dataset_path: Path, corpus: tuple[Path, Path]
) -> None:
    # #7:有入库失败的跑不是基线 —— 结果照写、表照打,但基线不动、退出码非 0。
    # (同名冲突已改名入库,不再算失败。)
    _manifest, root = corpus
    out = tmp_path / "unit.jsonl"
    baseline = tmp_path / "baseline.yaml"
    with pytest.raises(SystemExit) as info:
        main(_cli(dataset_path, root, out, baseline, "--update-baseline"))
    message = str(info.value)
    assert "refusing to update baseline" in message
    assert "failed=1" in message
    assert "name_collision" not in message
    assert out.exists()
    assert not baseline.exists()


def test_main_corpus_flag_overrides_dataset_manifest(
    tmp_path: Path, dataset_path: Path, corpus: tuple[Path, Path]
) -> None:
    manifest, root = corpus
    moved = tmp_path / "elsewhere" / "manifest.csv"
    moved.parent.mkdir()
    moved.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
    out = tmp_path / "unit.jsonl"
    code = main(
        _cli(
            dataset_path,
            root,
            out,
            tmp_path / "baseline.yaml",
            "--corpus",
            str(moved),
            "--limit-docs",
            "1",
        )
    )
    assert code == 0
    summary = json.loads(out.read_text(encoding="utf-8").splitlines()[-1])["summary"]
    assert summary["corpus"]["docs_ingested"] == 1
    assert summary["config"]["limit_docs"] == 1


def test_main_resolves_relative_manifest_against_dataset_dir(
    tmp_path: Path, corpus: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # #15:评测集里写的相对路径相对评测集文件所在目录,不随 cwd 变。
    _manifest, root = corpus
    dataset = _write_dataset(tmp_path / "rel_kb.yaml", "inventory/manifest.csv")
    monkeypatch.chdir(root)  # cwd 下没有 inventory/
    out = tmp_path / "rel.jsonl"
    assert main(_cli(dataset, root, out, tmp_path / "b.yaml")) == 0
    assert out.exists()


def test_main_rerank_on_needs_llm_env(
    tmp_path: Path,
    dataset_path: Path,
    corpus: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest, root = corpus
    monkeypatch.delenv("EXPERT_WORK_EVAL_LLM_API_KEY", raising=False)
    monkeypatch.delenv("EXPERT_WORK_EVAL_LLM_MODEL", raising=False)
    argv = _cli(dataset_path, root, tmp_path / "unit.jsonl", tmp_path / "b.yaml", "--rerank", "on")
    with pytest.raises(SystemExit, match="EXPERT_WORK_EVAL_LLM"):
        main(argv)


def test_main_rerank_threshold_needs_dashscope(
    tmp_path: Path, dataset_path: Path, corpus: tuple[Path, Path]
) -> None:
    _manifest, root = corpus
    argv = _cli(
        dataset_path,
        root,
        tmp_path / "unit.jsonl",
        tmp_path / "b.yaml",
        "--rerank-threshold",
        "0.7",
    )
    with pytest.raises(SystemExit, match="needs --rerank dashscope"):
        main(argv)


def test_main_refuses_baseline_with_rerank_threshold(
    tmp_path: Path, dataset_path: Path, corpus: tuple[Path, Path]
) -> None:
    _manifest, root = corpus
    argv = _cli(
        dataset_path,
        root,
        tmp_path / "unit.jsonl",
        tmp_path / "b.yaml",
        "--rerank",
        "dashscope",
        "--rerank-threshold",
        "0.7",
        "--update-baseline",
    )
    with pytest.raises(SystemExit, match="refusing to update baseline with --rerank-threshold"):
        main(argv)
    assert not (tmp_path / "b.yaml").exists()


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ((), ("dual", 100, False)),
        (("--rerank-view", "dual", "--rerank-pool", "50"), ("dual", 50, False)),
        (
            ("--rerank-view", "single", "--rerank-pool", "60", "--rerank-instruct"),
            ("single", 60, True),
        ),
        (("--rerank-view", "single_clean"), ("single_clean", 100, False)),
    ],
)
def test_main_rerank_arm_flags_reach_the_retriever_and_are_recorded(
    tmp_path: Path,
    dataset_path: Path,
    corpus: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    flags: tuple[str, ...],
    expected: tuple[str, int, bool],
) -> None:
    """B-206 K-14 A/B: ``--rerank-view`` / ``--rerank-pool`` / ``--rerank-instruct`` set the
    product retriever's own knobs (no flag = the product default), and the run records them."""
    built: list[KnowledgeRetriever] = []

    def _spy(**kwargs: Any) -> KnowledgeRetriever:
        retriever = cast(KnowledgeRetriever, build_retriever(**kwargs))
        built.append(retriever)
        return retriever

    monkeypatch.setattr("kb_eval.build_retriever", _spy)
    _manifest, root = corpus
    out = tmp_path / "unit.jsonl"
    argv = _cli(dataset_path, root, out, tmp_path / "b.yaml", "--limit-docs", "1", *flags)
    assert main(argv) == 0
    (retriever,) = built
    assert (retriever.rerank_view, retriever.rerank_pool, retriever.rerank_instruction) == expected
    config = json.loads(out.read_text(encoding="utf-8").splitlines()[-1])["summary"]["config"]
    recorded = (config["rerank_view"], config["rerank_pool"], config["rerank_instruct"])
    assert recorded == expected


@pytest.mark.parametrize(
    "flags",
    [
        ("--rerank-view", "single"),
        ("--rerank-pool", "50"),
        ("--rerank-view", "single", "--rerank-instruct"),
    ],
)
def test_main_refuses_baseline_for_a_non_default_rerank_arm(
    tmp_path: Path, dataset_path: Path, corpus: tuple[Path, Path], flags: tuple[str, ...]
) -> None:
    _manifest, root = corpus
    argv = _cli(
        dataset_path, root, tmp_path / "unit.jsonl", tmp_path / "b.yaml", "--update-baseline"
    )
    with pytest.raises(SystemExit, match="refusing to update baseline with a K-14 arm"):
        main([*argv, *flags])
    assert not (tmp_path / "b.yaml").exists()


class _NamedReranker(_FlatScoreReranker):
    """Scores the titled view; the body view (passages without the document
    title) fails — every search ends ``partial``."""

    model = "qwen3-rerank"

    async def rerank_scored(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[tuple[int, float | None]]:
        if not any(document.startswith("[Document:") for document in documents):
            raise RerankError("body view failed")
        return await super().rerank_scored(
            query=query, documents=documents, top_k=top_k, tenant_id=tenant_id
        )


def test_main_rerank_model_reaches_the_reranker_and_is_recorded(
    tmp_path: Path,
    dataset_path: Path,
    corpus: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-206 K-7b — ``--rerank-model`` picks the native rerank model; the run
    records it and how every search's rerank ended (``partial`` counted, not fatal)."""
    asked: list[tuple[str, str | None]] = []

    def _build(mode: str, *, model: str | None = None) -> tuple[Any, None]:
        asked.append((mode, model))
        return _NamedReranker(), None

    monkeypatch.setattr("kb_eval.build_rerank_mode", _build)
    _manifest, root = corpus
    out = tmp_path / "unit.jsonl"
    argv = _cli(dataset_path, root, out, tmp_path / "b.yaml", "--limit-docs", "1")
    assert main([*argv, "--rerank", "dashscope", "--rerank-model", "qwen3-rerank"]) == 0
    assert asked == [("dashscope", "qwen3-rerank")]
    summary = json.loads(out.read_text(encoding="utf-8").splitlines()[-1])["summary"]
    assert summary["config"]["rerank_model"] == "qwen3-rerank"
    assert summary["rerank_outcomes"] == {"partial": len(_cases())}


def test_main_can_put_the_threshold_on_the_base(
    tmp_path: Path,
    dataset_path: Path,
    corpus: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-206 K-7b — ``--rerank-threshold-on-base`` stores the threshold on the base,
    bound to the run's rerank model, instead of passing it with each search: the
    searches meet it as production does (the model binding checked), and refuse as
    with the passed threshold."""
    built: list[KnowledgeRetriever] = []

    def _spy(**kwargs: Any) -> KnowledgeRetriever:
        retriever = cast(KnowledgeRetriever, build_retriever(**kwargs))
        built.append(retriever)
        return retriever

    asked: list[float | None] = []
    search = KnowledgeRetriever.search_detailed

    async def _record(self: KnowledgeRetriever, **kwargs: Any) -> Any:
        asked.append(kwargs.get("rerank_score_threshold"))
        return await search(self, **kwargs)

    monkeypatch.setattr("kb_eval.build_retriever", _spy)
    monkeypatch.setattr(KnowledgeRetriever, "search_detailed", _record)
    monkeypatch.setattr(
        "kb_eval.build_rerank_mode", lambda mode, *, model=None: (_FlatScoreReranker(), None)
    )
    _manifest, root = corpus
    out = tmp_path / "unit.jsonl"
    argv = _cli(dataset_path, root, out, tmp_path / "b.yaml", "--limit-docs", "1")
    flags = ["--rerank", "dashscope", "--rerank-threshold", "0.5", "--rerank-threshold-on-base"]
    assert main([*argv, *flags]) == 0
    (retriever,) = built
    (base,) = asyncio.run(
        cast(InMemoryKnowledgeStore, retriever.store).list_bases(tenant_id=EVAL_TENANT_ID)
    )
    assert (base.rerank_score_threshold, base.rerank_threshold_model) == (
        0.5,
        "qwen/qwen3-vl-rerank",
    )
    assert asked and set(asked) == {None}  # nothing passed with the search
    lines = out.read_text(encoding="utf-8").splitlines()
    summary = json.loads(lines[-1])["summary"]
    assert summary["config"]["rerank_threshold_on_base"] is True
    cases = [json.loads(line) for line in lines[:-1]]
    assert all(case["refused"] is True for case in cases)  # every flat 0.3 is under 0.5


def test_main_needs_a_threshold_to_put_on_the_base(
    tmp_path: Path, dataset_path: Path, corpus: tuple[Path, Path]
) -> None:
    _manifest, root = corpus
    argv = _cli(dataset_path, root, tmp_path / "unit.jsonl", tmp_path / "b.yaml")
    with pytest.raises(SystemExit, match="--rerank-threshold-on-base needs --rerank-threshold"):
        main([*argv, "--rerank", "dashscope", "--rerank-threshold-on-base"])


def test_main_refuses_baseline_for_another_rerank_model(
    tmp_path: Path, dataset_path: Path, corpus: tuple[Path, Path]
) -> None:
    _manifest, root = corpus
    argv = _cli(
        dataset_path, root, tmp_path / "unit.jsonl", tmp_path / "b.yaml", "--update-baseline"
    )
    with pytest.raises(SystemExit, match="refusing to update baseline with --rerank-model"):
        main([*argv, "--rerank", "dashscope", "--rerank-model", "qwen3-rerank"])
    assert not (tmp_path / "b.yaml").exists()


def test_main_rerank_pool_must_be_positive(
    tmp_path: Path, dataset_path: Path, corpus: tuple[Path, Path]
) -> None:
    _manifest, root = corpus
    argv = _cli(dataset_path, root, tmp_path / "unit.jsonl", tmp_path / "b.yaml")
    with pytest.raises(SystemExit, match="--rerank-pool"):
        main([*argv, "--rerank-pool", "0"])


@pytest.mark.parametrize("flags", [("--rerank-instruct",), ("--rerank-view", "dual")])
def test_main_refuses_an_instruction_with_the_dual_view(
    tmp_path: Path, dataset_path: Path, corpus: tuple[Path, Path], flags: tuple[str, ...]
) -> None:
    """The dual view sends no instruction: such a run would be recorded as
    instructed while every request went out without one. Refused before ingest."""
    _manifest, root = corpus
    out = tmp_path / "unit.jsonl"
    argv = _cli(dataset_path, root, out, tmp_path / "b.yaml", "--rerank-instruct", *flags)
    with pytest.raises(SystemExit, match="--rerank-instruct needs --rerank-view single"):
        main(argv)
    assert not out.exists()


class _PartialReranker(_FlatScoreReranker):
    """Judges every candidate but the last, like a model capped below the pool."""

    async def rerank_scored(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[tuple[int, float | None]]:
        judged = documents[:-1] or documents
        ranked = await super().rerank_scored(
            query=query, documents=judged, top_k=top_k, tenant_id=tenant_id
        )
        return RerankRanking(ranked, dropped=len(documents) - len(judged))


@pytest.mark.asyncio
async def test_run_eval_aborts_when_rerank_left_candidates_unjudged(
    corpus: tuple[Path, Path], dataset_path: Path
) -> None:
    """A rerank over part of the pool would score an arm that did not run as
    configured (``--rerank-pool`` above the model's cap, or a size retry)."""
    dataset, plain, _store, (tenant, _report) = await _built(corpus, dataset_path)
    retriever = replace(plain, reranker=_PartialReranker())
    with pytest.raises(RerankDegradedError, match="never judged"):
        await run_eval(
            dataset=dataset,
            retriever=retriever,
            tenant_id=tenant,
            cfg=EvalConfig(top_k=20, rerank=True),
            concurrency=1,
        )


def test_main_real_embedder_needs_env(
    tmp_path: Path,
    dataset_path: Path,
    corpus: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest, root = corpus
    monkeypatch.delenv("EXPERT_WORK_EVAL_EMBED_API_KEY", raising=False)
    monkeypatch.delenv("EXPERT_WORK_EVAL_EMBED_MODEL", raising=False)
    argv = _cli(
        dataset_path, root, tmp_path / "unit.jsonl", tmp_path / "b.yaml", "--embedder", "real"
    )
    with pytest.raises(SystemExit, match="EXPERT_WORK_EVAL_EMBED"):
        main(argv)


# ---------------------------------------------------------------------------
# 锚文本判对(B-175 波 2 PR0):含同一锚文本的其他文档也算对
# ---------------------------------------------------------------------------

_SHARED = "益生菌可通过正常饮食摄入"
_COMMON = "请遵医嘱"


async def _seed_docs(
    store: InMemoryKnowledgeStore,
    tenant: UUID,
    docs: dict[str, str],
    titles: dict[str, str] | None = None,
) -> UUID:
    from control_plane.knowledge.ingestion import ingest_markdown

    embedder = build_fake_embedder()
    base = await store.create_base(tenant_id=tenant, name="kb")
    for filename, text in docs.items():
        doc = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename=filename, content=None
        )
        count = await ingest_markdown(
            store=store,
            embedder=embedder,
            tenant_id=tenant,
            document_id=doc.id,
            kb_id=base.id,
            markdown=text,
            chunk_max_tokens=base.chunk_max_tokens,
            chunk_overlap_tokens=base.chunk_overlap_tokens,
            doc_title=(titles or {}).get(filename),
        )
        await store.set_document_status(
            tenant_id=tenant, document_id=doc.id, status=DocumentStatus.READY, chunk_count=count
        )
    return base.id


def _one_case_dataset(*expected: tuple[str, str]) -> KbDataset:
    return KbDataset(
        name="kb",
        corpus_manifest=None,
        corpus_tiers=("A",),
        cases=(
            KbCase(
                id="q1",
                query="益生菌怎么补",
                source="faq",
                tags=(),
                expected=tuple(KbExpected(document=d, anchor=a, page=None) for d, a in expected),
                answer=None,
            ),
        ),
    )


async def test_resolve_anchor_documents_adds_other_documents_holding_the_anchor() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    with rls_tenant(tenant):
        kb_id = await _seed_docs(
            store,
            tenant,
            {
                "faq-2025.md": f"# 问答\n\n{_SHARED},不必额外补充。\n",
                "faq-2026.md": f"# 问答\n\n{_SHARED},不必额外补充。\n",
                "notes.md": "# 其他\n\n只有这里写了冷藏保存。\n",
            },
        )
        resolved, report = await resolve_anchor_documents(
            store=store,
            tenant_id=tenant,
            kb_id=kb_id,
            dataset=_one_case_dataset(("faq-2025.md", _SHARED), ("notes.md", "冷藏保存")),
        )
    shared, unique = resolved.cases[0].expected
    assert shared.documents == frozenset({"faq-2025.md", "faq-2026.md"})
    assert unique.documents == frozenset({"notes.md"})
    assert (report.expanded, report.expanded_cases, report.too_common) == (1, 1, ())


async def test_anchor_checks_ignore_the_document_title() -> None:
    """Hits are judged on the text the agent sees, which has no title: an
    anchor found only in a document's title is dead, and a document that got
    the anchor-bearing title (the shuffled control group) is not credited."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    number = "ZL202410048761.8"
    with rls_tenant(tenant):
        kb_id = await _seed_docs(
            store,
            tenant,
            {
                "patent.md": "# 证书\n\n发明名称:一种组合物。\n",
                "manual.md": "# 说明\n\n每日一袋。\n",
            },
            titles={"patent.md": f"专利证书 {number}", "manual.md": f"专利证书 {number}"},
        )
        dataset = _one_case_dataset(("patent.md", number))
        anchors = await check_anchors(store=store, tenant_id=tenant, kb_id=kb_id, dataset=dataset)
        resolved, report = await resolve_anchor_documents(
            store=store, tenant_id=tenant, kb_id=kb_id, dataset=dataset
        )
    assert [(d.document, d.reason) for d in anchors.dead] == [
        ("patent.md", "anchor_not_in_any_chunk")
    ]
    assert resolved.cases[0].expected[0].documents == frozenset({"patent.md"})
    assert report.expanded == 0


async def test_resolve_anchor_documents_leaves_a_too_common_anchor_alone() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    with rls_tenant(tenant):
        kb_id = await _seed_docs(
            store,
            tenant,
            {f"doc{i}.md": f"# 说明\n\n第 {i} 款,{_COMMON}。\n" for i in range(3)},
        )
        resolved, report = await resolve_anchor_documents(
            store=store,
            tenant_id=tenant,
            kb_id=kb_id,
            dataset=_one_case_dataset(("doc0.md", _COMMON)),
            max_docs=2,
        )
    assert resolved.cases[0].expected[0].documents == frozenset({"doc0.md"})
    assert report.expanded == 0
    assert [(t.case_id, t.document, t.n_docs) for t in report.too_common] == [("q1", "doc0.md", 3)]


async def test_resolve_anchor_documents_matches_through_spacing_and_markup() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    with rls_tenant(tenant):
        kb_id = await _seed_docs(
            store,
            tenant,
            {
                "a.md": "# 菌株\n\n菌株QX-01 专利号ZL201900001234.5\n",
                "b.md": "# 菌株\n\n**菌株QX-01** 专利号：ZL 2019 0 0001234.5\n",  # noqa: RUF001
            },
        )
        dataset = _one_case_dataset(("a.md", "菌株QX-01 专利号ZL201900001234.5"))
        resolved, _report = await resolve_anchor_documents(
            store=store, tenant_id=tenant, kb_id=kb_id, dataset=dataset
        )
        anchors = await check_anchors(
            store=store,
            tenant_id=tenant,
            kb_id=kb_id,
            dataset=_one_case_dataset(("b.md", "菌株QX-01 专利号ZL201900001234.5")),
        )
    assert resolved.cases[0].expected[0].documents == frozenset({"a.md", "b.md"})
    assert anchors.alive_cases == frozenset({"q1"})


def test_main_records_doc_match_mode_in_config(tmp_path: Path) -> None:
    parser = _build_parser()
    assert parser.parse_args(["--dataset", "x.yaml"]).doc_match == "anchor"
    assert parser.parse_args(["--dataset", "x.yaml", "--doc-match", "listed"]).doc_match == "listed"


def test_baseline_keeps_recall_and_final_ranking_apart() -> None:
    assert baseline_benchmark("huada_v1", "off") == "huada_v1"
    assert baseline_benchmark("huada_v1", "dashscope") == "huada_v1@rerank-dashscope"


@pytest.mark.asyncio
async def test_with_text_keeps_the_retrieved_text_and_default_leaves_it_out(
    corpus: tuple[Path, Path], dataset_path: Path
) -> None:
    """``--with-text`` writes each result's retrieval text (pooled relevance labelling needs
    it); without the flag the results file keeps its old shape — no ``text`` key."""
    dataset, retriever, _store, (tenant, _report) = await _built(corpus, dataset_path)
    plain = EvalConfig(method=RetrievalMethod.HYBRID, top_k=5, threshold=None, rerank=False)
    with_text = replace(plain, with_text=True)
    rows_plain = await run_eval(
        dataset=dataset, retriever=retriever, tenant_id=tenant, cfg=plain, concurrency=1
    )
    rows_text = await run_eval(
        dataset=dataset, retriever=retriever, tenant_id=tenant, cfg=with_text, concurrency=1
    )
    hit = next(r for r in rows_text if r.results)
    assert all(res.text for res in hit.results)
    assert any(_ANCHOR_A in (res.text or "") for r in rows_text for res in r.results)
    assert all("text" not in res for r in rows_plain for res in case_to_json(r)["results"])
    assert all("text" in res for res in case_to_json(hit)["results"])


# ---------------------------------------------------------------------------
# 给模型的文本里的锚文本命中 / 逐次检索记录(B-206 批 0)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FixedRetriever:
    """Returns the same ranked chunks for every query, so a test controls the ranks."""

    results: tuple[RetrievedChunk, ...]

    async def search_detailed(self, **_kwargs: Any) -> KnowledgeSearchOutcome:
        return KnowledgeSearchOutcome(
            results=self.results, status="ok", candidates=len(self.results)
        )


def _ranked(anchor_rank: int, n: int = 8) -> tuple[RetrievedChunk, ...]:
    """``n`` results; only the one at ``anchor_rank`` is manual_a.md's anchor chunk."""
    return tuple(
        RetrievedChunk(
            content=f"益生菌{_ANCHOR_A}。" if rank == anchor_rank else f"无关段落第 {rank} 条。",
            filename="manual_a.md" if rank == anchor_rank else f"other{rank}.md",
            chunk_index=rank,
            chunk_id=uuid4(),
            fused_score=1.0 / (60 + rank),
        )
        for rank in range(1, n + 1)
    )


async def _context_rows(anchor_rank: int) -> list[CaseRow]:
    return await run_eval(
        dataset=_one_case_dataset(("manual_a.md", _ANCHOR_A)),
        retriever=cast(KnowledgeRetriever, _FixedRetriever(_ranked(anchor_rank))),
        tenant_id=uuid4(),
        cfg=EvalConfig(top_k=20),
        concurrency=1,
    )


@pytest.mark.asyncio
async def test_context_hit_counts_only_what_the_tool_hands_the_model() -> None:
    """The tool shows its default 5 results: an anchor ranked 7th is a chunk hit
    within 10 but never reached the model."""
    shown = (await _context_rows(3))[0].score
    hidden = (await _context_rows(7))[0].score
    assert (shown.chunk_hit_10, shown.context_hit) == (1.0, 1.0)
    assert (hidden.chunk_hit_10, hidden.context_hit) == (1.0, 0.0)


@pytest.mark.asyncio
async def test_context_hit_follows_the_tools_own_limit_and_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Computed on the tool's real output, not on chunk ids: when the tool's
    default count or its rendering changes (parent chunks later), so does it."""
    monkeypatch.setattr(knowledge, "_DEFAULT_LIMIT", 8)
    assert (await _context_rows(7))[0].score.context_hit == 1.0
    monkeypatch.setattr(
        knowledge,
        "_format_results",
        lambda results: "\n".join(f"[{r.filename}#{r.chunk_index}]" for r in results),
    )
    assert (await _context_rows(3))[0].score.context_hit == 0.0


@pytest.mark.asyncio
async def test_context_hit_leaves_the_existing_metrics_unchanged() -> None:
    """Every other field of the case score is what scoring the same results
    without the rendered text gives."""
    for anchor_rank in (3, 7):
        row = (await _context_rows(anchor_rank))[0]
        dataset = _one_case_dataset(("manual_a.md", _ANCHOR_A))
        expected = score_case(_ranked(anchor_rank), dataset.cases[0].expected, threshold=None)
        assert replace(row.score, context_hit=None) == expected


@pytest.mark.asyncio
async def test_context_hit_is_none_when_the_tool_wants_more_than_top_k() -> None:
    """With ``--top-k 3`` the first 3 are not what the tool (5 by default) would
    have shown, so no context hit is claimed either way."""
    rows = await run_eval(
        dataset=_one_case_dataset(("manual_a.md", _ANCHOR_A)),
        retriever=cast(KnowledgeRetriever, _FixedRetriever(_ranked(2)[:3])),
        tenant_id=uuid4(),
        cfg=EvalConfig(top_k=3),
        concurrency=1,
    )
    assert rows[0].score.chunk_hit_10 == 1.0
    assert rows[0].score.context_hit is None


class _ReversingReranker:
    """A scoring reranker that puts the fused order upside down."""

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        return list(reversed(range(len(documents))))[:top_k]

    async def rerank_scored(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[tuple[int, float | None]]:
        order = list(reversed(range(len(documents))))[:top_k]
        return [(i, 1.0 - n / 100) for n, i in enumerate(order)]


@pytest.mark.asyncio
@pytest.mark.parametrize("reranker", [None, _ReversingReranker()])
async def test_replayed_search_renders_what_the_tool_gets_searching_itself(
    reranker: Any,
) -> None:
    """The eval hands the tool the first results of its own top-20 search instead
    of searching again; at the default top_k that must be exactly what the tool
    would have shown searching by itself."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    with rls_tenant(tenant):
        await _seed_docs(
            store,
            tenant,
            {
                f"doc{i}.md": f"# 说明 {i}\n\n益生菌第 {i} 款,每日 {i} 袋,冷藏保存。\n"
                for i in range(9)
            },
        )
        retriever = KnowledgeRetriever(
            store=store, embedder=build_fake_embedder(), reranker=reranker
        )
        for query in ("益生菌每日几袋", "冷藏保存"):
            outcome = await retriever.search_detailed(
                tenant_id=tenant, base_names=["kb"], query=query, limit=20
            )
            assert len(outcome.results) > 5  # the tool's default count must actually cut
            replayed = KnowledgeSearchTool(
                retriever=cast(KnowledgeRetriever, _ReplayRetriever(retriever, outcome)),
                knowledge_base_refs=("kb",),
            )
            searching = KnowledgeSearchTool(retriever=retriever, knowledge_base_refs=("kb",))
            texts = [
                await tool_message_text(tool, {"query": query}, tenant_id=tenant, call_id="c")
                for tool in (replayed, searching)
            ]
            assert texts[0] == texts[1]


@pytest.mark.asyncio
async def test_search_log_records_every_returned_chunk_and_what_was_shown() -> None:
    row = (await _context_rows(7))[0]
    log = search_log_json(row)
    assert (log["case_id"], log["query"], log["limit"], log["status"]) == (
        "q1",
        "益生菌怎么补",
        20,
        "ok",
    )
    assert log["shown"] == 5
    assert log["shown_chars"] > 0
    ranked = _ranked(7)
    assert [r["rank"] for r in log["results"]] == list(range(1, 9))
    assert [r["shown"] for r in log["results"]] == [True] * 5 + [False] * 3
    # B-206 K-1: only what was handed to the tool takes up any of its output.
    assert [r["shown_chars"] > 0 for r in log["results"]] == [True] * 5 + [False] * 3
    assert [(r["filename"], r["chunk_index"]) for r in log["results"]] == [
        (c.filename, c.chunk_index) for c in ranked
    ]
    assert [r["fused_score"] for r in log["results"]] == [c.fused_score for c in ranked]
    assert all(isinstance(r["chunk_id"], str) for r in log["results"])
    assert all(r["rerank_score"] is None for r in log["results"])
    assert log["rerank_outcome"] is None  # no rerank asked for


@pytest.mark.asyncio
async def test_search_log_keeps_rerank_scores(
    corpus: tuple[Path, Path], dataset_path: Path
) -> None:
    dataset, plain, _store, (tenant, _report) = await _built(corpus, dataset_path)
    rows = await run_eval(
        dataset=dataset,
        retriever=replace(plain, reranker=_FlatScoreReranker()),
        tenant_id=tenant,
        cfg=EvalConfig(top_k=20, rerank=True),
        concurrency=1,
    )
    storage = search_log_json(next(r for r in rows if r.case_id == "q-storage"))
    assert storage["results"]
    assert {r["rerank_score"] for r in storage["results"]} == {0.3}


def test_main_writes_the_search_log_only_when_asked(
    tmp_path: Path,
    dataset_path: Path,
    corpus: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _manifest, root = corpus
    log = tmp_path / "searches.jsonl"
    argv = _cli(dataset_path, root, tmp_path / "plain.jsonl", tmp_path / "b.yaml")
    assert main(argv) == 0
    assert not log.exists()
    assert "ctx_hit" in capsys.readouterr().out  # reported next to chk_hit@10

    argv = _cli(dataset_path, root, tmp_path / "logged.jsonl", tmp_path / "b.yaml")
    assert main([*argv, "--search-log", str(log)]) == 0
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert sorted(line["case_id"] for line in lines) == sorted(c["id"] for c in _cases())
    summary = json.loads((tmp_path / "logged.jsonl").read_text(encoding="utf-8").splitlines()[-1])[
        "summary"
    ]
    assert summary["overall"]["context_hit"] is not None

    argv = _cli(dataset_path, root, tmp_path / "x.jsonl", tmp_path / "b.yaml")
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        main([*argv, "--search-log", str(log)])
    assert not (tmp_path / "x.jsonl").exists()


# ---------------------------------------------------------------------------
# B-206 K-1:结果是父段 —— 判对、锚文本存活、段长、交给模型的字数、回放都按父段
# ---------------------------------------------------------------------------

_SPAN_P1 = "".join(f"第{i}条:每日一袋,温水冲服。" for i in range(1, 21)) + "开封后请在三天内用完。"
_SPAN_P2 = "冷藏可以延长保质期。" + "".join(f"第{i}项:菌株编号BL-{i:02d}。" for i in range(1, 21))
#: Too long for one 400-token chunk, short enough for one 1,024-token parent:
#: two chunks of one parent, and this anchor runs across the two.
_SPAN_DOC = f"# 用法\n\n{_SPAN_P1}\n\n{_SPAN_P2}\n"
_SPAN_ANCHOR = "开封后请在三天内用完。冷藏可以延长保质期"
#: One short chunk: no parent row, it stands for itself.
_SOLO_DOC = "本品专利号 ZL2019-1234567,菌株编号 BL-99。规格 2g x 30 袋。\n"


def _judged(chunk: Any) -> str:
    return chunk_retrieval_text(chunk.heading_path, chunk.content)


async def _rows_of(
    store: InMemoryKnowledgeStore, tenant: UUID, kb_id: UUID, filename: str
) -> tuple[list[Any], list[Any]]:
    """``(chunks, parent rows)`` of one document."""
    docs = await store.list_documents(tenant_id=tenant, kb_id=kb_id)
    doc = next(d for d in docs if d.filename == filename)
    chunks, _ = await store.list_chunks(tenant_id=tenant, document_id=doc.id, limit=200)
    parents, _ = await store.list_chunks(
        tenant_id=tenant, document_id=doc.id, limit=200, chunk_types={ChunkType.PARENT}
    )
    return chunks, parents


async def _k1_seeded() -> tuple[InMemoryKnowledgeStore, UUID, UUID]:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    with rls_tenant(tenant):
        kb_id = await _seed_docs(store, tenant, {"usage.md": _SPAN_DOC, "solo.md": _SOLO_DOC})
    span_chunks, span_parents = await _rows_of(store, tenant, kb_id, "usage.md")
    solo_chunks, solo_parents = await _rows_of(store, tenant, kb_id, "solo.md")
    # The fixture's shape, which every test below relies on.
    assert (len(span_chunks), len(span_parents)) == (2, 1)
    assert not any(anchor_in(_SPAN_ANCHOR, _judged(c)) for c in span_chunks)
    assert anchor_in(_SPAN_ANCHOR, _judged(span_parents[0]))
    assert (len(solo_chunks), solo_parents, solo_chunks[0].parent_chunk_id) == (1, [], None)
    return store, tenant, kb_id


def _k1_case(case_id: str, query: str, document: str, anchor: str) -> KbCase:
    return KbCase(
        id=case_id,
        query=query,
        source="faq_xlsx",
        tags=(),
        expected=(KbExpected(document=document, anchor=anchor, page=None),),
        answer=None,
    )


def _k1_dataset(*cases: KbCase) -> KbDataset:
    return KbDataset(name="kb", corpus_manifest=None, corpus_tiers=("A",), cases=cases)


_K1_SPAN = _k1_case("q-span", "开封后多久要用完,要冷藏吗", "usage.md", _SPAN_ANCHOR)
_K1_SOLO = _k1_case("q-solo", "这个产品的专利号是多少", "solo.md", "专利号 ZL2019-1234567")
_K1_GHOST = _k1_case("q-ghost", "维生素 D 推荐量", "ghost.md", "这个文档不在语料里")


@pytest.mark.asyncio
async def test_anchor_checks_judge_what_a_search_returns_and_report_the_chunk_level_too() -> None:
    """A search returns parents (and chunks that stand alone): an anchor that
    runs across two chunks of one parent can be found, so it is alive; the
    chunk-level count is reported next to it so the two can be compared."""
    store, tenant, kb_id = await _k1_seeded()
    with rls_tenant(tenant):
        anchors = await check_anchors(
            store=store,
            tenant_id=tenant,
            kb_id=kb_id,
            dataset=_k1_dataset(_K1_SPAN, _K1_SOLO, _K1_GHOST),
        )
    assert anchors.checked == 3
    assert anchors.alive_cases == frozenset({"q-span", "q-solo"})
    assert [(d.case_id, d.reason) for d in anchors.dead] == [("q-ghost", "document_missing")]
    assert [(d.case_id, d.reason) for d in anchors.child_dead] == [
        ("q-span", "anchor_not_in_any_chunk"),
        ("q-ghost", "document_missing"),
    ]
    block = summarise([], corpus=None, anchors=anchors)["anchors"]
    assert (block["checked"], block["dead_cases"], block["child_level_dead_cases"]) == (3, 1, 2)
    assert [d["case_id"] for d in block["dead"]] == ["q-ghost"]
    assert [(d["case_id"], d["reason"]) for d in block["child_level_dead"]] == [
        ("q-span", "anchor_not_in_any_chunk"),
        ("q-ghost", "document_missing"),
    ]
    table = format_summary_table(summarise([], corpus=None, anchors=anchors))
    assert "anchors: checked=3 dead_cases=1 child_level_dead_cases=2" in table


@pytest.mark.asyncio
async def test_other_documents_holding_the_anchor_are_found_in_their_parents() -> None:
    """Which documents count for an expectation is judged on the same text as
    the hits: a copy whose parent holds the anchor across two chunks counts."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    with rls_tenant(tenant):
        kb_id = await _seed_docs(store, tenant, {"usage.md": _SPAN_DOC, "usage-2026.md": _SPAN_DOC})
        resolved, report = await resolve_anchor_documents(
            store=store,
            tenant_id=tenant,
            kb_id=kb_id,
            dataset=_k1_dataset(_K1_SPAN),
        )
    assert resolved.cases[0].expected[0].documents == frozenset({"usage.md", "usage-2026.md"})
    assert report.expanded == 1


@pytest.mark.asyncio
async def test_a_hit_is_judged_on_the_parent_passage_the_search_returns() -> None:
    """No chunk holds the anchor, the parent the search returns does: a chunk
    hit (MRR / chunk_hit are parent level), and the row records the parent and
    the chunks of it that ranked."""
    store, tenant, kb_id = await _k1_seeded()
    retriever = build_retriever(store=store, embedder=build_fake_embedder(), reranker=None)
    with rls_tenant(tenant):
        (row,) = await run_eval(
            dataset=_k1_dataset(_K1_SPAN),
            retriever=retriever,
            tenant_id=tenant,
            cfg=EvalConfig(top_k=20, with_text=True),
            concurrency=1,
        )
    (rank,) = row.score.chunk_hit_ranks
    hit = row.results[rank - 1]
    assert hit.filename == "usage.md"
    assert hit.parent_index == 0
    assert sorted(hit.hit_chunk_indexes) == [0, 1]  # both chunks ranked, one result
    assert row.score.chunk_hit_10 == 1.0
    assert row.score.mrr_10 == 1.0 / rank
    assert row.score.context_hit == 1.0
    _, (parent,) = await _rows_of(store, tenant, kb_id, "usage.md")
    assert hit.text == _judged(parent)
    line = case_to_json(row)["results"][rank - 1]
    assert (line["parent_index"], sorted(line["hit_chunk_indexes"])) == (0, [0, 1])
    assert line["shown_chars"] > 0
    assert line["shown_tokens"] > 0
    assert line["truncated"] is False


@pytest.mark.asyncio
async def test_corpus_reports_parent_sizes_next_to_chunk_sizes(tmp_path: Path) -> None:
    """Chunk sizes stay the chunks'; parent sizes are what a search can return —
    the parent rows plus the chunks that stand alone — with their counts."""
    root = tmp_path / "k1"
    root.mkdir()
    (root / "usage.md").write_text(_SPAN_DOC, encoding="utf-8")
    (root / "solo.md").write_text(_SOLO_DOC, encoding="utf-8")
    manifest = tmp_path / "manifest.csv"
    with manifest.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(MANIFEST_FIELDS))
        writer.writeheader()
        writer.writerows([_row("usage.md"), _row("solo.md")])
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    report = await _build((manifest, root), store=store, tenant=tenant, tiers=("A",))

    rows = [await _rows_of(store, tenant, report.base_id, name) for name in ("usage.md", "solo.md")]
    chunks = [c for doc_chunks, _ in rows for c in doc_chunks]
    parents = [p for _, doc_parents in rows for p in doc_parents]
    standalone = [c for c in chunks if c.parent_chunk_id is None]
    assert (report.chunk_total, report.parent_total, report.standalone_total) == (3, 1, 1)
    assert report.chunk_tokens == _size_spread([count_tokens(_judged(c)) for c in chunks])
    assert report.parent_tokens == _size_spread(
        [count_tokens(_judged(c)) for c in [*parents, *standalone]]
    )
    assert report.parent_tokens["max"] > report.chunk_tokens["max"]

    summary = summarise([], corpus=report)
    corpus_block = summary["corpus"]
    assert (corpus_block["parent_total"], corpus_block["standalone_total"]) == (1, 1)
    assert corpus_block["parent_tokens"] == report.parent_tokens
    spread = " ".join(f"{k}={report.parent_tokens[k]}" for k in ("p10", "p50", "p90", "max"))
    assert f"parent tokens: {spread} (parents=1 + standalone chunks=1)" in format_summary_table(
        summary
    )


def _parent_result(rank: int, *, body: int, hit: int | None, anchor: str = "") -> RetrievedChunk:
    """A result whose parent is ``body`` characters long and whose hit chunk
    ``hit`` (``None``: a chunk that stands alone, no parent row)."""
    return RetrievedChunk(
        content=anchor + "甲" * body,
        filename=f"doc{rank}.md",
        chunk_index=rank * 2,
        chunk_id=uuid4(),
        fused_score=1.0 / (60 + rank),
        parent_chunk_id=None if hit is None else uuid4(),
        parent_index=None if hit is None else rank,
        hit_chunk_indexes=(rank * 2, rank * 2 + 1),
        hit_content=None if hit is None else anchor + "乙" * hit,
    )


#: Handed to the tool (default 5): the 1st fits whole, the 2nd only as its hit
#: chunk (``[已截断]``), the 3rd whole, the 4th (no hit chunk to fall back to)
#: not at all, so the 5th is dropped too; the 6th and 7th are past the limit.
_BUDGET_RESULTS = (
    _parent_result(1, body=6000, hit=100, anchor=_ANCHOR_A),
    _parent_result(2, body=6000, hit=200),
    _parent_result(3, body=100, hit=50),
    _parent_result(4, body=6000, hit=None),
    _parent_result(5, body=10, hit=5),
    _parent_result(6, body=10, hit=5),
    _parent_result(7, body=10, hit=5),
)


@dataclass(frozen=True)
class _ByQueryRetriever:
    """Returns the chunks listed for each query (cut to the asked-for limit)."""

    results: dict[str, tuple[RetrievedChunk, ...]]

    async def search_detailed(
        self, *, query: str, limit: int, **_kwargs: Any
    ) -> KnowledgeSearchOutcome:
        found = self.results[query][:limit]
        return KnowledgeSearchOutcome(
            results=found, status="ok" if found else "no_hits", candidates=len(found)
        )


@pytest.mark.asyncio
async def test_search_log_records_parents_and_what_each_result_added_to_the_output() -> None:
    tenant = uuid4()
    retriever = _ByQueryRetriever({"益生菌怎么补": _BUDGET_RESULTS})
    (row,) = await run_eval(
        dataset=_one_case_dataset(("doc1.md", _ANCHOR_A)),
        retriever=cast(KnowledgeRetriever, retriever),
        tenant_id=tenant,
        cfg=EvalConfig(top_k=20),
        concurrency=1,
    )
    text = await tool_message_text(
        KnowledgeSearchTool(
            retriever=cast(KnowledgeRetriever, retriever), knowledge_base_refs=("kb",)
        ),
        {"query": "益生菌怎么补"},
        tenant_id=tenant,
        call_id="q1",
    )
    body = knowledge._format_results(_BUDGET_RESULTS[:5])
    assert unspotlight(text).startswith(body)  # the tool's rendering, inside the real output

    log = search_log_json(row)
    assert (log["shown_chars"], log["shown_tokens"]) == (len(text), count_tokens(text))
    results = log["results"]
    # Existing fields the offline replay tooling reads are still there.
    assert [(r["rank"], r["filename"], r["chunk_index"]) for r in results] == [
        (rank, c.filename, c.chunk_index) for rank, c in enumerate(_BUDGET_RESULTS, start=1)
    ]
    assert all("rerank_score" in r for r in results)
    assert [r["parent_index"] for r in results] == [1, 2, 3, None, 5, 6, 7]
    assert results[0]["hit_chunk_indexes"] == [2, 3]
    assert [r["truncated"] for r in results] == [False, True, False, False, False, False, False]
    chars = [r["shown_chars"] for r in results]
    assert chars[0] > 6000
    assert 200 < chars[1] < 6000
    assert chars[2] > 100
    assert chars[3:] == [0, 0, 0, 0]
    assert sum(chars) == len(body)  # each result's share, separators included
    assert results[0]["shown_tokens"] == count_tokens(body[: chars[0]])
    assert [r["shown_tokens"] > 0 for r in results] == [True] * 3 + [False] * 4
    assert [r["shown"] for r in results] == [True] * 5 + [False] * 2  # handed to the tool
    # The results file carries the same per-result fields.
    lines = case_to_json(row)["results"]
    assert [(r["parent_index"], r["shown_chars"], r["truncated"]) for r in lines] == [
        (r["parent_index"], r["shown_chars"], r["truncated"]) for r in results
    ]
    assert lines[0]["hit_chunk_indexes"] == [2, 3]


@pytest.mark.asyncio
async def test_summary_reports_the_text_handed_to_the_model_per_search() -> None:
    """Measured on the tool's real output (fence, notice and budget included):
    mean and p90 per search, in characters and tokens, and how many results
    were shown cut to their hit chunk."""
    tenant = uuid4()
    retriever = _ByQueryRetriever(
        {
            "长": _BUDGET_RESULTS,
            "短": (_parent_result(1, body=300, hit=20, anchor=_ANCHOR_A),),
            "无": (),
        }
    )
    cases = tuple(_k1_case(f"q-{q}", q, "doc1.md", _ANCHOR_A) for q in retriever.results)
    rows = await run_eval(
        dataset=_k1_dataset(*cases),
        retriever=cast(KnowledgeRetriever, retriever),
        tenant_id=tenant,
        cfg=EvalConfig(top_k=20),
        concurrency=1,
    )
    tool = KnowledgeSearchTool(
        retriever=cast(KnowledgeRetriever, retriever), knowledge_base_refs=("kb",)
    )
    texts = [
        await tool_message_text(tool, {"query": q}, tenant_id=tenant, call_id="c")
        for q in retriever.results
    ]
    chars = [len(t) for t in texts]
    tokens = [count_tokens(t) for t in texts]
    summary = summarise(rows, corpus=None)
    assert summary["tool_output"] == {
        "searches": 3,
        "chars": {"mean": round(sum(chars) / 3, 1), "p90": _size_spread(chars)["p90"]},
        "tokens": {"mean": round(sum(tokens) / 3, 1), "p90": _size_spread(tokens)["p90"]},
        "truncated_results": 1,
    }
    assert "tool output per search: chars mean=" in format_summary_table(summary)

    # --top-k under the tool's own count: not what the tool would have shown, not counted.
    short = await run_eval(
        dataset=_k1_dataset(*cases),
        retriever=cast(KnowledgeRetriever, retriever),
        tenant_id=tenant,
        cfg=EvalConfig(top_k=3),
        concurrency=1,
    )
    assert summarise(short, corpus=None)["tool_output"] == {
        "searches": 0,
        "chars": {"mean": None, "p90": None},
        "tokens": {"mean": None, "p90": None},
        "truncated_results": 0,
    }


def test_shown_parts_refuses_a_rendering_it_cannot_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each result's share is read off the tool's renderer by prefixes; a
    renderer whose first n-1 results are not a prefix of its first n would
    give made-up shares, so the eval stops instead."""
    monkeypatch.setattr(
        knowledge,
        "_format_results",
        lambda results: "\n".join(f"[{r.filename}#{r.chunk_index}]" for r in reversed(results)),
    )
    with pytest.raises(ValueError, match="in order"):
        shown_parts(_ranked(2, n=3))


def test_mean_and_p90_use_the_nearest_rank() -> None:
    assert _mean_p90(range(10, 0, -1)) == {"mean": 5.5, "p90": 9}
    assert _mean_p90([]) == {"mean": None, "p90": None}


@pytest.mark.asyncio
async def test_replay_reproduces_the_tools_own_search_over_parents() -> None:
    """The eval hands the tool the first parents of its own top-20 search: with
    parents collapsed from several ranked chunks that is still exactly what the
    tool would have shown searching by itself."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    with rls_tenant(tenant):
        await _seed_docs(
            store,
            tenant,
            {f"use{i}.md": f"# 用法 {i}\n\n{_SPAN_P1}{i}\n\n{_SPAN_P2}{i}\n" for i in range(8)},
        )
        retriever = KnowledgeRetriever(store=store, embedder=build_fake_embedder())
        query = "开封后多久要用完,菌株编号"
        outcome = await retriever.search_detailed(
            tenant_id=tenant, base_names=["kb"], query=query, limit=20
        )
        assert len(outcome.results) > 5  # the tool's default count must actually cut
        assert all(r.parent_index is not None for r in outcome.results)
        assert any(len(r.hit_chunk_indexes) > 1 for r in outcome.results[:5])  # collapsed
        replayed = KnowledgeSearchTool(
            retriever=cast(KnowledgeRetriever, _ReplayRetriever(retriever, outcome)),
            knowledge_base_refs=("kb",),
        )
        searching = KnowledgeSearchTool(retriever=retriever, knowledge_base_refs=("kb",))
        texts = [
            await tool_message_text(tool, {"query": query}, tenant_id=tenant, call_id="c")
            for tool in (replayed, searching)
        ]
    assert texts[0] == texts[1]


def test_two_runs_of_the_same_code_write_the_same_results(tmp_path: Path) -> None:
    """Offline replay must line up: the same code run twice on the same corpus
    gives the same case lines, summary and search log (ids aside)."""
    root = tmp_path / "k1"
    root.mkdir()
    (root / "usage.md").write_text(_SPAN_DOC, encoding="utf-8")
    (root / "solo.md").write_text(_SOLO_DOC, encoding="utf-8")
    manifest = tmp_path / "manifest.csv"
    with manifest.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(MANIFEST_FIELDS))
        writer.writeheader()
        writer.writerows([_row("usage.md"), _row("solo.md")])
    cases = [
        {
            "id": case.id,
            "query": case.query,
            "source": case.source,
            "tags": [],
            "expected": [
                {"document": e.document, "anchor": e.anchor, "page": None} for e in case.expected
            ],
            "answer": None,
        }
        for case in (_K1_SPAN, _K1_SOLO, _K1_GHOST)
    ]
    dataset = _write_dataset(tmp_path / "k1.yaml", str(manifest), cases)

    def run(label: str) -> tuple[list[str], dict[str, Any], list[dict[str, Any]]]:
        out = tmp_path / f"{label}.jsonl"
        log = tmp_path / f"{label}.log.jsonl"
        argv = _cli(dataset, root, out, tmp_path / "b.yaml", "--tiers", "A")
        assert main([*argv, "--search-log", str(log)]) == 0
        *case_lines, summary_line = out.read_text(encoding="utf-8").splitlines()
        searches = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        for search in searches:
            for result in search["results"]:
                result.pop("chunk_id")  # a fresh id every ingest
        return case_lines, baseline_section(json.loads(summary_line)["summary"]), searches

    first, second = run("one"), run("two")
    assert first == second
    assert any(r["parent_index"] is not None for s in first[2] for r in s["results"])


# ---------------------------------------------------------------------------
# B-206 K-21② 保留副本的对照组 / K-22 关键词路降级
# ---------------------------------------------------------------------------


def test_select_rows_keep_dups_real_keeps_every_copy_as_itself(corpus: tuple[Path, Path]) -> None:
    manifest, _root = corpus
    selection = select_rows(
        _manifest_rows(manifest), tiers=("A", "D-A"), limit_docs=0, keep_dups="real"
    )
    assert "docs/manual_a_copy.md" in [r["path"] for r in selection.rows]
    assert selection.skipped_dup == 0
    assert selection.copies == {}


def test_select_rows_keep_dups_same_title_names_each_copy_after_its_original() -> None:
    rows = [
        _row("x/a.md", dup_group="g1"),
        _row("y/b.md"),
        _row("y/a2.md", dup_group="g1"),
        _row("z/a3.md", dup_group="g1"),
    ]
    selection = select_rows(rows, tiers=("A",), limit_docs=0, keep_dups="same-title")
    assert [r["path"] for r in selection.rows] == ["x/a.md", "y/b.md", "y/a2.md", "z/a3.md"]
    assert selection.skipped_dup == 0
    assert selection.copies == {
        "y/a2.md": DupCopy(original="x/a.md", filename="dup1/a.md"),
        "z/a3.md": DupCopy(original="x/a.md", filename="dup2/a.md"),
    }
    with pytest.raises(ValueError, match="keep_dups"):
        select_rows(rows, tiers=("A",), limit_docs=0, keep_dups="all")


@pytest.mark.parametrize("mode", ["real", "same-title"])
def test_limit_docs_counts_no_copy_and_keeps_the_copies_of_what_it_kept(mode: str) -> None:
    """The arm with copies holds the same originals as the arm without, plus the
    copies of those: a copy takes no slot of ``--limit-docs``."""
    rows = [
        _row("x/a.md", dup_group="g1"),
        _row("x/a_copy.md", dup_group="g1"),
        _row("x/b.md"),
        _row("x/c.md", dup_group="g2"),
        _row("x/c_copy.md", dup_group="g2"),
    ]
    without = select_rows(rows, tiers=("A",), limit_docs=2)
    with_copies = select_rows(rows, tiers=("A",), limit_docs=2, keep_dups=mode)
    assert [r["path"] for r in without.rows] == ["x/a.md", "x/b.md"]
    assert [r["path"] for r in with_copies.rows] == ["x/a.md", "x/a_copy.md", "x/b.md"]
    assert with_copies.skipped_limit == 2  # c and its copy
    assert set(with_copies.copies) == ({"x/a_copy.md"} if mode == "same-title" else set())


def test_same_title_copies_leave_the_originals_shuffled_titles_as_they_were() -> None:
    """The shuffled control arm permutes the originals' titles exactly as the
    run without copies does; each copy then takes its original's."""
    rows = [_row(f"docs/doc_{i}.md", dup_group="g1" if i in (0, 3) else "") for i in range(6)]
    without = corpus_titles(select_rows(rows, tiers=("A",), limit_docs=0), "shuffled")
    with_copies = corpus_titles(
        select_rows(rows, tiers=("A",), limit_docs=0, keep_dups="same-title"), "shuffled"
    )
    assert {p: t for p, t in with_copies.items() if p != "docs/doc_3.md"} == without
    assert with_copies["docs/doc_3.md"] == with_copies["docs/doc_0.md"]


def test_main_refuses_shuffled_titles_with_real_copies(
    tmp_path: Path, dataset_path: Path, corpus: tuple[Path, Path]
) -> None:
    """Real copies are documents of their own: shuffling would give the originals
    another permutation than the run they are compared with."""
    _manifest, root = corpus
    argv = _cli(dataset_path, root, tmp_path / "unit.jsonl", tmp_path / "b.yaml")
    with pytest.raises(SystemExit, match="--doc-titles shuffled"):
        main([*argv, "--keep-dups", "real", "--doc-titles", "shuffled"])
    assert not (tmp_path / "unit.jsonl").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "copy_name", "copy_title"),
    [("real", "manual_a_copy.md", "manual a copy"), ("same-title", "dup1/manual_a.md", "manual a")],
)
async def test_build_corpus_ingests_a_kept_copy(
    corpus: tuple[Path, Path], mode: str, copy_name: str, copy_title: str
) -> None:
    """``same-title`` copies carry the original's title, so every chunk's embedded
    text is the original's (an embedding cache hit, no spend); ``real`` copies
    keep their own name and title."""
    manifest, root = corpus
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    report = await build_corpus(
        store=store,
        embedder=build_fake_embedder(),
        tenant_id=tenant,
        base_name="unit_kb",
        corpus_root=root,
        selection=select_rows(
            _manifest_rows(manifest), tiers=("A", "D-A"), limit_docs=0, keep_dups=mode
        ),
        concurrency=2,
    )
    assert report.docs_skipped_dup == 0
    docs = {
        d.filename: d for d in await store.list_documents(tenant_id=tenant, kb_id=report.base_id)
    }
    assert docs[copy_name].status is DocumentStatus.READY

    def embedded(filename: str) -> list[str]:
        return [c.retrieval_text for c in store._chunks if c.document_id == docs[filename].id]

    copy = [c for c in store._chunks if c.document_id == docs[copy_name].id]
    assert {c.doc_title for c in copy} == {copy_title}
    assert (embedded(copy_name) == embedded("manual_a.md")) is (mode == "same-title")


async def _built_with_copies(
    corpus: tuple[Path, Path], dataset_path: Path, *, dedup: bool
) -> list[CaseRow]:
    manifest, root = corpus
    dataset = load_dataset(dataset_path)
    store = InMemoryKnowledgeStore()
    embedder = build_fake_embedder()
    tenant = uuid4()
    await build_corpus(
        store=store,
        embedder=embedder,
        tenant_id=tenant,
        base_name=dataset.name,
        corpus_root=root,
        selection=select_rows(
            _manifest_rows(manifest), tiers=("A", "D-A"), limit_docs=0, keep_dups="real"
        ),
        concurrency=1,
    )
    retriever = build_retriever(store=store, embedder=embedder, reranker=None, dedup=dedup)
    return await run_eval(
        dataset=dataset, retriever=retriever, tenant_id=tenant, cfg=EvalConfig(), concurrency=1
    )


@pytest.mark.asyncio
async def test_a_kept_copy_takes_top_ten_places_only_with_dedup_off(
    corpus: tuple[Path, Path], dataset_path: Path
) -> None:
    off = summarise(await _built_with_copies(corpus, dataset_path, dedup=False), corpus=None)
    on = summarise(await _built_with_copies(corpus, dataset_path, dedup=True), corpus=None)
    assert off["overall"]["dup_share@10"] > 0
    assert on["overall"]["dup_share@10"] == 0.0


async def _keyword_timeout(**_kwargs: Any) -> list[Any]:
    raise KnowledgeRecallError("canceling statement due to statement timeout")


@pytest.mark.asyncio
async def test_run_eval_aborts_when_the_keyword_path_degrades(
    corpus: tuple[Path, Path], dataset_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retriever keeps the vector results when the keyword path fails
    (B-206 K-22); the eval must not score them as the hybrid result."""
    dataset, retriever, store, (tenant, _report) = await _built(corpus, dataset_path)
    monkeypatch.setattr(store, "keyword_search_scored", _keyword_timeout)
    with pytest.raises(KeywordDegradedError, match="vector search alone"):
        await run_eval(
            dataset=dataset,
            retriever=retriever,
            tenant_id=tenant,
            cfg=EvalConfig(top_k=20),
            concurrency=1,
        )


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ((), (True, "drop", 3)),
        (("--dedup", "off"), (False, "drop", 3)),
        (("--keep-dups", "real"), (True, "real", 4)),
        (("--keep-dups", "same-title", "--dedup", "off"), (False, "same-title", 4)),
    ],
)
def test_main_duplicate_arm_flags_reach_the_run_and_are_recorded(
    tmp_path: Path,
    dataset_path: Path,
    corpus: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    flags: tuple[str, ...],
    expected: tuple[bool, str, int],
) -> None:
    built: list[KnowledgeRetriever] = []

    def _spy(**kwargs: Any) -> KnowledgeRetriever:
        retriever = cast(KnowledgeRetriever, build_retriever(**kwargs))
        built.append(retriever)
        return retriever

    monkeypatch.setattr("kb_eval.build_retriever", _spy)
    _manifest, root = corpus
    out = tmp_path / "unit.jsonl"
    assert main(_cli(dataset_path, root, out, tmp_path / "b.yaml", *flags)) == 0
    (retriever,) = built
    summary = json.loads(out.read_text(encoding="utf-8").splitlines()[-1])["summary"]
    recorded = (
        retriever.dedup,
        summary["config"]["keep_dups"],
        summary["corpus"]["docs_ingested"],
    )
    assert recorded == expected
    assert summary["config"]["dedup"] is expected[0]
    assert "dup_share@10" in summary["overall"]


@pytest.mark.parametrize(
    "flags", [("--dedup", "off"), ("--keep-dups", "real"), ("--keep-dups", "same-title")]
)
def test_main_refuses_baseline_for_a_duplicate_arm(
    tmp_path: Path, dataset_path: Path, corpus: tuple[Path, Path], flags: tuple[str, ...]
) -> None:
    _manifest, root = corpus
    argv = _cli(
        dataset_path, root, tmp_path / "unit.jsonl", tmp_path / "b.yaml", "--update-baseline"
    )
    with pytest.raises(SystemExit, match="refusing to update baseline with a K-21 arm"):
        main([*argv, *flags])
    assert not (tmp_path / "b.yaml").exists()
