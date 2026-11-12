"""B-301 PR 2 —— ``ask_image`` 问方向的上生产前体检(设计 §9.2 / §9.3,用户 11-12 改定)。

``docs/superpowers/specs/2026-11-11-b301-image-orientation-design.md``。体检的是**在用的**看图模型:
默认 ``doubao-seed-2-1-pro-260628``(生产 7 个走 ``ask_image`` 的智能体全部用它)与
``doubao-seed-2-1-pro-260915``;``--with-qwen`` 加 ``qwen3.8-max-0902``;``--with-probe`` 加 §8.1 的
小测量(只问方向,不经 ``ask_image``,主模型自己看图的 ``glm-5.3-flashx`` / ``deepseek-flash`` /
``glm-5.3-flash``)。两个开关默认关。

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
import ast
import hashlib
import json
import math
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import b301_images as images_mod  # noqa: E402

from expert_work.protocol.model_catalog import catalog_entry  # noqa: E402
from orchestrator.tools.image_orientation import ORIENTATION_QUESTION, TURNS  # noqa: E402

DEFAULT_MODELS = ("doubao-seed-2-1-pro-260628", "doubao-seed-2-1-pro-260915")
QWEN_MODEL = "qwen3.8-max-0902"
PROBE_MODELS = ("glm-5.3-flashx", "deepseek-flash", "glm-5.3-flash")
PROVIDERS = {
    "doubao-seed-2-1-pro-260628": "doubao",
    "doubao-seed-2-1-pro-260915": "doubao",
    QWEN_MODEL: "qwen",
    "glm-5.3-flashx": "glm",
    "glm-5.3-flash": "glm",
    "deepseek-flash": "deepseek",
}

#: 价目表迁移:``_PRICES`` 行 ``(provider, model, 输入, 输出, 缓存创建, 缓存读)``,微元 / 百万 token。
RATE_CARD_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "packages/expert-work-persistence/migrations/versions/0187_rate_card_dated_model_ids.py"
)
#: 没有自己价目行、按同档价的型号 → (取价的型号, 依据)。0187 的说明:豆包页面是 Seed 2.1 pro 档的
#: 标价,没有单列带日期的快照;09-24 生产发布清单记的豆包价也是 6 / 1.2 / 30。
TIER_PRICES = {
    "doubao-seed-2-1-pro-260628": (
        "doubao-seed-2-1-pro-260915",
        "tier price: 0187 docstring (Seed 2.1 pro tier, no dated snapshot listed) + "
        "2026-09-24 prod checklist (doubao 6 / 1.2 / 30)",
    ),
}
#: 价目表迁移里没有字面价的型号(§8.1 小测量用)→ (输入, 输出, 缓存读) 元 / 百万 token 与出处。
#: 只有带 ``--accept-documented-prices`` 才认,否则报价拒。
DOCUMENTED_PRICES = {
    "glm-5.3-flash": ((0.8, 2.8, 0.23), "B-302 tools/eval/b302_ab.py default_models"),
    "deepseek-flash": ((2.0, 8.0, 0.04), "B-302 tools/eval/b302_ab.py default_models"),
    "glm-5.3-flashx": ((2.0, 7.0, 0.57), "2026-09-24 prod checklist (glm-5.3-flashx 2 / 0.57 / 7)"),
}
#: §2 / §5 实测的单图输入 token(问方向用 150 万像素副本;读用原图)→ (问方向, 读, 出处)。
MEASURED_TOKENS = {
    "doubao-seed-2-1-pro-260915": (1361, 1329, "§2 11-11 measured"),
    QWEN_MODEL: (1522, 2529, "§2 11-11 measured"),
}
#: 没量过的型号按谁估:260628 按同家族的 260915(未验证);§8.1 的三个按已知最大值。
TOKEN_PROXIES = {
    "doubao-seed-2-1-pro-260628": "doubao-seed-2-1-pro-260915",
    "glm-5.3-flashx": QWEN_MODEL,
    "glm-5.3-flash": QWEN_MODEL,
    "deepseek-flash": QWEN_MODEL,
}
#: 系统提示 + 问题的文字 token、两类回答的输出 token(关思考;估算口径)。关不了思考的模型
#: (目录 ``always_thinking``,如 glm-5.3-flash / flashx,快看是最低档)每次按 1,000 输出
#: token 估(未量过)。
PROMPT_TOKENS, ORIENT_OUT_TOKENS, READ_OUT_TOKENS, THINKING_OUT_TOKENS = 150, 5, 60, 1000
#: 单次调用的上限:输入超过就停(图都是合成的、尺寸已知,超了说明有东西不对);输出由
#: ``max_tokens`` 在厂商那边截断。硬上限按它们算。不用 4096:``ModelSpec`` 把非 Anthropic 模型上的
#: 4096(B-105 之前的旧默认)归一成「不设」,上限就没了。
MAX_INPUT_TOKENS_PER_CALL, MAX_OUTPUT_TOKENS = 8000, 4000
#: 每家同时在跑的 ``ask_image`` 数(设计 §9.3「并发每家 2 路」)与一个 ``ask_image`` 里
#: 最多并行的调用数(问方向第 2 步三个角度并行)。
CONCURRENCY_PER_PROVIDER, MAX_PARALLEL_CALLS_PER_ITEM = 2, 3
STOP_LINE_FACTOR, STOP_LINE_MAX = 1.5, 200.0

#: §9.2 的线(绝对数)。
L1_MIN, L1_PER_CLASS_MIN = 94, 8
L1P_UNDETERMINED_MAX = 3
L2_MIN, L2_PREMISE_MIN, L2_CONTROL_LOW, L2_CONTROL_HIGH, WORSE_BY = 7, 2, 4, 7, 2
L6_MAX_RATE = 0.05


class PlanError(RuntimeError):
    """报价 / 计划的前提不成立 —— 停下,不猜。"""


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    source: str


@dataclass(frozen=True)
class Item:
    """一次要做的事:``on`` = 开问方向的 ``ask_image``;``control`` = 关着;
    ``probe`` = §8.1 只问方向。"""

    model: str
    mode: str
    image_id: str

    @property
    def key(self) -> str:
        return f"{self.model}|{self.mode}|{self.image_id}"

    @property
    def provider(self) -> str:
        return PROVIDERS[self.model]


# --------------------------------------------------------------------------- 单价


def rate_card_rows(path: Path = RATE_CARD_MIGRATION) -> dict[str, Price]:
    """价目表迁移里的 ``_PRICES``(不 import:它依赖 alembic 的运行上下文)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_PRICES" for t in node.targets
        ):
            rows = ast.literal_eval(node.value)
            return {
                str(model): Price(inp / 1e6, out / 1e6, read / 1e6, f"rate card {path.name}")
                for _provider, model, inp, out, _creation, read in rows
            }
    raise PlanError(f"no _PRICES in {path}")


