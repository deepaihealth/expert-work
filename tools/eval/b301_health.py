"""B-301 PR 2 —— ``ask_image`` 问方向的上生产前体检(设计 §9.2 / §9.3,用户 11-12 改定)。

``docs/superpowers/specs/2026-11-11-b301-image-orientation-design.md``。体检的是**在用的**看图模型:
默认 ``doubao-seed-2-1-pro-260628``(生产 7 个走 ``ask_image`` 的智能体全部用它)与
``doubao-seed-2-1-pro-260915``;``--with-qwen`` 加 ``qwen3.8-max-0902``;``--with-probe`` 加 §8.1 的
小测量(只问方向,不经 ``ask_image``,主模型自己看图的 ``glm-5.3-flashx`` / ``deepseek-flash`` /
``glm-5.3-flash``)。两个开关默认关。

这个文件是命令行入口;计划与报价在 :mod:`b301_plan`,花费在 :mod:`b301_spend`,真跑在
:mod:`b301_health_run`,判分与报告在 :mod:`b301_report`。

子命令(只有 ``run`` 花钱):

    python tools/eval/b301_health.py quote [--with-qwen] [--with-probe --accept-documented-prices]
                                           [--stop-line 元]
    python tools/eval/b301_health.py run --quote-id <quote 打印的编号> --results OUT.jsonl
                                         [与 quote 相同的开关] [--env-file F ...]
    python tools/eval/b301_health.py report --results OUT.jsonl
    python tools/eval/b301_images.py OUT_DIR       # 把图集写成文件,人工抽看(不花钱)

怎么跑(本机,不连集群;代码是本分支的 ``ask_image``,不是测试环境部署的那一版):

1. ``quote``:离线,按 §2 实测的单图 token 与价目表迁移报价,打印每个模型的调用数、token、金额、
   止损线、硬上限和 ``quote_id``。价目表里查不到、又不在「同档」表里的模型直接拒(fail-stop)。
2. 把报价交用户批。批了再 ``run``:``--quote-id`` 必须等于在**同一台机器、同样开关**下现算的编号
   (图集字节、模型、单价、止损线任一变了编号就变,旧批准不作数)。
3. 凭据只从环境变量或 ``--env-file``(可给多个,默认 ``~/.kube/eval-llm.env``;进程内解析,不 echo、
   不进 argv)取,变量名见 ``b301_health_run.KEY_VARIABLES``:豆包 ``ARK_API_KEY``;通义的
   ``HELIX_EVAL_LLM_API_KEY`` 在 ``~/.helix-eval.env``(``--with-qwen`` 时再加一个 ``--env-file``)。
   缺哪个就只报变量名、不开跑。
4. ``run`` 每次调用之前判止损线,并按单次上限预留、写进这个计划的花费账本(落盘后才发),调用后
   结算;过线、用量不可信的回答、单次输入超上限、厂商返回超时以外的任何错误、账本写不进去,都立刻
   停下(不再发新调用),已在飞的最多「每家 2 个 ``ask_image`` x 每个最多 3 个并行调用」。账本和锁
   **按计划**放在 ``~/.cache/b301-health/<plan_id>.spend.jsonl`` / ``.lock``(``--state-dir`` 可改,
   测试用),与 ``--results`` 无关:换结果路径续跑,止损线照样从这个计划已花的钱起算;同一个计划
   同一时间只许一个 run。结果文件只放跑完的条目,同一条命令再跑接着跑。**账本不能删**(删了就是把
   已花的钱清零);读不出来的行每行按单次上限算。
5. ``report``(:mod:`b301_report`):按 §9.2 出各线和每个模型的结论。**通过 = 数据齐 + L1'(正放
   33 + 无文字 4 张 0 张被转、「判不出」≤ 3)+ L6(超时 + 答非所问 ≤ 5%)**;厂商出错而停的次数从
   账本单独报;L1 / L2 照常报数(L2 严格 / 宽松并列,按类结论用严格,两者不一致的回答逐条列出供
   人工看),不挡发布,任何一类「变差」标出来交用户定。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import b301_health_run as runner  # noqa: E402
import b301_report  # noqa: E402
import b301_spend  # noqa: E402
from b301_plan import (  # noqa: E402
    DEFAULT_MODELS,
    PROBE_MODELS,
    PROVIDERS,
    QWEN_MODEL,
    PlanError,
    per_call_cap_yuan,
    price_of,
    quote,
    read_records,
)

#: 默认凭据文件(``--env-file`` 没给时用):里面有 ``ARK_API_KEY``(豆包)、``GLM_API_KEY``、
#: ``DEEPSEEK_API_KEY``;变量名对照见 ``b301_health_run.KEY_VARIABLES``。
DEFAULT_ENV_FILE = Path.home() / ".kube" / "eval-llm.env"


# --------------------------------------------------------------------------- CLI


def _selection(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    models = [*DEFAULT_MODELS, *([QWEN_MODEL] if args.with_qwen else [])]
    probe = list(PROBE_MODELS) if args.with_probe else []
    return models, probe


def _add_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--with-qwen", action="store_true", help=f"also check {QWEN_MODEL}")
    parser.add_argument(
        "--with-probe", action="store_true", help="also run the §8.1 orientation-only probe"
    )
    parser.add_argument("--accept-documented-prices", action="store_true")
    parser.add_argument(
        "--stop-line", type=float, default=None, help="yuan; default = max(worst, 1.5 x expected)"
    )


def _report(results: Path, state_dir: Path | None) -> int:
    """离线报告:结果文件 + 这个计划的花费账本(按结果里的 ``plan_id`` 找)。"""
    records = read_records(results)
    plan_ids = sorted({str(r["plan_id"]) for r in records if "plan_id" in r})
    caps = [per_call_cap_yuan(price_of(model, accept_documented=True)) for model in PROVIDERS]
    state = state_dir or b301_spend.DEFAULT_STATE_DIR
    ledger: list[dict[str, Any]] = []
    missing: list[str] = []
    for plan in plan_ids:
        path = b301_spend.ledger_path(state, plan)
        if not path.exists():
            missing.append(path.name)
        ledger.extend(b301_spend.read_ledger(path, broken_line_cost=max(caps)))
    result = b301_report.report(records, ledger_rows=ledger)
    if missing or any(m["error_stops"] is None for m in [*result["health"], *result["probe"]]):
        detail = f"missing {', '.join(missing)}" if missing else "it holds less than the results"
        sys.stdout.write(
            f"warning: the spend ledger in the state dir {state} does not cover these results "
            f"({detail}); ledger spend and error stops below are not reliable. Pass the "
            "--state-dir used for the run.\n"
        )
    sys.stdout.write(b301_report.render_report(result) + "\n")
    return 0


def _state_dir(value: str) -> Path:
    """``--state-dir``:展开 ``~``(``--state-dir=~/x`` 这种写法 shell 不展开)。"""
    return Path(value).expanduser()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    _add_selection(sub.add_parser("quote", help="offline quote"))
    run_parser = sub.add_parser("run", help="PAID: drive the real ask_image")
    _add_selection(run_parser)
    run_parser.add_argument("--quote-id", required=True)
    run_parser.add_argument("--results", type=Path, required=True)
    run_parser.add_argument(
        "--env-file",
        type=Path,
        action="append",
        help="credential file(s), parsed in-process; default ~/.kube/eval-llm.env",
    )
    report_parser = sub.add_parser("report", help="offline report")
    report_parser.add_argument("--results", type=Path, required=True)
    for each in (run_parser, report_parser):
        each.add_argument(
            "--state-dir",
            type=_state_dir,
            default=None,
            help=(
                "TEST ONLY - leave unset for real runs. Where the per-plan spend ledger and lock "
                "live; default ~/.cache/b301-health. A different directory starts a different "
                "budget."
            ),
        )
    args = parser.parse_args(argv)

    if args.command == "report":
        return _report(args.results, args.state_dir)
    models, probe = _selection(args)
    try:
        q = quote(
            models=models,
            probe_models=probe,
            stop_line=args.stop_line,
            accept_documented=args.accept_documented_prices,
        )
    except PlanError as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 2
    if args.command == "quote":
        sys.stdout.write(json.dumps(q, ensure_ascii=False, indent=2) + "\n")
        return 0
    if args.quote_id != q["quote_id"]:
        sys.stderr.write(
            f"refused: --quote-id {args.quote_id} != the quote computed here ({q['quote_id']})\n"
        )
        return 2
    env_files = args.env_file or [DEFAULT_ENV_FILE]
    state_dir = args.state_dir or b301_spend.DEFAULT_STATE_DIR
    try:
        summary = asyncio.run(
            runner.run_from_quote(
                q,
                models=models,
                probe_models=probe,
                results=args.results,
                env_files=env_files,
                state_dir=state_dir,
            )
        )
    except runner.RunRefusedError as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(asdict(summary), ensure_ascii=False, indent=2) + "\n")
    return 0 if summary.stopped_reason is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
