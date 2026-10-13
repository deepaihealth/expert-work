"""kb_eval.py 运行器:内存库 + 假嵌入 + ``tmp_path`` 里现造的小语料(B-175 L1-T3)。

CI 可跑的部分只有这里:真 SQL 库 / 真嵌入 / 真语料的那次跑在仓库外手工做。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from kb_corpus_inventory import MANIFEST_FIELDS  # type: ignore[import-not-found]
from kb_dataset import load_dataset  # type: ignore[import-not-found]
from kb_eval import (  # type: ignore[import-not-found]
    EvalConfig,
    build_corpus,
    build_fake_embedder,
    build_retriever,
    main,
    run_eval,
    select_rows,
    summarise,
)

from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.protocol import DocumentStatus, RetrievalMethod

_ANCHOR_A = "常温密封保存即可"
_ANCHOR_B = "专利号 ZL2019-1234567"
_DOC_A = (
    "# 产品说明书\n\n## 保存方式\n\n益生菌常温密封保存即可,不需要冷藏。开封后请尽快食用。\n\n"
    "## 成分\n\n每袋含活菌 100 亿 CFU,辅料为麦芽糊精。\n"
)
_DOC_B = "本品专利号 ZL2019-1234567,菌株编号 BL-99。规格 2g x 30 袋,零售价 198 元。\n"


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
    """返回 ``(manifest.csv, 语料根目录)``:2 份可入库 + 1 份重复 + 1 份坏文件 + 1 份 B 层。"""
    root = tmp_path / "corpus"
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "manual_a.md").write_text(_DOC_A, encoding="utf-8")
    (docs / "manual_b.txt").write_text(_DOC_B, encoding="utf-8")
    (docs / "manual_a_copy.md").write_text(_DOC_A, encoding="utf-8")
    (docs / "broken.xyz").write_bytes(b"\x00\x01 not a document")
    (docs / "scan.pdf").write_bytes(b"%PDF-1.4 fake")
    rows = [
        _row("docs/manual_a.md", dup_group="g1"),
        _row("docs/manual_b.txt"),
        _row("docs/manual_a_copy.md", dup_group="g1"),
        _row("docs/broken.xyz"),
        _row("docs/scan.pdf", tier="B"),
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
            "id": "q-none",
            "query": "这款产品含有多少咖啡因",
            "source": "no_answer",
            "tags": ["通用"],
            "expected": [],
            "answer": None,
        },
    ]


@pytest.fixture
def dataset_path(tmp_path: Path, corpus: tuple[Path, Path]) -> Path:
    manifest, _root = corpus
    path = tmp_path / "unit_kb.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "unit_kb",
                "corpus": {"manifest": str(manifest), "tiers": ["A", "D-A"]},
                "cases": _cases(),
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return path


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
    ]
    assert selection.skipped_dup == 1
    assert selection.skipped_tier == 1
    limited = select_rows(rows, tiers=("A",), limit_docs=1)
    assert [r["path"] for r in limited.rows] == ["docs/manual_a.md"]


# ---------------------------------------------------------------------------
# 建库
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_corpus_ingests_once_per_dup_group_and_records_failures(
    corpus: tuple[Path, Path],
) -> None:
    manifest, root = corpus
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    selection = select_rows(_manifest_rows(manifest), tiers=("A", "D-A"), limit_docs=0)
    report = await build_corpus(
        store=store,
        embedder=build_fake_embedder(),
        tenant_id=tenant,
        base_name="unit_kb",
        corpus_root=root,
        selection=selection,
        concurrency=2,
    )
    assert report.docs_ingested == 2
    assert report.docs_skipped_dup == 1
    assert report.docs_failed == 1
    assert report.chunk_total >= 2
    assert report.elapsed_s >= 0.0
    assert [(f.filename, f.error_type) for f in report.ingest_failures] == [
        ("broken.xyz", "DocumentParseError")
    ]
    docs = await store.list_documents(tenant_id=tenant, kb_id=report.base_id)
    status = {d.filename: d.status for d in docs}
    assert status == {
        "manual_a.md": DocumentStatus.READY,
        "manual_b.txt": DocumentStatus.READY,
        "broken.xyz": DocumentStatus.FAILED,
    }
    assert "manual_a_copy.md" not in status
    ready = [d for d in docs if d.status is DocumentStatus.READY]
    assert all(d.chunk_count >= 1 for d in ready)


def test_fake_embedder_dimension_matches_store_dim() -> None:
    import asyncio

    vectors = asyncio.run(build_fake_embedder().embed(["益生菌"], tenant_id=uuid4()))
    assert len(vectors[0]) == EMBEDDING_DIM


# ---------------------------------------------------------------------------
# 打分
# ---------------------------------------------------------------------------


async def _built(
    corpus: tuple[Path, Path], dataset_path: Path
) -> tuple[Any, Any, InMemoryKnowledgeStore, Any]:
    manifest, root = corpus
    dataset = load_dataset(dataset_path)
    store = InMemoryKnowledgeStore()
    embedder = build_fake_embedder()
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
    assert set(by_id) == {"q-storage", "q-patent", "q-ghost", "q-none"}

    storage = by_id["q-storage"]
    assert storage.score.doc_recall_20 == 1.0
    assert storage.score.chunk_recall_20 == 1.0
    # manual_a.md 按标题切成两段,只有「保存方式」那段含锚文本:段级命中必须**恰好一处**,
    # 且就是 manual_a.md 的那条结果(把锚文本判定去掉 → 两段都算命中 → 这里红)。
    assert len(storage.score.chunk_hit_ranks) == 1
    hit = storage.results[storage.score.chunk_hit_ranks[0] - 1]
    assert hit.filename == "manual_a.md"
    assert storage.score.doc_hit_ranks
    assert all(r.recall_source for r in storage.results)

    # manual_b.txt 只有一段且含锚文本:锚文本判定反了 → 段级 0 命中 → 这里红。
    patent = by_id["q-patent"]
    assert patent.score.doc_recall_20 == 1.0
    assert patent.score.chunk_recall_20 == 1.0
    assert len(patent.score.chunk_hit_ranks) == 1

    ghost = by_id["q-ghost"]
    assert ghost.score.doc_recall_20 == 0.0
    assert ghost.score.chunk_recall_20 == 0.0
    assert ghost.score.zero_hit is False  # 有返回,只是没命中


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
    assert none_row.score.refused is False  # 混合模式、无阈值:总会返回点东西
    assert none_row.score.doc_recall_10 is None

    strict = EvalConfig(method=RetrievalMethod.VECTOR, top_k=20, threshold=0.999, rerank=False)
    rows = await run_eval(
        dataset=dataset, retriever=retriever, tenant_id=tenant, cfg=strict, concurrency=1
    )
    by_id = {r.case_id: r for r in rows}
    assert by_id["q-none"].score.refused is True
    assert by_id["q-storage"].score.refused is True  # 阈值太高 → 有答案也被拒 = 误拒
    summary = summarise(rows, corpus=None)
    assert summary["overall"]["no_answer_refusal_rate"] == 1.0
    assert summary["overall"]["false_refusal_rate"] == 1.0


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
    assert summary["overall"]["n"] == 4
    assert summary["overall"]["n_answerable"] == 3
    assert set(summary["by_source"]) == {"faq_xlsx", "hard_exact", "manual_qa", "no_answer"}
    assert summary["by_source"]["faq_xlsx"]["doc_recall@20"] == 1.0
    assert summary["by_source"]["manual_qa"]["doc_recall@20"] == 0.0
    assert set(summary["by_tag"]) == {"通用", "保存", "专利"}
    assert summary["by_tag"]["通用"]["n"] == 3  # q-storage + q-ghost + q-none
    assert summary["by_tag"]["通用"]["doc_recall@20"] == 0.5  # 两条有答案的一中一不中
    assert summary["corpus"]["docs_ingested"] == 2
    assert summary["corpus"]["ingest_failures"] == [
        {"filename": "broken.xyz", "error_type": "DocumentParseError"}
    ]


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
    argv = _cli(dataset_path, root, out, baseline, "--update-baseline", "--concurrency", "2")
    assert main(argv) == 0

    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 5  # 4 case + 1 summary
    case_line = next(line for line in lines if line.get("id") == "q-storage")
    assert case_line["source"] == "faq_xlsx"
    assert case_line["tags"] == ["通用", "保存"]
    assert case_line["metrics"]["doc_recall@20"] == 1.0
    assert case_line["hits"]["doc_ranks"]
    assert {"filename", "chunk_index", "score", "recall_source"} <= set(case_line["results"][0])
    summary_line = lines[-1]["summary"]
    assert summary_line["overall"]["n"] == 4
    assert summary_line["corpus"]["docs_ingested"] == 2
    assert summary_line["corpus"]["docs_skipped_dup"] == 1
    assert summary_line["corpus"]["docs_failed"] == 1
    assert summary_line["corpus"]["embedder"] == "fake"
    assert summary_line["config"]["method"] == "hybrid"

    data = yaml.safe_load(baseline.read_text(encoding="utf-8"))
    assert data["kb"]["unit_kb"]["overall"]["n"] == 4
    fingerprint = data["meta"]["fingerprints"]["kb/unit_kb"]
    for key in (
        "dataset_sha256",
        "manifest_sha256",
        "embedder",
        "commit",
        "tiers",
        "limit_docs",
        "method",
        "top_k",
    ):
        assert key in fingerprint, key
    assert fingerprint["tiers"] == ["A", "D-A"]
    assert fingerprint["top_k"] == 20
    assert fingerprint["embedder"] == "fake"
    assert len(fingerprint["dataset_sha256"]) == 64
    assert len(fingerprint["manifest_sha256"]) == 64

    printed = capsys.readouterr().out
    assert "overall" in printed
    assert "doc_r@20" in printed


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
