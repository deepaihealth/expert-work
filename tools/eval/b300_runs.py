"""B-300 评测里真正花钱的那几段循环(从 ``b300_eval`` 拆出):A 组本机提炼、B / C 组对话、判官,
以及试点量 token(:func:`pilot_tokens`)。每一下都经 ``b300_spend.Guard`` 先预留后发送。"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import b300_live as live  # noqa: E402
import b300_plan as plan  # noqa: E402
import b300_report as rep  # noqa: E402
import b300_spend as spend  # noqa: E402
from b300_phases import (  # noqa: E402
    EvalError,
    Phase,
    agent_code,
    all_scenarios,
    append_jsonl,
    inpod_rows,
    latest,
    read_jsonl,
    unit_key,
)

# --------------------------------------------------------------------------- run


def done_keys(out: Path, phase: str) -> set[str]:
    return {r["key"] for r in read_jsonl(out / phase / "records.jsonl") if not r.get("error")}


async def run_extraction_phase(
    phase: Phase,
    units: Sequence[Mapping[str, Any]],
    *,
    out: Path,
    guard: spend.Guard,
    vendors: Mapping[str, plan.Vendor],
    embedder: Any,
    emit: Callable[[str], None],
) -> None:
    import httpx

    scenarios = all_scenarios()
    done = done_keys(out, phase.name)
    sems = {p: asyncio.Semaphore(live.CONCURRENCY_PER_VENDOR) for p in vendors}
    async with httpx.AsyncClient() as http:

        async def one(u: Mapping[str, Any]) -> None:
            key = unit_key(u)
            if key in done or not guard_ok(guard):
                return
            v = vendors[u["vendor"]]
            base_url, api_key = live.vendor_env(v.provider)
            transport = live.ChatTransport(
                v.memory,
                base_url,
                api_key,
                guard,
                http,
                live.EXTRACT_MAX_TOKENS,
                provider=v.provider,
            )
            async with sems[u["vendor"]]:
                try:
                    result = await live.run_extraction(
                        scenarios[u["scenario_id"]],
                        transport=transport,
                        embedder=embedder,
                        item=key,
                        phase=phase.name,
                    )
                    kinds = {c["kind"] for r in result["rounds"] for c in r["calls"]}
                    if not phase.baseline and kinds and not kinds & {"pass1", "pass2"}:
                        # 审查 M2:被测代码没有两次提炼(PR 1 之前的提交、或提示词改了认不出来),
                        # A 组的结果全是空转 —— 记下这一条并停下,别接着花钱。
                        guard.block(f"{key}: no two-pass extraction seen (kinds {sorted(kinds)})")
                    append_jsonl(
                        out / phase.name / "records.jsonl", {**u, "key": key, "result": result}
                    )
                except (Exception, spend.SpendStopError) as exc:
                    append_jsonl(
                        out / phase.name / "records.jsonl", {**u, "key": key, "error": repr(exc)}
                    )
                    emit(f"{key}: {exc!r}")

        await asyncio.gather(*(one(u) for u in units))


def guard_ok(guard: spend.Guard) -> bool:
    return guard.blocked is None


async def run_conversation_phase(
    phase: Phase,
    units: Sequence[Mapping[str, Any]],
    *,
    out: Path,
    guard: spend.Guard,
    vendors: Mapping[str, plan.Vendor],
    base_url: str,
    api_key: str,
    step: int | None,
    emit: Callable[[str], None],
) -> None:
    """对话段:B、C1~C3、C5 提问 = 一段对话;C4 按 ``--step`` 1 / 2 / 3 分三次跑(中间要等写回、
    在 pod 里查状态)。"""
    import httpx

    scenarios = all_scenarios()
    records_path = out / phase.name / "records.jsonl"
    previous = {r["key"]: r for r in read_jsonl(records_path) if not r.get("error")}
    seeded = latest(inpod_rows(out, phase.name, "seed"), "key")
    sems = {p: asyncio.Semaphore(live.CONCURRENCY_PER_VENDOR) for p in vendors}
    async with httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=live.RUN_TIMEOUT_S,
    ) as http:

        async def one(u: Mapping[str, Any]) -> None:
            key = unit_key(u)
            s = scenarios[u["scenario_id"]]
            prior = previous.get(key, {})
            convs = list(prior.get("convs", []))
            index = (step or 1) - 1
            if len(convs) > index or not guard_ok(guard):
                return
            if len(convs) != index:
                emit(f"{key}: step {step} needs step {index} first; skipped")
                return
            seed_ids = (seeded.get(u["user"]) or {}).get("memory_ids", {})
            if s.seeds and not seed_ids:
                emit(f"{key}: not seeded yet (ingest the seed step first); skipped")
                return
            v = vendors[u["vendor"]]
            writeback = phase.kind == "c4" and index < 2
            agent = agent_code(v.provider, writeback=writeback)
            async with sems[u["vendor"]]:
                try:
                    conv = await live.run_conversation(
                        http,
                        agent=agent,
                        user=u["user"],
                        turn=s.conversations[index],
                        guard=guard,
                        models=[v.main, v.memory],
                        item=f"{key}|c{index}",
                        phase=phase.name,
                    )
                    if writeback:  # 先按上限预留,之后按 pod 里读出的实际用量结算(审查 H4)
                        conv["writeback_reservation"] = live.reserve_writeback(
                            guard, item=f"{key}|c{index}", model=v.memory, phase=phase.name
                        )
                    convs.append(conv)
                    append_jsonl(
                        records_path, {**u, "key": key, "seed_ids": seed_ids, "convs": convs}
                    )
                except (Exception, spend.SpendStopError) as exc:
                    append_jsonl(
                        records_path,
                        {**u, "key": key, "seed_ids": seed_ids, "convs": convs, "error": repr(exc)},
                    )
                    emit(f"{key}: {exc!r}")

        await asyncio.gather(*(one(u) for u in units))


async def run_judge(
    items: Sequence[rep.JudgeItem],
    *,
    out_file: Path,
    guard: spend.Guard,
    votes: int,
    phase: str,
    emit: Callable[[str], None],
) -> None:
    import httpx

    base_url, api_key = (
        os.environ.get(n)
        for n in ("EXPERT_WORK_EVAL_JUDGE_BASE_URL", "EXPERT_WORK_EVAL_JUDGE_API_KEY")
    )
    if not base_url or not api_key:
        raise EvalError("set EXPERT_WORK_EVAL_JUDGE_BASE_URL and EXPERT_WORK_EVAL_JUDGE_API_KEY")
    done = {r["key"] for r in read_jsonl(out_file)}
    sem = asyncio.Semaphore(4)
    async with httpx.AsyncClient() as http:
        judge = live.Judge(
            live.ChatTransport(
                plan.JUDGE_MODEL,
                base_url,
                api_key,
                guard,
                http,
                live.JUDGE_MAX_TOKENS,
                provider=plan.JUDGE_PROVIDER,
            ),
            votes,
        )

        async def one(item: rep.JudgeItem) -> None:
            if item.key in done or not guard_ok(guard):
                return
            async with sem:
                try:
                    got = await judge.vote_with_usage(item.prompt, item=item.key, phase=phase)
                except (Exception, spend.SpendStopError) as exc:
                    emit(f"{item.key}: {exc!r}")
                    return
            votes_only = [v for v, _u in got]
            append_jsonl(
                out_file,
                {
                    "key": item.key,
                    "check": item.check,
                    "votes": votes_only,
                    "verdict": live.majority(votes_only),
                    "usage": [u for _v, u in got],
                },
            )

        await asyncio.gather(*(one(i) for i in items))


# ------------------------------------------------------------------ 写回结算 / 试点量 token


def open_writeback_reservations(out: Path, phase: str) -> dict[str, list[str]]:
    """每个评测用户还没结算的写回预留(审查 H4)。"""
    by_user: dict[str, list[str]] = {}
    for r in {x["key"]: x for x in read_jsonl(out / phase / "records.jsonl")}.values():
        for conv in r.get("convs", []):
            call_id = conv.get("writeback_reservation")
            if call_id:
                by_user.setdefault(r["user"], []).append(call_id)
    return by_user


def settle_writeback_usage(
    out: Path, phase: str, rows: Sequence[Mapping[str, Any]], guard: spend.Guard
) -> float:
    """按 pod 里 ``writeback_usage`` 读出的实际用量结算写回预留;读不到的用户按上限。"""
    usage = {r["key"]: r.get("models") for r in rows if r.get("op") == "writeback_usage"}
    total = 0.0
    for user, call_ids in open_writeback_reservations(out, phase).items():
        pending = [c for c in call_ids if guard.open_cap(c) > 0]
        if pending:
            total += live.settle_writebacks(guard, pending, usage.get(user), phase=phase)
    return total


def _spread(samples: Sequence[tuple[int, int]]) -> dict[str, list[int]]:
    ins = [a for a, _b in samples]
    outs = [b for _a, b in samples]
    return {
        "low": [int(statistics.median(ins)), int(statistics.median(outs))],
        "high": [max(ins), max(outs)],
    }


def pilot_tokens(out: Path) -> dict[str, Any]:
    """试点量出来的每次调用 token(审查 M4):低档 = 中位数,高档 = 最大值;交给 ``--tokens``。
    量不到的种类(如主模型与记忆模型是同一个型号时的读时复核)不写,报价里保留原假设。"""
    samples: dict[str, list[tuple[int, int]]] = {}
    for phase_dir in sorted(p for p in out.iterdir() if p.is_dir()):
        for r in read_jsonl(phase_dir / "records.jsonl"):
            for round_ in (r.get("result") or {}).get("rounds", []):
                for c in round_["calls"]:
                    kind = "reconcile" if c["kind"] == "reconcile" else "extract"
                    if c["kind"] in {"pass1", "pass2", "extract_old", "reconcile"}:
                        samples.setdefault(kind, []).append((c["input_tokens"], c["output_tokens"]))
            for conv in r.get("convs", []):
                steps = int(conv.get("steps") or 0)
                main_out = sum(
                    int(b.get("output_tokens") or 0)
                    for b in conv.get("usage_by_model") or []
                    if b.get("model") not in {plan.CONSOLIDATION_MODEL}
                )
                for tokens_in in conv.get("step_input_tokens") or []:
                    if steps:
                        samples.setdefault("main", []).append((int(tokens_in), main_out // steps))
        for v in read_jsonl(phase_dir / "verdicts.jsonl"):
            for u in v.get("usage") or []:
                samples.setdefault("judge", []).append((u["input_tokens"], u["output_tokens"]))
        for row in inpod_rows(out, phase_dir.name, "consolidate"):
            if row.get("op") == "consolidate_total" and row.get("calls"):
                n = int(row["calls"])
                samples.setdefault("consolidate", []).append(
                    (row["input_tokens"] // n, row["output_tokens"] // n)
                )
    return {kind: _spread(rows) for kind, rows in sorted(samples.items()) if rows}
