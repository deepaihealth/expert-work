#!/usr/bin/env python
"""知识库智能体层评测:**只重判、不重新答题**(B-206 11-10)。

读一份已有的结果 JSONL(``kb_agent_eval.py`` 写的),用当前尺子(``kb_agent_judge`` 的判分模板 +
:data:`kb_agent_judge.SCORING_RULES`)把每题的回答重新判一遍,写到一个新文件。评委看到的东西与
原来一样拼:问题、``searches[].content`` + ``reads[].content``
(:func:`kb_agent_judge.seen_passages`)、回答原文;参考信息照原来的题、近名干扰 / 难负例的
补充说明重新生成。

* 题与说明:用同一份 ``--dataset`` / ``--chat-dataset``(难负例轮再给原来那份 ``--hard-negatives``
  spec,内容哈希要与结果行的 ``run_kind`` 对上),标准轮的近名干扰题数取原文件 run 行的
  ``distractors``。
* 「含答案的完整段落」(有答案题的参考信息):11-10 起结果行记了 ``gold``,直接用;更早的行没有,
  就要 ``--corpus-root`` 在本机用内存库 + 假嵌入把库再建一遍取出来(不连任何接口、不花钱,切块与
  真跑同一条入库链路;难负例轮同样删句)。
* 新文件每行带新尺子的 ``yardstick`` / ``judge_votes``,``rejudged_from`` 记原来的文件、尺子、类别与
  答题花费;``cost_cny.answer`` 记 0(这次没答题)。末尾的 run 行带 ``rejudge`` 元数据。
* 答前复核剔除的行(``excluded_answer_still_present``)不重判,原样带过去(换上新尺子版本)。
* 同一个 ``--out`` 重跑只补没做完的题;里面有别的尺子判的行就拒绝。

先免费估价::

    uv run --no-sync python tools/eval/kb_agent_estimate.py --rejudge eval-out/kb/agent-x.jsonl

再重判(会花判分钱)::

    uv run --no-sync python tools/eval/kb_agent_rejudge.py --from eval-out/kb/agent-x.jsonl \\
        --out eval-out/kb/agent-x.rejudged.jsonl \\
        --dataset tools/eval/datasets/kb/huada_v1.yaml \\
        --chat-dataset tools/eval/datasets/kb/huada_v1.chat.yaml --corpus-root <资料根目录>
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).parent))

from kb_agent_cost import Spend, resolve_price  # type: ignore[import-not-found]
from kb_agent_judge import (  # type: ignore[import-not-found]
    UNSCORED,
    Judge,
    JudgeCache,
    decide,
    judge_prompt,
    judge_votes,
    seen_passages,
    yardstick_of,
)
from kb_agent_tally import read_results, tally  # type: ignore[import-not-found]
from kb_dataset import KbCase, KbDataset, load_dataset  # type: ignore[import-not-found]

YARDSTICK = yardstick_of()
DEFAULT_JUDGE_MODEL = "deepseek-v4-pro"
JUDGE_ENV = ("EXPERT_WORK_EVAL_JUDGE_BASE_URL", "EXPERT_WORK_EVAL_JUDGE_API_KEY")
DEFAULT_BUDGET_CNY = 20.0


# ---------------------------------------------------------------------------
# 题、说明、含答案段落
# ---------------------------------------------------------------------------


def run_kind_of_rows(rows: Sequence[Mapping[str, Any]]) -> str:
    kinds = {str(r.get("run_kind") or "standard") for r in rows}
    if len(kinds) != 1:
        raise SystemExit(f"the source mixes run kinds {sorted(kinds)}; rejudge one run at a time")
    return kinds.pop()


def check_spec(run_kind: str, spec: Path | None) -> None:
    """难负例轮要给原来那份 spec(内容哈希 = ``run_kind`` 里的那段);标准轮不能给。"""
    if not run_kind.startswith("hn:"):
        if spec is not None:
            raise SystemExit("the source is a standard run: leave out --hard-negatives")
        return
    if spec is None:
        raise SystemExit(
            f"the source is a hard-negative run ({run_kind}): pass its --hard-negatives"
        )
    digest = hashlib.sha256(spec.read_bytes()).hexdigest()[:12]
    if run_kind.split(":")[1] != digest:
        raise SystemExit(
            f"{spec} hashes to {digest}, the source was run with {run_kind}: pass the same spec"
        )


def plan_for(
    args: argparse.Namespace,
    runs: Sequence[Mapping[str, Any]],
    main_set: KbDataset,
    chat_set: KbDataset,
) -> Any:
    """与原来那一轮同样的选题与说明(``kb_agent_cases.plan_run``)。"""
    from kb_agent_cases import plan_run  # type: ignore[import-not-found]

    distractors = int(runs[-1].get("distractors") or 0) if runs else 0
    ns = SimpleNamespace(
        hard_negatives=args.hard_negatives,
        distractors=0 if args.hard_negatives is not None else distractors,
        corpus=args.corpus,
        dataset=args.dataset,
    )
    return plan_run(cast(argparse.Namespace, ns), main_set, chat_set)


def rows_gold(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[str]] | None:
    """结果行里记着的 ``gold``;有一道有答案的题没记就 None(要重建库取)。"""
    gold: dict[str, list[str]] = {}
    for row in rows:
        if row.get("category") in UNSCORED:
            continue
        if "gold" not in row:
            if row.get("label", "answerable" if row.get("answerable") else "") == "answerable":
                return None
            continue
        gold[str(row["case_id"])] = list(row["gold"])
    return gold


async def offline_gold(
    args: argparse.Namespace, main_set: KbDataset, plan: Any, cases: Sequence[KbCase]
) -> dict[str, list[str]]:
    """内存库 + 假嵌入把库再建一遍,取有答案题的「含答案的完整段落」(不连接口、不花钱)。"""
    from kb_agent_cases import corpus_selection, gold_passages  # type: ignore[import-not-found]
    from kb_eval import (  # type: ignore[import-not-found]
        EVAL_TENANT_ID,
        build_corpus,
        build_embedder,
        open_store,
        rls_tenant,
    )
    from kb_hard_sets import AnchorDeletingParser  # type: ignore[import-not-found]

    manifest, selection = corpus_selection(args, main_set)
    root = cast(Path, args.corpus_root) if args.corpus_root is not None else manifest.parent
    bundle = build_embedder("fake", concurrency=args.concurrency)
    deleting = (
        {"parser": AnchorDeletingParser(anchors=plan.delete_anchors, guard=plan.guard_anchors)}
        if plan.delete_anchors
        else {}
    )
    async with open_store("memory") as store:
        with rls_tenant(EVAL_TENANT_ID):
            report = await build_corpus(
                store=store,
                embedder=bundle.embedder,
                tenant_id=EVAL_TENANT_ID,
                base_name=main_set.name + ("-hn" if plan.delete_anchors else ""),
                corpus_root=root,
                selection=selection,
                concurrency=args.concurrency,
                **deleting,
            )
            if report.docs_ingested == 0:
                raise SystemExit(f"no document ingested — check --corpus-root ({root})")
            gold: dict[str, list[str]] = await gold_passages(
                store, tenant_id=EVAL_TENANT_ID, kb_id=report.base_id, cases=list(cases)
            )
            return gold


# ---------------------------------------------------------------------------
# 重判
# ---------------------------------------------------------------------------


def done_ids(path: Path, *, judge_model: str, votes: int, source: str, run_kind: str) -> set[str]:
    """``--out`` 里已重判的题;尺子、判分模型、票数、源文件、轮次有一项与这次不同就拒绝续写
    (审查 M3)。复核剔除的行不重判,不核判分模型。"""
    rows, _runs = read_results(path)
    for row in rows:
        wanted: dict[str, object] = {
            "yardstick": YARDSTICK,
            "judge_votes": votes,
            "run_kind": run_kind,
            "rejudged_from": source,
        }
        if row.get("category") not in UNSCORED:
            wanted["judge_model"] = judge_model
        for key, value in wanted.items():
            got = (
                (row.get("rejudged_from") or {}).get("file")
                if key == "rejudged_from"
                else row.get(key)
            )
            if got != value:
                raise SystemExit(
                    f"{path} has a row with {key}={got!r}, this rejudge is {value!r}; "
                    "use another --out"
                )
    return {str(r["case_id"]) for r in rows}


async def rejudge_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    cases: Mapping[str, KbCase],
    notes: Mapping[str, str],
    deleted: Mapping[str, Sequence[str]],
    gold: Mapping[str, list[str]],
    models: Judge,
    cache: JudgeCache,
    out: Path,
    votes: int,
    spend: Spend,
    source: str,
    budget: float = float("inf"),
    concurrency: int = 4,
    cache_verdicts: bool = True,
) -> int:
    """逐行重判,追加写进 ``out``;返回写了几行。花费到了 ``budget`` 就不再开新题。"""
    sem = asyncio.Semaphore(max(1, concurrency))
    lock = asyncio.Lock()
    written = 0

    async def one(row: Mapping[str, Any]) -> None:
        nonlocal written
        async with sem:
            if spend.judge_cny >= budget:
                return
            new = await _rejudged(row)
            async with lock:
                with out.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(new, ensure_ascii=False) + "\n")
                written += 1
                print(
                    f"{row['case_id']} {row.get('category')} -> {new['category']} "
                    f"| spent ¥{spend.judge_cny:.2f}",
                    file=sys.stderr,
                )

    async def _rejudged(row: Mapping[str, Any]) -> dict[str, Any]:
        was = {
            "file": source,
            "yardstick": row.get("yardstick"),
            "category": row.get("category"),
            "cost_cny": row.get("cost_cny"),
        }
        meta = {"yardstick": YARDSTICK, "judge_votes": votes, "rejudged_from": was}
        if row.get("category") in UNSCORED:
            # 不重判:这次没花钱(审查 L7),原来的花费留在 rejudged_from
            return {**row, **meta, "cost_cny": {"answer": 0.0, "judge": 0.0}}
        case_id = str(row["case_id"])
        if case_id not in cases:
            raise SystemExit(f"{case_id} is not in this run's cases: check --dataset / spec")
        case = cases[case_id]
        run = {
            "answer": row.get("answer") or "",
            "searches": row.get("searches") or [],
            "reads": row.get("reads") or [],
        }
        prompt = judge_prompt(case, run, gold, notes.get(case_id, ""))
        raws, cost = await judge_votes(
            models, cache, prompt, votes=votes, spend=spend, cache_verdicts=cache_verdicts
        )
        decided = decide(
            case.answer_label, raws, seen_passages(run), deleted=deleted.get(case_id, ())
        )
        return {
            **row,
            **decided,
            **meta,
            "label": case.answer_label,
            "gold": list(gold.get(case_id, [])),
            "judge_model": models.judge_model,
            "cost_cny": {"answer": 0.0, "judge": round(cost, 6)},
        }

    await asyncio.gather(*(one(row) for row in rows))
    return written


class _JudgeOnly:
    """重判只要判分模型。"""

    def __init__(self, client: Any) -> None:
        self.client = client

    @property
    def judge_model(self) -> str:
        return str(self.client.model)

    async def judge(self, prompt: str) -> tuple[str, float]:
        from kb_agent_judge import JUDGE_SYSTEM  # type: ignore[import-not-found]

        message, cost = await self.client.complete(
            {
                "messages": [
                    {"role": "system", "content": JUDGE_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
            }
        )
        return str(message.get("content") or ""), float(cost)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--from", dest="source", type=Path, required=True, help="已有的结果 JSONL")
    parser.add_argument("--out", type=Path, required=True, help="新文件(追加写,可续)")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--chat-dataset", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, help="manifest.csv;覆盖主集里的 corpus.manifest")
    parser.add_argument("--corpus-root", type=Path, help="结果行没记 gold 时重建库用")
    parser.add_argument("--hard-negatives", type=Path, help="难负例轮原来那份 spec")
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--judge-price", help="input,cached,output 元 / 百万 token")
    parser.add_argument("--judge-votes", type=int, help="默认与原文件相同")
    parser.add_argument("--judge-cache", type=Path)
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET_CNY, help="判分上限,元")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--dry", action="store_true", help="假判分模型,不花钱、不连网")
    return parser


async def amain(args: argparse.Namespace) -> int:
    rows, runs = read_results(args.source)
    if not rows:
        raise SystemExit(f"{args.source}: no result rows")
    run_kind = run_kind_of_rows(rows)
    check_spec(run_kind, args.hard_negatives)
    votes = args.judge_votes or int(rows[0].get("judge_votes") or 1)
    if votes < 1:
        raise SystemExit(f"--judge-votes must be at least 1, got {votes}")
    main_set, chat_set = load_dataset(args.dataset), load_dataset(args.chat_dataset)
    plan = plan_for(args, runs, main_set, chat_set)
    cases = {c.id: c for _label, c in plan.cases}
    out = cast(Path, args.out)
    if args.dry:
        from kb_agent_models import DRY_MODEL  # type: ignore[import-not-found]

        judge_model = DRY_MODEL
    else:
        judge_model = args.judge_model
    done = done_ids(
        out, judge_model=judge_model, votes=votes, source=args.source.name, run_kind=run_kind
    )
    todo = [r for r in rows if str(r["case_id"]) not in done]
    gold = rows_gold(todo)
    if gold is None:
        if args.corpus_root is None:
            raise SystemExit(
                "the source rows do not record their gold passages (written before 11-10): "
                "pass --corpus-root to rebuild them offline (in-memory store, fake embedder)"
            )
        needed = [cases[str(r["case_id"])] for r in todo if str(r["case_id"]) in cases]
        gold = await offline_gold(args, main_set, plan, needed)
    print(f"rows {len(rows)}; done {len(done)}; todo {len(todo)}", file=sys.stderr)
    spend = Spend()
    started = datetime.now(UTC)
    out.parent.mkdir(parents=True, exist_ok=True)

    async def run_with(models: Judge) -> int:
        from kb_agent_eval import JUDGE_CACHE_DB  # type: ignore[import-not-found]

        written: int = await rejudge_rows(
            todo,
            cases=cases,
            notes=plan.notes,
            deleted=plan.deleted_by_case,
            gold=gold,
            models=models,
            cache=JudgeCache(args.judge_cache or JUDGE_CACHE_DB),
            out=out,
            votes=votes,
            spend=spend,
            source=args.source.name,
            budget=args.budget,
            concurrency=args.concurrency,
            cache_verdicts=not args.dry,
        )
        return written

    if args.dry:
        from kb_agent_models import FakeModels  # type: ignore[import-not-found]

        written = await run_with(FakeModels())
    else:
        import httpx
        from kb_agent_models import ChatClient  # type: ignore[import-not-found]

        missing = [name for name in JUDGE_ENV if not os.environ.get(name)]
        if missing:
            raise SystemExit(f"missing env {', '.join(missing)}")
        base_url, key = (os.environ[name] for name in JUDGE_ENV)
        price = resolve_price(args.judge_model, args.judge_price)
        async with httpx.AsyncClient() as http:
            client = ChatClient(
                model=judge_model, base_url=base_url, api_key=key, price=price, http=http
            )
            written = await run_with(_JudgeOnly(client))
    stopped = written < len(todo)
    if stopped:
        print(
            f"stopped by budget: spent ¥{spend.judge_cny:.2f} >= ¥{args.budget:.2f}; "
            f"{len(todo) - written} row(s) left, rerun with the same --out to finish",
            file=sys.stderr,
        )
    source_run = runs[-1] if runs else {}
    run = {
        **{k: v for k, v in source_run.items() if k not in ("calls", "spend_cny", "questions")},
        "started": started.isoformat(),
        "finished": datetime.now(UTC).isoformat(),
        "questions": written,
        # 审查 L7:花费到了 --budget、还有题没重判
        "stopped_by_budget": stopped,
        "answer_model": rows[0].get("answer_model"),
        "judge_model": judge_model,
        "judge_votes": votes,
        "rejudge": {
            "source": args.source.name,
            "source_yardstick": sorted({str(r.get("yardstick")) for r in rows}),
            "yardstick": YARDSTICK,
            "run_kind": run_kind,
        },
        "calls": {"answer": 0, "judge": spend.judge_calls},
        "spend_cny": {"answer": 0.0, "judge": round(spend.judge_cny, 6), "rerank": 0.0},
    }
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"run": run}, ensure_ascii=False) + "\n")
    print(tally(out))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
