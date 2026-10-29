#!/usr/bin/env python
"""知识库智能体层评测(B-206 批 0)—— 真模型答题 + 真模型判分,**不进 CI**。

由 B-175 波 2 PR3 那次 A/B 脚本改成可复用的单臂版:

* 检索走生产同一条路:``kb_eval.build_corpus`` 真入库链路建库(默认 SQL 库 + 真嵌入),库开着
  重排(控制面建库的默认),重排用生产同款 ``qwen3-vl-rerank``(带缓存);
* 工具就是当前主干的 ``knowledge_search``:说明 / 参数取自工具自己的 ``spec``,结果经工具节点
  同一段代码交给模型(``kb_eval.tool_message_text``:防注入围栏 + 围栏外的提示);
* 答题模型每题最多搜 3 次;判分模型给每个回答出 ``refused`` / ``fabricated`` /
  ``matches_reference``,归成 答对 / 编造 / 正确说没写 / 误拒(另有 答偏 / 其它 / 判分失败)。
* B-206 K-2 起, 生产里有知识库的智能体还有 ``knowledge_read``:默认一起给模型(每题最多读 3 次,
  不占检索次数;模型最多回 8 轮), 读到的内容记进结果行的 ``reads``、也给判分看;
  ``--no-read`` = 不给 ``knowledge_read``(读文档关掉)。它**不是**改动前的基线:检索结果标头、
  工具说明、模型最多回几轮都已是 K-2 之后的;改动前那一臂要在改动前的检出上用当时的脚本跑。

选题:主集与真实问法集各取全部「资料没写」题 + 固定种子各抽 15 道有答案的题(与 B-175 PR3
同一批 60 题:30 + 30)。判分时给的「含答案的完整段落」= 期望文档里含锚文本的段,建库后从库里取。

用法(仓库根目录)::

    # 密钥由操作者 source 自己的 env 文件后映射成下面的变量;脚本只读变量、不打印值。
    export EXPERT_WORK_EVAL_ANSWER_BASE_URL=https://open.bigmodel.cn/api/paas/v4
    export EXPERT_WORK_EVAL_ANSWER_API_KEY=...
    export EXPERT_WORK_EVAL_JUDGE_BASE_URL=https://api.deepseek.com
    export EXPERT_WORK_EVAL_JUDGE_API_KEY=...
    export EXPERT_WORK_EVAL_EMBED_API_KEY=... EXPERT_WORK_EVAL_EMBED_MODEL=...  # 同 kb_eval
    DOCKER_HOST=... uv run --no-sync python tools/eval/kb_agent_eval.py \\
        --dataset tools/eval/datasets/kb/<主集>.yaml \\
        --chat-dataset tools/eval/datasets/kb/<真实问法集>.yaml \\
        --corpus-root <资料根目录> --out eval-out/kb/agent-<label>.jsonl

    # 只出统计(不建库、不调模型)
    uv run --no-sync python tools/eval/kb_agent_eval.py --tally eval-out/kb/agent-<label>.jsonl

    # 空跑:两个模型换成脚本化的假模型,嵌入 / 重排只读缓存,一分钱不花、不连模型接口
    EXPERT_WORK_EVAL_EMBED_API_KEY=offline-cache-only uv run --no-sync python \\
        tools/eval/kb_agent_eval.py ... --dry

B-206 K-14 对照组:``--rerank-view dual|single|single_clean`` / ``--rerank-pool N`` /
``--rerank-instruct`` / ``--rerank-model``(与 ``kb_eval.py`` 同义,默认 = 生产默认 dual + 100、
生产同款模型;``--rerank-instruct`` 只能配单视图);每组写自己的 ``--out``,
组别记在结果文件末尾的 run 行里。

``--answer-model`` / ``--judge-model`` 换模型(默认 glm-5.3 / deepseek-v4-pro);不在
:data:`PRICES` 里的模型要配 ``--answer-price`` / ``--judge-price``
(元 / 百万 token:输入,缓存命中,输出)。

花费:一轮 60 题约 ¥15(glm-5.3 答题 + deepseek-v4-pro 判分 + 重排;判分按提示词缓存在 sqlite,
同样的回答复跑不再花钱)。带 ``knowledge_read`` 的一轮更贵:每次读最多 ``max_tokens``(默认 4,000)
token 进上下文、之后每轮都重发, 判分也要看, 约 ¥15~25。
DeepSeek 闲时(北京时间工作日 9-12 点、14-18 点以外,周末全天)半价,按每次调用的时刻折算。
``--budget`` 到了**不退出**:不再开新题,在跑的题跑完照常落盘,然后暂停、库留着,
等操作者往 ``<out>.budget`` 写新上限(元)继续,或写 ``stop`` 收尾。结果 JSONL 每题一行、追加写,
同一个 ``--out`` 重跑只补没做完的题。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))

import httpx
from kb_dataset import KbCase, KbDataset, load_dataset  # type: ignore[import-not-found]
from kb_eval import (  # type: ignore[import-not-found]
    EVAL_OUT_DIR,
    EVAL_TENANT_ID,
    INSTRUCT_NEEDS_SINGLE_VIEW,
    _all_chunk_texts,
    _resolve_manifest,
    _resolve_tiers,
    build_corpus,
    build_embedder,
    build_rerank_mode,
    build_retriever,
    open_store,
    read_manifest,
    rls_tenant,
    select_rows,
    tool_message_text,
)
from kb_metrics import anchor_in  # type: ignore[import-not-found]
from kb_rerank import (  # type: ignore[import-not-found]
    OFFLINE_KEY,
    RERANK_KEY_ENV,
    resolve_rerank_model,
)

from expert_work.common.spotlight import SPOTLIGHT_SYSTEM_CLAUSE
from expert_work.persistence import KnowledgeStore
from expert_work.protocol import DocumentStatus, rerank_model_key
from orchestrator.llm.providers.openai import _to_openai_tool
from orchestrator.tools.knowledge import (
    DEFAULT_RERANK_POOL,
    DEFAULT_RERANK_VIEW,
    RERANK_VIEWS,
    KnowledgeSearchTool,
)
from orchestrator.tools.knowledge_read import KnowledgeReadTool
from orchestrator.tools.registry import ToolContext, ToolResult, ToolSpec

AGENT_SYSTEM = (
    "你是一家公司的客服助手。回答客户的问题时,先用 knowledge_search 查公司知识库,再回答。"
)
MAX_SEARCHES = 3
#: B-206 K-2: knowledge_read 单独计数, 不占检索次数。
MAX_READS = 3
#: 模型最多回几轮(一轮可并发多个工具调用);B-206 K-2 起 6 -> 8, 容得下 3 次检索 + 3 次读文档。
MAX_CALLS = 8
SEED = 20261018
N_ANSWERABLE_PER_SET = 15
#: 判分时每题最多给几段「含答案的完整段落」。
GOLD_PASSAGES = 2
SEARCH_LIMIT_NOTE = "[search limit reached in this evaluation; answer with what you have]"
READ_LIMIT_NOTE = "[read limit reached in this evaluation; answer with what you have]"
READ_TOOL = "knowledge_read"

DEFAULT_ANSWER_MODEL = "glm-5.3"
DEFAULT_JUDGE_MODEL = "deepseek-v4-pro"
ANSWER_ENV = ("EXPERT_WORK_EVAL_ANSWER_BASE_URL", "EXPERT_WORK_EVAL_ANSWER_API_KEY")
JUDGE_ENV = ("EXPERT_WORK_EVAL_JUDGE_BASE_URL", "EXPERT_WORK_EVAL_JUDGE_API_KEY")
DEFAULT_BUDGET_CNY = 20.0
#: 暂停时多久看一次 ``<out>.budget``(秒)。
BUDGET_POLL_S = 5.0
JUDGE_CACHE_DB = EVAL_OUT_DIR / "agent_judge_cache.sqlite"
#: ``--dry`` 的结果行记这个模型名,与真跑的行混不到一个 ``--out`` 里。
DRY_MODEL = "dry"

JUDGE_SYSTEM = "你是严格、细致的评审。只输出一个 JSON 对象,不要输出别的文字。"
JUDGE_TEMPLATE = """评审一个客服智能体的回答。智能体只能通过知识库检索获得公司资料,下面给出它检索到的全部段落。

