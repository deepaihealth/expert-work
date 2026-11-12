"""B-302 PR 2 A/B —— 运行脚本(设计 §5.3 任务 / 组别 / 次数 / 先后,§5.5 报价与止损)。

``docs/superpowers/specs/2026-11-11-b302-runaway-reverify-design.md``。**会花钱**:``run`` 子命令调
真模型,必须带用户批过的报价编号(``--approved-quote``)和止损线(``--stop-line``,元),否则拒跑。
其余子命令全部离线:

    python tools/eval/b302_ab.py plan --docs OUT --doubao-model <§6.2 查到的型号> --stop-line 650
    python tools/eval/b302_ab.py manifests --out AGENTS --doubao-model <...>
    python tools/eval/b302_ab.py run --phase pilot|arm_a|rest --docs OUT --out RECORDS \\
        --doubao-model <...> --stop-line 650 --approved-quote <plan 打印的 quote_id>
    python tools/eval/b302_ab.py report --docs OUT --out RECORDS
    python tools/eval/b302_ab.py cleanup --base-url URL [--yes]   # 收尾必做:删临时智能体

* 组别:A = 今天的行为;B = 只开第 1 层;C = 第 1 层 + 第 2 层(N=3)。三个临时智能体只差
  ``policies.reading_rule`` / ``policies.figure_view_steps``(:func:`write_manifests`,控制台导入)。
* 先后:各模型试点(1 份 x 3 轮,只 A 组)→ 各模型 A 组 → 判复现前提后再跑 B、C 与回归集。
  试点之后人工按 §5.3 查 ``token_usage`` 的全部用量种类(``conversation`` 以外不为 0 就停)。
* 止损与计费见 ``b302_live``:每一轮开始前判止损线;计不了账(没有 ``end`` 帧也查不到用量、没单价的
  型号、某个会话出错、更新帧里没有 ``usage_metadata``)就阻塞;单次调用输入超过 25 万 token 就取消
  这次运行。每家并发 2 路。``--approved-quote`` 必须等于 ``plan`` 打印的 ``quote_id``
  (报价内容的哈希)。
* 只走对外平面 ``/v1/agents/{code}/...``(凭据:``EXPERT_WORK_EVAL_API_KEY``)。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import statistics
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import b302_metrics as metrics  # noqa: E402
from b302_live import (  # noqa: E402
    CONCURRENCY_PER_VENDOR,
    DELIVERABLE_EXT,
    MAX_INPUT_TOKENS,
    RUN_TIMEOUT_S,
    Session,
    SpendTracker,
    TurnCollector,
    cleanup,
    dedupe,
    deliverable_structure,
    deliverable_text,
    first_deliverable_step,
    read_records,
    run_phase,
    run_session,
    template_followed,
)

__all__ = [
    "DELIVERABLE_EXT",
    "MAX_INPUT_TOKENS",
    "Session",
    "SpendTracker",
    "TurnCollector",
    "deliverable_structure",
    "deliverable_text",
    "first_deliverable_step",
    "run_session",
    "template_followed",
]

ARMS: dict[str, dict[str, Any]] = {
    "A": {"reading_rule": False, "figure_view_steps": 0},
    "B": {"reading_rule": True, "figure_view_steps": 0},
    "C": {"reading_rule": True, "figure_view_steps": 3},
}
VIEW_STEPS = 3
#: ``--stop-line`` 的上限(元):D4 定的止损线是 ¥650,远超多半是手误。
STOP_LINE_MAX = 2000.0
#: §5.5 的报价口径:每次运行 15 步、每步 3 万输入 / 3,000 输出、无压缩。
EXPECTED_STEPS, EXPECTED_IN, EXPECTED_OUT = 15, 30_000, 3_000
#: 每次运行的上限(百万 token):主循环 356.6 万(全按输出价)、压缩 650 万输入 + 85.2 万输出、
#: 超 25 万的 2 次 206.6 万(全按输出价)。
CAP_MAIN_M, CAP_SUMMARY_IN_M, CAP_SUMMARY_OUT_M, CAP_OVERSIZE_M = 3.566, 6.5, 0.852, 2.066
MAX_ITERATIONS, MAX_COMPRESSIONS = 40, 26


class PlanError(RuntimeError):
    """计划 / 报价 / 计费的前提不成立 —— 一律停下,不猜。"""


@dataclass(frozen=True)
class ModelPlan:
    name: str
    provider: str
    arms: tuple[str, ...]
    #: 元 / 百万 token:输入 / 输出 / 缓存读。
    price: tuple[float, float, float]
    regression: bool = False


def default_models(doubao_model: str) -> list[ModelPlan]:
    """§5.3 + D9:glm-5.3-flash(事故模型,另跑回归集)、deepseek-flash 三组;豆包只 A、C 两组。
    单价取自设计 §5.5 / §5.7 引的价目表。豆包型号以 §6.2 的查询为准,由调用方给。"""
    return [
        ModelPlan("glm-5.3-flash", "glm", ("A", "B", "C"), (0.8, 2.8, 0.23), regression=True),
        ModelPlan("deepseek-flash", "deepseek", ("A", "B", "C"), (2.0, 8.0, 0.04)),
        ModelPlan(doubao_model, "doubao", ("A", "C"), (6.0, 30.0, 1.2)),
    ]


def phase_of(session: Session) -> str:
    if session.phase == "pilot":
        return "pilot"
    return "arm_a" if session.phase == "main" and session.arm == "A" else "rest"


def load_docs(docs_dir: Path) -> list[dict[str, Any]]:
    manifest = json.loads((docs_dir / "manifest.json").read_text(encoding="utf-8"))
    if not manifest.get("complete"):
        raise PlanError("document set has no incident category (--incident-template); refused")
    return [
        json.loads((docs_dir / f"{d}.gold.json").read_text(encoding="utf-8"))
        for d in manifest["docs"]
    ]


def _regression_ids(docs_dir: Path | None) -> list[str]:
    if docs_dir is None:
        return ["regression-4p", "regression-6p"]
    return list(json.loads((docs_dir / "manifest.json").read_text())["regression"])


def plan(
    docs: Sequence[Mapping[str, Any]], models: Sequence[ModelPlan], docs_dir: Path | None = None
) -> list[Session]:
    """§5.3 的全部会话,按先后排:试点 → A 组 → B、C 与回归集。"""
    pilot = [Session("pilot", m.name, "A", str(docs[0]["doc_id"]), 3) for m in models]
    main = [
        Session("main", m.name, arm, str(d["doc_id"]), 3 if d.get("correction") else 2)
        for m in models
        for arm in m.arms
        for d in docs
    ]
    regression = [
        Session("regression", m.name, arm, rid, 1, repeat)
        for m in models
        if m.regression
        for arm in m.arms
        for rid in _regression_ids(docs_dir)
        for repeat in range(2)
    ]
    sessions = [*pilot, *main, *regression]
    order = ["pilot", "arm_a", "rest"]
    return sorted(sessions, key=lambda s: order.index(phase_of(s)))


def runs_per_model(sessions: Iterable[Session]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for s in sessions:
        counts[s.model] += s.turns
    return dict(counts)


def run_cap_yuan(price: tuple[float, float, float]) -> float:
    inp, out, _cache = price
    return (
        CAP_MAIN_M * out + CAP_SUMMARY_IN_M * inp + CAP_SUMMARY_OUT_M * out + CAP_OVERSIZE_M * out
    )


def run_expected_yuan(price: tuple[float, float, float]) -> float:
    inp, out, _cache = price
    return EXPECTED_STEPS * (EXPECTED_IN * inp + EXPECTED_OUT * out) / 1e6


def hard_ceiling_yuan(stop_line: float, models: Sequence[ModelPlan]) -> float:
    """实际花费硬上限:止损线 + 每家同时在跑的 2 路各按**一次运行**的上限算。

    成立的前提是 ``b302_live.run_session`` 每一轮开始前都判止损线(过线时在跑的会话不再开下一轮),
    所以过线那一刻还会花钱的只有每家最多 2 次正在跑的运行。"""
    return stop_line + sum(CONCURRENCY_PER_VENDOR * run_cap_yuan(m.price) for m in models)


def call_estimate(sessions: Sequence[Session]) -> dict[str, int]:
    runs = sum(s.turns for s in sessions)
    return {
        "runs": runs,
        "expected_calls": runs * EXPECTED_STEPS,
        "max_calls": runs * (MAX_ITERATIONS + 2 + MAX_COMPRESSIONS),
    }


def quote(
    sessions: Sequence[Session], models: Sequence[ModelPlan], stop_line: float
) -> dict[str, Any]:
    runs = runs_per_model(sessions)
    by_model = {
        m.name: {
            "runs": runs.get(m.name, 0),
            "expected_yuan": round(runs.get(m.name, 0) * run_expected_yuan(m.price), 2),
            "theoretical_cap_yuan": round(runs.get(m.name, 0) * run_cap_yuan(m.price), 2),
        }
        for m in models
    }
    body: dict[str, Any] = {
        "models": by_model,
        "expected_total_yuan": round(sum(v["expected_yuan"] for v in by_model.values()), 2),
        "theoretical_total_yuan": round(
            sum(v["theoretical_cap_yuan"] for v in by_model.values()), 2
        ),
        "stop_line_yuan": stop_line,
        "hard_ceiling_yuan": round(hard_ceiling_yuan(stop_line, models), 2),
        **call_estimate(sessions),
    }
    #: 报价编号 = 报价内容 + 全部会话的哈希;改了止损线、型号或计划,编号就变,旧批准不再作数。
    digest = json.dumps(
        {"quote": body, "sessions": [asdict(s) for s in sessions]}, sort_keys=True
    ).encode()
    return {**body, "quote_id": hashlib.sha256(digest).hexdigest()[:12]}


# --------------------------------------------------------------------------- manifests


def manifest(model: ModelPlan, arm: str) -> dict[str, Any]:
    """§5.3 的临时智能体;三组只差 ``ARMS`` 里的两个字段。"""
    return {
        "apiVersion": "expert_work.io/v1",
        "kind": "Agent",
        "metadata": {"name": agent_code(model.name, arm), "version": "1.0.0", "tenant": "b302-ab"},
        "spec": {
            "description": "B-302 PR 2 A/B temporary agent (synthetic documents only).",
            "tenant_config": {"isolation_level": "dedicated_sandbox"},
            "model": {
                "provider": model.provider,
                "name": model.name,
                "supports_vision": True,
                "max_tokens": 32_768,
            },
            "system_prompt": {
                "template": (
                    "You are a general office assistant working in a workspace. Read the user's "
                    "files, write files and run scripts to do what the user asks."
                )
            },
            "workflow": {"type": "react", "max_iterations": MAX_ITERATIONS},
            "dynamic_workers": {"enabled": False},
            "policies": {"run_deadline_s": 5400, "token_budget": 3_000_000, **ARMS[arm]},
            "sandbox": {
                "runtime": "gvisor",
                "resources": {"cpu": "0.5", "memory": "512Mi"},
                "network": {"egress": "none"},
                "filesystem": {
                    "readonly_root": True,
                    "writable": ["/tmp"],  # noqa: S108 - the sandbox scratch dir, not a host path
                    "persistent_workspace": True,
                },
            },
        },
    }


def agent_code(model: str, arm: str) -> str:
    return "eval-b302-" + re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-") + "-" + arm.lower()


def write_manifests(out: Path, models: Sequence[ModelPlan]) -> dict[tuple[str, str], Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths: dict[tuple[str, str], Path] = {}
    for model in models:
        for arm in model.arms:
            path = out / f"{agent_code(model.name, arm)}.yaml"
            path.write_text(
                yaml.safe_dump(manifest(model, arm), allow_unicode=True, sort_keys=False)
            )
            paths[(model.name, arm)] = path
    return paths


# --------------------------------------------------------------------------- report


def _steps(turn: Mapping[str, Any]) -> tuple[metrics.Step, ...]:
    return tuple(
        metrics.Step(
            index=s["index"],
            thinking=s["thinking"],
            reply=s["reply"],
            calls=tuple(metrics.Call(c["name"], c["args"]) for c in s["calls"]),
            reasoning_tokens=s.get("reasoning_tokens"),
        )
        for s in turn["steps"]
    )


def _per_run_refetches(
    turns: Mapping[int, Mapping[str, Any]], *, view_steps: int
) -> tuple[tuple[int, ...], int]:
    """每一轮(= 一次运行)分别计:过期后重取次数、以 ``no_progress`` 收尾的运行数(MEDIUM-2)。
    前几轮读过的文档,本轮第一次再读也算过期后重取(跨轮一律过期)。"""
    read_before: set[str] = set()
    counts: list[int] = []
    no_progress = 0
    for number in sorted(turns):
        steps = _steps(turns[number])
        counts.append(
            metrics.refetches_after_retire(steps, view_steps=view_steps, read_earlier=read_before)
        )
        read_before |= {name for _i, name, _u in metrics.read_events(steps)}
        no_progress += turns[number].get("exit_reason") == "no_progress"
    return tuple(counts), no_progress


def doc_result(
    record: Mapping[str, Any], doc: Mapping[str, Any], *, view_steps: int
) -> metrics.DocResult:
    """一个会话(主实验)的汇总:第 2 轮为主,第 3 轮只看更正有没有照改;重取与无进展按每一轮计。"""
    fields = [metrics.Field(f["tag"], f["label"], f["value"]) for f in doc["fields"]]
    name = f"{doc['doc_id']}.pdf"
    pages = {(name, i + 1): text for i, text in enumerate(doc["pages"])}
    turns = {t["turn"]: t for t in record["turns"]}
    second = turns.get(2) or {"steps": [], "exit_reason": None, "warnings": [], "wall_s": 0.0}
    steps = _steps(second)
    first_out = first_deliverable_step(steps, str(doc["deliverable"]))
    turn = metrics.Turn(
        doc_id=doc["doc_id"],
        steps=steps,
        exit_reason=second.get("exit_reason"),
        first_deliverable_step=first_out,
        warnings=tuple(second.get("warnings") or ()),
    )
    texts = [s.text for s in steps]
    thinking = [s.reasoning_tokens for s in steps if s.reasoning_tokens is not None]
    refetches, no_progress = _per_run_refetches(turns, view_steps=view_steps)
    deliverable = second.get("deliverable_text") or ""
    correction = doc.get("correction")
    third = turns.get(3)
    usage = second.get("usage_by_model") or []
    return metrics.DocResult(
        doc_id=doc["doc_id"],
        category=doc["category"],
        runaway=bool(metrics.runaway_reasons(turn, fields, pages)),
        correct=metrics.count_correct(deliverable, fields),
        flipped_fields=sum(metrics.changes(metrics.readings(texts, f)) >= 3 for f in fields),
        recopy_share=(sum(metrics.recopy_flags(steps, pages)) / len(steps)) if steps else 0.0,
        calls_after_deliverable=(len(steps) - 1 - first_out) if first_out is not None else None,
        thinking_median=float(statistics.median(thinking)) if thinking else None,
        refetches=refetches,
        no_progress_runs=no_progress,
        wall_s=float(second.get("wall_s") or 0.0),
        tokens=sum(
            int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0) for u in usage
        ),
        correction_applied=(
            metrics.normalize(correction["value"])
            in metrics.normalize(third.get("deliverable_text") or "")
            if correction and third
            else None
        ),
        template_ok=template_followed(deliverable, doc["template"])
        if doc.get("template")
        else None,
        structure=second.get("deliverable_structure"),
    )


def report(docs_dir: Path, out: Path) -> dict[str, Any]:
    """按 (phase, model, arm, doc_id, repeat) 去重(留最后一条)后出判据与结论;另报出错、被拦下的
    会话数和交付件来源(登记过 / 只在工作区、没登记 / 没有 —— 后两种的 0 分不是读错)。"""
    docs = {d["doc_id"]: d for d in load_docs(docs_dir)}
    records = dedupe(read_records(out))
    errors = [r for r in records if r.get("error")]
    kept = [r for r in records if not r.get("error")]
    arms: dict[str, dict[str, list[metrics.DocResult]]] = {}
    regression: dict[str, dict[str, list[Mapping[str, Any]]]] = {}
    sources: dict[str, dict[str, Counter[str]]] = {}
    for rec in kept:
        if rec["phase"] == "main":
            arms.setdefault(rec["model"], {}).setdefault(rec["arm"], []).append(
                doc_result(rec, docs[rec["doc_id"]], view_steps=VIEW_STEPS)
            )
            second = next((t for t in rec["turns"] if t.get("turn") == 2), None)
            source = (second or {}).get("deliverable_source") or "missing"
            sources.setdefault(rec["model"], {}).setdefault(rec["arm"], Counter())[source] += 1
        elif rec["phase"] == "regression":
            regression.setdefault(rec["model"], {}).setdefault(rec["arm"], []).append(rec)
    verdicts = {
        model: metrics.model_verdict(
            by_arm, regression_ok=_regression_ok(regression.get(model), docs_dir)
        )
        for model, by_arm in arms.items()
        if "A" in by_arm and "C" in by_arm
    }
    spent_file = out / "spent.json"
    spent = json.loads(spent_file.read_text()) if spent_file.exists() else {}
    return {
        "sessions": {
            "records": len(records),
            "errors": len(errors),
            "stopped": sum(bool(r.get("stopped")) for r in kept),
        },
        "blocked": spent.get("blocked"),
        "spent_yuan": spent.get("spent"),
        "deliverables": {
            model: {arm: dict(counter) for arm, counter in by_arm.items()}
            for model, by_arm in sources.items()
        },
        "verdicts": {m: asdict(v) for m, v in verdicts.items()},
        "landing": metrics.landing(verdicts),
    }


def _regression_ok(
    by_arm: Mapping[str, list[Mapping[str, Any]]] | None, docs_dir: Path
) -> bool | None:
    """判据 7(只 glm):B、C 都没有轮转、``read_page`` 次数 ≤ 页数 + 2、答对的个数不少于 A。"""
    if not by_arm:
        return None

    def _score(rec: Mapping[str, Any]) -> tuple[bool, bool, int]:
        gold = json.loads((docs_dir / f"{rec['doc_id']}.gold.json").read_text(encoding="utf-8"))
        steps = _steps(rec["turns"][0]) if rec["turns"] else ()
        calls = [list(units) for _i, _n, units in metrics.read_events(steps)]
        reply = metrics.normalize(" ".join(s.reply for s in steps))
        right = sum(str(r["total"]) in reply for r in gold["rows"])
        return (
            metrics.rotation(calls, pages=gold["pages"]),
            len(calls) <= gold["pages"] + 2,
            right,
        )

    scores = {arm: [_score(r) for r in recs] for arm, recs in by_arm.items()}
    a_right = sum(s[2] for s in scores.get("A", []))
    for arm in ("B", "C"):
        rows = scores.get(arm, [])
        if any(rot or not few for rot, few, _ in rows) or sum(s[2] for s in rows) < a_right:
            return False
    return True


# --------------------------------------------------------------------------- CLI


def _stop_line(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {raw!r}") from exc
    if not math.isfinite(value) or not 0 < value <= STOP_LINE_MAX:
        raise argparse.ArgumentTypeError(f"must be a finite number in (0, {STOP_LINE_MAX:g}]")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="B-302 PR 2 A/B (design §5)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--docs", type=Path, required=True)
    p_plan.add_argument("--doubao-model", required=True)
    p_plan.add_argument("--stop-line", type=_stop_line, default=650.0)
    p_man = sub.add_parser("manifests")
    p_man.add_argument("--out", type=Path, required=True)
    p_man.add_argument("--doubao-model", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--phase", choices=("pilot", "arm_a", "rest"), required=True)
    p_run.add_argument("--docs", type=Path, required=True)
    p_run.add_argument("--out", type=Path, required=True)
    p_run.add_argument("--doubao-model", default=None)
    p_run.add_argument("--stop-line", type=_stop_line, required=True)
    p_run.add_argument("--approved-quote", required=True)
    p_run.add_argument("--base-url", default=os.environ.get("EXPERT_WORK_EVAL_BASE_URL"))
    p_rep = sub.add_parser("report")
    p_rep.add_argument("--docs", type=Path, required=True)
    p_rep.add_argument("--out", type=Path, required=True)
    p_clean = sub.add_parser("cleanup")
    p_clean.add_argument("--base-url", default=os.environ.get("EXPERT_WORK_EVAL_BASE_URL"))
    p_clean.add_argument("--yes", action="store_true")
    return parser


async def _run_cli(
    args: argparse.Namespace,
    sessions: Sequence[Session],
    models: Sequence[ModelPlan],
    tracker: SpendTracker,
    quote_id: str,
    api_key: str,
    emit: Callable[[str], None],
) -> None:
    import httpx

    async with httpx.AsyncClient(
        base_url=args.base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=httpx.Timeout(RUN_TIMEOUT_S),
    ) as http:
        await run_phase(
            sessions,
            docs={d["doc_id"]: d for d in load_docs(args.docs)},
            docs_dir=args.docs,
            out=args.out,
            http=http,
            tracker=tracker,
            vendor={m.name: m.provider for m in models},
            agent_for=lambda s: agent_code(s.model, s.arm),
            quote_id=quote_id,
            emit=emit,
        )


async def _cleanup_cli(base_url: str, token: str, yes: bool, emit: Callable[[str], None]) -> None:
    import httpx

    async with httpx.AsyncClient(
        base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=60.0
    ) as http:
        await cleanup(http, yes=yes, emit=emit)


def main(argv: Sequence[str] | None = None, *, emit: Callable[[str], None] = print) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.cmd == "plan":
        models = default_models(args.doubao_model)
        sessions = plan(load_docs(args.docs), models, args.docs)
        emit(json.dumps(quote(sessions, models, args.stop_line), indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "manifests":
        for path in write_manifests(args.out, default_models(args.doubao_model)).values():
            emit(str(path))
        return 0
    if args.cmd == "report":
        emit(json.dumps(report(args.docs, args.out), indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "cleanup":
        token = os.environ.get("EXPERT_WORK_EVAL_CONSOLE_TOKEN")
        if not args.base_url or not token:
            parser.error("cleanup needs --base-url and EXPERT_WORK_EVAL_CONSOLE_TOKEN (console)")
        asyncio.run(_cleanup_cli(args.base_url, token, args.yes, emit))
        return 0
    api_key = os.environ.get("EXPERT_WORK_EVAL_API_KEY")
    if not args.doubao_model or not args.base_url or not api_key:
        parser.error("run needs --doubao-model, --base-url and EXPERT_WORK_EVAL_API_KEY")
    models = default_models(args.doubao_model)
    full_plan = plan(load_docs(args.docs), models, args.docs)
    quote_id = quote(full_plan, models, args.stop_line)["quote_id"]
    if args.approved_quote != quote_id:
        parser.error(
            f"--approved-quote does not match this plan's quote {quote_id}; "
            "run `plan` with the same arguments and get that quote approved"
        )
    sessions = [s for s in full_plan if phase_of(s) == args.phase]
    tracker = SpendTracker(stop_line=args.stop_line, prices={m.name: m.price for m in models})
    spent_file = args.out / "spent.json"
    if spent_file.exists():
        saved = json.loads(spent_file.read_text())
        if saved.get("quote_id") != quote_id:
            parser.error(f"{spent_file} belongs to quote {saved.get('quote_id')}, not {quote_id}")
        tracker.spent = float(saved["spent"])
        tracker.blocked = saved.get("blocked")
    emit(
        f"approved quote {quote_id}; stop line {args.stop_line}; "
        f"already spent {tracker.spent:.2f}; blocked={tracker.blocked}"
    )
    try:
        asyncio.run(_run_cli(args, sessions, models, tracker, quote_id, api_key, emit))
    finally:
        args.out.mkdir(parents=True, exist_ok=True)
        spent_file.write_text(
            json.dumps({"spent": tracker.spent, "quote_id": quote_id, "blocked": tracker.blocked})
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