def price_of(
    model: str, *, accept_documented: bool, rows: Mapping[str, Price] | None = None
) -> Price:
    """这个型号的单价,并说明出处;核不了就拒。"""
    card = rate_card_rows() if rows is None else rows
    if model in card:
        return card[model]
    if model in TIER_PRICES:
        target, why = TIER_PRICES[model]
        if target not in card:
            raise PlanError(f"{model}: tier price source {target} has no rate card row")
        base = card[target]
        return Price(base.input, base.output, base.cache_read, why)
    if model in DOCUMENTED_PRICES:
        if not accept_documented:
            raise PlanError(
                f"{model} has no rate card row in a migration; its price "
                f"{DOCUMENTED_PRICES[model][0]} comes from {DOCUMENTED_PRICES[model][1]}. "
                "Re-run with --accept-documented-prices only if that price was confirmed."
            )
        (inp, out, read), why = DOCUMENTED_PRICES[model]
        return Price(inp, out, read, f"documented, not in a migration: {why}")
    raise PlanError(f"{model}: no validated price (not in {RATE_CARD_MIGRATION.name})")


def tokens_of(model: str) -> tuple[int, int, str]:
    if model in MEASURED_TOKENS:
        return MEASURED_TOKENS[model]
    proxy = TOKEN_PROXIES.get(model)
    if proxy is None or proxy not in MEASURED_TOKENS:
        raise PlanError(f"{model}: no measured or proxy image token count")
    orient, read, _ = MEASURED_TOKENS[proxy]
    return orient, read, f"not measured; estimated from {proxy}"


