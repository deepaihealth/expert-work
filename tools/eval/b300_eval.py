"""B-300 长期记忆来源评测 —— 命令行。

设计 ``docs/superpowers/specs/2026-11-11-b300-memory-provenance-design.md`` §8。

**会花钱的子命令**(``run`` / ``judge`` / ``calibrate``,``bundle --op consolidate``,以及结算用的
``ingest --op consolidate|writeback_usage``)一律要带用户批过的报价编号 ``--approved-quote`` 与止损线
``--stop-line``,并独占状态目录的锁(``b300_spend.state_lock``);其余离线。上限 ¥400 跨全部报价
累计,每次跑前先 ``quote``,会超就按 §8.6 的顺序删(``b300_plan``),删完仍超就停下问用户。止损线
按 B → C → A 分给三段(分预算)。``--pilot`` 是先跑的小试点(单独报价、单独批),量出的 token 用
``pilot-tokens`` 写成 ``--tokens`` 的表,再报正式的价。

    python tools/eval/b300_eval.py scenarios --out scenarios.json          # 离线:场景与覆盖检查
    python tools/eval/b300_eval.py quote --stop-line 400 [--single-vendor qwen] [--pilot]
    python tools/eval/b300_eval.py manifests --out agents/                  # 临时评测智能体
    python tools/eval/b300_eval.py prepare --phase b --out OUT --tenant-id T --approved-quote Q
    python tools/eval/b300_eval.py bundle  --phase b --op recalled --out OUT --tenant-id T ...
    python tools/eval/b300_eval.py ingest  --phase b --op recalled --out OUT --file out.jsonl ...
    python tools/eval/b300_eval.py run     --phase b --out OUT --approved-quote Q --stop-line 300 \\
        --release-tag f2a21c6b2
    python tools/eval/b300_eval.py judge   --phase b --out OUT --approved-quote Q --stop-line 300
    python tools/eval/b300_eval.py pilot-tokens --out PILOT_OUT --write tokens.json
    python tools/eval/b300_eval.py report  --out OUT

B-300 重做(设计 ``docs/superpowers/specs/2026-11-15-b300-extraction-redesign-design.md`` §11)的
回归闸门是单独一份报价 ``quote --phases g``,段 ``gate``;正式一轮的段是 ``a`` 与 ``s`` / ``shape``
/ ``a13`` / ``a14`` / ``a15`` / ``sd``(都在 A 段)。命令见 ``tools/eval/README.md``。

各段怎么接见 ``tools/eval/README.md`` 的 B-300 一节。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
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
import b300_runs as runs  # noqa: E402
import b300_scenarios as sc  # noqa: E402
import b300_spend as spend  # noqa: E402
import b300_steps as steps  # noqa: E402
from b300_approve import approved, make_quote, spending, vendors_of  # noqa: E402
from b300_outcomes import judge_items, latest_records, report  # noqa: E402
from b300_phases import (  # noqa: E402
    GATE_PHASES,
    INPOD_SOURCE,
    PHASES,
    EvalError,
    agent_code,
    build_units,
    bundle_source,
    calibration_ok,
    calibration_path,
    check_labels,
    latest,
    load_units,
    meaningful_text,
    progress,
    read_jsonl,
    release_meta,
    seed_payload,
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

_INPOD_OPS = (
    "seed",
    "recalled",
    "candidates",
    "consolidate",
    "state",
    "writeback_usage",
    "preflight",
)


# --------------------------------------------------------------------------- 参数


def _stop_line(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {raw!r}") from exc
    if not math.isfinite(value) or not 0 < value <= plan.CAP_YUAN:
        raise argparse.ArgumentTypeError(f"must be in (0, {plan.CAP_YUAN:g}]")
    return value


def _price(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a positive number (yuan per million tokens)")
    return value


def _words(raw: str) -> str:
    try:
        return meaningful_text(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _quote_args(p: argparse.ArgumentParser, *, stop_required: bool) -> None:
    p.add_argument("--vendor", action="append", help="provider=main[:memory]; repeat; default four")
    p.add_argument(
        "--single-vendor", default=None, help="one vendor for cuts 2 / 3, baselines, pilot"
    )
    p.add_argument(
        "--stop-line",
        type=_stop_line,
        required=stop_required,
        default=None,  # 不给:正式报价 ¥400,试点取它自己的高档估计(审查 M-c)
    )
    p.add_argument("--tokens", type=Path, default=None, help="token table measured in a pilot")
    p.add_argument("--accept-documented-prices", action="store_true")
    p.add_argument("--no-compare", action="store_true", help="leave out the §8.5 count")
    p.add_argument("--pilot", action="store_true", help="the small pilot plan (its own quote)")
    p.add_argument("--a-judge-votes", type=int, choices=(1, 3), default=plan.A_JUDGE_VOTES)
    p.add_argument(
        "--gate-judge-votes",
        type=int,
        choices=(1, 3),
        default=plan.A_JUDGE_VOTES,
        help="judge votes in the regression gate (G); the quote prices 1 and 3 votes either way",
    )
    p.add_argument("--embed-price", type=_price, default=None, help="yuan / M tokens, if known")
    p.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help=f"tests only ({spend.TEST_STATE_DIR_ENV}=1); spending always uses the default dir",
    )
    p.add_argument("--phases", default=None, help="phases this quote covers, e.g. b or b,c,a")
    p.add_argument("--allocation", default=None, help="sub-budgets, e.g. b=250,c=100,a=50")
    p.add_argument("--progress", type=Path, default=None, help="`progress` output: remaining work")
    p.add_argument(
        "--user-said", type=_words, default=None, help="the user's go-ahead on stop_and_ask; kept"
    )
    p.add_argument(
        "--unblock", type=_words, default=None, metavar="REASON", help="go on after a recorded stop"
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
            p.add_argument("--op", choices=_INPOD_OPS, required=True)
            p.add_argument("--bundle-out", type=Path, default=None)
        if name == "ingest":
            p.add_argument("--op", choices=_INPOD_OPS, required=True)
            p.add_argument("--file", type=Path, required=True)
        if name == "prepare":
            p.add_argument("--reruns", action="store_true", help="add attempts for excluded units")
            p.add_argument(
                "--resume-from",
                type=Path,
                default=None,
                help="old out dir: carry finished records over, prepare only the rest",
            )
            p.add_argument(
                "--release-tag", default=None, help="with --resume-from: the release to run on"
            )
        _quote_args(p, stop_required=name in {"run", "judge", "bundle", "ingest"})
        p.add_argument("--approved-quote", required=True)
        if name == "run":
            p.add_argument("--release-tag", default=None)
            p.add_argument("--base-url", default=os.environ.get("EXPERT_WORK_EVAL_BASE_URL"))
    p = sub.add_parser("calibrate")
    p.add_argument("--labels", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--set", dest="which", choices=("a", "b"), default="b")
    _quote_args(p, stop_required=True)
    p.add_argument("--approved-quote", required=True)
    p = sub.add_parser("labels-todo")  # 不花钱:把已经跑出来的记录变成待人工标注的清单
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--phase", action="append", required=True, choices=sorted(PHASES))
    p.add_argument("--write", type=Path, required=True)
    p.add_argument("--pick", type=int, default=plan.CALIBRATION_ITEMS)
    p = sub.add_parser("progress")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--write", type=Path, required=True)
    p = sub.add_parser("pilot-tokens")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--write", type=Path, required=True)
    p = sub.add_parser("report")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--quote-file", type=Path, default=None, help="the approved quote JSON")
    return root


# --------------------------------------------------------------------------- 临时智能体


def write_manifests(out: Path, vendors: Sequence[plan.Vendor]) -> list[Path]:
    """每家两个临时智能体:``eval-b300-<家>``(不写回;B / C 组提问)与 ``-wb``(写回;C4 造数)。
    开了长期记忆;系统提示词一句通用话,不带领域词。配置格式要求 ``sandbox`` 段,读上传的文件也要
    文件工具,所以带一个最小的沙箱(不出网)—— 沙箱时长不是模型花费,报价的 ``not_priced`` 里写明。"""
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
                    # 与 b302_ab.manifest 同一套沙箱写法。
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


# --------------------------------------------------------------------------- 花钱的子命令


def _run(args: argparse.Namespace, emit: Callable[[str], None]) -> None:
    phase = PHASES[args.phase]
    if phase.kind != "extraction" and not steps.preflight_ok(args.out):
        raise EvalError(
            "no passing preflight for the eval tenant (memory purge and due review must be off): "
            "`bundle --op preflight`, exec it in the control plane, `ingest --op preflight`"
        )
    with spending(args):
        q, guard = approved(args, cmd=f"run {phase.name}", phase=phase.name)
        units = load_units(args.out, phase.name, q["quote_id"])
        meta = {**release_meta(phase, args.release_tag), "user_said": args.user_said}
        write_meta(args.out, phase, meta, step=args.step)
        vendors = {v["provider"]: plan.Vendor(**v) for v in q["vendors"]}
        emit(f"approved quote {q['quote_id']}; stop ¥{args.stop_line}; spent ¥{guard.spent:.2f}")
        if phase.kind == "extraction":
            from longmem.embedders import build_real_embedder

            asyncio.run(
                runs.run_extraction_phase(
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
                raise EvalError("c4 runs in three steps: --step 1, then 2, then 3")
            asyncio.run(
                runs.run_conversation_phase(
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


def a_votes_ready(out: Path, q: Mapping[str, Any], phase_name: str = "") -> int:
    """A 组判官(审查 M3):按报价的票数校准过(≥ 18 / 20)才判;一票没过就要按 3 票重新报价、
    重新校准。回归闸门(``gate``)用报价里 G 段的票数(审查 M5)。"""
    judge = q["judge"]
    votes = int(
        judge.get("gate_votes", judge["a_group_votes"])
        if phase_name in GATE_PHASES
        else judge["a_group_votes"]
    )
    path = calibration_path(out, "a")
    if path.exists():
        made_with = json.loads(path.read_text(encoding="utf-8")).get("wording_digest")
        if made_with != rep.a_judge_wording_digest():
            raise EvalError(
                "the A-group calibration was made with other judge wording "
                f"({made_with or 'not recorded'}); run `calibrate --set a` again, then judge"
            )
    if calibration_ok(out, "a", votes=votes):
        return votes
    if votes == 1 and calibration_path(out, "a").exists():
        raise EvalError(
            "the A-group judge failed calibration at 1 vote: re-quote with --a-judge-votes 3, "
            "get it approved, run `calibrate --set a` again, then judge"
        )
    raise EvalError(f"the A-group judge is not calibrated at {votes} vote(s); `calibrate --set a`")


def judge_votes(out: Path, q: Mapping[str, Any], phase_kind: str, phase_name: str = "") -> int:
    if q["pilot"]:  # 试点的判分不作数,只为量 token
        return int(q["judge"]["a_group_votes"]) if phase_kind == "extraction" else plan.JUDGE_VOTES
    if phase_kind == "extraction":
        return a_votes_ready(out, q, phase_name)
    if not calibration_ok(out, "b", votes=plan.JUDGE_VOTES):
        raise EvalError("the judge is not calibrated (≥ 18 / 20 on the labels, §8.3); `calibrate`")
    return plan.JUDGE_VOTES


def _judge(args: argparse.Namespace, emit: Callable[[str], None]) -> None:
    phase = PHASES[args.phase]
    with spending(args):
        q, guard = approved(args, cmd=f"judge {phase.name}", phase=phase.name)
        load_units(args.out, phase.name, q["quote_id"])
        votes = judge_votes(args.out, q, phase.kind, phase.name)
        items, _facts = judge_items(args.out, phase)
        # 审查 M3:A 组每条判分记判官措辞;措辞对不上的旧判分重判,报告不认
        stamp = (
            {"wording_digest": rep.a_judge_wording_digest()} if phase.kind == "extraction" else {}
        )
        asyncio.run(
            runs.run_judge(
                items,
                out_file=args.out / phase.name / "verdicts.jsonl",
                guard=guard,
                votes=votes,
                phase=phase.name,
                emit=emit,
                stamp=stamp,
            )
        )
    emit(f"{len(items)} items; spent on this quote ¥{guard.spent:.2f}; blocked={guard.blocked}")


#: 每套 A 组校准要覆盖的判官题(审查 M5):闸门只判有据程度与事件日期;正式一轮另有 P2~P5 / P8。
CALIBRATION_CHECKS = {
    "calibration-g": frozenset({"grounded", "event_date"}),
    # 第 9 版:正式一轮多了 K 组的四种判官题(闸门的 K 段不送判官)
    "calibration-a": frozenset(
        {"plain", "statement", "grounded", "event_date", "hedge", "half", "fuller", "summary"}
    ),
}


def calibration_phase(which: str, phases_arg: str | None) -> str:
    """校准记在哪一段:B / C 组 → ``calibration``;A 组判官 → ``calibration-a``,只覆盖回归闸门的
    那份报价(``--phases g``)→ ``calibration-g``(闸门的输出目录单独校一次,§11.1 第 2 步)。"""
    if which == "b":
        return "calibration"
    covered = plan.parse_phases(phases_arg)
    return "calibration-a" if "A" in covered or "G" not in covered else "calibration-g"


def _calibrate(args: argparse.Namespace, emit: Callable[[str], None]) -> None:
    labels = read_jsonl(args.labels)
    check_labels(labels)
    if args.which == "a":  # 审查 LOW:标注文件里的判官题要是现行措辞生成的
        stale = [i for i, r in enumerate(labels) if not rep.wording_in_prompt(r["prompt"])]
        if stale:
            raise EvalError(
                f"labels rows {stale} were not built with the current A-group judge wording; "
                "rebuild their prompts with the current code"
            )
        phase_name = calibration_phase(args.which, args.phases)
        needed = CALIBRATION_CHECKS[phase_name]
        have = {rep.label_check(r["prompt"]) for r in labels}
        if needed - have:
            raise EvalError(
                f"the labels have no item for {sorted(needed - have)}: {phase_name} judges "
                f"{sorted(needed)} and each needs labelled items (a new labelling batch)"
            )
    items = [rep.JudgeItem(f"cal|{i}", "calibration", r["prompt"]) for i, r in enumerate(labels)]
    args.out.mkdir(parents=True, exist_ok=True)
    phase = calibration_phase(args.which, args.phases)
    with spending(args):
        q, guard = approved(args, cmd=f"calibrate {args.which}", phase=phase)
        judge = q["judge"]
        if args.which == "b":
            votes = plan.JUDGE_VOTES
        elif phase == "calibration-g":  # 审查二:闸门的校准按闸门的票数(报价就按它算的)
            votes = int(judge.get("gate_votes", judge["a_group_votes"]))
        else:
            votes = int(judge["a_group_votes"])
        out_file = args.out / f"{phase}-votes-{votes}.jsonl"
        # 换了标注文件或判官措辞,旧的投票不再沿用(以前同一个序号会直接跳过、拿旧票对新标注)
        prompts = "\n\0".join(r["prompt"] for r in labels)
        stamp = {"labels_digest": hashlib.sha256(prompts.encode("utf-8")).hexdigest()[:16]}
        if args.which == "a":
            stamp["wording_digest"] = rep.a_judge_wording_digest()
        asyncio.run(
            runs.run_judge(
                items,
                out_file=out_file,
                guard=guard,
                votes=votes,
                phase=phase,
                emit=emit,
                stamp=stamp,
            )
        )
    rows = read_jsonl(out_file)
    got = latest([r for r in rows if all(r.get(k) == v for k, v in stamp.items())], "key")
    agree = sum(
        got.get(f"cal|{i}", {}).get("verdict") == bool(r["label"]) for i, r in enumerate(labels)
    )
    result: dict[str, Any] = {
        "judge_model": plan.JUDGE_MODEL,
        "votes": votes,
        "agree": agree,
        "of": len(labels),
    }
    result.update(stamp)  # 审查 LOW-2:校准绑定判官措辞(A 组)与标注文件
    calibration_path(args.out, args.which).write_text(json.dumps(result), encoding="utf-8")
    emit(
        f"judge ({args.which}, {votes} vote(s)) agrees on {agree} / {len(labels)} "
        f"(need ≥ {plan.CALIBRATION_MIN_AGREE})"
    )


# --------------------------------------------------------------------------- 待标清单


def labels_todo(out: Path, names: Sequence[str], write: Path, pick: int) -> dict[str, Any]:
    """校准要的人工标注从哪来(不调模型、不花钱):把 ``out`` 里这几段**已经跑出来**的记录变成判官
    题,写成待标清单 —— 每行 ``key`` / ``check``(题型)/ ``domain`` / ``prompt`` / ``label``(空着,
    人填 true / false)/ ``suggested``(建议先标的那 ``pick`` 条:题型轮着取、每个领域至多 4 条)。
    人标完,把选定的 20 条(是 / 否各半、至少 5 个领域、每个至多 4 条、每种题型都有)另存一份交给
    ``calibrate --set a --labels``。"""
    rows: list[dict[str, Any]] = []
    for name in names:
        items, _facts = judge_items(out, PHASES[name])
        domains = {key: r["domain"] for key, r in latest_records(out, name).items()}
        for item in items:
            domain = next((d for k, d in domains.items() if item.key.startswith(f"{k}|")), None)
            kind = rep.label_check(item.prompt)
            if domain is None or kind is None:
                continue
            row = {"key": item.key, "check": kind, "domain": domain, "prompt": item.prompt}
            rows.append({**row, "label": None, "suggested": False})
    per_domain: dict[str, int] = {}
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_kind.setdefault(row["check"], []).append(row)
    picked = 0
    while picked < pick and any(by_kind.values()):
        for kind in sorted(by_kind):
            queue = by_kind[kind]
            while queue and per_domain.get(queue[0]["domain"], 0) >= 4:
                queue.pop(0)
            if queue and picked < pick:
                row = queue.pop(0)
                row["suggested"] = True
                per_domain[row["domain"]] = per_domain.get(row["domain"], 0) + 1
                picked += 1
    write.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    kinds = {k: sum(r["check"] == k for r in rows) for k in sorted({r["check"] for r in rows})}
    needed = sorted(set().union(*CALIBRATION_CHECKS.values()) - set(kinds))
    return {
        "items": len(rows),
        "by_check": kinds,
        "suggested": picked,
        "suggested_by_domain": dict(sorted(per_domain.items())),
        "checks_without_items": needed,
        "wording_digest": rep.a_judge_wording_digest(),
    }


# --------------------------------------------------------------------------- 入口


def _scenarios(args: argparse.Namespace, emit: Callable[[str], None]) -> int:
    everything = [*sc.build_all(), *sc.build_compare()]
    problems = sc.coverage_problems(sc.build_all())
    args.out.write_text(
        json.dumps(sc.to_jsonable(everything), indent=1, ensure_ascii=False), encoding="utf-8"
    )
    summary = {
        "scenarios": len(everything),
        "digest": sc.scenario_digest(everything),
        "problems": problems,
    }
    emit(json.dumps(summary))
    return 1 if problems else 0


def main(argv: Sequence[str] | None = None, *, emit: Callable[[str], None] = print) -> int:
    args = parser().parse_args(argv)
    try:
        if hasattr(args, "state_dir"):  # 只有一个状态目录(审查第四轮 M1)
            args.state_dir = spend.check_state_dir(args.state_dir or spend.DEFAULT_STATE_DIR)
        if args.cmd == "scenarios":
            return _scenarios(args, emit)
        if args.cmd == "quote":
            emit(json.dumps(make_quote(args), indent=2, ensure_ascii=False))
            return 0
        if args.cmd == "manifests":
            for path in write_manifests(args.out, vendors_of(args.vendor)):
                emit(str(path))
            return 0
        if args.cmd == "labels-todo":
            emit(json.dumps(labels_todo(args.out, args.phase, args.write, args.pick)))
            return 0
        if args.cmd == "progress":
            body = progress(args.out)
            args.write.write_text(json.dumps(body, indent=2), encoding="utf-8")
            emit(json.dumps(body))
            return 0
        if args.cmd == "pilot-tokens":
            table = runs.pilot_tokens(args.out)
            args.write.write_text(json.dumps(table, indent=2), encoding="utf-8")
            emit(json.dumps(table))
            return 0
        if args.cmd == "report":
            q = json.loads(args.quote_file.read_text(encoding="utf-8")) if args.quote_file else None
            emit(json.dumps(report(args.out, q), indent=2, ensure_ascii=False))
            return 0
        handlers: dict[str, Callable[[argparse.Namespace, Callable[[str], None]], None]] = {
            "prepare": steps.prepare,
            "bundle": steps.bundle,
            "ingest": steps.ingest,
            "run": _run,
            "judge": _judge,
            "calibrate": _calibrate,
        }
        handlers[args.cmd](args, emit)
        return 0
    except (EvalError, plan.PlanError, spend.LockedError, spend.StateDirError) as exc:
        emit(f"error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