【用户问题】
{question}

【智能体检索到的段落】(可能为空)
{passages}

【参考信息】
{reference}

【智能体的回答】
{answer}

请判断并输出 JSON,字段:
- "refused":布尔。回答是否明确表示知识库 / 资料里没有写这个问题要的具体信息(或没查到)。只说「资料里没写 X」但同时给出了 X 的具体答案的,仍算 true,另由 fabricated 判断。
- "fabricated":布尔。回答是否把上面段落里没有的具体信息当作事实说出来(例如产品是否有某认证、价格、编码、成分、功效、用法、公司政策等),且没有说明这不是来自资料。依据段落能直接推出的不算;明确说明「资料没写、以下仅是一般常识」的泛泛建议不算。
- "matches_reference":"yes" / "partial" / "no" / "n/a"。回答是否给出了参考答案的要点;参考信息写着「本题资料里没有答案」时填 "n/a"。
- "reason":一句话中文理由,点出关键依据。
"""  # noqa: E501

CATEGORIES_NO_ANSWER = ("正确说没写", "编造", "其它", "判分失败")
CATEGORIES_ANSWERABLE = ("答对", "误拒", "编造", "答偏", "判分失败")

_JSON_RE = re.compile(r"\{.*\}", re.S)
_BEIJING = ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------------------
# 计费
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Price:
    """元 / 百万 token,价目表上的(忙时)价。"""

    input: float
    cached: float
    output: float


PRICES: dict[str, Price] = {
    "glm-5.3": Price(input=8.0, cached=2.0, output=28.0),
    "deepseek-v4-pro": Price(input=9.0, cached=0.3, output=27.0),
}


def is_deepseek_offpeak(at: datetime) -> bool:
    """DeepSeek 忙时 = 北京时间工作日 9-12 点、14-18 点;其余时间(含周末全天)半价。"""
    local = at.astimezone(_BEIJING)
    if local.weekday() >= 5:
        return True
    return not (9 <= local.hour < 12 or 14 <= local.hour < 18)


def call_cost(usage: Mapping[str, Any], price: Price, *, model: str, at: datetime) -> float:
    """一次调用的花费(元);DeepSeek 的模型在闲时按半价。"""
    prompt = int(usage.get("prompt_tokens") or 0)
    cached = int(
        (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        or usage.get("prompt_cache_hit_tokens")
        or 0
    )
    out = int(usage.get("completion_tokens") or 0)
    cny = ((prompt - cached) * price.input + cached * price.cached + out * price.output) / 1e6
    return cny / 2 if model.startswith("deepseek") and is_deepseek_offpeak(at) else cny


def parse_price(raw: str) -> Price:
    try:
        values = [float(v) for v in raw.split(",")]
    except ValueError:
        values = []
    if len(values) != 3:
        raise SystemExit(f"price must be 'input,cached,output' CNY per million tokens: {raw!r}")
    return Price(*values)


def resolve_price(model: str, raw: str | None) -> Price:
    if raw is not None:
        return parse_price(raw)
    if model not in PRICES:
        raise SystemExit(f"no price for model {model!r}: pass --answer-price / --judge-price")
    return PRICES[model]


@dataclass
class Spend:
    answer_cny: float = 0.0
    judge_cny: float = 0.0
    answer_calls: int = 0
    judge_calls: int = 0


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------


class Models(Protocol):
    answer_model: str
    judge_model: str

    async def agent(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], float]:
        """One answer-model turn: the assistant message and what it cost (CNY)."""

    async def judge(self, prompt: str) -> tuple[str, float]:
        """The judge's raw reply and what it cost (CNY)."""