def per_call_cap_yuan(price: Price) -> float:
    return (MAX_INPUT_TOKENS_PER_CALL * price.input + MAX_OUTPUT_TOKENS * price.output) / 1e6


# --------------------------------------------------------------------------- 计划


def probe_images(image_set: Sequence[images_mod.SetImage]) -> list[images_mod.SetImage]:
    """§8.1:33 张正放 + 每张底图一个转过的方向(按底图顺序轮 90 / 180 / 270)+ 4 张无文字。"""
    by_id = {image.image_id: image for image in image_set}
    picked: list[images_mod.SetImage] = []
    bases = [spec.base_id for spec in images_mod.base_specs()]
    for index, base_id in enumerate(bases):
        picked.append(by_id[f"{base_id}@0"])
        picked.append(by_id[f"{base_id}@{TURNS[index % len(TURNS)]}"])
    picked.extend(image for image in image_set if image.image_class == images_mod.NO_TEXT_CLASS)
    return picked


def plan_items(
    models: Sequence[str],
    probe_models: Sequence[str],
    image_set: Sequence[images_mod.SetImage],
) -> list[Item]:
    """先后:每个模型先跑正放 + 无文字(L1' 最要紧,止损时先保住它),再跑转过的,最后对照。"""
    upright = [image for image in image_set if image.upright]
    rotated = [image for image in image_set if not image.upright]
    items: list[Item] = []
    for model in models:
        items.extend(Item(model, "on", image.image_id) for image in upright)
    for model in models:
        items.extend(Item(model, "on", image.image_id) for image in rotated)
    for model in models:
        items.extend(Item(model, "control", image.image_id) for image in rotated)
    probe = probe_images(image_set)
    for model in probe_models:
        items.extend(Item(model, "probe", image.image_id) for image in probe)
    return items


def plan_id(models: Sequence[str], probe_models: Sequence[str], digest: str) -> str:
    """计划编号:图集、模型、问法、单次上限。续跑只认同一个计划编号。"""
    payload = {
        "set": digest,
        "models": list(models),
        "probe": list(probe_models),
        "question": ORIENTATION_QUESTION,
        "max_output": MAX_OUTPUT_TOKENS,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]


def call_counts(
    items: Iterable[Item], image_set: Sequence[images_mod.SetImage]
) -> dict[str, dict[str, int]]:
    """每个模型:问方向(预计 = 每张 1 次 + 转过的 99 张各 3 次;最坏 = 每张 4 次)、读。"""
    rotation = {image.image_id: image.rotation for image in image_set}
    counts: dict[str, dict[str, int]] = {}
    for item in items:
        c = counts.setdefault(item.model, {"orientation": 0, "orientation_worst": 0, "read": 0})
        if item.mode == "on":
            c["orientation"] += 1 + (len(TURNS) if rotation[item.image_id] else 0)
            c["orientation_worst"] += 1 + len(TURNS)
            c["read"] += 1
        elif item.mode == "control":
            c["read"] += 1
        else:
            c["orientation"] += 1
            c["orientation_worst"] += 1
    return counts


