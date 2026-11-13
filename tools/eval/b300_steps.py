"""B-300 评测的 ``prepare`` / ``bundle`` / ``ingest`` 三个子命令(从 ``b300_eval`` 拆出)。

``bundle`` 生成在控制面进程里跑的自包含脚本(``b300_inpod``),``ingest`` 收它的输出。花钱的只有
整理(``--op consolidate``):``bundle`` 时按上限预留(每生成一次一个 nonce,一份预留),``ingest``
时按 pod 报回的 token 结算;同一个 nonce 的输出收了两次 = 那份脚本跑了两次,第二次的花费照实补记并
停下。带写回的 C4 对话:``--op writeback_usage`` 读出实际用量,结算 ``run`` 时留着的写回预留。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import b300_live as live  # noqa: E402
import b300_plan as plan  # noqa: E402
import b300_runs as runs  # noqa: E402
import b300_spend as spend  # noqa: E402
from b300_approve import approved, check_quote, spending  # noqa: E402
from b300_outcomes import judge_items, latest_records  # noqa: E402
from b300_phases import (  # noqa: E402
    PHASES,
    RERUN_SHARE,
    EvalError,
    all_scenarios,
    append_jsonl,
    build_units,
    bundle_source,
    earliest_start,
    inpod_rows,
    latest,
    load_units,
    phase_dir,
    read_jsonl,
    recalled_payload,
    save_units,
    seed_payload,
    user_name,
    users_payload,
    writeback_usage_payload,
)

#: 整理一次外层调用的上限:路由器在结构化输出没过校验时最多再发 2 次(``MAX_VALIDATION_RETRIES``),
#: 回包里只有最后那次的 token;输出不设上限(产品不传 ``max_tokens``),按 16k 算。
CONSOLIDATION_ATTEMPTS = 3
CONSOLIDATION_MAX_OUTPUT = 16_000
PREFLIGHT_FILE = "preflight.json"


def preflight_ok(out: Path) -> bool:
    path = out / PREFLIGHT_FILE
    return path.exists() and bool(json.loads(path.read_text(encoding="utf-8")).get("ok"))


def _consolidation_call_cap(price: Any) -> float:
    return spend.call_cap_yuan(price, live.MAX_CALL_INPUT_TOKENS, CONSOLIDATION_MAX_OUTPUT)


# --------------------------------------------------------------------------- prepare


def rerun_units(
    units: Sequence[Mapping[str, Any]],
    records: Mapping[str, Mapping[str, Any]],
    facts: Mapping[str, Mapping[str, Any]],
    *,
    quote_id: str,
    shared_user: bool,
) -> list[dict[str, Any]]:
    """要补跑的单元:没召回到种下的那条、前提不成立(§8.3 / §8.7)。出错的那几条**不**另开一次 ——
    它们在原地续跑(审查 LOW:以前两边都跑,同一条跑两遍)。同一个场景 / 次 / 家只加一次。"""
    want = {
        k
        for k, info in facts.items()
        if info.get("recall_ok") is False or info.get("precondition") is False
    }
    have = {(u["scenario_id"], u["vendor"], u["repeat"], u["attempt"]) for u in units}
    added: list[dict[str, Any]] = []
    for key in sorted(want):
        r = records[key]
        attempt = int(r["attempt"]) + 1
        if (r["scenario_id"], r["vendor"], r["repeat"], attempt) in have:
            continue
        have.add((r["scenario_id"], r["vendor"], r["repeat"], attempt))
        vendor = None if shared_user else r["vendor"]
        added.append(
            {
                **{k: r[k] for k in ("phase", "scenario_id", "cls", "domain", "vendor", "repeat")},
                "attempt": attempt,
                "user": user_name(
                    quote_id, r["phase"], r["scenario_id"], vendor, r["repeat"], attempt
                ),
            }
        )
    return added


def prepare(args: argparse.Namespace, emit: Callable[[str], None]) -> None:
    q = check_quote(args)
    phase = PHASES[args.phase]
    path = phase_dir(args.out, phase.name) / "units.json"
    if args.reruns:
        units = load_units(args.out, phase.name, q["quote_id"])
        _items, facts = judge_items(args.out, phase)
        added = rerun_units(
            units,
            latest_records(args.out, phase.name),
            facts,
            quote_id=q["quote_id"],
            shared_user=phase.kind == "c5",
        )
        base = [u for u in units if u["attempt"] == 0]
        if len(units) - len(base) + len(added) > RERUN_SHARE * len(base):
            raise EvalError(
                f"{len(added)} more reruns would pass {RERUN_SHARE:.0%} of the phase; ask the user"
            )
        units = [*units, *added]
        emit(f"{len(added)} reruns added")
    else:
        if path.exists():
            raise EvalError(f"{path} exists; use --reruns to add attempts")
        units = build_units(phase, q)
    save_units(args.out, phase.name, units, q)
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


# --------------------------------------------------------------------------- bundle


def _reserve_consolidation(
    args: argparse.Namespace, phase: str, users: Sequence[Any]
) -> dict[str, Any]:
    with spending(args):
        _q, guard = approved(args, cmd=f"bundle consolidate {phase}", phase=phase)
        max_calls = len(users) * int(plan.CONSOLIDATE_PER_SWEEP[1])
        cap = (
            max_calls
            * CONSOLIDATION_ATTEMPTS
            * _consolidation_call_cap(guard.prices[plan.CONSOLIDATION_MODEL])
        )
        nonce = uuid.uuid4().hex
        call_id = guard.reserve(
            item=f"{phase}|consolidate|{nonce}",
            model=plan.CONSOLIDATION_MODEL,
            cap=cap,
            phase=phase,
        )
    row = {"nonce": nonce, "call_id": call_id, "cap": cap, "max_calls": max_calls}
    append_jsonl(phase_dir(args.out, phase) / "consolidate-reserve.jsonl", row)
    return row


def bundle(args: argparse.Namespace, emit: Callable[[str], None]) -> None:
    phase = PHASES[args.phase]
    q = check_quote(args)
    if args.op == "preflight":
        payload: dict[str, Any] = {"op": "preflight", "tenant_id": args.tenant_id}
    else:
        units = load_units(args.out, phase.name, q["quote_id"])
        if args.op == "seed":
            payload = seed_payload(phase, units, args.tenant_id)
        elif args.op == "recalled":
            payload = recalled_payload(args.out, phase.name, args.tenant_id)
        elif args.op == "writeback_usage":
            reservations = runs.open_writeback_reservations(args.out, phase.name)
            run_ids = {user: [r["run_id"] for r in rows] for user, rows in reservations.items()}
            payload = writeback_usage_payload(
                units, args.tenant_id, earliest_start(args.out, phase.name), run_ids
            )
        elif args.op == "consolidate":
            users = users_payload("consolidate", units, args.tenant_id)["users"]
            reserve = _reserve_consolidation(args, phase.name, users)
            payload = {
                "op": "consolidate",
                "users": users,
                "max_calls": reserve["max_calls"],
                "model": plan.CONSOLIDATION_MODEL,
                "nonce": reserve["nonce"],
            }
        else:
            payload = users_payload(args.op, units, args.tenant_id)
    suffix = f"-step{args.step}" if args.step else ""
    target = args.bundle_out or phase_dir(args.out, phase.name) / f"bundle-{args.op}{suffix}.py"
    target.write_text(bundle_source(payload), encoding="utf-8")
    emit(f"bundle -> {target}")


# --------------------------------------------------------------------------- ingest


def settle_consolidation(
    out: Path, phase: str, rows: Sequence[Mapping[str, Any]], guard: spend.Guard
) -> float:
    """按 pod 报回的 token 结算整理的预留;没过结构化校验的调用另按 2 次重试的上限补记(那几次的
    token 回包里没有)。同一个 nonce 收第二次 = 脚本跑了两次:照实补记,并停下。"""
    reserves = {r["nonce"]: r for r in read_jsonl(out / phase / "consolidate-reserve.jsonl")}
    price = guard.prices[plan.CONSOLIDATION_MODEL]
    total = 0.0
    for row in rows:
        if row.get("op") != "consolidate_total":
            continue
        reserve = reserves.get(str(row.get("nonce")))
        if reserve is None:
            raise EvalError(f"consolidation output with an unknown nonce {row.get('nonce')!r}")
        retries = int(row.get("unparsed_calls") or 0) * (CONSOLIDATION_ATTEMPTS - 1)
        cost = (row["input_tokens"] * price.input + row["output_tokens"] * price.output) / 1e6
        cost += retries * _consolidation_call_cap(price)
        item = f"{phase}|consolidate|{reserve['nonce']}"
        if guard.open_cap(reserve["call_id"]) > 0:
            total += guard.settle(
                reserve["call_id"],
                item=item,
                model=plan.CONSOLIDATION_MODEL,
                phase=phase,
                cost=cost,
            )
        else:
            total += guard.settle(
                uuid.uuid4().hex,
                item=f"{item}|again",
                model=plan.CONSOLIDATION_MODEL,
                phase=phase,
                cost=cost,
                reason="ran_twice",
            )
            guard.block(
                f"consolidation bundle {reserve['nonce']} ran more than once; spend recorded"
            )
    return total


def ingest(args: argparse.Namespace, emit: Callable[[str], None]) -> None:
    phase = PHASES[args.phase]
    rows = [
        json.loads(line)
        for line in args.file.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("{")
    ]
    if args.op == "preflight":
        (row,) = [r for r in rows if r.get("op") == "preflight"]
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / PREFLIGHT_FILE).write_text(json.dumps(row), encoding="utf-8")
        emit(f"preflight ok={row['ok']}: {json.dumps(row)}")
        return
    suffix = f"-step{args.step}" if args.step else ""
    target = phase_dir(args.out, phase.name) / f"inpod-{args.op}{suffix}.jsonl"
    for row in rows:
        append_jsonl(target, row)
    emit(f"{len(rows)} rows -> {target}")
    if args.op in {"consolidate", "writeback_usage"}:
        with spending(args):
            _q, guard = approved(
                args, cmd=f"ingest {args.op} {phase.name}", phase=phase.name, allow_blocked=True
            )
            if args.op == "consolidate":
                cost = settle_consolidation(args.out, phase.name, rows, guard)
            else:
                result = runs.settle_writeback_usage(args.out, phase.name, rows, guard)
                cost = float(result["settled_yuan"])
                emit(json.dumps(result))
        emit(f"{args.op} settled ¥{cost:.4f}; blocked={guard.blocked}")
