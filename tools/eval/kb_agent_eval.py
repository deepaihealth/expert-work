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
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parent))

import httpx
from kb_agent_answer import Tool, run_agent  # type: ignore[import-not-found]
from kb_agent_cases import (  # type: ignore[import-not-found]
    DEFAULT_DISTRACTORS,
    RunPlan,
    check_hard_negative_corpus,
    corpus_selection,
    gold_passages,
    plan_run,
    stop_on_lost_controls,
)
from kb_agent_cost import (  # type: ignore[import-not-found]
    BudgetGate,
    Price,
    Spend,
    resolve_price,
)
from kb_agent_judge import (  # type: ignore[import-not-found]
    DEFAULT_JUDGE_VOTES,
    JudgeCache,
    decide,
    judge_prompt,
    judge_votes,
    seen_passages,
    yardstick_of,
)
from kb_agent_models import (  # type: ignore[import-not-found]
    DRY_MODEL,
    ChatClient,
    FakeModels,
    Models,
    RealModels,
)
from kb_agent_prod_prompt import (  # type: ignore[import-not-found]
    ProductionPrompt,
    load_production_prompt,
)
from kb_agent_recheck import (  # type: ignore[import-not-found]
    DEFAULT_RECHECK_K,
    MAX_RECHECK_K,
    excluded_row,
    recheck_hard_negatives,
    recheck_references,
    write_excluded,
)
from kb_agent_tally import read_results, tally  # type: ignore[import-not-found]
from kb_dataset import KbCase, KbDataset, load_dataset  # type: ignore[import-not-found]
from kb_eval import (  # type: ignore[import-not-found]
    EVAL_OUT_DIR,
    EVAL_TENANT_ID,
    INSTRUCT_NEEDS_SINGLE_VIEW,
    build_corpus,
    build_embedder,
    build_rerank_mode,
    build_retriever,
    open_store,
    rls_tenant,
)
from kb_hard_sets import (  # type: ignore[import-not-found]
    HARD_NEGATIVE_SET,
    AnchorDeletingParser,
)
from kb_rerank import (  # type: ignore[import-not-found]
    OFFLINE_KEY,
    RERANK_KEY_ENV,
    resolve_rerank_model,
)

from expert_work.protocol import rerank_model_key
from orchestrator.tools.knowledge import (
    DEFAULT_RERANK_POOL,
    DEFAULT_RERANK_VIEW,
    RERANK_VIEWS,
    KnowledgeSearchTool,
)
from orchestrator.tools.knowledge_read import KnowledgeReadTool

#: 尺子版本(B-206 11-09:按结论判 + 多数票 + 三种标签):规则版本 + 判分提示词的哈希,
#: 改提示词自动变;每行都记,续跑时不同版本的行不许混。
YARDSTICK = yardstick_of()
DEFAULT_ANSWER_MODEL = "glm-5.3"
DEFAULT_JUDGE_MODEL = "deepseek-v4-pro"
ANSWER_ENV = ("EXPERT_WORK_EVAL_ANSWER_BASE_URL", "EXPERT_WORK_EVAL_ANSWER_API_KEY")
JUDGE_ENV = ("EXPERT_WORK_EVAL_JUDGE_BASE_URL", "EXPERT_WORK_EVAL_JUDGE_API_KEY")
DEFAULT_BUDGET_CNY = 20.0
JUDGE_CACHE_DB = EVAL_OUT_DIR / "agent_judge_cache.sqlite"