def quote(
    *,
    models: Sequence[str],
    probe_models: Sequence[str],
    stop_line: float | None,
    accept_documented: bool,
    image_set: Sequence[images_mod.SetImage] | None = None,
    rows: Mapping[str, Price] | None = None,
) -> dict[str, Any]:
    image_set = images_mod.build_set() if image_set is None else image_set
    items = plan_items(models, probe_models, image_set)
    counts = call_counts(items, image_set)
    by_model: dict[str, Any] = {}
    for model in [*models, *probe_models]:
        price = price_of(model, accept_documented=accept_documented, rows=rows)
        orient_in, read_in, token_source = tokens_of(model)
        c = counts[model]
        per_orient_in, per_read_in = (
            orient_in + PROMPT_TOKENS,
            max(orient_in, read_in) + PROMPT_TOKENS,
        )
        catalog = catalog_entry(PROVIDERS[model], model)
        thinks = THINKING_OUT_TOKENS if catalog is not None and catalog.always_thinking else 0
        per_orient_out, per_read_out = ORIENT_OUT_TOKENS + thinks, READ_OUT_TOKENS + thinks
        tokens_in = c["orientation"] * per_orient_in + c["read"] * per_read_in
        tokens_out = c["orientation"] * per_orient_out + c["read"] * per_read_out
        worst_in = c["orientation_worst"] * per_orient_in + c["read"] * per_read_in
        worst_out = c["orientation_worst"] * per_orient_out + c["read"] * per_read_out
        by_model[model] = {
            "provider": PROVIDERS[model],
            "kind": "probe" if model in probe_models else "health",
            "price_yuan_per_mtok": {"input": price.input, "output": price.output},
            "price_source": price.source,
            "image_tokens": {"orientation": orient_in, "read": read_in, "source": token_source},
            "calls": {**c, "total": c["orientation"] + c["read"]},
            "tokens": {"input": tokens_in, "output": tokens_out},
            "expected_yuan": round((tokens_in * price.input + tokens_out * price.output) / 1e6, 2),
            "worst_yuan": round((worst_in * price.input + worst_out * price.output) / 1e6, 2),
            "per_call_cap_yuan": round(per_call_cap_yuan(price), 4),
        }
    expected = round(sum(m["expected_yuan"] for m in by_model.values()), 2)
    worst = round(sum(m["worst_yuan"] for m in by_model.values()), 2)
    line = (
        float(math.ceil(max(worst, expected * STOP_LINE_FACTOR)))
        if stop_line is None
        else float(stop_line)
    )
    if line < expected:
        raise PlanError(f"stop line {line} is below the expected total {expected}")
    if line > STOP_LINE_MAX:
        raise PlanError(f"stop line {line} is above {STOP_LINE_MAX} — likely a typo")
    in_flight = CONCURRENCY_PER_PROVIDER * MAX_PARALLEL_CALLS_PER_ITEM
    caps: dict[str, float] = {}
    for entry in by_model.values():
        caps[entry["provider"]] = max(caps.get(entry["provider"], 0.0), entry["per_call_cap_yuan"])
    hard_cap = round(line + sum(in_flight * cap for cap in caps.values()), 2)
    body: dict[str, Any] = {
        "plan_id": plan_id(models, probe_models, images_mod.set_digest(image_set)),
        "images": len(image_set),
        "items": len(items),
        "models": by_model,
        "expected_total_yuan": expected,
        "worst_total_yuan": worst,
        "stop_line_yuan": line,
        "hard_cap_yuan": hard_cap,
        "in_flight_calls_per_provider": in_flight,
    }
    digest = json.dumps(body, sort_keys=True).encode()
    return {**body, "quote_id": hashlib.sha256(digest).hexdigest()[:12]}


def read_records(path: Path) -> list[dict[str, Any]]:
    """结果文件(JSONL):每行一条跑完的条目。"""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


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
    import b301_report
    import b301_spend

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
    import asyncio

    import b301_health_run as runner

    env_files = args.env_file or [runner.DEFAULT_ENV_FILE]
    import b301_spend

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
