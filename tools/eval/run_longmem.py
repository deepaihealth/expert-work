#!/usr/bin/env python
"""CM-N5 benchmark runner — LongMemEval + LoCoMo (NOT run in CI).

Usage (from the repo root, ``.venv`` active or via ``uv run``)::

    # P0 retrieval tier — fake embedder smoke over the fixtures
    python tools/eval/run_longmem.py --benchmark fixture_longmemeval --tier retrieval --arms all

    # P0 full baseline — real embedder (EXPERT_WORK_EVAL_EMBED_* env), full set
    python tools/eval/run_longmem.py --benchmark longmemeval_s --tier retrieval \
        --arms all --embedder real --update-baseline

    # P1 end-to-end — full run with checkpoint-resume (ANTHROPIC_API_KEY)
    python tools/eval/run_longmem.py --benchmark locomo --tier endtoend \
        --embedder real --results eval-out/locomo_endtoend.jsonl --update-baseline

CI never runs this file (run_baseline.py precedent) — the pipeline is
covered by ``test_longmem_*.py`` against the synthetic fixtures.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from longmem.adapter import RetrievalInstance, load_locomo, load_longmemeval
from longmem.download import DATASETS, ensure_dataset, file_sha256
from longmem.embedders import KeywordEmbedder, build_real_embedder
from longmem.endtoend import EndToEndConfig, run_end_to_end
from longmem.retrieval import evaluate_retrieval
from longmem.runner import (
    append_result,
    limit_instances,
    load_results,
    merge_results,
    resolve_arms,
    stratified_sample,
    summarise,
    update_baseline,
    with_top_k,
)

_FIXTURES = Path(__file__).parent / "datasets" / "longmem_fixture"
_DEFAULT_BASELINE = Path(__file__).parent / "baselines" / "longmem_baseline.yaml"

_BENCHMARKS = (
    "longmemeval_oracle",
    "longmemeval_s",
    "locomo",
    "fixture_longmemeval",
    "fixture_locomo",
)


def _load_benchmark(
    name: str, *, include_abstention: bool
) -> tuple[list[RetrievalInstance], str, str]:
    """Returns ``(instances, family, dataset_sha256)``."""
    if name == "fixture_longmemeval":
        path = _FIXTURES / "longmemeval_mini.json"
        return (
            load_longmemeval(path, include_abstention=include_abstention),
            "longmemeval",
            file_sha256(path),
        )
    if name == "fixture_locomo":
        path = _FIXTURES / "locomo_mini.json"
        return load_locomo(path), "locomo", file_sha256(path)
    if name == "locomo":
        path = ensure_dataset("locomo10")
        return load_locomo(path), "locomo", DATASETS["locomo10"].sha256
    path = ensure_dataset(name)
    return (
        load_longmemeval(path, include_abstention=include_abstention),
        "longmemeval",
        DATASETS[name].sha256,
    )


def _git_commit() -> str:
    git = shutil.which("git")
    if git is None:
        return "unknown"
    try:
        # Fixed argv, binary resolved via shutil.which — fingerprint-only.
        return subprocess.run(  # noqa: S603
            [git, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).parent,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return "unknown"


def _build_embedder(args: argparse.Namespace) -> object:
    if args.embedder != "real":
        return KeywordEmbedder()
    from longmem.download import cache_dir

    return build_real_embedder(
        cache_db=cache_dir() / "embeddings.sqlite", concurrency=args.concurrency
    )


def _build_llm(args: argparse.Namespace) -> tuple[Any, Any]:
    """Returns ``(llm_caller, judge)`` for the selected provider."""
    if args.llm_provider == "openai-compat":
        api_key = os.environ.get("EXPERT_WORK_EVAL_LLM_API_KEY")
        if not api_key:
            raise SystemExit("--llm-provider openai-compat needs EXPERT_WORK_EVAL_LLM_API_KEY")
        from longmem.openai_client import (
            DASHSCOPE_COMPAT_BASE_URL,
            OpenAICompatCaller,
            OpenAICompatTextJudge,
        )

        base_url = os.environ.get("EXPERT_WORK_EVAL_LLM_BASE_URL", DASHSCOPE_COMPAT_BASE_URL)
        return (
            OpenAICompatCaller(api_key=api_key, model=args.llm_model, base_url=base_url),
            OpenAICompatTextJudge(api_key=api_key, model=args.judge_model, base_url=base_url),
        )
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("--llm-provider anthropic needs ANTHROPIC_API_KEY")
    from longmem.anthropic_client import AnthropicCaller, AnthropicTextJudge

    return (
        AnthropicCaller(api_key=api_key, model=args.llm_model),
        AnthropicTextJudge(api_key=api_key, model=args.judge_model),
    )


async def _run_retrieval(args: argparse.Namespace) -> None:
    instances, _family, dataset_sha = _load_benchmark(args.benchmark, include_abstention=False)
    instances = limit_instances(instances, args.limit)
    embedder = _build_embedder(args)

    reranker = None
    arms = resolve_arms(args.arms)
    if any(cfg.rerank for cfg in arms.values()):
        from orchestrator.tools.llm_reranker import LLMReranker

        llm_caller, _judge = _build_llm(args)
        reranker = LLMReranker(llm_caller=llm_caller)

    section: dict[str, Any] = {}
    for arm_name, arm_config in arms.items():
        config = with_top_k(arm_config, args.top_k)
        report = await evaluate_retrieval(
            instances,
            embedder=embedder,  # type: ignore[arg-type]
            config=config,
            reranker=reranker if config.rerank else None,
            concurrency=args.concurrency,
        )
        section[arm_name] = report.to_dict()
        print(
            f"[{args.benchmark}/{arm_name}] n={report.n_instances} "
            f"session_recall@{config.top_k}={report.mean_session_recall:.4f} "
            f"turn_recall={report.mean_turn_recall:.4f} ndcg={report.mean_ndcg:.4f} "
            f"mrr={report.mean_mrr:.4f} blocked={report.blocked_writes} "
            f"deduped={report.deduped_writes}"
        )
    if args.update_baseline:
        update_baseline(
            Path(args.baseline_out),
            tier="retrieval",
            benchmark=args.benchmark,
            section=section,
            fingerprint={
                "embedder": args.embedder
                if args.embedder == "fake"
                else os.environ.get("EXPERT_WORK_EVAL_EMBED_MODEL", "real"),
                "dataset_sha256": dataset_sha,
                "commit": _git_commit(),
                "limit": args.limit,
            },
        )
        print(f"baseline updated: {args.baseline_out}")


async def _run_endtoend(args: argparse.Namespace) -> None:
    llm_caller, judge = _build_llm(args)

    instances, family, dataset_sha = _load_benchmark(args.benchmark, include_abstention=True)
    instances = limit_instances(instances, args.limit)
    instances = stratified_sample(list(instances), args.sample)
    embedder = _build_embedder(args)

    results_path = Path(args.results)
    prior = load_results(results_path)
    done_ids = frozenset(r.question_id for r in prior)
    if prior:
        print(f"resume: {len(prior)} verdict(s) loaded from {results_path}")

    report = await run_end_to_end(
        instances,
        benchmark="locomo" if family == "locomo" else "longmemeval",
        embedder=embedder,  # type: ignore[arg-type]
        llm_caller=llm_caller,
        judge=judge,
        config=EndToEndConfig(reconcile=not args.no_reconcile),
        done_ids=done_ids,
        on_result=lambda r: asyncio.to_thread(append_result, results_path, r),
        concurrency=args.concurrency,
    )
    merged = merge_results(prior, list(report.results))
    summary = summarise(merged)
    print(
        f"[{args.benchmark}/endtoend] n={summary['n_instances']} "
        f"accuracy={summary['accuracy']:.4f} (+{report.n_instances} new, "
        f"{report.memories_written} memories written this run)"
    )
    for qtype, row in summary["by_question_type"].items():
        print(f"  {qtype}: n={int(row['n'])} accuracy={row['accuracy']:.4f}")
    if args.update_baseline:
        update_baseline(
            Path(args.baseline_out),
            tier="endtoend",
            benchmark=args.benchmark,
            section=summary,
            fingerprint={
                "embedder": args.embedder
                if args.embedder == "fake"
                else os.environ.get("EXPERT_WORK_EVAL_EMBED_MODEL", "real"),
                "llm_provider": args.llm_provider,
                "llm_model": args.llm_model,
                "judge_model": args.judge_model,
                "judge_note": (
                    f"{args.llm_provider} judge (upstream protocols use gpt-4o judges) — CM-K6"
                ),
                "reconcile": not args.no_reconcile,
                "dataset_sha256": dataset_sha,
                "commit": _git_commit(),
                "limit": args.limit,
                "sample": args.sample,
            },
        )
        print(f"baseline updated: {args.baseline_out}")


def main() -> None:
    parser = argparse.ArgumentParser(description="CM-N5 LongMemEval/LoCoMo runner")
    parser.add_argument("--benchmark", required=True, choices=_BENCHMARKS)
    parser.add_argument("--tier", required=True, choices=("retrieval", "endtoend"))
    parser.add_argument("--arms", default="default", help="retrieval arms: comma list or 'all'")
    parser.add_argument("--embedder", default="fake", choices=("fake", "real"))
    parser.add_argument("--limit", type=int, default=0, help="instance cap; 0 = full set")
    parser.add_argument(
        "--sample",
        type=int,
        default=0,
        help="endtoend: deterministic stratified sample by question type; 0 = full set",
    )
    parser.add_argument("--top-k", type=int, default=None, help="override arm top_k")
    parser.add_argument("--results", default="eval-out/longmem_results.jsonl")
    parser.add_argument("--no-reconcile", action="store_true")
    parser.add_argument(
        "--llm-provider",
        default="openai-compat",
        choices=("openai-compat", "anthropic"),
        help="openai-compat = EXPERT_WORK_EVAL_LLM_* env (DashScope/Qwen default); "
        "anthropic = ANTHROPIC_API_KEY",
    )
    parser.add_argument(
        "--llm-model",
        default=None,
        help="defaults: EXPERT_WORK_EVAL_LLM_MODEL (openai-compat) / repo haiku pin (anthropic)",
    )
    parser.add_argument("--judge-model", default=None, help="defaults to --llm-model")
    parser.add_argument(
        "--concurrency", type=int, default=1, help="in-flight LLM/embedding work cap"
    )
    parser.add_argument("--baseline-out", default=str(_DEFAULT_BASELINE))
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args()

    if args.llm_model is None:
        if args.llm_provider == "openai-compat":
            args.llm_model = os.environ.get("EXPERT_WORK_EVAL_LLM_MODEL")
            if not args.llm_model and args.tier == "endtoend":
                raise SystemExit("set EXPERT_WORK_EVAL_LLM_MODEL or pass --llm-model")
        else:
            from longmem.anthropic_client import DEFAULT_EVAL_MODEL

            args.llm_model = DEFAULT_EVAL_MODEL
    if args.judge_model is None:
        args.judge_model = args.llm_model

    if args.tier == "retrieval":
        asyncio.run(_run_retrieval(args))
    else:
        asyncio.run(_run_endtoend(args))


if __name__ == "__main__":
    main()