async def _post(
    client: httpx.AsyncClient, url: str, key: str, body: Mapping[str, Any]
) -> dict[str, Any]:
    for attempt in range(5):
        try:
            resp = await client.post(
                url, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=180.0
            )
        except httpx.TransportError:
            if attempt == 4:
                raise
            await asyncio.sleep(2 * (attempt + 1))
            continue
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 4:
            await asyncio.sleep(3 * (attempt + 1))
            continue
        resp.raise_for_status()
        return cast(dict[str, Any], resp.json())
    raise RuntimeError("unreachable")


@dataclass(frozen=True)
class ChatClient:
    """OpenAI 兼容的 ``/chat/completions``,按回包的 ``usage`` 与调用时刻记账。"""

    model: str
    base_url: str
    api_key: str
    price: Price
    http: httpx.AsyncClient

    async def complete(self, body: Mapping[str, Any]) -> tuple[dict[str, Any], float]:
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        data = await _post(self.http, url, self.api_key, {"model": self.model, **body})
        cost = call_cost(
            data.get("usage") or {}, self.price, model=self.model, at=datetime.now(UTC)
        )
        return cast(dict[str, Any], data["choices"][0]["message"]), cost


@dataclass(frozen=True)
class RealModels:
    answer_client: ChatClient
    judge_client: ChatClient

    @property
    def answer_model(self) -> str:
        return self.answer_client.model

    @property
    def judge_model(self) -> str:
        return self.judge_client.model

    async def agent(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], float]:
        # 厂商默认的思考设置,与 B-175 PR3 那次一样不另传。
        return await self.answer_client.complete({"messages": messages, "tools": tools})

    async def judge(self, prompt: str) -> tuple[str, float]:
        message, cost = await self.judge_client.complete(
            {
                "messages": [
                    {"role": "system", "content": JUDGE_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
            }
        )
        return str(message.get("content") or ""), cost


class FakeModels:
    """``--dry``:用题目原文搜一次;给了 knowledge_read 就再读一次检索结果里第一个
    ``[D<n>#<k>]``(B-206 K-2);然后回一句占位。判分固定。不花钱、不连网。"""

    answer_model = DRY_MODEL
    judge_model = DRY_MODEL

    async def agent(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], float]:
        results = [str(m["content"]) for m in messages if m["role"] == "tool"]
        if not results:
            question = messages[-1]["content"]
            return _dry_call(tools[0]["function"]["name"], {"query": question}), 0.0
        offered = {t["function"]["name"] for t in tools}
        reference = _DRY_REFERENCE.search(results[0])
        if READ_TOOL in offered and len(results) == 1 and reference is not None:
            return _dry_call(READ_TOOL, {"document": reference.group(1)}), 0.0
        return {"role": "assistant", "content": "(空跑)"}, 0.0

    async def judge(self, prompt: str) -> tuple[str, float]:
        verdict = {"refused": False, "fabricated": False, "matches_reference": "n/a"}
        return json.dumps({**verdict, "reason": "dry"}), 0.0


_DRY_REFERENCE = re.compile(r"\[(D[0-9]+#[0-9]+)\]")


def _dry_call(name: str, args: Mapping[str, Any]) -> dict[str, Any]:
    call = {
        "id": f"call_dry_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }
    return {"role": "assistant", "content": "", "tool_calls": [call]}


# ---------------------------------------------------------------------------
# 答题
# ---------------------------------------------------------------------------


class Tool(Protocol):
    """``knowledge_search`` / ``knowledge_read``(或测试替身)。"""

    @property
    def spec(self) -> ToolSpec: ...

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult: ...


@dataclass
class _RecordingTool:
    """把工具的 ``ToolResult`` 留一份(判分与结果文件用);模型看到的仍是工具节点渲染的那段。"""

    tool: Tool
    results: list[ToolResult] = field(default_factory=list)

    @property
    def spec(self) -> ToolSpec:
        return self.tool.spec

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        result = await self.tool.call(args, ctx=ctx)
        self.results.append(result)
        return result


def _call_args(call: Mapping[str, Any]) -> dict[str, Any]:
    try:
        args = json.loads(call["function"]["arguments"] or "{}")
    except json.JSONDecodeError:
        args = {}
    return args if isinstance(args, dict) else {}


async def _search(
    recorder: _RecordingTool, call: Mapping[str, Any], *, done: int, tenant_id: UUID
) -> tuple[str, dict[str, Any]]:
    """一次工具调用:``(给模型的文本, 记进结果文件的这次检索)``。"""
    args = _call_args(call)
    if done >= MAX_SEARCHES:
        return SEARCH_LIMIT_NOTE, {"query": args.get("query"), "skipped": True}
    before = len(recorder.results)
    rendered = await tool_message_text(
        recorder, args, tenant_id=tenant_id, call_id=str(call.get("id") or "call")
    )
    if len(recorder.results) == before:  # 参数不合格 / 工具报错:模型看到的就是那条报错
        return rendered, {"query": args.get("query"), "error": rendered}
    result = recorder.results[-1]
    return rendered, {
        "query": args.get("query"),
        "limit": args.get("limit"),
        "status": result.meta.get("status"),
        "degraded": bool(result.meta.get("rerank_degraded")),
        # B-206 K-8: the query was not embedded, this search ran on keywords alone.
        "embedding_degraded": bool(result.meta.get("embedding_degraded")),
        # B-206 K-22: the keyword path failed, this search ran on vectors alone.
        "keyword_degraded": bool(result.meta.get("keyword_degraded")),
        # B-206 K-7: how the rerank ended. The eval's base always asks for rerank, so a
        # result without the key ended ``ok``; K-7b: the base's threshold left out
        # (set under another model).
        "rerank_outcome": result.meta.get("rerank_outcome", "ok"),
        "rerank_threshold_paused": bool(result.meta.get("rerank_threshold_paused")),
        "content": result.content,
    }


async def _read(
    recorder: _RecordingTool, call: Mapping[str, Any], *, done: int, tenant_id: UUID
) -> tuple[str, dict[str, Any]]:
    """一次 knowledge_read(B-206 K-2):``(给模型的文本, 记进结果文件的这次读取)``。"""
    args = _call_args(call)
    if done >= MAX_READS:
        return READ_LIMIT_NOTE, {"args": args, "skipped": True}
    before = len(recorder.results)
    rendered = await tool_message_text(
        recorder, args, tenant_id=tenant_id, call_id=str(call.get("id") or "call")
    )
    if len(recorder.results) == before:
        return rendered, {"args": args, "error": rendered}
    meta = recorder.results[-1].meta
    return rendered, {
        "args": args,
        "status": meta.get("status"),
        "mode": meta.get("mode"),
        "page": meta.get("page"),
        "pages": meta.get("pages"),
        "passages": len(meta.get("passages") or ()),
        "content": recorder.results[-1].content,
    }


async def run_agent(
    models: Models,
    tool: Tool,
    *,
    question: str,
    tenant_id: UUID,
    spend: Spend,
    reader: Tool | None = None,
) -> tuple[dict[str, Any], float]:
    """答一道题:``({answer, searches, reads}, 答题花费)``。``reader``(B-206 K-2)给了就把
    knowledge_read 一起给模型:调用名是它的走它、记进 ``reads``, 其余都当检索(与之前一样)。
    两种各有各的次数上限。"""
    recorder = _RecordingTool(tool)
    reading = _RecordingTool(reader) if reader is not None else None
    tools = [_to_openai_tool(tool.spec)]
    if reader is not None:
        tools.append(_to_openai_tool(reader.spec))
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": AGENT_SYSTEM + "\n\n" + SPOTLIGHT_SYSTEM_CLAUSE},
        {"role": "user", "content": question},
    ]
    searches: list[dict[str, Any]] = []
    reads: list[dict[str, Any]] = []
    answer = ""
    cost = 0.0
    for _ in range(MAX_CALLS):
        message, call_cny = await models.agent(messages, tools)
        cost += call_cny
        spend.answer_cny += call_cny
        spend.answer_calls += 1
        calls = message.get("tool_calls") or []
        if not calls:
            answer = message.get("content") or ""
            break
        messages.append(
            {"role": "assistant", "content": message.get("content") or "", "tool_calls": calls}
        )
        for call in calls:
            if reading is not None and call["function"]["name"] == READ_TOOL:
                rendered, record = await _read(reading, call, done=len(reads), tenant_id=tenant_id)
                reads.append(record)
            else:
                rendered, record = await _search(
                    recorder, call, done=len(searches), tenant_id=tenant_id
                )
                searches.append(record)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": rendered})
    return {"answer": answer, "searches": searches, "reads": reads}, cost


# ---------------------------------------------------------------------------
# 选题 / 判分
# ---------------------------------------------------------------------------


def select_cases(main: KbDataset, chat: KbDataset) -> list[tuple[str, KbCase]]:
    """两个集各取全部无答案题 + 固定种子抽 :data:`N_ANSWERABLE_PER_SET` 道有答案题。"""
    rng = random.Random(SEED)  # noqa: S311 - a fixed, reproducible sample
    picked: list[tuple[str, KbCase]] = []
    for label, dataset in (("main", main), ("chat", chat)):
        no_answer = [c for c in dataset.cases if not c.expected]
        answerable = [c for c in dataset.cases if c.expected]
        picked += [(label, c) for c in no_answer]
        picked += [(label, c) for c in rng.sample(answerable, N_ANSWERABLE_PER_SET)]
    return picked


async def gold_passages(
    store: KnowledgeStore, *, tenant_id: UUID, kb_id: UUID, cases: Sequence[KbCase]
) -> dict[str, list[str]]:
    """每道有答案题给判分的「含答案的完整段落」:期望文档里第一段含锚文本的段,最多
    :data:`GOLD_PASSAGES` 段。"""
    docs = {
        d.filename: d
        for d in await store.list_documents(tenant_id=tenant_id, kb_id=kb_id)
        if d.status is DocumentStatus.READY
    }
    chunks: dict[str, list[str]] = {}
    gold: dict[str, list[str]] = {}
    for case in cases:
        found: list[str] = []
        for exp in case.expected:
            if exp.document not in docs:
                continue
            if exp.document not in chunks:
                chunks[exp.document] = await _all_chunk_texts(
                    store, tenant_id=tenant_id, document_id=docs[exp.document].id
                )
            text = next((t for t in chunks[exp.document] if anchor_in(exp.anchor, t)), None)
            if text is not None and text not in found:
                found.append(text)
        gold[case.id] = found[:GOLD_PASSAGES]
    return gold


def judge_prompt(case: KbCase, run: Mapping[str, Any], gold: Mapping[str, list[str]]) -> str:
    # B-206 K-2: 读文档拿到的也是智能体看过的资料;没有读文档时提示词与之前逐字相同(判分缓存照用)。
    seen = [*run["searches"], *run.get("reads", ())]
    passages = (
        "\n\n=====\n\n".join(s["content"] for s in seen if s.get("content")) or "(没有检索到段落)"
    )
    if case.expected:
        anchors = "\n".join(f"- 《{e.document}》中的原文片段:{e.anchor}" for e in case.expected[:3])
        passages_gold = "\n\n-----\n\n".join(gold.get(case.id, [])) or "(未取到)"
        reference = (
            f"参考答案:{case.answer or '(无文字答案,以下面的资料原文为准)'}\n"
            f"正确答案所在的资料原文片段:\n{anchors}\n含答案的完整段落:\n{passages_gold}"
        )
    else:
        reference = "本题资料里没有答案(公司知识库没有写这个问题要的具体信息)。"
    return JUDGE_TEMPLATE.format(
        question=case.query, passages=passages, reference=reference, answer=run["answer"] or "(空)"
    )


class JudgeCache:
    """判分结果按(判分模型, 提示词)缓存;同样的回答复跑不再花钱。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS j (k TEXT PRIMARY KEY, v TEXT)")

    def get(self, model: str, prompt: str) -> str | None:
        row = self.db.execute("SELECT v FROM j WHERE k = ?", (self._key(model, prompt),)).fetchone()
        return cast(str, row[0]) if row else None

    def put(self, model: str, prompt: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO j VALUES (?, ?)", (self._key(model, prompt), value))
        self.db.commit()

    @staticmethod
    def _key(model: str, prompt: str) -> str:
        return hashlib.sha256(f"{model}\0{prompt}".encode()).hexdigest()


def parse_verdict(raw: str) -> dict[str, Any]:
    match = _JSON_RE.search(raw)
    if not match:
        return {"parse_error": raw[:200]}
    try:
        return cast(dict[str, Any], json.loads(match.group(0)))
    except json.JSONDecodeError:
        return {"parse_error": raw[:200]}


def category(answerable: bool, verdict: Mapping[str, Any]) -> str:
    if "parse_error" in verdict:
        return "判分失败"
    if verdict.get("fabricated"):
        return "编造"
    if answerable:
        if verdict.get("matches_reference") in ("yes", "partial"):
            return "答对"
        if verdict.get("refused"):
            return "误拒"
        return "答偏"
    return "正确说没写" if verdict.get("refused") else "其它"


# ---------------------------------------------------------------------------
# 预算 / 跑题
# ---------------------------------------------------------------------------


@dataclass
class BudgetGate:
    """``--budget``:花到上限就不再开新题;暂停时库与已落盘的结果都留着,等操作者往
    ``control`` 写新上限(元)继续,或写 ``stop`` 收尾。"""

    budget: float
    control: Path
    poll_s: float = BUDGET_POLL_S

    async def wait_for_raise(self, spent: Callable[[], float]) -> bool:
        """暂停到上限被调高(True)或收到 ``stop``(False)。"""
        print(
            f"PAUSED: spent ¥{spent():.2f} >= budget ¥{self.budget:.2f}. Write a new budget "
            f"(CNY) into {self.control} to continue, or 'stop' to finish; finished questions "
            "are kept and a rerun with the same --out resumes.",
            file=sys.stderr,
        )
        while True:
            raw = self._take()
            if raw == "stop":
                return False
            if raw is not None:
                try:
                    value = float(raw)
                except ValueError:
                    value = None
                if value is not None and value > spent():
                    self.budget = value
                    print(f"RESUMED: budget ¥{value:.2f}", file=sys.stderr)
                    return True
                print(
                    f"ignored {raw!r} in {self.control}: need a number above ¥{spent():.2f} "
                    "or 'stop'",
                    file=sys.stderr,
                )
            await asyncio.sleep(self.poll_s)

    def _take(self) -> str | None:
        try:
            raw = self.control.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        if not raw:  # 操作者的 ``echo 30 > …`` 还没写完,下一轮再读
            return None
        self.control.unlink(missing_ok=True)
        return raw


async def run_questions(
    *,
    todo: Sequence[tuple[str, KbCase]],
    models: Models,
    tool: Tool,
    tenant_id: UUID,
    gold: Mapping[str, list[str]],
    cache: JudgeCache,
    out: Path,
    gate: BudgetGate,
    spend: Spend,
    other_cost: Callable[[], float] = lambda: 0.0,
    concurrency: int = 4,
    cache_verdicts: bool = True,
    reader: Tool | None = None,
) -> int:
    """答题 + 判分,每题一行追加进 ``out``;返回写了几行。``other_cost`` 是计入预算的其它花费
    (重排)。"""
    sem = asyncio.Semaphore(max(1, concurrency))
    gate_lock = asyncio.Lock()
    write_lock = asyncio.Lock()
    stop = asyncio.Event()
    written = 0

    def total() -> float:
        return spend.answer_cny + spend.judge_cny + other_cost()

    async def admit() -> bool:
        async with gate_lock:
            while not stop.is_set() and total() >= gate.budget:
                if not await gate.wait_for_raise(total):
                    stop.set()
            return not stop.is_set()

    async def one(label: str, case: KbCase) -> None:
        nonlocal written
        async with sem:
            if not await admit():
                return
            run, answer_cny = await run_agent(
                models, tool, question=case.query, tenant_id=tenant_id, spend=spend, reader=reader
            )
            prompt = judge_prompt(case, run, gold)
            raw = cache.get(models.judge_model, prompt)
            judge_cny = 0.0
            if raw is None:
                raw, judge_cny = await models.judge(prompt)
                spend.judge_cny += judge_cny
                spend.judge_calls += 1
                if cache_verdicts:
                    cache.put(models.judge_model, prompt, raw)
            verdict = parse_verdict(raw)
            row = {
                "case_id": case.id,
                "set": label,
                "answerable": bool(case.expected),
                "question": case.query,
                "answer": run["answer"],
                "searches": run["searches"],
                "reads": run["reads"],
                "verdict": verdict,
                "category": category(bool(case.expected), verdict),
                "answer_model": models.answer_model,
                "judge_model": models.judge_model,
                "cost_cny": {"answer": round(answer_cny, 6), "judge": round(judge_cny, 6)},
            }
            async with write_lock:
                with out.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                written += 1
                print(f"{case.id} -> {row['category']} | spent ¥{total():.2f}", file=sys.stderr)

    await asyncio.gather(*(one(label, case) for label, case in todo))
    return written


# ---------------------------------------------------------------------------
# 结果文件 / 统计
# ---------------------------------------------------------------------------


def read_results(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(每题一行, 每次运行末尾的 run 行)``;文件不存在时两个空列表。"""
    rows: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    if not path.exists():
        return rows, runs
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if "case_id" in item:
            rows.append(item)
        elif "run" in item:
            runs.append(item["run"])
    return rows, runs


def done_case_ids(path: Path, *, answer_model: str, judge_model: str) -> set[str]:
    """已做完的题;文件里是别的模型答 / 判的就拒绝续写(混在一起的统计没有意义)。"""
    rows, _runs = read_results(path)
    for row in rows:
        if (row.get("answer_model"), row.get("judge_model")) != (answer_model, judge_model):
            raise SystemExit(
                f"{path} was written with answer model {row.get('answer_model')!r} / judge "
                f"model {row.get('judge_model')!r}; use another --out for "
                f"{answer_model!r} / {judge_model!r}"
            )
    return {row["case_id"] for row in rows}


def tally(path: Path) -> str:
    """「资料没写」「有答案」两组,各按全部 / 主集 / 真实问法集数各类判定;外加总数、检索次数
    与花费。"""
    rows, runs = read_results(path)
    lines: list[str] = []
    for answerable, cats in ((False, CATEGORIES_NO_ANSWER), (True, CATEGORIES_ANSWERABLE)):
        lines.append("## 有答案" if answerable else "## 资料没写")
        for scope in ("all", "main", "chat"):
            sel = [r for r in rows if r["answerable"] == answerable and scope in ("all", r["set"])]
            counts = Counter(r["category"] for r in sel)
            lines.append(f"  {scope:4} n={len(sel)} " + " ".join(f"{k} {counts[k]}" for k in cats))
    total = Counter(r["category"] for r in rows)
    lines.append(f"total n={len(rows)} " + " ".join(f"{k} {v}" for k, v in sorted(total.items())))
    searches = [s for r in rows for s in r["searches"]]
    # B-206 K-2: 读文档单独统计(之前的结果行没有 reads)。
    reads = [s for r in rows for s in r.get("reads", ())]
    lines.append(
        f"searches per question {len(searches) / max(1, len(rows)):.2f}; "
        f"degraded {sum(1 for s in searches if s.get('degraded'))}, "
        f"embedding degraded {sum(1 for s in searches if s.get('embedding_degraded'))}, "
        f"keyword degraded {sum(1 for s in searches if s.get('keyword_degraded'))}, "
        f"errors {sum(1 for s in searches if s.get('error'))}, "
        f"skipped {sum(1 for s in searches if s.get('skipped'))}"
    )
    ran = [s for s in searches if "status" in s]
    outcomes = Counter(s.get("rerank_outcome") or "ok" for s in ran)
    lines.append(
        "rerank outcomes "
        + ", ".join(f"{k} {v}" for k, v in sorted(outcomes.items()))
        + f"; threshold paused {sum(1 for s in ran if s.get('rerank_threshold_paused'))}"
    )
    if reads:
        with_read = sum(1 for r in rows if r.get("reads"))
        modes = Counter(s["mode"] for s in reads if s.get("mode"))
        lines.append(
            f"reads per question {len(reads) / max(1, len(rows)):.2f} "
            f"(questions with a read {with_read}); modes "
            + ", ".join(f"{k} {v}" for k, v in sorted(modes.items()))
            + f"; errors {sum(1 for s in reads if s.get('error'))}, "
            f"skipped {sum(1 for s in reads if s.get('skipped'))}"
        )
    answer = sum(r["cost_cny"]["answer"] for r in rows)
    judge = sum(r["cost_cny"]["judge"] for r in rows)
    rerank = sum(run["spend_cny"]["rerank"] for run in runs)
    lines.append(
        f"spend ¥{answer + judge + rerank:.2f} (answer ¥{answer:.2f}, judge ¥{judge:.2f}, "
        f"rerank ¥{rerank:.2f} over {len(runs)} run(s))"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--tally", type=Path, help="只打印这个结果文件的统计,不跑")
    parser.add_argument("--dataset", type=Path, help="主集 YAML(库名 = 它的 name)")
    parser.add_argument("--chat-dataset", type=Path, help="真实问法集 YAML(同一语料)")
    parser.add_argument("--corpus", type=Path, help="manifest.csv;覆盖主集里的 corpus.manifest")
    parser.add_argument("--corpus-root", type=Path, help="清单里 path 的根目录(默认清单所在目录)")
    parser.add_argument("--out", type=Path, help="结果 JSONL(追加写;重跑同一文件只补没做完的题)")
    parser.add_argument("--judge-cache", type=Path, default=JUDGE_CACHE_DB)
    parser.add_argument("--answer-model", default=DEFAULT_ANSWER_MODEL)
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--answer-price", help="input,cached,output 元 / 百万 token")
    parser.add_argument("--judge-price", help="input,cached,output 元 / 百万 token")
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET_CNY, help="本次上限,元")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="只跑选出来的前 N 题(试跑)")
    parser.add_argument("--ids", default="", help="只跑这些题(逗号分隔,试跑)")
    parser.add_argument("--dry", action="store_true", help="假模型空跑,不花钱")
    parser.add_argument("--store", default="sql", choices=("sql", "memory"))
    parser.add_argument("--embedder", default="real", choices=("real", "fake"))
    parser.add_argument(
        "--query-marker",
        default="on",
        choices=("on", "off"),
        help="查询侧标记(B-206 K-5,同 kb_eval):off = 对照臂,查询不带标记",
    )
    parser.add_argument(
        "--no-read",
        action="store_true",
        help=(
            "读文档关掉:不给模型 knowledge_read。不是改动前的基线(标头、工具说明、最多回几轮"
            "都已是 K-2 之后的)"
        ),
    )
    parser.add_argument("--rerank", default="dashscope", choices=("dashscope", "off"))
    # B-206 K-14 对照组,与 kb_eval.py 同名同义;默认 = 生产默认。
    parser.add_argument("--rerank-view", default=DEFAULT_RERANK_VIEW, choices=RERANK_VIEWS)
    parser.add_argument("--rerank-pool", type=int, default=DEFAULT_RERANK_POOL)
    parser.add_argument("--rerank-instruct", action="store_true")
    parser.add_argument("--rerank-model")  # B-206 K-7b
    # B-206 K-7b:设到库上的重排分数下限(绑到这次的重排模型),智能体层看到的与生产相同;
    # 不给 = 不设。
    parser.add_argument("--rerank-threshold", type=float)
    return parser


