#!/usr/bin/env python
"""知识库检索评测运行器(B-175 L1-T3)—— 走**真实入库链路**,不进 CI。

一次跑 = 建库 + 打分:

1. 读评测集(``kb_dataset.load_dataset``)与语料清单(``kb_corpus_inventory.py`` 产出的
   ``manifest.csv``);按 ``--tiers`` 选行,同一 ``dup_group`` 只入第一份;
2. 单库,库名 = 评测集 ``name``;每份文档 ``upsert_document(content=None)`` →
   ``control_plane.knowledge.ingestion.ingest_document_bytes``(解析 → 语义切块 → 嵌入 → 落段)→
   ``set_document_status(READY, chunk_count)``;解析失败记进 ``ingest_failures``,不中断;
3. 每条 case 调 ``KnowledgeRetriever.search(limit=top_k)``,按 ``kb_metrics`` 打分;
   总体 / 按 ``source`` / 按 ``tag`` 各出一份汇总;结果 JSONL 每 case 一行 + 末尾一行汇总;
   ``--update-baseline`` 把汇总与指纹并进基线 YAML(复用 ``longmem.runner.update_baseline``)。

用法(仓库根目录)::

    # 冒烟:内存库 + 假嵌入(关键词桶向量,维度跟 EXPERT_WORK_EMBEDDING_DIM)
    uv run --no-sync python tools/eval/kb_eval.py --dataset tools/eval/datasets/kb/<name>.yaml \\
        --corpus <清单>/manifest.csv --corpus-root <资料根目录> --store memory --embedder fake

    # 基线:pgvector 容器(testcontainers,需 DOCKER_HOST)+ 真嵌入(EXPERT_WORK_EVAL_EMBED_* 环境变量)
    uv run --no-sync python tools/eval/kb_eval.py --dataset tools/eval/datasets/kb/<name>.yaml \\
        --store sql --embedder real --tiers A,D-A --label baseline-$(git rev-parse --short HEAD) \\
        --update-baseline

凭据只从环境变量读(``EXPERT_WORK_EVAL_EMBED_API_KEY`` / ``_MODEL`` / ``_BASE_URL``;
``--rerank on`` 另要 ``EXPERT_WORK_EVAL_LLM_API_KEY`` / ``_MODEL`` / ``_BASE_URL``),没有命令行开关。
真嵌入包在 ``longmem.embedders.CachedEmbedder`` 里(``eval-out/kb/embed_cache.sqlite``),
第二次跑近零费用。

**测试覆盖的只有内存库 + 假嵌入 + ``tmp_path`` 小语料那条路**(``test_kb_eval.py``);
``--store sql`` 与 ``--embedder real`` 在仓库外手工跑,CI 不碰真语料、不建容器、不调嵌入 API。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

sys.path.insert(0, str(Path(__file__).parent))

from kb_dataset import (  # type: ignore[import-not-found]
    KbCase,
    KbDataset,
    load_dataset,
)
from kb_metrics import (  # type: ignore[import-not-found]
    CaseScore,
    aggregate,
    grouped,
    score_case,
)
from longmem.download import file_sha256  # type: ignore[import-not-found]
from longmem.embedders import (  # type: ignore[import-not-found]
    KeywordEmbedder,
    build_real_embedder,
)
from longmem.runner import update_baseline  # type: ignore[import-not-found]

from control_plane.knowledge.ingestion import ingest_document_bytes
from expert_work.persistence import InMemoryKnowledgeStore, KnowledgeStore
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.protocol import DocumentStatus, RetrievalMethod
from orchestrator.llm import Embedder, LLMCaller
from orchestrator.tools.knowledge import KnowledgeRetriever, Reranker, RetrievedChunk

_EVAL_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _EVAL_DIR.parents[1]
#: 评测是单租户的;固定 id 让两次跑的库 / 文档 id 可比。
EVAL_TENANT_ID = uuid5(NAMESPACE_URL, "expert-work-eval://kb/tenant")
#: 评测集没写 ``corpus.tiers`` 时的默认:有文字层、会进评测库的两层。
DEFAULT_TIERS: tuple[str, ...] = ("A", "D-A")
DEFAULT_TOP_K = 20
DEFAULT_CONCURRENCY = 4
#: 与根 ``conftest.py`` 的 ``postgres_container`` 同一镜像(GHCR 镜像站,CI 卫兵只放行这个源)。
PGVECTOR_IMAGE = "ghcr.io/deepaihealth/mirror/pgvector:pg16"
ALEMBIC_INI = _REPO_ROOT / "packages" / "expert-work-persistence" / "alembic.ini"
EVAL_OUT_DIR = Path("eval-out") / "kb"
EMBED_CACHE_DB = EVAL_OUT_DIR / "embed_cache.sqlite"
#: 失败文档 ``error`` 字段截断长度(与入库 runner 的 ``_ERROR_CAP`` 同口径)。
_ERROR_CAP = 500
_RERANK_ENV = ("EXPERT_WORK_EVAL_LLM_API_KEY", "EXPERT_WORK_EVAL_LLM_MODEL")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalConfig:
    """打分阶段的检索参数(建库参数走库默认:512/64 token)。"""

    method: RetrievalMethod = RetrievalMethod.HYBRID
    top_k: int = DEFAULT_TOP_K
    threshold: float | None = None
    rerank: bool = False


@dataclass(frozen=True)
class CorpusSelection:
    rows: tuple[Mapping[str, str], ...]
    skipped_dup: int
    skipped_tier: int


@dataclass(frozen=True)
class IngestFailure:
    filename: str
    error_type: str


@dataclass(frozen=True)
class CorpusReport:
    base_id: UUID
    docs_ingested: int
    docs_skipped_dup: int
    docs_failed: int
    chunk_total: int
    elapsed_s: float
    ingest_failures: tuple[IngestFailure, ...]


@dataclass(frozen=True)
class ResultRow:
    filename: str
    chunk_index: int
    score: float | None
    recall_source: str | None


@dataclass(frozen=True)
class CaseRow:
    case_id: str
    source: str
    tags: tuple[str, ...]
    results: tuple[ResultRow, ...]
    score: CaseScore


class FakeKbEmbedder(KeywordEmbedder):  # type: ignore[misc]
    """关键词桶向量(CJK 二元组 + ASCII 词),维度对齐库的向量列。

    ``longmem.embedders.KeywordEmbedder`` 是 256 维;SQL 库的 ``vector`` 列建表时钉在
    ``EXPERT_WORK_EMBEDDING_DIM``(默认 1024),维度不等写不进去,所以这里跟着库走。
    用它而不用 ``orchestrator.llm.FakeEmbedder``:后者是纯哈希,向量路等于随机,冒烟看不出排序。
    """

    DIM = EMBEDDING_DIM


def build_fake_embedder() -> FakeKbEmbedder:
    return FakeKbEmbedder()


# ---------------------------------------------------------------------------
# 选文档
# ---------------------------------------------------------------------------


def read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def select_rows(
    rows: Sequence[Mapping[str, str]], *, tiers: Sequence[str], limit_docs: int
) -> CorpusSelection:
    """按层过滤 → 同 ``dup_group`` 只留第一份 → ``limit_docs`` 截头(0 = 不限)。"""
    wanted = set(tiers)
    in_tier = [r for r in rows if r.get("tier") in wanted]
    seen_groups: set[str] = set()
    kept: list[Mapping[str, str]] = []
    skipped_dup = 0
    for row in in_tier:
        group = row.get("dup_group") or ""
        if group and group in seen_groups:
            skipped_dup += 1
            continue
        if group:
            seen_groups.add(group)
        kept.append(row)
    if limit_docs > 0:
        kept = kept[:limit_docs]
    return CorpusSelection(
        rows=tuple(kept), skipped_dup=skipped_dup, skipped_tier=len(rows) - len(in_tier)
    )


# ---------------------------------------------------------------------------
# 建库
# ---------------------------------------------------------------------------


async def build_corpus(
    *,
    store: KnowledgeStore,
    embedder: Embedder,
    tenant_id: UUID,
    base_name: str,
    corpus_root: Path,
    selection: CorpusSelection,
    concurrency: int,
) -> CorpusReport:
    """一次性建单库并把 ``selection`` 里的文档全部走真实入库链路。

    同名文件(不同目录、同 basename、又不是同一 ``dup_group``)只入第一份:库内
    ``(kb_id, filename)`` 唯一,再 upsert 会把前一份重置并删段;这种情况记作
    ``DuplicateFilename`` 失败,让人去看清单。
    """
    started = time.monotonic()
    base = await store.create_base(tenant_id=tenant_id, name=base_name, rerank_enabled=False)
    sem = asyncio.Semaphore(max(1, concurrency))
    claimed: set[str] = set()
    total = len(selection.rows)

    async def _one(index: int, row: Mapping[str, str]) -> tuple[int, IngestFailure | None]:
        filename = row["path"].replace("\\", "/").rsplit("/", 1)[-1]
        if filename in claimed:
            print(
                f"[ingest {index}/{total}] {filename}: duplicate filename, skipped",
                file=sys.stderr,
            )
            return 0, IngestFailure(filename=filename, error_type="DuplicateFilename")
        claimed.add(filename)
        async with sem:
            doc = await store.upsert_document(
                tenant_id=tenant_id, kb_id=base.id, filename=filename, content=None
            )
            try:
                raw = (corpus_root / row["path"]).read_bytes()
                count = await ingest_document_bytes(
                    store=store,
                    embedder=embedder,
                    tenant_id=tenant_id,
                    document_id=doc.id,
                    kb_id=base.id,
                    filename=filename,
                    raw=raw,
                    chunk_max_tokens=base.chunk_max_tokens,
                    chunk_overlap_tokens=base.chunk_overlap_tokens,
                )
            except Exception as exc:  # 解析 / 嵌入失败只记账,整轮继续
                error = f"{type(exc).__name__}: {exc}"[:_ERROR_CAP]
                await store.set_document_status(
                    tenant_id=tenant_id,
                    document_id=doc.id,
                    status=DocumentStatus.FAILED,
                    error=error,
                )
                print(f"[ingest {index}/{total}] {filename}: FAILED {error}", file=sys.stderr)
                return 0, IngestFailure(filename=filename, error_type=type(exc).__name__)
            await store.set_document_status(
                tenant_id=tenant_id,
                document_id=doc.id,
                status=DocumentStatus.READY,
                chunk_count=count,
            )
            print(f"[ingest {index}/{total}] {filename}: {count} chunks", file=sys.stderr)
            return count, None

    outcomes = await asyncio.gather(*(_one(i + 1, r) for i, r in enumerate(selection.rows)))
    failures = tuple(f for _count, f in outcomes if f is not None)
    return CorpusReport(
        base_id=base.id,
        docs_ingested=total - len(failures),
        docs_skipped_dup=selection.skipped_dup,
        docs_failed=len(failures),
        chunk_total=sum(count for count, _f in outcomes),
        elapsed_s=round(time.monotonic() - started, 3),
        ingest_failures=failures,
    )


# ---------------------------------------------------------------------------
# 打分
# ---------------------------------------------------------------------------


def build_retriever(
    *,
    store: KnowledgeStore,
    embedder: Embedder,
    reranker: Reranker | None,
    top_k: int = DEFAULT_TOP_K,
) -> KnowledgeRetriever:
    """两路各召回 ``recall_limit`` 再融合;``top_k`` 大于默认召回宽度时把宽度抬到 ``top_k``。"""
    recall_limit = max(KnowledgeRetriever.recall_limit, top_k)
    return KnowledgeRetriever(
        store=store, embedder=embedder, reranker=reranker, recall_limit=recall_limit
    )


async def run_eval(
    *,
    dataset: KbDataset,
    retriever: KnowledgeRetriever,
    tenant_id: UUID,
    cfg: EvalConfig,
    concurrency: int,
) -> list[CaseRow]:
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(case: KbCase) -> CaseRow:
        async with sem:
            results = await retriever.search(
                tenant_id=tenant_id,
                base_names=[dataset.name],
                query=case.query,
                limit=cfg.top_k,
                method=cfg.method,
                score_threshold=cfg.threshold,
                rerank=cfg.rerank,
            )
        return CaseRow(
            case_id=case.id,
            source=case.source,
            tags=case.tags,
            results=tuple(_result_row(r) for r in results),
            score=score_case(results, case.expected, threshold=cfg.threshold),
        )

    return list(await asyncio.gather(*(_one(c) for c in dataset.cases)))


def _result_row(result: RetrievedChunk) -> ResultRow:
    return ResultRow(
        filename=result.filename,
        chunk_index=result.chunk_index,
        score=result.score,
        recall_source=result.recall_source,
    )


# ---------------------------------------------------------------------------
# 汇总 / 输出
# ---------------------------------------------------------------------------


def summarise(
    rows: Sequence[CaseRow],
    *,
    corpus: CorpusReport | None,
    embedder: str = "unknown",
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    scores = [r.score for r in rows]
    corpus_block: dict[str, Any] | None = None
    if corpus is not None:
        corpus_block = asdict(corpus)
        corpus_block["base_id"] = str(corpus.base_id)
        corpus_block["ingest_failures"] = [asdict(f) for f in corpus.ingest_failures]
        corpus_block["embedder"] = embedder
    return {
        "overall": aggregate(scores),
        "by_source": grouped((r.source, r.score) for r in rows),
        "by_tag": grouped((tag, r.score) for r in rows for tag in r.tags),
        "corpus": corpus_block,
        "config": dict(config or {}),
    }


def case_to_json(row: CaseRow) -> dict[str, Any]:
    return {
        "id": row.case_id,
        "source": row.source,
        "tags": list(row.tags),
        "answerable": row.score.answerable,
        "refused": row.score.refused,
        "zero_hit": row.score.zero_hit,
        "results": [asdict(r) for r in row.results],
        "hits": {
            "doc_ranks": list(row.score.doc_hit_ranks),
            "chunk_ranks": list(row.score.chunk_hit_ranks),
        },
        "metrics": row.score.metrics(),
    }


def refuse_existing(path: Path) -> None:
    if path.exists():
        raise SystemExit(f"refusing to overwrite existing results file: {path}")


def write_jsonl(path: Path, rows: Sequence[CaseRow], summary: Mapping[str, Any]) -> None:
    refuse_existing(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(case_to_json(row), ensure_ascii=False) + "\n")
        fh.write(json.dumps({"summary": summary}, ensure_ascii=False) + "\n")


_TABLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("n", "n"),
    ("doc_r@10", "doc_recall@10"),
    ("doc_r@20", "doc_recall@20"),
    ("chunk_r@10", "chunk_recall@10"),
    ("chunk_r@20", "chunk_recall@20"),
    ("mrr@10", "mrr@10"),
    ("ndcg@10", "ndcg@10"),
    ("zero_hit", "zero_hit_rate"),
    ("no_ans_ref", "no_answer_refusal_rate"),
    ("false_ref", "false_refusal_rate"),
)


def format_summary_table(summary: Mapping[str, Any]) -> str:
    def cell(value: Any) -> str:
        if value is None:
            return "-"
        if isinstance(value, int):
            return str(value)
        return f"{float(value):.3f}"

    header = f"{'group':28}" + "".join(f"{label:>11}" for label, _ in _TABLE_COLUMNS)
    lines = [header]
    groups: list[tuple[str, Mapping[str, Any]]] = [("overall", summary["overall"])]
    groups.extend((f"source:{k}", v) for k, v in summary["by_source"].items())
    groups.extend((f"tag:{k}", v) for k, v in summary["by_tag"].items())
    for name, agg in groups:
        lines.append(
            f"{name[:28]:28}" + "".join(f"{cell(agg.get(key)):>11}" for _, key in _TABLE_COLUMNS)
        )
    corpus = summary.get("corpus")
    if corpus:
        lines.append(
            f"corpus: ingested={corpus['docs_ingested']} skipped_dup={corpus['docs_skipped_dup']} "
            f"failed={corpus['docs_failed']} chunks={corpus['chunk_total']} "
            f"elapsed={corpus['elapsed_s']}s embedder={corpus['embedder']}"
        )
    return "\n".join(lines)


def fingerprint(
    *,
    dataset_path: Path,
    manifest_path: Path,
    embedder: str,
    store: str,
    tiers: Sequence[str],
    limit_docs: int,
    cfg: EvalConfig,
) -> dict[str, Any]:
    return {
        "dataset_sha256": file_sha256(dataset_path),
        "manifest_sha256": file_sha256(manifest_path),
        "embedder": embedder,
        "store": store,
        "commit": _git_commit(),
        "tiers": list(tiers),
        "limit_docs": limit_docs,
        "method": cfg.method.value,
        "top_k": cfg.top_k,
        "threshold": cfg.threshold,
        "rerank": cfg.rerank,
    }


def _git_commit() -> str:
    git = shutil.which("git")
    if git is None:
        return "unknown"
    try:
        # 固定 argv、二进制经 shutil.which 解析 —— 只为指纹。
        return subprocess.run(  # noqa: S603
            [git, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=_EVAL_DIR,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return "unknown"


# ---------------------------------------------------------------------------
# 库 / 嵌入 / 重排 的构造
# ---------------------------------------------------------------------------


@asynccontextmanager
async def open_store(kind: str) -> AsyncIterator[KnowledgeStore]:
    """``memory`` → ``InMemoryKnowledgeStore``;``sql`` → testcontainers 起 pgvector 并迁移到 head。

    SQL 那条路与 ``packages/expert-work-persistence/tests/test_sql_knowledge_store.py`` 的 fixture
    同一套做法:同步 DSN 跑迁移,异步 DSN 建 engine / session factory。关键词路的语义
    (``plainto_tsquery`` 全 AND)只有这条路量得到。
    """
    if kind == "memory":
        yield InMemoryKnowledgeStore()
        return
    if not os.environ.get("DOCKER_HOST"):
        print(
            "warning: DOCKER_HOST is not set; testcontainers may fail to reach Docker",
            file=sys.stderr,
        )
    from alembic import command
    from alembic.config import Config
    from testcontainers.postgres import PostgresContainer

    from expert_work.persistence import (
        DatabaseConfig,
        SqlKnowledgeStore,
        create_async_engine_from_config,
        create_async_session_factory,
    )

    container = PostgresContainer(PGVECTOR_IMAGE)
    with container:
        url = str(container.get_connection_url())
        sync_dsn = url.replace("+psycopg2", "+psycopg").replace(
            "postgresql://", "postgresql+psycopg://", 1
        )
        async_dsn = url.replace("+psycopg2", "+asyncpg").replace(
            "postgresql://", "postgresql+asyncpg://", 1
        )
        alembic_cfg = Config(str(ALEMBIC_INI))
        alembic_cfg.set_main_option("sqlalchemy.url", sync_dsn)
        command.upgrade(alembic_cfg, "head")
        engine = create_async_engine_from_config(DatabaseConfig(dsn=async_dsn))
        try:
            yield SqlKnowledgeStore(create_async_session_factory(engine))
        finally:
            await engine.dispose()


def build_embedder(kind: str, *, concurrency: int) -> tuple[Embedder, str]:
    """返回 ``(embedder, 标签)``;``real`` 的凭据只从 ``EXPERT_WORK_EVAL_EMBED_*`` 读,缺了就退出。"""
    if kind == "fake":
        return build_fake_embedder(), "fake"
    backend = build_real_embedder(cache_db=EMBED_CACHE_DB, concurrency=concurrency)
    return cast(Embedder, backend), os.environ["EXPERT_WORK_EVAL_EMBED_MODEL"]


def build_reranker(enabled: bool) -> Reranker | None:
    """``--rerank on`` 要能从 ``EXPERT_WORK_EVAL_LLM_*`` 建出 LLM 重排器,否则明确报错退出。"""
    if not enabled:
        return None
    missing = [name for name in _RERANK_ENV if not os.environ.get(name)]
    if missing:
        raise SystemExit(
            f"--rerank on requires env {', '.join(missing)} "
            "(plus optional EXPERT_WORK_EVAL_LLM_BASE_URL)"
        )
    from longmem.openai_client import (  # type: ignore[import-not-found]
        DASHSCOPE_COMPAT_BASE_URL,
        OpenAICompatCaller,
    )

    from orchestrator.tools.knowledge import LLMReranker

    caller = OpenAICompatCaller(
        api_key=os.environ["EXPERT_WORK_EVAL_LLM_API_KEY"],
        model=os.environ["EXPERT_WORK_EVAL_LLM_MODEL"],
        base_url=os.environ.get("EXPERT_WORK_EVAL_LLM_BASE_URL", DASHSCOPE_COMPAT_BASE_URL),
    )
    # OpenAICompatCaller 只实现 (messages=, tools=) 两个关键字,没有 LLMCaller 协议里的
    # output_schema / on_delta;LLMReranker 也只用前两个,与 run_longmem.py 同样按形状接入。
    return LLMReranker(llm_caller=cast(LLMCaller, caller))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dataset", type=Path, required=True, help="评测集 YAML")
    parser.add_argument("--corpus", type=Path, help="manifest.csv;覆盖评测集里的 corpus.manifest")
    parser.add_argument(
        "--corpus-root",
        type=Path,
        help="清单里 path 的根目录(默认 = manifest.csv 所在目录)",
    )
    parser.add_argument("--tiers", help="逗号分隔;默认评测集 corpus.tiers,再默认 A,D-A")
    parser.add_argument("--limit-docs", type=int, default=0, help="只入前 N 份文档;0 = 全部")
    parser.add_argument("--store", default="sql", choices=("memory", "sql"))
    parser.add_argument("--embedder", default="fake", choices=("fake", "real"))
    parser.add_argument(
        "--method", default="hybrid", choices=tuple(m.value for m in RetrievalMethod)
    )
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--threshold", type=float, default=None, help="向量路相似度下限")
    parser.add_argument("--rerank", default="off", choices=("off", "on"))
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--label", default="run", help="结果文件名;--out 未给时用它")
    parser.add_argument("--out", type=Path, help=f"结果 JSONL(默认 {EVAL_OUT_DIR}/<label>.jsonl)")
    parser.add_argument(
        "--baseline-out",
        type=Path,
        help="基线 YAML(默认 tools/eval/baselines/kb_<dataset name>_baseline.yaml)",
    )
    parser.add_argument("--update-baseline", action="store_true")
    return parser


def _resolve_tiers(raw: str | None, dataset: KbDataset) -> tuple[str, ...]:
    if raw:
        return tuple(t.strip() for t in raw.split(",") if t.strip())
    return dataset.corpus_tiers or DEFAULT_TIERS


def _resolve_manifest(args: argparse.Namespace, dataset: KbDataset) -> Path:
    if args.corpus is not None:
        return cast(Path, args.corpus)
    if dataset.corpus_manifest is None:
        raise SystemExit("dataset has no corpus.manifest; pass --corpus <manifest.csv>")
    return Path(dataset.corpus_manifest).expanduser()


async def _run(
    *,
    args: argparse.Namespace,
    dataset: KbDataset,
    manifest: Path,
    corpus_root: Path,
    tiers: Sequence[str],
    cfg: EvalConfig,
    embedder: Embedder,
    embedder_tag: str,
    reranker: Reranker | None,
) -> tuple[list[CaseRow], dict[str, Any]]:
    selection = select_rows(read_manifest(manifest), tiers=tiers, limit_docs=args.limit_docs)
    print(
        f"corpus: {len(selection.rows)} docs selected (tiers={','.join(tiers)}, "
        f"skipped_dup={selection.skipped_dup}, skipped_tier={selection.skipped_tier})",
        file=sys.stderr,
    )
    async with open_store(args.store) as store:
        report = await build_corpus(
            store=store,
            embedder=embedder,
            tenant_id=EVAL_TENANT_ID,
            base_name=dataset.name,
            corpus_root=corpus_root,
            selection=selection,
            concurrency=args.concurrency,
        )
        if report.docs_ingested == 0:
            raise SystemExit(
                f"no document ingested ({report.docs_failed} failed) — check --corpus-root "
                f"({corpus_root}) against the manifest's path column"
            )
        retriever = build_retriever(
            store=store, embedder=embedder, reranker=reranker, top_k=cfg.top_k
        )
        rows = await run_eval(
            dataset=dataset,
            retriever=retriever,
            tenant_id=EVAL_TENANT_ID,
            cfg=cfg,
            concurrency=args.concurrency,
        )
    config = {
        "store": args.store,
        "tiers": list(tiers),
        "limit_docs": args.limit_docs,
        "method": cfg.method.value,
        "top_k": cfg.top_k,
        "threshold": cfg.threshold,
        "rerank": cfg.rerank,
        "label": args.label,
    }
    return rows, summarise(rows, corpus=report, embedder=embedder_tag, config=config)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    dataset = load_dataset(args.dataset)
    manifest = _resolve_manifest(args, dataset)
    if not manifest.is_file():
        raise SystemExit(f"corpus manifest not found: {manifest}")
    corpus_root = cast(Path, args.corpus_root) if args.corpus_root is not None else manifest.parent
    tiers = _resolve_tiers(args.tiers, dataset)
    out = cast(Path, args.out) if args.out is not None else EVAL_OUT_DIR / f"{args.label}.jsonl"
    refuse_existing(out)
    baseline_out = (
        cast(Path, args.baseline_out)
        if args.baseline_out is not None
        else _EVAL_DIR / "baselines" / f"kb_{dataset.name}_baseline.yaml"
    )
    cfg = EvalConfig(
        method=RetrievalMethod(args.method),
        top_k=args.top_k,
        threshold=args.threshold,
        rerank=args.rerank == "on",
    )
    embedder, embedder_tag = build_embedder(args.embedder, concurrency=args.concurrency)
    reranker = build_reranker(cfg.rerank)

    rows, summary = asyncio.run(
        _run(
            args=args,
            dataset=dataset,
            manifest=manifest,
            corpus_root=corpus_root,
            tiers=tiers,
            cfg=cfg,
            embedder=embedder,
            embedder_tag=embedder_tag,
            reranker=reranker,
        )
    )
    write_jsonl(out, rows, summary)
    print(format_summary_table(summary))
    print(f"results: {out}")
    if args.update_baseline:
        update_baseline(
            baseline_out,
            tier="kb",
            benchmark=dataset.name,
            section=summary,
            fingerprint=fingerprint(
                dataset_path=args.dataset,
                manifest_path=manifest,
                embedder=embedder_tag,
                store=args.store,
                tiers=tiers,
                limit_docs=args.limit_docs,
                cfg=cfg,
            ),
        )
        print(f"baseline updated: {baseline_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
