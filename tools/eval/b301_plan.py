"""B-301 PR 2 体检的计划与报价(设计 §9.2 / §9.3):模型、单价核对、条目、调用数 / token / 金额、
止损线、硬上限、``plan_id`` / ``quote_id``,以及 §9.2 各线的及格数。纯计算,不发任何调用。

这是体检各模块共用的底:只依赖图集(:mod:`b301_images`),不依赖花费 / 报告 / 真跑 / 命令行那几个
模块。命令行入口在 ``b301_health.py``。
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
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