def _check_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    for name in ("dataset", "chat_dataset", "out"):
        if getattr(args, name) is None:
            parser.error(f"--{name.replace('_', '-')} is required (unless --tally)")
    if args.rerank_pool < 1:
        parser.error(f"--rerank-pool must be at least 1, got {args.rerank_pool}")
    if args.rerank_instruct and args.rerank_view == "dual":
        parser.error(INSTRUCT_NEEDS_SINGLE_VIEW)
    if args.rerank_threshold is not None and args.rerank != "dashscope":
        parser.error("--rerank-threshold needs --rerank dashscope (a model that scores)")
    if args.rerank_threshold is not None and not 0 <= args.rerank_threshold <= 1:
        parser.error(f"--rerank-threshold must be within [0, 1], got {args.rerank_threshold}")
    cache_only = args.embedder == "real" or args.rerank == "dashscope"
    if args.dry and cache_only and os.environ.get(RERANK_KEY_ENV) != OFFLINE_KEY:
        parser.error(
            f"--dry with the real embedder / reranker needs {RERANK_KEY_ENV}={OFFLINE_KEY}"
        )
    if not args.dry:
        missing = [name for name in (*ANSWER_ENV, *JUDGE_ENV) if not os.environ.get(name)]
        if missing:
            parser.error(f"missing env {', '.join(missing)}")


