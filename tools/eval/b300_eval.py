"""B-300 长期记忆来源评测 —— 命令行。

设计 ``docs/superpowers/specs/2026-11-11-b300-memory-provenance-design.md`` §8。

**会花钱的子命令**(``run`` / ``judge`` / ``calibrate``,以及 ``bundle --op consolidate``)
一律要带用户批过的报价编号 ``--approved-quote`` 与止损线 ``--stop-line``;其余离线。
上限 ¥400 跨全部报价累计(``b300_spend``),每次跑前先 ``quote``,会超就按 §8.6 的顺序删
(``b300_plan``),删完仍超就停下问用户。

    python tools/eval/b300_eval.py scenarios --out scenarios.json          # 离线:场景与覆盖检查
    python tools/eval/b300_eval.py quote --stop-line 400 [--single-vendor qwen] [--tokens t.json]
    python tools/eval/b300_eval.py manifests --out agents/                  # 临时评测智能体
    python tools/eval/b300_eval.py prepare --phase b --out OUT --tenant-id T --approved-quote Q
    python tools/eval/b300_eval.py bundle  --phase b --op recalled --out OUT --tenant-id T
    python tools/eval/b300_eval.py ingest  --phase b --op recalled --out OUT --file pod-out.jsonl
    python tools/eval/b300_eval.py run     --phase b --out OUT --approved-quote Q --stop-line 300 \\
        --release-tag f2a21c6b2
    python tools/eval/b300_eval.py judge   --phase b --out OUT --approved-quote Q --stop-line 300
    python tools/eval/b300_eval.py report  --out OUT

各段怎么接见 ``tools/eval/README.md`` 的 B-300 一节。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
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
import b300_scenarios as sc  # noqa: E402
import b300_spend as spend  # noqa: E402
from b300_outcomes import judge_items, latest_records, report  # noqa: E402
from b300_phases import (  # noqa: E402
    INPOD_SOURCE,
    PHASES,
    RERUN_SHARE,
    EvalError,
    Phase,
    agent_code,
    all_scenarios,
    append_jsonl,
    build_units,
    bundle_source,
    calibration_ok,
    check_labels,
    inpod_rows,
    latest,
    load_units,
    phase_dir,
    read_jsonl,
    recalled_payload,
    release_meta,
    seed_payload,
    unit_key,
    user_name,
    users_payload,
    write_meta,
)

__all__ = [
    "INPOD_SOURCE",
    "PHASES",
    "EvalError",
    "build_units",
    "bundle_source",
    "check_labels",
    "judge_items",
    "main",
    "release_meta",
    "report",
    "seed_payload",
    "users_payload",
    "write_meta",
]

# --------------------------------------------------------------------------- 报价 / 账


def _vendors(raw: Sequence[str] | None) -> list[plan.Vendor]:
    return [plan.parse_vendor(v) for v in raw] if raw else list(plan.DEFAULT_VENDORS)


def _tokens(path: Path | None) -> Mapping[str, Any]:
    if path is None:
        return plan.CALL_TOKENS
    return plan.load_token_overrides(json.loads(path.read_text(encoding="utf-8")))


def make_quote(args: argparse.Namespace, *, exclude: str | None = None) -> dict[str, Any]:
    vendors = _vendors(args.vendor)
    prices = plan.prices_for(vendors, accept_documented=args.accept_documented_prices)
    already = sum(spend.history_total(args.state_dir, exclude=exclude).values())
    return plan.quote(
        vendors,
        prices,
        already_spent=already,
        single_vendor=args.single_vendor,
        stop_line=args.stop_line,
        scenario_digest=sc.scenario_digest([*sc.build_all(), *sc.build_compare()]),
        include_compare=not args.no_compare,
        tokens=_tokens(args.tokens),
    )


def approved(args: argparse.Namespace) -> tuple[dict[str, Any], spend.Guard]:
    """重算报价(不计这个报价自己的账)并核对编号;拒绝超过 ¥400 总账的止损线。"""
    q = make_quote(args, exclude=args.approved_quote)
    if q["quote_id"] != args.approved_quote:
        raise EvalError(
            f"--approved-quote {args.approved_quote} does not match this plan's quote "
            f"{q['quote_id']}; run `quote` with the same arguments and get that one approved"
        )
    if q["status"] != "within_cap" and not getattr(args, "user_said", None):
        raise EvalError(
            "the quote says stop_and_ask (design §8.6 step 4): ask the user first; if they say go "
            "ahead, pass their words with --user-said (kept in the phase record)"
        )
    if q["already_spent_yuan"] + args.stop_line > plan.CAP_YUAN:
        raise EvalError(
            f"other quotes already spent ¥{q['already_spent_yuan']}; stop line ¥{args.stop_line} "
            f"would cross the ¥{plan.CAP_YUAN:g} cap"
        )
    prices = plan.prices_for(_vendors(args.vendor), accept_documented=args.accept_documented_prices)
    guard = spend.Guard.resume(
        stop_line=args.stop_line, prices=prices, state_dir=args.state_dir, quote_id=q["quote_id"]
    )
    return q, guard


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
                v.memory, base_url, api_key, guard, http, live.EXTRACT_MAX_TOKENS
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
                    if writeback:
                        live.charge_writeback(
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
                plan.JUDGE_MODEL, base_url, api_key, guard, http, live.JUDGE_MAX_TOKENS
            ),
            votes,
        )

        async def one(item: rep.JudgeItem) -> None:
            if item.key in done or not guard_ok(guard):
                return
            async with sem:
                try:
                    got = await judge.vote(item.prompt, item=item.key, phase=phase)
                except (Exception, spend.SpendStopError) as exc:
                    emit(f"{item.key}: {exc!r}")
                    return
            append_jsonl(
                out_file,
                {"key": item.key, "check": item.check, "votes": got, "verdict": live.majority(got)},
            )

        await asyncio.gather(*(one(i) for i in items))


# --------------------------------------------------------------------------- 命令行


def _stop_line(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {raw!r}") from exc
    if not math.isfinite(value) or not 0 < value <= plan.CAP_YUAN:
        raise argparse.ArgumentTypeError(f"must be in (0, {plan.CAP_YUAN:g}]")
    return value


def _quote_args(p: argparse.ArgumentParser, *, stop_required: bool) -> None:
    p.add_argument("--vendor", action="append", help="provider=main[:memory]; repeat; default four")
    p.add_argument(
        "--single-vendor", default=None, help="the one vendor for cuts 2 / 3 and baselines"
    )
    p.add_argument(
        "--stop-line",
        type=_stop_line,
        required=stop_required,
        default=None if stop_required else plan.CAP_YUAN,
    )
    p.add_argument("--tokens", type=Path, default=None, help="token overrides measured in a pilot")
    p.add_argument("--accept-documented-prices", action="store_true")
    p.add_argument(
        "--no-compare", action="store_true", help="leave out the §8.5 before/after count"
    )
    p.add_argument("--state-dir", type=Path, default=spend.DEFAULT_STATE_DIR)
    p.add_argument(
        "--user-said",
        default=None,
        help="the user's go-ahead when the quote is stop_and_ask (§8.6 step 4); recorded",
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="B-300 memory provenance eval (design §8)")
    sub = root.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scenarios")
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("quote")
    _quote_args(p, stop_required=False)
    p = sub.add_parser("manifests")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--vendor", action="append")
    for name in ("prepare", "bundle", "ingest", "run", "judge"):
        p = sub.add_parser(name)
        p.add_argument("--phase", choices=sorted(PHASES), required=True)
        p.add_argument("--out", type=Path, required=True)
        p.add_argument("--step", type=int, choices=(1, 2, 3), default=None)
        if name in {"prepare", "bundle"}:
            p.add_argument("--tenant-id", required=True)
        if name == "bundle":
            p.add_argument(
                "--op",
                choices=("seed", "recalled", "candidates", "consolidate", "state"),
                required=True,
            )
            p.add_argument("--bundle-out", type=Path, default=None)
        if name == "ingest":
            p.add_argument(
                "--op",
                choices=("seed", "recalled", "candidates", "consolidate", "state"),
                required=True,
            )
            p.add_argument("--file", type=Path, required=True)
        if name == "prepare":
            p.add_argument(
                "--reruns", action="store_true", help="add an attempt for excluded units"
            )
        if name in {"prepare", "bundle", "ingest", "run", "judge"}:
            _quote_args(p, stop_required=name in {"run", "judge", "bundle", "ingest"})
            p.add_argument("--approved-quote", required=True)
        if name == "run":
            p.add_argument("--release-tag", default=None)
            p.add_argument("--base-url", default=os.environ.get("EXPERT_WORK_EVAL_BASE_URL"))
    p = sub.add_parser("calibrate")
    p.add_argument("--labels", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    _quote_args(p, stop_required=True)
    p.add_argument("--approved-quote", required=True)
    p = sub.add_parser("report")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--quote-file", type=Path, default=None, help="the approved quote JSON")
    return root


def write_manifests(out: Path, vendors: Sequence[plan.Vendor]) -> list[Path]:
    """每家两个临时智能体:``eval-b300-<家>``(不写回;B / C 组提问)与 ``-wb``(写回;C4 造数)。
    开了长期记忆、没有沙箱工具;系统提示词一句通用话,不带领域词。"""
    import yaml

    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for v in vendors:
        for wb in (False, True):
            code = agent_code(v.provider, writeback=wb)
            manifest = {
                "apiVersion": "expert_work.io/v1",
                "kind": "Agent",
                "metadata": {"name": code, "version": "1.0.0", "tenant": "b300-eval"},
                "spec": {
                    "description": "B-300 eval temporary agent (synthetic content only).",
                    # 与 b302_ab.manifest 同一套沙箱写法(读上传的文件要用文件工具)。
                    "tenant_config": {"isolation_level": "dedicated_sandbox"},
                    "sandbox": {
                        "runtime": "gvisor",
                        "resources": {"cpu": "0.5", "memory": "512Mi"},
                        "network": {"egress": "none"},
                        "filesystem": {
                            "readonly_root": True,
                            "writable": ["/tmp"],  # noqa: S108 - the sandbox scratch dir
                            "persistent_workspace": True,
                        },
                    },
                    "model": {"provider": v.provider, "name": v.main},
                    "system_prompt": {
                        "template": (
                            "You are a helpful assistant. Read files the user uploads when asked."
                        )
                    },
                    "workflow": {"type": "react", "max_iterations": live.RUN_MAX_ITERATIONS},
                    "memory": {"long_term": {"write_back": wb, "retrieve_top_k": 5}},
                    "policies": {"run_deadline_s": 600, "token_budget": live.RUN_TOKEN_BUDGET},
                },
            }
            path = out / f"{code}.yaml"
            path.write_text(
                yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            paths.append(path)
    return paths


def _prepare(args: argparse.Namespace, emit: Callable[[str], None]) -> None:
    q, _guard = approved_or_quote(args)
    phase = PHASES[args.phase]
    path = phase_dir(args.out, phase.name) / "units.json"
    if args.reruns:
        units = load_units(args.out, phase.name)
        _items, facts = judge_items(args.out, phase)
        records = latest_records(args.out, phase.name)
        # 没召回到种下的那条(§8.3)、C4 的前提不成立(§8.7)→ 不计入、补跑;还没查的(None)不算。
        retry = [
            records[k]
            for k, info in facts.items()
            if info.get("recall_ok") is False or info.get("precondition") is False
        ]
        retry += [r for r in records.values() if r.get("error")]
        base = [u for u in units if u["attempt"] == 0]
        if len(units) - len(base) + len(retry) > RERUN_SHARE * len(base):
            raise EvalError(
                f"{len(retry)} more reruns would pass {RERUN_SHARE:.0%} of the phase; ask the user"
            )
        for r in retry:
            attempt = int(r["attempt"]) + 1
            vendor = None if phase.kind == "c5" else r["vendor"]
            units.append(
                {
                    **{
                        k: r[k]
                        for k in ("phase", "scenario_id", "cls", "domain", "vendor", "repeat")
                    },
                    "attempt": attempt,
                    "user": user_name(
                        q["quote_id"], phase.name, r["scenario_id"], vendor, r["repeat"], attempt
                    ),
                }
            )
        emit(f"{len(retry)} reruns added")
    else:
        if path.exists():
            raise EvalError(f"{path} exists; use --reruns to add attempts")
        units = build_units(phase, q)
    path.write_text(json.dumps(units, indent=1, ensure_ascii=False), encoding="utf-8")
    emit(f"{len(units)} units -> {path}")
    seeded = latest(inpod_rows(args.out, phase.name, "seed"), "key")
    unseeded = [u for u in units if u["user"] not in seeded]
    if phase.kind != "extraction" and any(
        all_scenarios()[u["scenario_id"]].seeds for u in unseeded
    ):
        target = phase_dir(args.out, phase.name) / "bundle-seed.py"
        target.write_text(
            bundle_source(seed_payload(phase, unseeded, args.tenant_id)), encoding="utf-8"
        )
        emit(f"seed bundle -> {target} (kubectl exec -i POD -- python - < it)")


def approved_or_quote(args: argparse.Namespace) -> tuple[dict[str, Any], spend.Guard | None]:
    """``prepare`` 不花钱,但单元(用户名、跑哪几家)取决于报价 —— 同样核对报价编号。"""
    if getattr(args, "stop_line", None) is None:
        args.stop_line = plan.CAP_YUAN
    q = make_quote(args, exclude=args.approved_quote)
    if q["quote_id"] != args.approved_quote:
        raise EvalError(f"--approved-quote does not match this plan's quote {q['quote_id']}")
    return q, None


def _bundle(args: argparse.Namespace, emit: Callable[[str], None]) -> None:
    phase = PHASES[args.phase]
    units = load_units(args.out, phase.name)
    if args.op == "seed":
        payload = seed_payload(phase, units, args.tenant_id)
    elif args.op == "recalled":
        payload = recalled_payload(args.out, phase.name, args.tenant_id)
    elif args.op == "consolidate":
        _q, guard = approved(args)
        users = users_payload("consolidate", units, args.tenant_id)["users"]
        max_calls = len(users) * int(plan.CONSOLIDATE_PER_SWEEP[1])
        price = guard.prices[plan.CONSOLIDATION_MODEL]
        cap = max_calls * spend.call_cap_yuan(
            price, live.MAX_CALL_INPUT_TOKENS, live.EXTRACT_MAX_TOKENS
        )
        call_id = guard.reserve(
            item=f"{phase.name}|consolidate",
            model=plan.CONSOLIDATION_MODEL,
            cap=cap,
            phase=phase.name,
        )
        (phase_dir(args.out, phase.name) / "consolidate-reserve.json").write_text(
            json.dumps({"call_id": call_id, "cap": cap, "max_calls": max_calls}), encoding="utf-8"
        )
        payload = {
            "op": "consolidate",
            "users": users,
            "max_calls": max_calls,
            "model": plan.CONSOLIDATION_MODEL,
        }
    else:
        payload = users_payload(args.op, units, args.tenant_id)
    suffix = f"-step{args.step}" if args.step else ""
    target = args.bundle_out or phase_dir(args.out, phase.name) / f"bundle-{args.op}{suffix}.py"
    target.write_text(bundle_source(payload), encoding="utf-8")
    emit(f"bundle -> {target}")


def _ingest(args: argparse.Namespace, emit: Callable[[str], None]) -> None:
    phase = PHASES[args.phase]
    rows = [
        json.loads(line)
        for line in args.file.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("{")
    ]
    suffix = f"-step{args.step}" if args.step else ""
    target = phase_dir(args.out, phase.name) / f"inpod-{args.op}{suffix}.jsonl"
    for row in rows:
        append_jsonl(target, row)
    emit(f"{len(rows)} rows -> {target}")
    if args.op == "consolidate":
        _q, guard = approved(args)
        reserve = json.loads(
            (args.out / phase.name / "consolidate-reserve.json").read_text(encoding="utf-8")
        )
        total = next((r for r in rows if r.get("op") == "consolidate_total"), None)
        if total is None:
            guard.settle(
                reserve["call_id"],
                item=f"{phase.name}|consolidate",
                model=plan.CONSOLIDATION_MODEL,
                phase=phase.name,
                cost=reserve["cap"],
                reason="cap_no_usage",
            )
            guard.block("consolidation output has no token total; charged at the cap")
            return
        price = guard.prices[plan.CONSOLIDATION_MODEL]
        cost = (total["input_tokens"] * price.input + total["output_tokens"] * price.output) / 1e6
        guard.settle(
            reserve["call_id"],
            item=f"{phase.name}|consolidate",
            model=plan.CONSOLIDATION_MODEL,
            phase=phase.name,
            cost=cost,
        )
        emit(f"consolidation settled ¥{cost:.4f} ({total['calls']} calls)")


def _run(args: argparse.Namespace, emit: Callable[[str], None]) -> None:
    phase = PHASES[args.phase]
    q, guard = approved(args)
    meta = {**release_meta(phase, args.release_tag), "user_said": args.user_said}
    write_meta(args.out, phase, meta)
    units = load_units(args.out, phase.name)
    vendors = {v["provider"]: plan.Vendor(**v) for v in q["vendors"]}
    emit(f"approved quote {q['quote_id']}; stop line ¥{args.stop_line}; already ¥{guard.spent:.2f}")
    with spend.exclusive(args.state_dir, q["quote_id"]):
        if phase.kind == "extraction":
            from longmem.embedders import build_real_embedder

            asyncio.run(
                run_extraction_phase(
                    phase,
                    units,
                    out=args.out,
                    guard=guard,
                    vendors=vendors,
                    embedder=build_real_embedder(),
                    emit=emit,
                )
            )
        else:
            api_key = os.environ.get("EXPERT_WORK_EVAL_API_KEY")
            if not args.base_url or not api_key:
                raise EvalError("conversation phases need --base-url and EXPERT_WORK_EVAL_API_KEY")
            if phase.kind == "c4" and args.step is None:
                raise EvalError(
                    "c4 runs in three steps: --step 1, then 2, then 3 (state checks between)"
                )
            asyncio.run(
                run_conversation_phase(
                    phase,
                    units,
                    out=args.out,
                    guard=guard,
                    vendors=vendors,
                    base_url=args.base_url,
                    api_key=api_key,
                    step=args.step,
                    emit=emit,
                )
            )
    emit(f"spent on this quote ¥{guard.spent:.2f}; blocked={guard.blocked}")


def _judge(args: argparse.Namespace, emit: Callable[[str], None]) -> None:
    phase = PHASES[args.phase]
    q, guard = approved(args)
    if not calibration_ok(args.out):
        raise EvalError(
            "the judge is not calibrated (≥ 18 / 20 on the human labels, §8.3); run `calibrate`"
        )
    items, _facts = judge_items(args.out, phase)
    votes = plan.A_JUDGE_VOTES if phase.kind == "extraction" else plan.JUDGE_VOTES
    with spend.exclusive(args.state_dir, q["quote_id"]):
        asyncio.run(
            run_judge(
                items,
                out_file=args.out / phase.name / "verdicts.jsonl",
                guard=guard,
                votes=votes,
                phase=phase.name,
                emit=emit,
            )
        )
    emit(f"{len(items)} items; spent on this quote ¥{guard.spent:.2f}; blocked={guard.blocked}")


def _calibrate(args: argparse.Namespace, emit: Callable[[str], None]) -> None:
    q, guard = approved(args)
    labels = read_jsonl(args.labels)
    check_labels(labels)
    items = [
        rep.JudgeItem(f"cal|{i}", "calibration", row["prompt"]) for i, row in enumerate(labels)
    ]
    out_file = args.out / "calibration-votes.jsonl"
    args.out.mkdir(parents=True, exist_ok=True)
    with spend.exclusive(args.state_dir, q["quote_id"]):
        asyncio.run(
            run_judge(
                items,
                out_file=out_file,
                guard=guard,
                votes=plan.JUDGE_VOTES,
                phase="calibration",
                emit=emit,
            )
        )
    got = latest(read_jsonl(out_file), "key")
    agree = sum(
        got.get(f"cal|{i}", {}).get("verdict") == bool(row["label"]) for i, row in enumerate(labels)
    )
    (args.out / "calibration.json").write_text(
        json.dumps({"judge_model": plan.JUDGE_MODEL, "agree": agree, "of": len(labels)}),
        encoding="utf-8",
    )
    emit(f"judge agrees on {agree} / {len(labels)} (need ≥ {plan.CALIBRATION_MIN_AGREE})")


def main(argv: Sequence[str] | None = None, *, emit: Callable[[str], None] = print) -> int:
    args = parser().parse_args(argv)
    try:
        if args.cmd == "scenarios":
            everything = [*sc.build_all(), *sc.build_compare()]
            problems = sc.coverage_problems(sc.build_all())
            args.out.write_text(
                json.dumps(sc.to_jsonable(everything), indent=1, ensure_ascii=False),
                encoding="utf-8",
            )
            emit(
                json.dumps(
                    {
                        "scenarios": len(everything),
                        "digest": sc.scenario_digest(everything),
                        "problems": problems,
                    }
                )
            )
            return 1 if problems else 0
        if args.cmd == "quote":
            emit(json.dumps(make_quote(args), indent=2, ensure_ascii=False))
            return 0
        if args.cmd == "manifests":
            for path in write_manifests(args.out, _vendors(args.vendor)):
                emit(str(path))
            return 0
        if args.cmd == "report":
            q = json.loads(args.quote_file.read_text(encoding="utf-8")) if args.quote_file else None
            emit(json.dumps(report(args.out, q), indent=2, ensure_ascii=False))
            return 0
        handler = {
            "prepare": _prepare,
            "bundle": _bundle,
            "ingest": _ingest,
            "run": _run,
            "judge": _judge,
            "calibrate": _calibrate,
        }[args.cmd]
        handler(args, emit)
        return 0
    except (EvalError, plan.PlanError, spend.LockedError) as exc:
        emit(f"error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
