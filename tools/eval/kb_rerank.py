"""评测里的生产同款重排 + 结果缓存(B-175 波 2 PR0)。

生产知识库重排用 DashScope 原生接口的 ``qwen3-vl-rerank``(测试、生产都是它);评测原来的
``--rerank on`` 走聊天模型排序(``LLMReranker``),量的不是生产那条路。这里用生产同一个
``DashScopeReranker`` / ``HTTPDashScopeRerankClient``,外面套一层 sqlite 缓存:

* 键 = (模型, 查询, 候选全文按序, ``top_n``, 有就带任务说明) 的 sha256 —— 任一变化都是新请求,
  候选顺序变了也算(B-206 K-14 各对照组的送审文本 / 候选数 / 任务说明不同,不会互相串分数);
* 值 = 接口返回的整个 JSON;复跑同样的候选不再花钱,结果也逐条一致(评测可复现,见 #1811);
* 密钥为 :data:`OFFLINE_KEY` 时不建网络客户端,缓存没命中直接 :class:`RerankCacheMissError`
  —— 与嵌入缓存的零成本复跑同一个约定。检索器把重排出错降级成融合序(PR3),``kb_eval``
  见到降级整轮退出(``RerankDegradedError``),这个约定才不被吞掉。

计费:``qwen3-vl-rerank`` 文本输入 ¥0.5 / 百万 token(百炼控制台,2026-10-14);按接口返回的
``usage.total_tokens`` 累计,只算真发出去的请求。

B-206 K-7b 换模型对照:``--rerank-model``(:func:`resolve_rerank_model`,只收模型目录里的
重排模型)> 环境变量 ``EXPERT_WORK_EVAL_RERANK_MODEL`` > 生产同款。模型在缓存键里,各组不串。
纯文本重排模型(``qwen3-rerank`` / ``qwen3.7-text-rerank``)同样 ¥0.5 / 百万 token;2026-10-22
探针:同一请求计费 token 比 ``qwen3-vl-rerank`` 少(每篇约少 50),``usage.total_tokens`` 三个都有。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from expert_work.protocol import catalog_entry
from orchestrator.llm.rerank import DashScopeReranker, HTTPDashScopeRerankClient, RerankClient
from orchestrator.llm.rerank_guard import RerankGuards

#: 生产知识库重排模型(测试 / 生产平台配置均为它,10-14 核对)。
DEFAULT_RERANK_MODEL = "qwen3-vl-rerank"
RERANK_KEY_ENV = "EXPERT_WORK_EVAL_EMBED_API_KEY"  # 同一个 DashScope key
RERANK_MODEL_ENV = "EXPERT_WORK_EVAL_RERANK_MODEL"
RERANK_PRICE_ENV = "EXPERT_WORK_EVAL_RERANK_PRICE_CNY_PER_M"
DEFAULT_RERANK_PRICE_CNY_PER_M = 0.5
#: 与嵌入缓存同一个假密钥约定:只读缓存,未命中即报错。
OFFLINE_KEY = "offline-cache-only"
#: DashScope 的模型在模型目录里记在这个厂商下。
_CATALOG_PROVIDER = "qwen"


class RerankCacheMissError(RuntimeError):
    """Offline mode and the rerank cache has no entry for this request."""


@dataclass
class CachedRerankClient:
    """:class:`RerankClient` that answers from a sqlite cache before calling ``inner``."""

    inner: RerankClient | None
    path: Path
    price_cny_per_m: float = DEFAULT_RERANK_PRICE_CNY_PER_M
    _calls: int = field(default=0, init=False)
    _hits: int = field(default=0, init=False)
    _tokens: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS rerank_cache (key TEXT PRIMARY KEY, body TEXT)"
            )

    async def rerank(
        self,
        *,
        model: str,
        query: str,
        documents: Sequence[str],
        top_n: int,
        instruction: str | None = None,
    ) -> Mapping[str, Any]:
        self._calls += 1
        key = _cache_key(
            model=model, query=query, documents=documents, top_n=top_n, instruction=instruction
        )
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("SELECT body FROM rerank_cache WHERE key = ?", (key,)).fetchone()
        if row is not None:
            self._hits += 1
            cached: Mapping[str, Any] = json.loads(row[0])
            return cached
        if self.inner is None:
            msg = f"rerank cache miss in offline mode (model={model!r}, query={query[:40]!r})"
            raise RerankCacheMissError(msg)
        extra: dict[str, Any] = {} if instruction is None else {"instruction": instruction}
        body = await self.inner.rerank(
            model=model, query=query, documents=documents, top_n=top_n, **extra
        )
        usage = body.get("usage")
        if isinstance(usage, Mapping) and isinstance(usage.get("total_tokens"), int):
            self._tokens += usage["total_tokens"]
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO rerank_cache (key, body) VALUES (?, ?)",
                (key, json.dumps(body, ensure_ascii=False)),
            )
        return body

    def stats(self) -> dict[str, float | int]:
        return {
            "calls": self._calls,
            "cache_hits": self._hits,
            "tokens": self._tokens,
            "cost_cny": round(self._tokens / 1_000_000 * self.price_cny_per_m, 6),
        }


def _cache_key(
    *, model: str, query: str, documents: Sequence[str], top_n: int, instruction: str | None
) -> str:
    fields: dict[str, Any] = {
        "model": model,
        "query": query,
        "documents": list(documents),
        "top_n": top_n,
    }
    if instruction is not None:
        # 不带任务说明的请求键与 K-14 之前逐字相同:旧缓存照样命中。
        fields["instruction"] = instruction
    payload = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_rerank_model(requested: str | None, *, rerank_mode: str) -> str | None:
    """这次跑用哪个重排模型(B-206 K-7b):``--rerank-model`` > 环境变量 > 生产同款;
    只有 ``--rerank dashscope`` 有模型可选(其它取法回 ``None``,给了 ``--rerank-model`` 就拒)。
    不在模型目录里的拒掉:没有目录的 ``rpm`` / 单次上限,请求既不限速也不按上限截。"""
    if rerank_mode != "dashscope":
        if requested is not None:
            raise SystemExit("--rerank-model needs --rerank dashscope (the native rerank models)")
        return None
    model = requested or os.environ.get(RERANK_MODEL_ENV) or DEFAULT_RERANK_MODEL
    entry = catalog_entry(_CATALOG_PROVIDER, model)
    if entry is None or not entry.rerank:
        raise SystemExit(f"{model!r} is not a rerank model in the model catalog")
    return model


def rerank_outcome_counts(outcomes: Iterable[str | None]) -> dict[str, int]:
    """每种 ``rerank_outcome`` 的检索次数(没要重排的不计)。``partial`` 只计数不退出:
    整轮只在 ``rerank_degraded`` 时退出。"""
    return dict(sorted(Counter(o for o in outcomes if o is not None).items()))


def build_dashscope_reranker(
    *, cache_path: Path, model: str | None = None
) -> tuple[DashScopeReranker, CachedRerankClient]:
    """``--rerank dashscope``:生产同款重排器 + 缓存客户端(后者用于汇总里的调用 / 费用统计)。
    ``model`` 不给 = 环境变量 / 生产同款。"""
    key = os.environ.get(RERANK_KEY_ENV)
    if not key:
        raise SystemExit(f"--rerank dashscope requires env {RERANK_KEY_ENV} (the DashScope key)")
    model = model or os.environ.get(RERANK_MODEL_ENV) or DEFAULT_RERANK_MODEL
    # B-206 K-7a —— 网络客户端套生产同款限流 / 退避(按目录 rpm 匀速、429 重试),
    # 套在缓存里面:命中缓存不占令牌、不等待。
    inner = (
        None
        if key == OFFLINE_KEY
        else RerankGuards().wrap(
            HTTPDashScopeRerankClient(api_key=key),
            provider="qwen",
            model=model,
            secret_ref=RERANK_KEY_ENV,
        )
    )
    price = float(os.environ.get(RERANK_PRICE_ENV, DEFAULT_RERANK_PRICE_CNY_PER_M))
    client = CachedRerankClient(inner=inner, path=cache_path, price_cny_per_m=price)
    return DashScopeReranker(client=client, model=model), client


__all__ = [
    "DEFAULT_RERANK_MODEL",
    "OFFLINE_KEY",
    "CachedRerankClient",
    "RerankCacheMissError",
    "build_dashscope_reranker",
    "rerank_outcome_counts",
    "resolve_rerank_model",
]