def _chat_client(
    model: str, price: Price, env: tuple[str, str], http: httpx.AsyncClient
) -> ChatClient:
    base_url, key = (os.environ[name] for name in env)
    return ChatClient(model=model, base_url=base_url, api_key=key, price=price, http=http)


async def amain(args: argparse.Namespace) -> int:
    main_set = load_dataset(args.dataset)
    chat_set = load_dataset(args.chat_dataset)
    cases = select_cases(main_set, chat_set)
    if args.ids:
        wanted = set(args.ids.split(","))
        cases = [(label, c) for label, c in cases if c.id in wanted]
    if args.limit:
        cases = cases[: args.limit]
    answer_model = DRY_MODEL if args.dry else args.answer_model
    judge_model = DRY_MODEL if args.dry else args.judge_model
    out = cast(Path, args.out)
    done = done_case_ids(out, answer_model=answer_model, judge_model=judge_model)
    todo = [(label, c) for label, c in cases if c.id not in done]
    print(f"cases {len(cases)}; done {len(done)}; todo {len(todo)}", file=sys.stderr)
    if todo and args.dry:
        await _run(args, main_set=main_set, todo=todo, models=FakeModels(), out=out)
    elif todo:
        answer_price = resolve_price(args.answer_model, args.answer_price)
        judge_price = resolve_price(args.judge_model, args.judge_price)
        async with httpx.AsyncClient() as http:
            models = RealModels(
                answer_client=_chat_client(args.answer_model, answer_price, ANSWER_ENV, http),
                judge_client=_chat_client(args.judge_model, judge_price, JUDGE_ENV, http),
            )
            await _run(args, main_set=main_set, todo=todo, models=models, out=out)
    print(tally(out))
    return 0