# ---------------------------------------------------------------------------
# 预算 / 跑题
# ---------------------------------------------------------------------------


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
    votes: int = DEFAULT_JUDGE_VOTES,
    notes: Mapping[str, str] | None = None,
    run_kind: str = "standard",
    deleted: Mapping[str, Sequence[str]] | None = None,
    production: ProductionPrompt | None = None,
    recheck_costs: Mapping[str, float] | None = None,
) -> int:
    """答题 + 判分,每题一行追加进 ``out``;返回写了几行。``other_cost`` 是计入预算的其它花费
    (重排)。``production``:照生产加的提示段(见 ``kb_agent_prod_prompt``)。
    每题判 ``votes`` 次取多数(B-206 11-09);``notes`` 是派生题给判分的补充说明。"""
    sem = asyncio.Semaphore(max(1, concurrency))
    gate_lock = asyncio.Lock()
    write_lock = asyncio.Lock()
    stop = asyncio.Event()
    written = 0

    def total() -> float:
        return float(spend.answer_cny + spend.judge_cny + other_cost())

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
                models,
                tool,
                question=case.query,
                tenant_id=tenant_id,
                spend=spend,
                reader=reader,
                prompt=production,
            )
            prompt = judge_prompt(case, run, gold, (notes or {}).get(case.id, ""))
            raws, judge_cny = await judge_votes(
                models, cache, prompt, votes=votes, spend=spend, cache_verdicts=cache_verdicts
            )
            gone = (deleted or {}).get(case.id, ())
            decided = decide(case.answer_label, raws, seen_passages(run), deleted=gone)
            row = {
                "case_id": case.id,
                "set": label,
                "answerable": bool(case.expected),
                "label": case.answer_label,
                "question": case.query,
                "answer": run["answer"],
                "searches": run["searches"],
                "reads": run["reads"],
                # 11-10:判分看到的「含答案的完整段落」也记下,以后重判(kb_agent_rejudge)不用重建库。
                "gold": list(gold.get(case.id, [])),
                **decided,
                "answer_model": models.answer_model,
                "judge_model": models.judge_model,
                "yardstick": YARDSTICK,
                "judge_votes": votes,
                "run_kind": run_kind,
                "cost_cny": {
                    "answer": round(answer_cny, 6),
                    "judge": round(judge_cny, 6),
                    # 审查 M4:难负例答前复核的判分花费记在留下来答题的那一行上
                    **(
                        {"recheck": round(recheck_costs[case.id], 6)}
                        if recheck_costs and case.id in recheck_costs
                        else {}
                    ),
                },
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


def done_case_ids(
    path: Path, *, answer_model: str, judge_model: str, judge_votes: int, run_kind: str
) -> set[str]:
    """已做完的题;文件里是别的模型答 / 判的、别的尺子版本(含 11-09 之前没记版本的行)、别的票数
    或别的轮次(标准轮 / 某份难负例 spec)的,就拒绝续写 —— 混在一起的统计没有意义。"""
    rows, _runs = read_results(path)
    for row in rows:
        if (row.get("answer_model"), row.get("judge_model")) != (answer_model, judge_model):
            raise SystemExit(
                f"{path} was written with answer model {row.get('answer_model')!r} / judge "
                f"model {row.get('judge_model')!r}; use another --out for "
                f"{answer_model!r} / {judge_model!r}"
            )
        wanted = {"yardstick": YARDSTICK, "judge_votes": judge_votes, "run_kind": run_kind}
        for key, value in wanted.items():
            if row.get(key) != value:
                raise SystemExit(
                    f"{path} has a row with {key}={row.get(key)!r}, this run is {value!r}; "
                    "use another --out"
                )
    return {row["case_id"] for row in rows}


def run_kind_of(args: argparse.Namespace) -> str:
    """``standard`` 或 ``hn:<spec 内容 sha256 前 12 位>:recheck<k>`` / ``…:norecheck``
    (答前复核开没开、取几段的结果不混,11-10)。"""
    if args.hard_negatives is None:
        return "standard"
    digest = hashlib.sha256(Path(args.hard_negatives).read_bytes()).hexdigest()[:12]
    recheck = "norecheck" if args.no_recheck else f"recheck{args.recheck_k}"
    return f"hn:{digest}:{recheck}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--tally",
        type=Path,
        nargs="+",
        help="只打印这些结果文件的统计(按答题模型分节;几个模型就多一张对比表),不跑",
    )
    parser.add_argument("--dataset", type=Path, help="主集 YAML(库名 = 它的 name)")
    parser.add_argument("--chat-dataset", type=Path, help="真实问法集 YAML(同一语料)")
    parser.add_argument("--corpus", type=Path, help="manifest.csv;覆盖主集里的 corpus.manifest")
    parser.add_argument("--corpus-root", type=Path, help="清单里 path 的根目录(默认清单所在目录)")
    parser.add_argument("--out", type=Path, help="结果 JSONL(追加写;重跑同一文件只补没做完的题)")
    parser.add_argument("--judge-cache", type=Path, default=JUDGE_CACHE_DB)
    parser.add_argument("--answer-model", default=DEFAULT_ANSWER_MODEL)
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument(
        "--judge-votes",
        type=int,
        default=DEFAULT_JUDGE_VOTES,
        help="每题判几次取多数(B-206 11-09;没有严格多数记「判分分歧」)",
    )
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
    parser.add_argument(
        "--distractors",
        type=int,
        help=(
            f"标准一轮另加几道近名干扰题(有答案;B-206 11-09),默认 {DEFAULT_DISTRACTORS},"
            "0 = 不加;难负例轮不加"
        ),
    )
    parser.add_argument(
        "--hard-negatives",
        type=Path,
        help=(
            "难负例 spec(kb_hard_sets.py 生成):这一轮建删过答案句的库,只跑难负例 + 30 道"
            "有答案对照题;与 --distractors 分开跑"
        ),
    )
    parser.add_argument(
        "--no-recheck",
        action="store_true",
        help="难负例轮不做答前复核(11-10;默认做:检索前 k 段让判分模型判答案还在不在,会花判分钱)",
    )
    parser.add_argument(
        "--recheck-k",
        type=int,
        default=DEFAULT_RECHECK_K,
        help=f"答前复核每题取前几段(1~{MAX_RECHECK_K},knowledge_search 的 limit 上限)",
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
    if args.hard_negatives is not None and args.distractors:
        parser.error("--hard-negatives runs on its own corpus: leave out --distractors")
    if args.distractors is None:
        args.distractors = 0 if args.hard_negatives is not None else DEFAULT_DISTRACTORS
    if args.distractors < 0:
        parser.error(f"--distractors must be at least 0, got {args.distractors}")
    if not 1 <= args.recheck_k <= MAX_RECHECK_K:
        parser.error(f"--recheck-k must be within [1, {MAX_RECHECK_K}], got {args.recheck_k}")
    if args.judge_votes < 1:
        parser.error(f"--judge-votes must be at least 1, got {args.judge_votes}")
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
    plan = plan_run(args, main_set, chat_set)
    cases = plan.cases
    if args.ids:
        wanted = set(args.ids.split(","))
        cases = [(label, c) for label, c in cases if c.id in wanted]
    if args.limit:
        cases = cases[: args.limit]
    answer_model = DRY_MODEL if args.dry else args.answer_model
    judge_model = DRY_MODEL if args.dry else args.judge_model
    out = cast(Path, args.out)
    done = done_case_ids(
        out,
        answer_model=answer_model,
        judge_model=judge_model,
        judge_votes=args.judge_votes,
        run_kind=run_kind_of(args),
    )
    todo = [(label, c) for label, c in cases if c.id not in done]
    print(f"cases {len(cases)}; done {len(done)}; todo {len(todo)}", file=sys.stderr)
    if todo and args.dry:
        await _run(args, main_set=main_set, todo=todo, models=FakeModels(), out=out, plan=plan)
    elif todo:
        answer_price = resolve_price(args.answer_model, args.answer_price)
        judge_price = resolve_price(args.judge_model, args.judge_price)
        async with httpx.AsyncClient() as http:
            models = RealModels(
                answer_client=_chat_client(args.answer_model, answer_price, ANSWER_ENV, http),
                judge_client=_chat_client(args.judge_model, judge_price, JUDGE_ENV, http),
            )
            await _run(args, main_set=main_set, todo=todo, models=models, out=out, plan=plan)
    print(tally(out))
    return 0


async def _run(
    args: argparse.Namespace,
    *,
    main_set: KbDataset,
    todo: Sequence[tuple[str, KbCase]],
    models: Models,
    out: Path,
    plan: RunPlan,
) -> None:
    started = datetime.now(UTC)
    manifest, selection = corpus_selection(args, main_set)
    corpus_root = cast(Path, args.corpus_root) if args.corpus_root is not None else manifest.parent
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
            # B-206 11-09 难负例臂:解析后删掉答案句再切块,文件名不变;库另起名。
            deleting = (
                {
                    "parser": AnchorDeletingParser(
                        anchors=plan.delete_anchors, guard=plan.guard_anchors
                    )
                }
                if plan.delete_anchors
                else {}
            )
            base_name = main_set.name + ("-hn" if plan.delete_anchors else "")
            report = await build_corpus(
                store=store,
                embedder=bundle.embedder,
                tenant_id=EVAL_TENANT_ID,
                base_name=base_name,
                corpus_root=corpus_root,
                selection=selection,
                concurrency=args.concurrency,
                **deleting,
            )
            print(
                f"corpus: ingested {report.docs_ingested}, failed {report.docs_failed}",
                file=sys.stderr,
            )
            if report.docs_ingested == 0:
                raise SystemExit(f"no document ingested — check --corpus-root ({corpus_root})")
            stop_on_lost_controls(report.ingest_failures)
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
            hard_negatives = (
                await check_hard_negative_corpus(
                    store,
                    tenant_id=EVAL_TENANT_ID,
                    kb_id=report.base_id,
                    todo=todo,
                    gold=gold,
                    plan=plan,
                )
                if plan.delete_anchors
                else None
            )
            retriever = build_retriever(
                store=store,
                embedder=bundle.embedder,
                reranker=reranker,
                rerank_view=args.rerank_view,
                rerank_pool=args.rerank_pool,
                rerank_instruction=args.rerank_instruct,
            )
            refs = (base_name,)
            search = KnowledgeSearchTool(retriever=retriever, knowledge_base_refs=refs)
            cache = JudgeCache(args.judge_cache)
            gate = BudgetGate(budget=args.budget, control=Path(f"{out}.budget"))
            todo, recheck, recheck_costs = await _recheck(
                args,
                todo=todo,
                plan=plan,
                search=search,
                models=models,
                cache=cache,
                spend=spend,
                gate=gate,
                total=lambda: float(spend.answer_cny + spend.judge_cny + rerank_cny()),
            )
            # B-206 K-2:生产里有知识库的智能体两个工具都有;--no-read 只是读文档关掉。
            reader = (
                None if args.no_read else KnowledgeReadTool(store=store, knowledge_base_refs=refs)
            )
            # B-206 #2066:照生产加的提示段(本分支有哪几个产品模块就加哪几段),记进 run 行。
            production = load_production_prompt(
                [search.spec.name, *([reader.spec.name] if reader is not None else [])]
            )
            written = await run_questions(
                todo=todo,
                models=models,
                tool=search,
                reader=reader,
                tenant_id=EVAL_TENANT_ID,
                gold=gold,
                cache=cache,
                out=out,
                gate=gate,
                spend=spend,
                other_cost=rerank_cny,
                concurrency=args.concurrency,
                cache_verdicts=not args.dry,
                votes=args.judge_votes,
                notes=plan.notes,
                run_kind=run_kind_of(args),
                deleted=plan.deleted_by_case,
                production=production,
                recheck_costs=recheck_costs,
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
        "judge_votes": args.judge_votes,
        "prod_prompt_parts": list(production.parts),
        # B-206 11-09:这一轮加了几道近名干扰题;难负例臂的删句核对。
        "distractors": plan.distractors,
        "hard_negatives": hard_negatives,
        # 11-10:难负例答前复核(没开 / 标准轮为 None)。
        "recheck": recheck,
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


async def _recheck(
    args: argparse.Namespace,
    *,
    todo: Sequence[tuple[str, KbCase]],
    plan: RunPlan,
    search: KnowledgeSearchTool,
    models: Models,
    cache: JudgeCache,
    spend: Spend,
    gate: BudgetGate,
    total: Callable[[], float],
) -> tuple[list[tuple[str, KbCase]], dict[str, object] | None, dict[str, float]]:
    """难负例答前复核(``kb_agent_recheck``):判为答案仍在库里 / 复核检索报错的题落一行不计分的
    记录、不再答题。受同一个 ``--budget`` 约束(审查 L2):暂停时等操作者调高上限,收到 ``stop``
    就不再复核、也不再答题。返回 ``(还要答的题, 记进 run 行的复核摘要, 留下的题各自的
    复核花费)``。"""
    if not plan.delete_anchors or args.no_recheck:
        return list(todo), None, {}
    stopped = False

    async def admit() -> bool:
        nonlocal stopped
        while total() >= gate.budget:
            if not await gate.wait_for_raise(total):
                stopped = True
                return False
        return True

    hard = [case for label, case in todo if label == HARD_NEGATIVE_SET]
    checks = await recheck_hard_negatives(
        cases=hard,
        search=search,
        tenant_id=EVAL_TENANT_ID,
        models=models,
        cache=cache,
        references=recheck_references(plan.deleted_by_case, plan.original_answers),
        spend=spend,
        votes=args.judge_votes,
        k=args.recheck_k,
        cache_verdicts=not args.dry,
        admit=admit,
    )
    meta = {
        "answer_model": models.answer_model,
        "judge_model": models.judge_model,
        "yardstick": YARDSTICK,
        "judge_votes": args.judge_votes,
        "run_kind": run_kind_of(args),
    }
    gone = {cid for cid, check in checks.items() if check["excluded"]}
    failed = {cid for cid, check in checks.items() if check["search_error"]}
    write_excluded(
        cast(Path, args.out),
        [
            excluded_row(label, case, checks[case.id], meta=meta)
            for label, case in todo
            if case.id in gone | failed
        ],
    )
    for case_id in sorted(gone):
        print(f"{case_id} -> excluded: answer still retrievable after deletion", file=sys.stderr)
    for case_id in sorted(failed):
        print(f"{case_id} -> not scored: recheck search failed", file=sys.stderr)
    summary = {
        "k": args.recheck_k,
        "checked": len(checks),
        "excluded": sorted(gone),
        "search_errors": sorted(failed),
        "stopped_by_budget": stopped,
    }
    if stopped:  # 操作者写了 stop:后面的题都不答(没复核的难负例不能直接答)
        return [], summary, {}
    costs = {cid: float(check["cost_cny"]) for cid, check in checks.items()}
    left = [(label, case) for label, case in todo if case.id not in gone | failed]
    return left, summary, costs


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.tally is not None:
        print(tally(cast(list[Path], args.tally)))
        return 0
    _check_args(parser, args)
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
