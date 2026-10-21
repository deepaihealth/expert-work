#!/usr/bin/env python
"""知识库检索评测运行器(B-175 L1-T3)—— 走**真实入库链路**,不进 CI。

一次跑 = 建库 + 打分:

1. 读评测集(``kb_dataset.load_dataset``)与语料清单(``kb_corpus_inventory.py`` 产出的
   ``manifest.csv``);按 ``--tiers`` 选行,同一 ``dup_group`` 只入第一份,``--limit-docs``
   在去重之后截头;
2. 单库,库名 = 评测集 ``name``;解析在**进程池**里并发跑(PyMuPDF 非线程安全;每份有
   ``--parse-timeout``),落库按清单顺序逐份串行:``upsert_document(content=None)`` →
   ``control_plane.knowledge.ingestion.ingest_markdown``(语义切块 → 嵌入 → 落段,与上传后的
   入库链路同一段代码,段上带文档名,``--doc-titles`` 可换成不带 / 在文档间打乱的对照组)→
   ``set_document_status(READY, chunk_count)``;失败按阶段
   ``parse|embed|store|other`` 记进 ``ingest_failures``,文档标 FAILED,不中断;同名不同内容的
   文件后到的一份以「上级目录/文件名」入库,记进 ``name_collisions``;0 字节文件直接跳过;
   **可复现**:SQL 库默认固定读行顺序(关掉索引扫描、并行扫描、同步顺序扫描与
   autovacuum,向量路走精确检索),加上串行落库,同一份代码两次跑出逐条相同的结果;
   ``--ann`` 改走生产同款 HNSW(量近似检索的召回损失时用,不可复现);
3. 入库后核「锚文本存活」:每条 expected 的 anchor 至少出现在其文档的某一段里,不存活的 case
   单列(是数据集 / 切块的问题,不是检索的问题);
4. 每条 case 调 ``KnowledgeRetriever.search(limit=top_k)``,按 ``kb_metrics`` 打分(``match``
   取 case 自己的 any|all);同一次检索的前几名再交给生产 ``knowledge_search`` 工具按默认条数渲染
   (工具节点同一段代码),锚文本是否落在这段**给模型的文本**里记 ``context_hit``(B-206
   「父段级段命中」,与 ``chk_hit@10`` 并列);总体 / 按 ``source`` / 按 ``tag`` 各出一份汇总;
   结果 JSONL 每 case 一行(含 ``best_score`` 便于离线扫阈值)+ 末尾一行汇总;
   ``--search-log`` 另写每次检索的逐段记录(段号、名次、各路分数、是否交给了模型,默认不写);
   ``--update-baseline`` 把汇总与指纹
   并进基线 YAML(复用 ``longmem.runner.update_baseline``);**有任何失败 / 同名冲突时拒绝
   更新基线**(退出码非 0)。

用法(仓库根目录)::

    # 冒烟:内存库 + 假嵌入(关键词桶向量,维度跟 EXPERT_WORK_EMBEDDING_DIM)
    uv run --no-sync python tools/eval/kb_eval.py --dataset tools/eval/datasets/kb/<name>.yaml \\
        --corpus <清单>/manifest.csv --corpus-root <资料根目录> --store memory --embedder fake

    # 基线:pgvector 容器(testcontainers,需 DOCKER_HOST)+ 真嵌入(EXPERT_WORK_EVAL_EMBED_* 环境变量)
    uv run --no-sync python tools/eval/kb_eval.py --dataset tools/eval/datasets/kb/<name>.yaml \\
        --store sql --embedder real --tiers A,D-A --label baseline-$(git rev-parse --short HEAD) \\
        --update-baseline

B-206 K-14 重排对照组:``--rerank-view dual|single|single_clean`` / ``--rerank-pool N`` /
``--rerank-instruct`` 直接设到生产检索器上(不给 = 生产默认 single + 100、不带任务说明;
K-14 之前的生产 = ``--rerank-view dual --rerank-pool 50``,请求与那时逐字相同,重排缓存照样命中)。

凭据只从环境变量读(``EXPERT_WORK_EVAL_EMBED_API_KEY`` / ``_MODEL`` / ``_BASE_URL``;
``--rerank on`` 另要 ``EXPERT_WORK_EVAL_LLM_API_KEY`` / ``_MODEL`` / ``_BASE_URL``),没有命令行开关。
真嵌入包在 ``longmem.embedders.CachedEmbedder`` 里(``eval-out/kb/embed_cache.sqlite``),
第二次跑近零费用;这里不截断输入(上限设到 1M 字符,超长文本让嵌入 API 自己报错),
汇总里报送入嵌入器的文本数 / 超过 8000 字符的条数 / token 数,费用分两个数:实花
(``cost_cny_actual``,只算缓存没命中、真送到接口的)与名义(``cost_cny_nominal``,假设
一条都没缓存;假嵌入都报 0)。真嵌入与生产同一套 B-206 K-5 查询侧标记(按模型目录,
``--query-marker off`` 是不带标记的对照臂);标记进缓存键,两臂各用各的查询向量。

**测试覆盖的只有内存库 + 假嵌入 + ``tmp_path`` 小语料那条路**(``test_kb_eval.py``);
``--store sql`` 与 ``--embedder real`` 在仓库外手工跑,CI 不碰真语料、不建容器、不调嵌入 API。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import random
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from concurrent.futures import Executor, ProcessPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

sys.path.insert(0, str(Path(__file__).parent))

from kb_dataset import (  # type: ignore[import-not-found]
    KbCase,
    KbDataset,
    KbExpected,
    load_dataset,
)
from kb_metrics import (  # type: ignore[import-not-found]
    CaseScore,
    aggregate,
    anchor_in,
    anchor_key,
    grouped,
    score_case,
)
from kb_rerank import (  # type: ignore[import-not-found]
    CachedRerankClient,
    build_dashscope_reranker,
)
from longmem.download import file_sha256  # type: ignore[import-not-found]
from longmem.embedders import (  # type: ignore[import-not-found]
    KeywordEmbedder,
    build_real_embedder,
)
from longmem.runner import update_baseline  # type: ignore[import-not-found]

from control_plane.knowledge.chunking import count_tokens
from control_plane.knowledge.ingestion import ingest_markdown
from control_plane.knowledge.parsing import is_plain_text_file, parse_document
from expert_work.persistence import InMemoryKnowledgeStore, KnowledgeStore
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.persistence.rls import current_tenant_id_var
from expert_work.protocol import (
    MODEL_CATALOG,
    DocumentStatus,
    RetrievalMethod,
    chunk_retrieval_text,
    knowledge_document_title,
)
from expert_work.protocol.model_catalog import EmbeddingCaps
from orchestrator.graph_builder.builder import _invoke_tool
from orchestrator.llm import Embedder, LLMCaller
from orchestrator.llm.embedder import (
    NO_MARKER,
    QWEN_EMBEDDING_BASE_URL,
    EmbeddingMarker,
    embedding_marker,
)
from orchestrator.tools.knowledge import (
    DEFAULT_RERANK_POOL,
    DEFAULT_RERANK_VIEW,
    RERANK_VIEWS,
    KnowledgeRetriever,
    KnowledgeSearchOutcome,
    KnowledgeSearchTool,
    Reranker,
    RerankView,
    RetrievedChunk,
    default_recall_depth,
)
from orchestrator.tools.registry import Tool, ToolContext

_EVAL_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _EVAL_DIR.parents[1]
#: 评测是单租户的;固定 id 让两次跑的库 / 文档 id 可比。
EVAL_TENANT_ID = uuid5(NAMESPACE_URL, "expert-work-eval://kb/tenant")
#: 评测集没写 ``corpus.tiers`` 时的默认:有文字层、会进评测库的两层。
DEFAULT_TIERS: tuple[str, ...] = ("A", "D-A")
DEFAULT_TOP_K = 20
DEFAULT_CONCURRENCY = 4
#: 单份文档解析超时(秒)。超时记 parse 失败;进程池里那个 worker 会继续占着直到解析完。
DEFAULT_PARSE_TIMEOUT_S = 120.0
#: 解析超过时限后接着等同一个解析,总共最多等这么多倍时限才记失败。时限是防卡死的,不是判慢:
#: 10-15 两条评测并行跑时一份大 PDF 超过 120 秒被跳过,语料静默少了 4,130 段,结果照常输出。
SLOW_PARSE_GRACE = 4
#: 段上的文档名:``real`` = 生产入库同款(文件名清洗后);``none`` = 不带(PR2b 之前的入库);
#: ``shuffled`` = 对照组,同一批文档名在文档间打乱,量「真名字」比「有个名字」多带来多少。
DocTitles = Literal["real", "shuffled", "none"]
DOC_TITLE_CHOICES: tuple[str, ...] = ("real", "shuffled", "none")
#: ``shuffled`` 的固定种子:对照组每次跑同一种打乱,两次结果可比。
DOC_TITLE_SHUFFLE_SEED = 20261016
#: 与根 ``conftest.py`` 的 ``postgres_container`` 同一镜像(GHCR 镜像站,CI 卫兵只放行这个源)。
PGVECTOR_IMAGE = "ghcr.io/deepaihealth/mirror/pgvector:pg16"
ALEMBIC_INI = _REPO_ROOT / "packages" / "expert-work-persistence" / "alembic.ini"
EVAL_OUT_DIR = Path("eval-out") / "kb"
EMBED_CACHE_DB = EVAL_OUT_DIR / "embed_cache.sqlite"
RERANK_CACHE_DB = EVAL_OUT_DIR / "rerank_cache.sqlite"
#: 不让 ``CachedEmbedder`` 截断输入:超长文本由真嵌入 API 报错,这边只计数。
EMBED_MAX_TEXT_CHARS = 1_000_000
#: 「超长」计数线 = ``CachedEmbedder`` 原来的截断线(DashScope 单条 8192 token)。
EMBED_OVERLONG_CHARS = 8000
#: 真嵌入估算费用的单价(元 / 千 token);默认按 DashScope text-embedding-v4 价目,
#: 换模型用环境变量覆盖。
EMBED_PRICE_ENV = "EXPERT_WORK_EVAL_EMBED_PRICE_CNY_PER_1K"
DEFAULT_EMBED_PRICE_CNY_PER_1K = 0.0005
#: 失败文档 ``error`` 字段截断长度(与入库 runner 的 ``_ERROR_CAP`` 同口径)。
_ERROR_CAP = 500
_RERANK_ENV = ("EXPERT_WORK_EVAL_LLM_API_KEY", "EXPERT_WORK_EVAL_LLM_MODEL")
#: 失败阶段。``store`` 按异常所属模块判(数据库驱动 / 持久化层),``other`` 是剩下的(读文件等)。
FAILURE_STAGES: tuple[str, ...] = ("parse", "embed", "store", "other")
_STORE_EXC_MODULES = ("sqlalchemy", "asyncpg", "psycopg", "expert_work.persistence")
#: ``list_chunks`` 翻页大小(锚文本存活检查)。
_CHUNK_PAGE = 200
#: 工具结果的防注入围栏 nonce(生产默认开 spotlight,每次构建随机取);固定下来渲染结果才可复现。
TOOL_SPOTLIGHT_NONCE = "kbeval0nonce"


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
    #: 重排分数下限(B-175 波 2 PR3,``--rerank-threshold``);设了以后「拒答」= 检索返回空
    rerank_threshold: float | None = None
    #: off | llm(聊天模型排序)| dashscope(生产同款 ``qwen3-vl-rerank``,带缓存)
    rerank_mode: str = "off"
    #: 结果里带上每段的 retrieval 文本(``--with-text``,汇集标注要用;默认不带,结果文件形状不变)
    with_text: bool = False
    #: B-206 K-14 对照组(``--rerank-view`` / ``--rerank-pool`` / ``--rerank-instruct``):
    #: 直接设到生产检索器上,默认即生产默认。
    rerank_view: RerankView = DEFAULT_RERANK_VIEW
    rerank_pool: int = DEFAULT_RERANK_POOL
    rerank_instruction: bool = False


@dataclass(frozen=True)
class CorpusSelection:
    rows: tuple[Mapping[str, str], ...]
    skipped_dup: int
    skipped_tier: int
    skipped_limit: int
    skipped_empty: int = 0


@dataclass(frozen=True)
class IngestFailure:
    filename: str
    path: str
    stage: str  # FAILURE_STAGES 之一
    error_type: str
    error: str


@dataclass(frozen=True)
class NameCollision:
    """A later file with the same basename as an earlier one (different
    directory, not the same ``dup_group``). It is still ingested — under
    ``ingested_as`` (``<parent dir>/<basename>``), since ``(kb_id, filename)``
    is unique per base — so the dataset's basename-keyed expectations keep
    pointing at the first one while nothing is dropped from the corpus."""

    filename: str
    path: str
    kept_path: str
    ingested_as: str


@dataclass(frozen=True)
class EmbedStats:
    texts: int
    chars: int
    overlong: int  # 超过 EMBED_OVERLONG_CHARS 的条数
    tokens: int  # 只有真嵌入计;含缓存命中(按送入嵌入器的文本算)
    #: tokens 乘单价:缓存一条都没命中时要花的钱(上限,不是实花)
    cost_cny_nominal: float
    fetched: int  # 真送到嵌入接口的条数(缓存没命中的;前面没有缓存时 = texts)
    fetched_tokens: int
    #: fetched_tokens 乘单价:这次跑实际花的钱
    cost_cny_actual: float


@dataclass(frozen=True)
class CorpusReport:
    base_id: UUID
    docs_ingested: int
    docs_skipped_dup: int
    docs_skipped_limit: int
    docs_skipped_empty: int
    docs_name_collision: int
    docs_failed: int
    failed_by_stage: dict[str, int]
    chunk_total: int
    elapsed_s: float
    ingest_failures: tuple[IngestFailure, ...]
    name_collisions: tuple[NameCollision, ...]
    embed: EmbedStats | None


@dataclass(frozen=True)
class DeadAnchor:
    case_id: str
    document: str
    reason: str  # document_missing | anchor_not_in_any_chunk


@dataclass(frozen=True)
class AnchorReport:
    checked: int  # 核过的 expected 条数
    alive_cases: frozenset[str]  # 全部 expected 都存活的 case
    dead: tuple[DeadAnchor, ...]

    @property
    def dead_cases(self) -> frozenset[str]:
        return frozenset(d.case_id for d in self.dead)


@dataclass(frozen=True)
class TooCommonAnchor:
    case_id: str
    document: str
    n_docs: int  # 含该锚文本的已入库文档数


@dataclass(frozen=True)
class AnchorDocsReport:
    """锚文本判对的扩展记账:多少条期望 / 多少道题多了可接受文档,哪些锚文本太常见没扩展。"""

    expanded: int
    expanded_cases: int
    too_common: tuple[TooCommonAnchor, ...]


@dataclass(frozen=True)
class ResultRow:
    filename: str
    chunk_index: int
    score: float | None
    recall_source: str | None
    text: str | None = None
    #: 重排分数(B-175 波 2 PR3);没开重排 / 重排没给分时 None,结果文件里不出现这个键
    rerank_score: float | None = None


@dataclass(frozen=True)
class SearchLog:
    """一次检索返回了什么(``--search-log``):逐段的段号、各路分数,以及前 ``shown`` 名交给了
    ``knowledge_search``(工具默认条数),诊断时不用再重跑。"""

    query: str
    limit: int
    status: str
    candidates: int
    rerank_degraded: bool
    results: tuple[RetrievedChunk, ...]
    shown: int
    shown_chars: int  # 工具渲染给模型的文本长度


@dataclass(frozen=True)
class CaseRow:
    case_id: str
    source: str
    tags: tuple[str, ...]
    results: tuple[ResultRow, ...]
    score: CaseScore
    match: str = "any"
    anchors_alive: bool | None = None  # 无答案 case 为 None
    search: SearchLog | None = None


class EmbeddingDimensionError(RuntimeError):
    """嵌入器返回的维度与库的向量列不一致 —— 配置错,整轮退出。"""


class EmbedStageError(RuntimeError):
    """嵌入器抛出的任何异常都包成它,让建库那边把失败归到 ``embed`` 阶段。"""


class RerankDegradedError(RuntimeError):
    """开了重排却有一题退回了融合序(B-175 波 2 PR3)—— 整轮退出。

    检索器把重排出错 / 返回空降级成融合序、不再抛错(生产要这样),评测不能:离线复跑缓存
    没命中(:class:`kb_rerank.RerankCacheMissError`)或线上 429 / 5xx 会让那一题悄悄按融合序
    计分,汇总混进没重排的题,结果文件里也看不出来。
    """


class EmbeddingDegradedError(RuntimeError):
    """有一题的查询嵌入失败、检索退成了只走关键词(B-206 K-8)—— 整轮退出。

    同 :class:`RerankDegradedError`:生产要降级,评测不能把只走关键词的结果当混合检索计分。
    """


class FakeKbEmbedder(KeywordEmbedder):  # type: ignore[misc]
    """关键词桶向量(CJK 二元组 + ASCII 词),维度对齐库的向量列。

    ``longmem.embedders.KeywordEmbedder`` 是 256 维;SQL 库的 ``vector`` 列建表时钉在
    ``EXPERT_WORK_EMBEDDING_DIM``(默认 1024),维度不等写不进去,所以这里跟着库走。
    用它而不用 ``orchestrator.llm.FakeEmbedder``:后者是纯哈希,向量路等于随机,冒烟看不出排序。
    """

    DIM = EMBEDDING_DIM


def build_fake_embedder() -> FakeKbEmbedder:
    return FakeKbEmbedder()


class CountingEmbedder:
    """包在任何嵌入器外面:校维度、计数、把嵌入失败归到 ``embed`` 阶段。

    计数是进程内累计(文本数 / 字符数 / 超长条数 / token / 费用),``stats()`` 取快照。
    ``embed`` 走 ``inner``(入库的段);``embed_query``(检索器给查询用,B-206 K-5)走
    ``query_inner``,没给就也走 ``inner``——两臂 A/B 时查询侧带不带标记由它决定。
    实花只算真送到嵌入接口的(内层缓存没命中的,见 ``CachedEmbedder.fetched_*``)。
    """

    def __init__(
        self,
        inner: Any,
        *,
        expected_dim: int,
        query_inner: Any = None,
        count_tokens: bool = False,
        price_cny_per_1k_tokens: float = 0.0,
        overlong_chars: int = EMBED_OVERLONG_CHARS,
    ) -> None:
        self._inner = inner
        self._query_inner = query_inner if query_inner is not None else inner
        self._expected_dim = expected_dim
        self._count_tokens = count_tokens
        self._price = price_cny_per_1k_tokens
        self._overlong_chars = overlong_chars
        self._texts = 0
        self._chars = 0
        self._overlong = 0
        self._tokens = 0

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        try:
            vectors = await self._inner.embed(texts, tenant_id=tenant_id)
        except Exception as exc:
            raise EmbedStageError(f"{type(exc).__name__}: {exc}") from exc
        return self._counted(texts, vectors)

    async def embed_query(self, query: str, *, tenant_id: UUID) -> tuple[float, ...]:
        # 不包成 EmbedStageError(那是给建库归阶段用的):检索器按原始异常决定降不降级
        # (B-206 K-8),与生产同一个判断;降级了 run_eval 会整轮退出。
        vectors = await self._query_inner.embed([query], tenant_id=tenant_id)
        (vector,) = self._counted([query], vectors)
        return vector

    def _counted(self, texts: Sequence[str], vectors: Any) -> list[tuple[float, ...]]:
        for vector in vectors:
            if len(vector) != self._expected_dim:
                raise EmbeddingDimensionError(
                    f"embedder returned {len(vector)} dims but the store's vector column is "
                    f"{self._expected_dim} (EXPERT_WORK_EMBEDDING_DIM) — pick a matching model "
                    "or re-create the store with the model's dimension"
                )
        self._texts += len(texts)
        self._chars += sum(len(t) for t in texts)
        self._overlong += sum(1 for t in texts if len(t) > self._overlong_chars)
        if self._count_tokens:
            self._tokens += sum(count_tokens(t) for t in texts)
        return cast(list[tuple[float, ...]], vectors)

    def stats(self) -> EmbedStats:
        fetched, fetched_tokens = self._fetched()
        return EmbedStats(
            texts=self._texts,
            chars=self._chars,
            overlong=self._overlong,
            tokens=self._tokens,
            cost_cny_nominal=round(self._tokens / 1000 * self._price, 6),
            fetched=fetched,
            fetched_tokens=fetched_tokens,
            cost_cny_actual=round(fetched_tokens / 1000 * self._price, 6),
        )

    def _fetched(self) -> tuple[int, int]:
        """``(条数, token)`` 真送到嵌入接口的:内层是缓存就读它的计数;没有缓存的内层
        (假嵌入)每条都送了,等于名义值。"""
        inners = {id(inner): inner for inner in (self._inner, self._query_inner)}.values()
        cached = [inner for inner in inners if hasattr(inner, "fetched_texts")]
        if not cached:
            return self._texts, self._tokens
        return (
            sum(int(inner.fetched_texts) for inner in cached),
            sum(int(inner.fetched_tokens) for inner in cached),
        )


# ---------------------------------------------------------------------------
# 选文档
# ---------------------------------------------------------------------------


def read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def select_rows(
    rows: Sequence[Mapping[str, str]], *, tiers: Sequence[str], limit_docs: int
) -> CorpusSelection:
    """按层过滤 → 同 ``dup_group`` 只留第一份 → ``limit_docs`` 截头(0 = 不限;在去重之后数)。"""
    wanted = set(tiers)
    in_tier = [r for r in rows if r.get("tier") in wanted]
    seen_groups: set[str] = set()
    kept: list[Mapping[str, str]] = []
    skipped_dup = 0
    skipped_empty = 0
    for row in in_tier:
        if (row.get("size") or "").strip() == "0":
            skipped_empty += 1  # a 0-byte file cannot parse; not a retrieval problem
            continue
        group = row.get("dup_group") or ""
        if group and group in seen_groups:
            skipped_dup += 1
            continue
        if group:
            seen_groups.add(group)
        kept.append(row)
    skipped_limit = 0
    if limit_docs > 0:
        skipped_limit = max(0, len(kept) - limit_docs)
        kept = kept[:limit_docs]
    return CorpusSelection(
        rows=tuple(kept),
        skipped_dup=skipped_dup,
        skipped_tier=len(rows) - len(in_tier),
        skipped_limit=skipped_limit,
        skipped_empty=skipped_empty,
    )


# ---------------------------------------------------------------------------
# 建库
# ---------------------------------------------------------------------------


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def doc_titles_for(rows: Sequence[Mapping[str, str]], mode: str) -> dict[str, str | None]:
    """清单每行(``path``)入库时段上带的文档名,见 :data:`DocTitles`。``shuffled`` 按固定种子
    把文档排成一圈,每份拿下一份的名字 —— 没有一份留着自己的名字(只有一份文档时除外)。"""
    paths = [row["path"] for row in rows]
    if mode == "none":
        return dict.fromkeys(paths)
    real = [knowledge_document_title(_basename(path)) for path in paths]
    if mode == "real":
        return dict(zip(paths, real, strict=True))
    if mode != "shuffled":
        raise ValueError(f"unknown doc title mode {mode!r}")
    order = list(range(len(paths)))
    random.Random(DOC_TITLE_SHUFFLE_SEED).shuffle(order)  # noqa: S311 - a reproducible control group
    shuffled: list[str | None] = [None] * len(paths)
    for position, index in enumerate(order):
        shuffled[index] = real[order[(position + 1) % len(order)]]
    return dict(zip(paths, shuffled, strict=True))


def classify_failure(exc: BaseException) -> tuple[str, str]:
    """``(阶段, 异常类型名)``。``EmbedStageError`` 解包出原始异常类型。"""
    if isinstance(exc, EmbedStageError):
        cause = exc.__cause__
        return "embed", type(cause).__name__ if cause is not None else "EmbedStageError"
    module = type(exc).__module__ or ""
    if module.startswith(_STORE_EXC_MODULES):
        return "store", type(exc).__name__
    return "other", type(exc).__name__


async def build_corpus(
    *,
    store: KnowledgeStore,
    embedder: Embedder,
    tenant_id: UUID,
    base_name: str,
    corpus_root: Path,
    selection: CorpusSelection,
    concurrency: int,
    executor: Executor | None = None,
    parser: Callable[[str, bytes], str] = parse_document,
    parse_timeout_s: float = DEFAULT_PARSE_TIMEOUT_S,
    doc_titles: str = "real",
) -> CorpusReport:
    """一次性建单库并把 ``selection`` 里的文档全部走真实入库链路。

    解析在 ``executor`` 里**并发**跑(默认进程池,``min(concurrency, CPU)`` 个 worker),每份
    ``parse_timeout_s`` 秒超时;落库(建文档行 → ``ingest_markdown`` 切块 / 嵌入 / 落段 → 置状态)
    **按清单顺序逐份串行**:行的物理顺序每次都一样,检索并列分数时谁排前面才不随并发抖动
    (10-14 实测:并发落库时同一份代码连跑两次,303 题里 28 题排序不同)。
    段上的文档名按 ``doc_titles``(:func:`doc_titles_for`)。
    同名文件(不同目录、同 basename、又不是同一 ``dup_group``)只入第一份:库内
    ``(kb_id, filename)`` 唯一,再 upsert 会把前一份重置并删段;后到的记进 ``name_collisions``。
    ``EmbeddingDimensionError`` 不按文档失败记,直接抛出 —— 那是配置错。
    """
    started = time.monotonic()
    base = await store.create_base(tenant_id=tenant_id, name=base_name, rerank_enabled=False)
    sem = asyncio.Semaphore(max(1, concurrency))
    claimed: dict[str, str] = {}
    collisions: list[NameCollision] = []
    total = len(selection.rows)
    titles = doc_titles_for(selection.rows, doc_titles)
    loop = asyncio.get_running_loop()

    async def _parse(pool: Executor, filename: str, raw: bytes) -> str:
        future = loop.run_in_executor(pool, parser, filename, raw)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=parse_timeout_s)
        except TimeoutError:
            print(
                f"[parse] {filename}: slower than {parse_timeout_s:g}s, "
                f"waiting up to {SLOW_PARSE_GRACE}x in total",
                file=sys.stderr,
            )
        return await asyncio.wait_for(future, timeout=parse_timeout_s * (SLOW_PARSE_GRACE - 1))

    async def _parse_row(pool: Executor, row: Mapping[str, str]) -> str:
        path = row["path"]
        raw = (corpus_root / path).read_bytes()
        async with sem:
            try:
                markdown = await _parse(pool, _basename(path), raw)
                markdown.encode("utf-8")  # 孤立代理项在这里炸,算解析失败
            except Exception as exc:
                raise _ParseStageError(exc) from exc
        return markdown

    async def _ingest(doc_id: UUID, path: str, parsed: asyncio.Task[str]) -> int:
        markdown = await parsed
        return await ingest_markdown(
            store=store,
            embedder=embedder,
            tenant_id=tenant_id,
            document_id=doc_id,
            kb_id=base.id,
            markdown=markdown,
            chunk_max_tokens=base.chunk_max_tokens,
            chunk_overlap_tokens=base.chunk_overlap_tokens,
            doc_title=titles[path],
            plain_text=is_plain_text_file(path),
        )

    async def _one(
        index: int, row: Mapping[str, str], parsed: asyncio.Task[str]
    ) -> tuple[int, IngestFailure | None]:
        path = row["path"]
        filename = _basename(path)
        if filename in claimed:
            # Same basename, different file: ingest under a directory-qualified
            # name (full relative path if even that collides).
            qualified = f"{Path(path).parent.name}/{filename}"
            if qualified in claimed:
                qualified = path
            collisions.append(
                NameCollision(
                    filename=filename, path=path, kept_path=claimed[filename], ingested_as=qualified
                )
            )
            print(
                f"[ingest {index}/{total}] {path}: same filename as {claimed[filename]}, "
                f"ingested as {qualified!r}",
                file=sys.stderr,
            )
            filename = qualified
        claimed[filename] = path
        doc = await store.upsert_document(
            tenant_id=tenant_id, kb_id=base.id, filename=filename, content=None
        )
        try:
            count = await _ingest(doc.id, path, parsed)
            await store.set_document_status(
                tenant_id=tenant_id,
                document_id=doc.id,
                status=DocumentStatus.READY,
                chunk_count=count,
            )
        except EmbeddingDimensionError:
            raise
        except Exception as exc:  # 解析 / 嵌入 / 落段失败只记账,整轮继续
            stage, error_type = _stage_of(exc)
            error = f"{error_type}: {exc.__cause__ or exc}"[:_ERROR_CAP]
            await store.mark_document_failed_terminal(
                tenant_id=tenant_id, document_id=doc.id, error=error
            )
            print(
                f"[ingest {index}/{total}] {filename}: FAILED ({stage}) {error}",
                file=sys.stderr,
            )
            return 0, IngestFailure(
                filename=filename, path=path, stage=stage, error_type=error_type, error=error
            )
        print(f"[ingest {index}/{total}] {filename}: {count} chunks", file=sys.stderr)
        return count, None

    async def _all(pool: Executor) -> list[tuple[int, IngestFailure | None] | BaseException]:
        # Parse ahead concurrently; write strictly in manifest order.
        parses = [asyncio.create_task(_parse_row(pool, row)) for row in selection.rows]
        outcomes: list[tuple[int, IngestFailure | None] | BaseException] = []
        try:
            for i, (row, parsed) in enumerate(zip(selection.rows, parses, strict=True)):
                try:
                    outcomes.append(await _one(i + 1, row, parsed))
                except EmbeddingDimensionError:
                    raise
                except Exception as exc:  # _one 自己没兜住的(写状态失败等)
                    outcomes.append(exc)
        finally:
            for task in parses:
                task.cancel()
            await asyncio.gather(*parses, return_exceptions=True)
        return outcomes

    if executor is None:
        workers = max(1, min(concurrency, os.cpu_count() or 1))
        with ProcessPoolExecutor(max_workers=workers) as pool:
            outcomes = await _all(pool)
    else:
        outcomes = await _all(executor)

    counts: list[int] = []
    failures: list[IngestFailure] = []
    for row, outcome in zip(selection.rows, outcomes, strict=True):
        if isinstance(outcome, EmbeddingDimensionError):
            raise outcome
        if isinstance(outcome, BaseException):  # _one 自己没兜住的(写状态失败等)
            stage, error_type = classify_failure(outcome)
            failures.append(
                IngestFailure(
                    filename=_basename(row["path"]),
                    path=row["path"],
                    stage=stage,
                    error_type=error_type,
                    error=f"{error_type}: {outcome}"[:_ERROR_CAP],
                )
            )
            continue
        count, failure = outcome
        counts.append(count)
        if failure is not None:
            failures.append(failure)
    by_stage: dict[str, int] = {}
    for failure in failures:
        by_stage[failure.stage] = by_stage.get(failure.stage, 0) + 1
    return CorpusReport(
        base_id=base.id,
        docs_ingested=total - len(failures),
        docs_skipped_dup=selection.skipped_dup,
        docs_skipped_limit=selection.skipped_limit,
        docs_skipped_empty=selection.skipped_empty,
        docs_name_collision=len(collisions),
        docs_failed=len(failures),
        failed_by_stage=by_stage,
        chunk_total=sum(counts),
        elapsed_s=round(time.monotonic() - started, 3),
        ingest_failures=tuple(failures),
        name_collisions=tuple(collisions),
        embed=embedder.stats() if isinstance(embedder, CountingEmbedder) else None,
    )


class _ParseStageError(Exception):
    """解析阶段(进程池 / 超时 / 编码)的失败标记;``__cause__`` 是原始异常。"""


def _stage_of(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, _ParseStageError):
        cause = exc.__cause__
        return "parse", type(cause).__name__ if cause is not None else "ParseStageError"
    return classify_failure(exc)


# ---------------------------------------------------------------------------
# 锚文本存活
# ---------------------------------------------------------------------------


async def check_anchors(
    *, store: KnowledgeStore, tenant_id: UUID, kb_id: UUID, dataset: KbDataset
) -> AnchorReport:
    """每条 expected 的 anchor 要能在其文档的**某一段**里 ``in`` 到;否则检索再好也命不中段级。

    两种不存活分开报:``document_missing``(文档没入库 / 不在清单)与
    ``anchor_not_in_any_chunk``(切块把锚文本切断了,或评测集抄错)。
    """
    docs = {
        d.filename: d
        for d in await store.list_documents(tenant_id=tenant_id, kb_id=kb_id)
        if d.status is DocumentStatus.READY
    }
    chunk_cache: dict[str, list[str]] = {}

    async def _chunks(filename: str) -> list[str]:
        if filename not in chunk_cache:
            chunk_cache[filename] = await _all_chunk_texts(
                store, tenant_id=tenant_id, document_id=docs[filename].id
            )
        return chunk_cache[filename]

    checked = 0
    dead: list[DeadAnchor] = []
    alive: set[str] = set()
    for case in dataset.cases:
        if not case.expected:
            continue
        case_dead = False
        for exp in case.expected:
            checked += 1
            if exp.document not in docs:
                dead.append(DeadAnchor(case.id, exp.document, "document_missing"))
                case_dead = True
            elif not any(anchor_in(exp.anchor, text) for text in await _chunks(exp.document)):
                dead.append(DeadAnchor(case.id, exp.document, "anchor_not_in_any_chunk"))
                case_dead = True
        if not case_dead:
            alive.add(case.id)
    return AnchorReport(checked=checked, alive_cases=frozenset(alive), dead=tuple(dead))


async def _all_chunk_texts(
    store: KnowledgeStore, *, tenant_id: UUID, document_id: UUID
) -> list[str]:
    texts: list[str] = []
    offset = 0
    while True:
        page, total = await store.list_chunks(
            tenant_id=tenant_id, document_id=document_id, offset=offset, limit=_CHUNK_PAGE
        )
        # The text hits are judged on (``RetrievedChunk.retrieval_text``):
        # breadcrumb + body, without the document title the index also
        # carries — an anchor only in a title must not count as found.
        texts.extend(chunk_retrieval_text(c.heading_path, c.content) for c in page)
        offset += len(page)
        if not page or offset >= total:
            return texts


#: 一段锚文本出现在超过这么多份已入库文档里,就太常见 —— 拿它判对会把不相干的文档也算对,
#: 只报告、不扩展。
MAX_ANCHOR_DOCS = 5


async def resolve_anchor_documents(
    *,
    store: KnowledgeStore,
    tenant_id: UUID,
    kb_id: UUID,
    dataset: KbDataset,
    max_docs: int = MAX_ANCHOR_DOCS,
) -> tuple[KbDataset, AnchorDocsReport]:
    """把「含同一锚文本的其他已入库文档」填进每条期望的 ``also``。

    评测集只手列了答案所在的几份文档;内容重复的文档(同一份 FAQ 的两个版本)排在前面时,
    按文件名判就会算错 —— 10-14 实测产品 FAQ 题里 33 道排第一的是没列的 FAQ-2026。
    比对用 :func:`kb_metrics.anchor_key` 的归一形;一份文档的各段分别归一后用 ``\x00`` 连起来
    再查(归一形只有字母与数字),锚文本不会跨段误配。
    """
    texts: dict[str, str] = {}
    for doc in await store.list_documents(tenant_id=tenant_id, kb_id=kb_id):
        if doc.status is DocumentStatus.READY:
            chunks = await _all_chunk_texts(store, tenant_id=tenant_id, document_id=doc.id)
            texts[doc.filename] = "\x00".join(anchor_key(chunk) for chunk in chunks)
    expanded = 0
    expanded_cases = 0
    too_common: list[TooCommonAnchor] = []
    cases: list[KbCase] = []
    for case in dataset.cases:
        resolved: list[KbExpected] = []
        for exp in case.expected:
            key = anchor_key(exp.anchor)
            holders = {name for name, text in texts.items() if key in text}
            others = holders - {exp.document}
            if others and len(holders) > max_docs:
                too_common.append(TooCommonAnchor(case.id, exp.document, len(holders)))
                resolved.append(exp)
            elif others:
                resolved.append(replace(exp, also=frozenset(others)))
                expanded += 1
            else:
                resolved.append(exp)
        if tuple(resolved) != case.expected:
            expanded_cases += 1
        cases.append(replace(case, expected=tuple(resolved)))
    report = AnchorDocsReport(
        expanded=expanded, expanded_cases=expanded_cases, too_common=tuple(too_common)
    )
    return replace(dataset, cases=tuple(cases)), report


# ---------------------------------------------------------------------------
# 打分
# ---------------------------------------------------------------------------


def build_retriever(
    *,
    store: KnowledgeStore,
    embedder: Embedder,
    reranker: Reranker | None,
    top_k: int = DEFAULT_TOP_K,
    rerank_view: RerankView = DEFAULT_RERANK_VIEW,
    rerank_pool: int = DEFAULT_RERANK_POOL,
    rerank_instruction: bool = False,
) -> KnowledgeRetriever:
    """两路召回深度跟生产同一个默认(:func:`default_recall_depth`);只有 ``top_k`` 超过它时
    才把深度抬到 ``top_k``,否则前 k 名凑不满。``rerank_*`` 是生产检索器自己的 K-14 旋钮
    (默认 = 生产默认)。"""
    recall_limit = top_k if top_k > default_recall_depth(top_k) else None
    return KnowledgeRetriever(
        store=store,
        embedder=embedder,
        reranker=reranker,
        recall_limit=recall_limit,
        rerank_view=rerank_view,
        rerank_pool=rerank_pool,
        rerank_instruction=rerank_instruction,
    )


async def run_eval(
    *,
    dataset: KbDataset,
    retriever: KnowledgeRetriever,
    tenant_id: UUID,
    cfg: EvalConfig,
    concurrency: int,
    anchors: AnchorReport | None = None,
) -> list[CaseRow]:
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(case: KbCase) -> CaseRow:
        async with sem:
            outcome = await retriever.search_detailed(
                tenant_id=tenant_id,
                base_names=[dataset.name],
                query=case.query,
                limit=cfg.top_k,
                method=cfg.method,
                score_threshold=cfg.threshold,
                rerank=cfg.rerank,
                rerank_score_threshold=cfg.rerank_threshold,
            )
        if cfg.rerank and outcome.rerank_degraded:
            msg = (
                f"rerank degraded on case {case.id} (see the knowledge.rerank_failed / "
                "rerank_empty warning): its numbers would be the fused order"
            )
            raise RerankDegradedError(msg)
        if outcome.embedding_degraded:
            msg = (
                f"query embedding failed on case {case.id} (see the knowledge.embedding_degraded "
                "warning): its numbers would be keyword search alone"
            )
            raise EmbeddingDegradedError(msg)
        results = list(outcome.results)
        replay = _ReplayRetriever(inner=retriever, outcome=outcome)
        tool = KnowledgeSearchTool(
            retriever=cast(KnowledgeRetriever, replay), knowledge_base_refs=(dataset.name,)
        )
        shown_text = await tool_message_text(
            tool, {"query": case.query}, tenant_id=tenant_id, call_id=case.id
        )
        # 工具要的条数比这次检索的 top_k 还多时,截下来的前几名不是它自己搜会拿到的,不算。
        faithful = replay.requested is not None and replay.requested <= cfg.top_k
        alive: bool | None = None
        if anchors is not None and case.expected:
            alive = case.id in anchors.alive_cases
        return CaseRow(
            case_id=case.id,
            source=case.source,
            tags=case.tags,
            results=tuple(_result_row(r, with_text=cfg.with_text) for r in results),
            score=score_case(
                results,
                case.expected,
                threshold=cfg.threshold,
                match=case.match,
                rerank_threshold=cfg.rerank_threshold,
                shown_text=shown_text if faithful else None,
            ),
            match=case.match,
            anchors_alive=alive,
            search=SearchLog(
                query=case.query,
                limit=cfg.top_k,
                status=outcome.status,
                candidates=outcome.candidates,
                rerank_degraded=outcome.rerank_degraded,
                results=outcome.results,
                shown=min(replay.requested or 0, len(results)),
                shown_chars=len(shown_text),
            ),
        )

    return list(await asyncio.gather(*(_one(c) for c in dataset.cases)))


@dataclass
class _ReplayRetriever:
    """``knowledge_search`` 那次检索不重跑:给它这次检索结果的前 ``limit`` 条(模型不传条数时
    就是工具默认条数)。召回深度只在 ``limit`` > 25、双视图重排的前段只在 ``limit`` > 20 时才随
    ``limit`` 变(单视图整池排序,不随 ``limit`` 变),所以默认 ``--top-k 20`` 下与工具自己搜一次
    逐条相同,也不多花一次嵌入 / 重排;
    其余属性转给真检索器(工具以后要从库里取父段之类的,照样拿得到)。"""

    inner: KnowledgeRetriever
    outcome: KnowledgeSearchOutcome
    requested: int | None = None

    async def search_detailed(self, *, limit: int, **_overrides: Any) -> KnowledgeSearchOutcome:
        self.requested = limit
        return replace(self.outcome, results=self.outcome.results[:limit])

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


async def tool_message_text(
    tool: Tool, args: Mapping[str, Any], *, tenant_id: UUID, call_id: str
) -> str:
    """工具节点交给模型的那段文本 —— 直接走生产的 ``_invoke_tool``:参数校验、调用、出错时的
    报错格式、超长外置(默认智能体不开工作区,没有外置)、防注入围栏、围栏外的提示,都与生产
    同一段代码,工具的渲染 / 条数 / 截断以后怎么改都跟着走。"""
    message, _updates, _refund, _error = await _invoke_tool(
        tool,
        dict(args),
        call_id,
        ToolContext(tenant_id=tenant_id),
        spotlight_nonce=TOOL_SPOTLIGHT_NONCE,
    )
    return str(message.content)


def _result_row(result: RetrievedChunk, *, with_text: bool = False) -> ResultRow:
    return ResultRow(
        filename=result.filename,
        chunk_index=result.chunk_index,
        score=result.score,
        recall_source=result.recall_source,
        text=result.retrieval_text if with_text else None,
        rerank_score=result.rerank_score,
    )


# ---------------------------------------------------------------------------
# 汇总 / 输出
# ---------------------------------------------------------------------------


def summarise(
    rows: Sequence[CaseRow],
    *,
    corpus: CorpusReport | None,
    anchors: AnchorReport | None = None,
    anchor_docs: AnchorDocsReport | None = None,
    embedder: str = "unknown",
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    scores = [r.score for r in rows]
    corpus_block: dict[str, Any] | None = None
    if corpus is not None:
        corpus_block = asdict(corpus)
        corpus_block["base_id"] = str(corpus.base_id)
        corpus_block["ingest_failures"] = [asdict(f) for f in corpus.ingest_failures]
        corpus_block["name_collisions"] = [asdict(c) for c in corpus.name_collisions]
        corpus_block["embedder"] = embedder
    anchors_block: dict[str, Any] | None = None
    if anchors is not None:
        anchors_block = {
            "checked": anchors.checked,
            "dead_cases": len(anchors.dead_cases),
            "dead": [asdict(d) for d in anchors.dead],
        }
    if anchor_docs is not None:
        anchors_block = dict(anchors_block or {})
        anchors_block["expanded"] = anchor_docs.expanded
        anchors_block["expanded_cases"] = anchor_docs.expanded_cases
        anchors_block["too_common"] = [asdict(t) for t in anchor_docs.too_common]
    return {
        "overall": aggregate(scores),
        "by_source": grouped((r.source, r.score) for r in rows),
        # 同一 case 重复写的 tag 只算一次(dict.fromkeys 保序去重)。
        "by_tag": grouped((tag, r.score) for r in rows for tag in dict.fromkeys(r.tags)),
        "corpus": corpus_block,
        "anchors": anchors_block,
        "config": dict(config or {}),
    }


#: 写进基线时剔掉的、每次跑都会变的字段(``(块, 键)``)。
_BASELINE_VOLATILE: tuple[tuple[str, str], ...] = (
    ("corpus", "base_id"),
    ("corpus", "elapsed_s"),
    ("config", "label"),
    ("rerank", "calls"),
    ("rerank", "cache_hits"),
    ("rerank", "tokens"),
    ("rerank", "cost_cny"),
)


def baseline_section(summary: Mapping[str, Any]) -> dict[str, Any]:
    """基线里存的汇总 = ``summary`` 去掉会抖的字段。"""
    section = {k: (dict(v) if isinstance(v, Mapping) else v) for k, v in summary.items()}
    for block, key in _BASELINE_VOLATILE:
        if isinstance(section.get(block), dict):
            section[block].pop(key, None)
    return section


def _result_json(result: ResultRow) -> dict[str, Any]:
    out = asdict(result)
    for key in ("text", "rerank_score"):
        if out[key] is None:
            del out[key]
    return out


def case_to_json(row: CaseRow) -> dict[str, Any]:
    return {
        "id": row.case_id,
        "source": row.source,
        "tags": list(row.tags),
        "match": row.match,
        "answerable": row.score.answerable,
        "refused": row.score.refused,
        "zero_hit": row.score.zero_hit,
        "best_score": row.score.best_score,
        "best_rerank_score": max(
            (r.rerank_score for r in row.results if r.rerank_score is not None), default=None
        ),
        "anchors_alive": row.anchors_alive,
        "results": [_result_json(r) for r in row.results],
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


def search_log_json(row: CaseRow) -> dict[str, Any]:
    """``--search-log`` 的一行:这一题那次检索返回的每一段(不含正文)。"""
    log = row.search
    if log is None:
        raise ValueError(f"case {row.case_id} has no search log")
    return {
        "case_id": row.case_id,
        "query": log.query,
        "limit": log.limit,
        "status": log.status,
        "candidates": log.candidates,
        "rerank_degraded": log.rerank_degraded,
        "shown": log.shown,
        "shown_chars": log.shown_chars,
        "results": [
            {
                "rank": rank,
                "filename": r.filename,
                "chunk_index": r.chunk_index,
                "chunk_id": None if r.chunk_id is None else str(r.chunk_id),
                "recall_source": r.recall_source,
                "vector_score": r.vector_score,
                "keyword_score": r.keyword_score,
                "fused_score": r.fused_score,
                "rerank_score": r.rerank_score,
                "shown": rank <= log.shown,
            }
            for rank, r in enumerate(log.results, start=1)
        ],
    }


def write_search_log(path: Path, rows: Sequence[CaseRow]) -> None:
    refuse_existing(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(search_log_json(row), ensure_ascii=False) + "\n")


_TABLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("n", "n"),
    ("doc_hit@10", "doc_hit@10"),
    ("doc_hit@20", "doc_hit@20"),
    ("doc_cov@20", "doc_coverage@20"),
    ("chk_hit@10", "chunk_hit@10"),
    ("ctx_hit", "context_hit"),
    ("chk_hit@20", "chunk_hit@20"),
    ("chk_cov@20", "chunk_coverage@20"),
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
            f"skipped_limit={corpus['docs_skipped_limit']} "
            f"skipped_empty={corpus['docs_skipped_empty']} "
            f"name_collision={corpus['docs_name_collision']} failed={corpus['docs_failed']} "
            f"{corpus['failed_by_stage']} chunks={corpus['chunk_total']} "
            f"elapsed={corpus['elapsed_s']}s embedder={corpus['embedder']}"
        )
        if corpus["docs_failed"]:
            names = ", ".join(f["filename"] for f in corpus.get("ingest_failures", []))
            lines.append(
                f"WARNING corpus incomplete: {corpus['docs_failed']} document(s) failed ({names}) "
                f"— not comparable with a run whose chunks= differs"
            )
        embed = corpus.get("embed")
        if embed:
            lines.append(
                f"embed: texts={embed['texts']} chars={embed['chars']} "
                f"overlong(>{EMBED_OVERLONG_CHARS})={embed['overlong']} tokens={embed['tokens']} "
                f"fetched={embed['fetched']} fetched_tokens={embed['fetched_tokens']} "
                f"cost_cny_actual={embed['cost_cny_actual']} "
                f"cost_cny_nominal={embed['cost_cny_nominal']} (nominal = if nothing were cached)"
            )
    anchors = summary.get("anchors")
    if anchors:
        lines.append(
            f"anchors: checked={anchors['checked']} dead_cases={anchors['dead_cases']} "
            f"(dead anchors are dataset / chunking problems, not retrieval misses)"
        )
    return "\n".join(lines)


def fingerprint(
    *,
    dataset_path: Path,
    manifest_path: Path,
    corpus_root: Path,
    embedder: str,
    embed_base_url: str | None,
    query_marker: str,
    store: str,
    tiers: Sequence[str],
    limit_docs: int,
    cfg: EvalConfig,
    ann: bool = False,
) -> dict[str, Any]:
    return {
        "dataset_sha256": file_sha256(dataset_path),
        "manifest_sha256": file_sha256(manifest_path),
        # Directory name only: the baseline is checked in, a machine-local
        # absolute path is not part of what makes two runs comparable.
        "corpus_root": corpus_root.resolve().name,
        "embedder": embedder,
        "embed_base_url": embed_base_url,
        # B-206 K-5: the query-side marker changes every query vector.
        "query_marker": query_marker,
        "embedding_dim": EMBEDDING_DIM,
        "store": store,
        # exact = reproducible (serial writes + exact KNN); ann = production HNSW.
        "vector_search": "ann" if ann else "exact",
        "commit": _git(["rev-parse", "--short", "HEAD"]) or "unknown",
        "git_dirty": bool(_git(["status", "--porcelain"])),
        "tiers": list(tiers),
        "limit_docs": limit_docs,
        "method": cfg.method.value,
        "top_k": cfg.top_k,
        "threshold": cfg.threshold,
        "rerank": cfg.rerank_mode,
        "rerank_threshold": cfg.rerank_threshold,
        **rerank_arm(cfg),
    }


def rerank_arm(cfg: EvalConfig) -> dict[str, Any]:
    """B-206 K-14:这次跑的是哪个重排对照组(结果汇总 / 基线指纹里都记)。"""
    return {
        "rerank_view": cfg.rerank_view,
        "rerank_pool": cfg.rerank_pool,
        "rerank_instruct": cfg.rerank_instruction,
    }


def _git(args: Sequence[str]) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        # 固定 argv、二进制经 shutil.which 解析 —— 只为指纹。
        return subprocess.run(  # noqa: S603
            [git, *args], capture_output=True, text=True, check=True, cwd=_EVAL_DIR
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return None


# ---------------------------------------------------------------------------
# 库 / 嵌入 / 重排 的构造
# ---------------------------------------------------------------------------


@contextmanager
def rls_tenant(tenant_id: UUID) -> Iterator[None]:
    """像应用层(入库 runner / 各 job)一样把租户放进 ContextVar:SQL 库经 ``build_rls_sessionmaker``
    后每个事务 ``set_config('app.tenant_id', …)``,RLS 真开着时才查得到自己的行。"""
    token = current_tenant_id_var.set(tenant_id)
    try:
        yield
    finally:
        current_tenant_id_var.reset(token)


@asynccontextmanager
async def open_store(kind: str, *, ann: bool = False) -> AsyncIterator[KnowledgeStore]:
    """``memory`` → ``InMemoryKnowledgeStore``;``sql`` → testcontainers 起 pgvector 并迁移到 head。

    默认(``ann=False``)这个一次性库固定读行顺序(见 :func:`_exact_search_defaults`),
    向量路走精确检索。pgvector 的 HNSW 建图带随机层级,同样的插入顺序也建不出同一张图,
    近似结果每次可能不同;评测要的是同一份代码两次跑出同一份结果(10-14 实测:开着 HNSW 时
    303 题里 17 题结果集不同)。
    ``ann=True`` 走生产同款 HNSW,量近似检索本身的召回损失时用,结果不保证可复现。

    SQL 那条路与 ``packages/expert-work-persistence/tests/test_sql_knowledge_store.py`` 的 fixture
    同一套做法:同步 DSN 跑迁移,异步 DSN 建 engine / session factory,再按应用层的样子包一层
    ``build_rls_sessionmaker``(配合 :func:`rls_tenant`)。关键词路的语义
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
    from expert_work.persistence.rls import build_rls_sessionmaker

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
        if not ann:
            _exact_search_defaults(sync_dsn)
        engine = create_async_engine_from_config(DatabaseConfig(dsn=async_dsn))
        try:
            yield SqlKnowledgeStore(build_rls_sessionmaker(create_async_session_factory(engine)))
        finally:
            await engine.dispose()