async def _run(
    args: argparse.Namespace,
    *,
    main_set: KbDataset,
    todo: Sequence[tuple[str, KbCase]],
    models: Models,
    out: Path,
) -> None:
    started = datetime.now(UTC)
    manifest = _resolve_manifest(args, main_set)
    corpus_root = cast(Path, args.corpus_root) if args.corpus_root is not None else manifest.parent
    selection = select_rows(
        read_manifest(manifest), tiers=_resolve_tiers(None, main_set), limit_docs=0
    )
    bundle = build_embedder(
        args.embedder, concurrency=args.concurrency, query_marker=args.query_marker == "on"
    )
    rerank_model = resolve_rerank_model(args.rerank_model, rerank_mode=args.rerank)
    reranker, rerank_client = build_rerank_mode(args.rerank, model=rerank_model)

    def rerank_cny() -> float:
        return float(rerank_client.stats()["cost_cny"]) if rerank_client is not None else 0.0

    spend = Spend()
    out.parent.mkdir(parents=True, exist_ok=True)
    async with open_store(args.store) as store:
        with rls_tenant(EVAL_TENANT_ID):
            report = await build_corpus(
                store=store,
                embedder=bundle.embedder,
                tenant_id=EVAL_TENANT_ID,
                base_name=main_set.name,
                corpus_root=corpus_root,
                selection=selection,
                concurrency=args.concurrency,
            )
            print(
                f"corpus: ingested {report.docs_ingested}, failed {report.docs_failed}",
                file=sys.stderr,
            )
            if report.docs_ingested == 0:
                raise SystemExit(f"no document ingested — check --corpus-root ({corpus_root})")
            # 控制面建库默认开重排;评测建库时没开,工具读的是库上的设置。
            await store.update_base(
                tenant_id=EVAL_TENANT_ID,
                kb_id=report.base_id,
                rerank_enabled=True,
                rerank_score_threshold=args.rerank_threshold,
                rerank_threshold_model=(
                    None
                    if args.rerank_threshold is None or rerank_model is None
                    else rerank_model_key("qwen", rerank_model)
                ),
            )
            gold = await gold_passages(
                store,
                tenant_id=EVAL_TENANT_ID,
                kb_id=report.base_id,
                cases=[case for _label, case in todo],
            )
            retriever = build_retriever(
                store=store,
                embedder=bundle.embedder,
                reranker=reranker,
                rerank_view=args.rerank_view,
                rerank_pool=args.rerank_pool,
                rerank_instruction=args.rerank_instruct,
            )
            refs = (main_set.name,)
            written = await run_questions(
                todo=todo,
                models=models,
                tool=KnowledgeSearchTool(retriever=retriever, knowledge_base_refs=refs),
                # B-206 K-2:生产里有知识库的智能体两个工具都有;--no-read 只是读文档关掉。
                reader=None
                if args.no_read
                else KnowledgeReadTool(store=store, knowledge_base_refs=refs),
                tenant_id=EVAL_TENANT_ID,
                gold=gold,
                cache=JudgeCache(args.judge_cache),
                out=out,
                gate=BudgetGate(budget=args.budget, control=Path(f"{out}.budget")),
                spend=spend,
                other_cost=rerank_cny,
                concurrency=args.concurrency,
                cache_verdicts=not args.dry,
            )
    run = {
        "started": started.isoformat(),
        "finished": datetime.now(UTC).isoformat(),
        "questions": written,
        "answer_model": models.answer_model,
        "judge_model": models.judge_model,
        "query_marker": bundle.query_marker,
        # B-206 K-2:这次有没有给模型 knowledge_read。
        "read_tool": not args.no_read,
        # B-206 K-14:这次跑的是哪个重排对照组(不同组请写不同的 --out)。
        "rerank_arm": {
            "view": args.rerank_view,
            "pool": args.rerank_pool,
            "instruct": args.rerank_instruct,
            "model": rerank_model,
            "threshold": args.rerank_threshold,
        },
        "calls": {"answer": spend.answer_calls, "judge": spend.judge_calls},
        "spend_cny": {
            "answer": round(spend.answer_cny, 6),
            "judge": round(spend.judge_cny, 6),
            "rerank": round(rerank_cny(), 6),
        },
        # 嵌入不计入预算:实花只算缓存没命中、真送到接口的;名义值假设一条都没缓存。
        "embed_cny_actual": bundle.embedder.stats().cost_cny_actual,
        "embed_cny_nominal": bundle.embedder.stats().cost_cny_nominal,
    }
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"run": run}, ensure_ascii=False) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.tally is not None:
        print(tally(cast(Path, args.tally)))
        return 0
    _check_args(parser, args)
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