def _exact_search_defaults(sync_dsn: str) -> None:
    """Make this throwaway database return ties in the same order every run.

    Equal scores are common (duplicate documents embed to identical vectors),
    and Postgres hands ties back in the order it reads rows, so the read
    order itself has to be fixed:

    * no index scan — HNSW never used, the vector side is exact KNN (GIN still
      serves keyword search through bitmap scans, which read in table order);
    * no parallel workers — gather order would vary;
    * no synchronized seq scans — a scan of the big chunk table would join a
      concurrent one mid-table (10-14: 203 of 303 cases differed);
    * no autovacuum — a vacuum during the build records the gaps left in
      earlier pages and later inserts fill them, so rows land in a different
      table order depending on when it ran (10-14: still 103 of 303 reordered
      with only the three settings above).
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    database = make_url(sync_dsn).database
    engine = create_engine(sync_dsn, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            for setting in (
                "enable_indexscan = off",
                "max_parallel_workers_per_gather = 0",
                "synchronize_seqscans = off",
            ):
                conn.execute(text(f'ALTER DATABASE "{database}" SET {setting}'))
            # Server-wide only; the container is ours alone.
            conn.execute(text("ALTER SYSTEM SET autovacuum = off"))
            conn.execute(text("SELECT pg_reload_conf()"))
    finally:
        engine.dispose()


@dataclass(frozen=True)
class EmbedderBundle:
    embedder: CountingEmbedder
    tag: str  # fake | 模型名
    base_url: str | None  # 假嵌入 None
    #: 查询侧标记(B-206 K-5):``off`` = 关掉了;``none`` = 开着但模型没有(假嵌入 / 目录外 /
    #: asymmetry="none");其余 = 实际发出去的标记(``EmbeddingMarker.fingerprint``)。
    query_marker: str = "none"


def catalog_embedding_caps(model: str) -> EmbeddingCaps | None:
    """评测只知道模型名(``EXPERT_WORK_EVAL_EMBED_MODEL``):在模型目录里按名找嵌入模型的能力。
    找不到 = 目录外模型,与生产一样不加标记。"""
    for entries in MODEL_CATALOG.values():
        for entry in entries:
            if entry.name == model and entry.embeddings:
                return entry.embedding_caps
    return None


def build_embedder(kind: str, *, concurrency: int, query_marker: bool = True) -> EmbedderBundle:
    """``real`` 的凭据只从 ``EXPERT_WORK_EVAL_EMBED_*`` 读,缺了就退出;两种都包进
    :class:`CountingEmbedder`(校维度 / 计数 / 失败归阶段)。

    ``real`` 与生产同一套 B-206 K-5 标记:入库的段带模型目录的文档侧标记,查询带查询侧
    标记(``query_marker=False`` 关掉查询侧,做 A/B 的对照臂)。标记进嵌入缓存的键,两臂
    不会互相读到对方的查询向量;文档侧两臂相同,语料只嵌一次。"""
    if kind == "fake":
        return EmbedderBundle(
            embedder=CountingEmbedder(build_fake_embedder(), expected_dim=EMBEDDING_DIM),
            tag="fake",
            base_url=None,
        )
    model = os.environ.get("EXPERT_WORK_EVAL_EMBED_MODEL", "")
    caps = catalog_embedding_caps(model)
    query_side = embedding_marker(caps, "query") if query_marker else NO_MARKER

    def _cached(marker: EmbeddingMarker) -> Any:
        return build_real_embedder(
            cache_db=EMBED_CACHE_DB,
            concurrency=concurrency,
            max_text_chars=EMBED_MAX_TEXT_CHARS,
            marker=marker,
            count_tokens=count_tokens,
        )

    price = float(os.environ.get(EMBED_PRICE_ENV, DEFAULT_EMBED_PRICE_CNY_PER_1K))
    return EmbedderBundle(
        embedder=CountingEmbedder(
            _cached(embedding_marker(caps, "document")),
            query_inner=_cached(query_side),
            expected_dim=EMBEDDING_DIM,
            count_tokens=True,
            price_cny_per_1k_tokens=price,
        ),
        tag=os.environ["EXPERT_WORK_EVAL_EMBED_MODEL"],
        base_url=os.environ.get("EXPERT_WORK_EVAL_EMBED_BASE_URL") or QWEN_EMBEDDING_BASE_URL,
        query_marker=(query_side.fingerprint or "none") if query_marker else "off",
    )


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


def baseline_benchmark(dataset_name: str, rerank_mode: str) -> str:
    """基线文件里的条目名:不开重排(召回层)用评测集名;开了重排(最终排序)另起一条,
    两层口径互不覆盖。"""
    return dataset_name if rerank_mode == "off" else f"{dataset_name}@rerank-{rerank_mode}"


def build_rerank_mode(mode: str) -> tuple[Reranker | None, CachedRerankClient | None]:
    """``--rerank`` 的三种取法;``dashscope`` 另返回缓存客户端,汇总里记调用与费用。"""
    if mode == "dashscope":
        reranker, client = build_dashscope_reranker(cache_path=RERANK_CACHE_DB)
        return reranker, client
    return build_reranker(mode == "llm"), None


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
    parser.add_argument("--limit-docs", type=int, default=0, help="去重后只入前 N 份文档;0 = 全部")
    parser.add_argument("--store", default="sql", choices=("memory", "sql"))
    parser.add_argument("--embedder", default="fake", choices=("fake", "real"))
    parser.add_argument(
        "--query-marker",
        default="on",
        choices=("on", "off"),
        help="查询侧标记(B-206 K-5,按模型目录;生产默认开):off = 对照臂,查询不带标记",
    )
    parser.add_argument(
        "--method", default="hybrid", choices=tuple(m.value for m in RetrievalMethod)
    )
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--threshold", type=float, default=None, help="向量路相似度下限")
    parser.add_argument(
        "--rerank-threshold",
        type=float,
        default=None,
        help="重排分数下限(要配 --rerank);设了以后「拒答」= 检索返回空",
    )
    parser.add_argument(
        "--rerank",
        default="off",
        choices=("off", "on", "llm", "dashscope"),
        help="dashscope = 生产同款 qwen3-vl-rerank(带缓存);llm = 聊天模型排序(on 是它的旧名)",
    )
    parser.add_argument(
        "--rerank-view",
        default=DEFAULT_RERANK_VIEW,
        choices=RERANK_VIEWS,
        help=(
            "B-206 K-14 对照组:dual = 带标题 / 正文两视图加权(K-14 之前);single = 结构化单串"
            "打一次分;single_clean = 单串、正文先去 Markdown / URL / 表格线 / HTML。默认 = 生产默认"
        ),
    )
    parser.add_argument(
        "--rerank-pool",
        type=int,
        default=DEFAULT_RERANK_POOL,
        help="重排候选数(融合后前 N 条,再按模型单次上限封顶);K-14 之前等于 50。默认 = 生产默认",
    )
    parser.add_argument(
        "--rerank-instruct",
        action="store_true",
        help="单视图带英文任务说明(只发给支持的模型,生产默认关)",
    )
    parser.add_argument(
        "--doc-match",
        default="anchor",
        choices=("anchor", "listed"),
        help="anchor = 含同一锚文本的已入库文档都算对(默认);listed = 只认评测集手列的文档",
    )
    parser.add_argument(
        "--ann",
        action="store_true",
        help="向量路走生产同款 HNSW 近似检索(默认精确检索,结果可复现;ANN 不保证)",
    )
    parser.add_argument(
        "--with-text",
        action="store_true",
        help="结果文件里带上每段的检索文本(汇集标注用;含语料原文,结果文件只留在仓库外)",
    )
    parser.add_argument(
        "--doc-titles",
        default="real",
        choices=DOC_TITLE_CHOICES,
        help="段上的文档名:real = 生产同款;none = 不带(改前);shuffled = 在文档间打乱的对照组",
    )
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument(
        "--parse-timeout",
        type=float,
        default=DEFAULT_PARSE_TIMEOUT_S,
        help="单份文档解析超时(秒);超时记 parse 失败",
    )
    parser.add_argument("--label", default="run", help="结果文件名;--out 未给时用它")
    parser.add_argument("--out", type=Path, help=f"结果 JSONL(默认 {EVAL_OUT_DIR}/<label>.jsonl)")
    parser.add_argument(
        "--search-log",
        type=Path,
        help="另写每次检索的逐段记录 JSONL(每题一行:段号、名次、各路分数、是否交给了模型);默认不写",
    )
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
    """``--corpus`` 优先;评测集里的相对路径相对**评测集文件所在目录**解析,不随 cwd 变。"""
    if args.corpus is not None:
        return cast(Path, args.corpus)
    if dataset.corpus_manifest is None:
        raise SystemExit("dataset has no corpus.manifest; pass --corpus <manifest.csv>")
    manifest = Path(dataset.corpus_manifest).expanduser()
    if manifest.is_absolute():
        return manifest
    return (cast(Path, args.dataset).resolve().parent / manifest).resolve()


async def _run(
    *,
    args: argparse.Namespace,
    dataset: KbDataset,
    manifest: Path,
    corpus_root: Path,
    tiers: Sequence[str],
    cfg: EvalConfig,
    bundle: EmbedderBundle,
    reranker: Reranker | None,
) -> tuple[list[CaseRow], dict[str, Any], CorpusReport]:
    selection = select_rows(read_manifest(manifest), tiers=tiers, limit_docs=args.limit_docs)
    print(
        f"corpus: {len(selection.rows)} docs selected (tiers={','.join(tiers)}, "
        f"skipped_dup={selection.skipped_dup}, skipped_tier={selection.skipped_tier}, "
        f"skipped_limit={selection.skipped_limit}, skipped_empty={selection.skipped_empty}; "
        f"limit counts after dedup)",
        file=sys.stderr,
    )
    async with open_store(args.store, ann=args.ann) as store:
        with rls_tenant(EVAL_TENANT_ID):
            report = await build_corpus(
                store=store,
                embedder=bundle.embedder,
                tenant_id=EVAL_TENANT_ID,
                base_name=dataset.name,
                corpus_root=corpus_root,
                selection=selection,
                concurrency=args.concurrency,
                parse_timeout_s=args.parse_timeout,
                doc_titles=args.doc_titles,
            )
            if report.docs_ingested == 0:
                raise SystemExit(
                    f"no document ingested ({report.docs_failed} failed) — check --corpus-root "
                    f"({corpus_root}) against the manifest's path column"
                )
            anchors = await check_anchors(
                store=store, tenant_id=EVAL_TENANT_ID, kb_id=report.base_id, dataset=dataset
            )
            anchor_docs: AnchorDocsReport | None = None
            if args.doc_match == "anchor":
                dataset, anchor_docs = await resolve_anchor_documents(
                    store=store, tenant_id=EVAL_TENANT_ID, kb_id=report.base_id, dataset=dataset
                )
                print(
                    f"anchor docs: {anchor_docs.expanded} expectations in "
                    f"{anchor_docs.expanded_cases} cases gained other documents holding the "
                    f"anchor; {len(anchor_docs.too_common)} anchors too common to use",
                    file=sys.stderr,
                )
            retriever = build_retriever(
                store=store,
                embedder=bundle.embedder,
                reranker=reranker,
                top_k=cfg.top_k,
                rerank_view=cfg.rerank_view,
                rerank_pool=cfg.rerank_pool,
                rerank_instruction=cfg.rerank_instruction,
            )
            rows = await run_eval(
                dataset=dataset,
                retriever=retriever,
                tenant_id=EVAL_TENANT_ID,
                cfg=cfg,
                concurrency=args.concurrency,
                anchors=anchors,
            )
    # 建库报告里的嵌入计数在打分前取的快照;这里换成含 query 嵌入的最终值。
    report = replace(report, embed=bundle.embedder.stats())
    config = {
        "store": args.store,
        "vector_search": "ann" if args.ann else "exact",
        "query_marker": bundle.query_marker,
        "doc_match": args.doc_match,
        "doc_titles": args.doc_titles,
        "tiers": list(tiers),
        "limit_docs": args.limit_docs,
        "method": cfg.method.value,
        "top_k": cfg.top_k,
        "threshold": cfg.threshold,
        "rerank": cfg.rerank_mode,
        "rerank_threshold": cfg.rerank_threshold,
        **rerank_arm(cfg),
        "label": args.label,
    }
    summary = summarise(
        rows,
        corpus=report,
        anchors=anchors,
        anchor_docs=anchor_docs,
        embedder=bundle.tag,
        config=config,
    )
    return rows, summary, report


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
    if args.search_log is not None:
        refuse_existing(args.search_log)
    baseline_out = (
        cast(Path, args.baseline_out)
        if args.baseline_out is not None
        else _EVAL_DIR / "baselines" / f"kb_{dataset.name}_baseline.yaml"
    )
    rerank_mode = "llm" if args.rerank == "on" else args.rerank
    if args.rerank_threshold is not None and rerank_mode != "dashscope":
        raise SystemExit(
            "--rerank-threshold needs --rerank dashscope: other modes give no rerank scores, "
            "so the floor would filter nothing while every empty result counted as a refusal"
        )
    if args.rerank_threshold is not None and args.update_baseline:
        raise SystemExit(
            "refusing to update baseline with --rerank-threshold: the baseline section is "
            "keyed by rerank mode only, so a thresholded run would overwrite the plain one"
        )
    if args.rerank_pool < 1:
        raise SystemExit(f"--rerank-pool must be at least 1, got {args.rerank_pool}")
    cfg = EvalConfig(
        method=RetrievalMethod(args.method),
        top_k=args.top_k,
        threshold=args.threshold,
        rerank=rerank_mode != "off",
        rerank_threshold=args.rerank_threshold,
        rerank_mode=rerank_mode,
        with_text=args.with_text,
        rerank_view=args.rerank_view,
        rerank_pool=args.rerank_pool,
        rerank_instruction=args.rerank_instruct,
    )
    if args.update_baseline and rerank_arm(cfg) != rerank_arm(EvalConfig()):
        raise SystemExit(
            "refusing to update baseline with a K-14 arm (--rerank-view / --rerank-pool / "
            "--rerank-instruct off the product default): the baseline section is keyed by "
            "rerank mode only, so an A/B arm would overwrite the product's numbers"
        )
    bundle = build_embedder(
        args.embedder, concurrency=args.concurrency, query_marker=args.query_marker == "on"
    )
    reranker, rerank_client = build_rerank_mode(rerank_mode)

    rows, summary, report = asyncio.run(
        _run(
            args=args,
            dataset=dataset,
            manifest=manifest,
            corpus_root=corpus_root,
            tiers=tiers,
            cfg=cfg,
            bundle=bundle,
            reranker=reranker,
        )
    )
    if rerank_client is not None:
        summary["rerank"] = {"model": cast(Any, reranker).model, **rerank_client.stats()}
        print(f"rerank: {summary['rerank']}", file=sys.stderr)
    write_jsonl(out, rows, summary)
    print(format_summary_table(summary))
    print(f"results: {out}")
    if args.search_log is not None:
        write_search_log(args.search_log, rows)
        print(f"search log: {args.search_log}")
    if args.update_baseline:
        if report.docs_failed:
            raise SystemExit(
                f"refusing to update baseline: failed={report.docs_failed} "
                f"{report.failed_by_stage} — a run with ingest failures is not a baseline; "
                f"see {out}"
            )
        update_baseline(
            baseline_out,
            tier="kb",
            benchmark=baseline_benchmark(dataset.name, cfg.rerank_mode),
            section=baseline_section(summary),
            fingerprint=fingerprint(
                dataset_path=args.dataset,
                manifest_path=manifest,
                corpus_root=corpus_root,
                embedder=bundle.tag,
                embed_base_url=bundle.base_url,
                query_marker=bundle.query_marker,
                store=args.store,
                tiers=tiers,
                limit_docs=args.limit_docs,
                cfg=cfg,
                ann=args.ann,
            ),
        )
        print(f"baseline updated: {baseline_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
