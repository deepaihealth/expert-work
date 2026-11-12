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
                                         [与 quote 相同的开关] [--env-file ~/.helix-eval.env]
    python tools/eval/b301_health.py report --results OUT.jsonl
    python tools/eval/b301_images.py OUT_DIR       # 把图集写成文件,人工抽看(不花钱)

怎么跑(本机,不连集群;代码是本分支的 ``ask_image``,不是测试环境部署的那一版):

1. ``quote``:离线,按 §2 实测的单图 token 与价目表迁移报价,打印每个模型的调用数、token、金额、
   止损线、硬上限和 ``quote_id``。价目表里查不到、又不在「同档」表里的模型直接拒(fail-stop)。
2. 把报价交用户批。批了再 ``run``:``--quote-id`` 必须等于在**同一台机器、同样开关**下现算的编号
   (图集字节、模型、单价、止损线任一变了编号就变,旧批准不作数)。
3. 凭据只从环境变量或 ``--env-file``(进程内解析,不 echo、不进 argv)取,变量名见
   ``b301_health_run.KEY_VARIABLES``;豆包是 ``EXPERT_WORK_EVAL_DOUBAO_API_KEY``。缺哪个就报变量名、
   不开跑。
4. ``run`` 每次调用之前判止损线;过线、遇到没有用量的回答、单次输入超上限、或厂商返回超时以外的
   任何错误,都立刻停下(不再发新调用),已在飞的最多「每家 2 个 ``ask_image`` x 每个最多 3 个并行
   调用」。结果按条追加进 JSONL,断了再跑同一条命令接着跑(已花的钱计入止损线)。
5. ``report``:按 §9.2 出各线和每个模型的结论。**通过 = 数据齐 + L1'(正放 33 + 无文字 4 张 0 张被转、
   「判不出」≤ 3)+ L6(超时 + 答非所问 + 出错 ≤ 5%)**;L1 / L2 照常报数,不挡发布,任何一类
   「变差」标出来交用户定。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import re
import statistics
import sys
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import b301_images as images_mod  # noqa: E402

from expert_work.protocol.model_catalog import catalog_entry  # noqa: E402
from orchestrator.tools.image_orientation import (  # noqa: E402
    ORIENTATION_QUESTION,
    ROTATED_NOTE,
    TURNS,
    UNDETERMINED_NOTE,
    first_conclusion,
    turned_conclusion,
)
from orchestrator.tools.region_zoom import REGION_HINT  # noqa: E402

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


# --------------------------------------------------------------------------- 判分


#: 弯引号写成转义:ruff 的模糊字符规则会把全角标点「修」成半角。
_DROP = re.compile(
    "[\\s,\"'\\u201c\\u201d\\u2018\\u2019`*()\\[\\]{}<>\\u300a\\u300b\\u300c\\u300d\\u3010\\u3011]"
)
_TRAILING = re.compile(r"[。.!?;]+$")


def normalize(text: str) -> str:
    """NFKC(全角转半角)、小写、去空白 / 千分位逗号 / 引号括号 / markdown 记号、去句末标点。
    保留 ``. - / : % _``:它们在日期、编号、读数里有意义。"""
    folded = unicodedata.normalize("NFKC", text).lower()
    return _TRAILING.sub("", _DROP.sub("", folded))


def field_matches(key: str, answer: str) -> bool:
    """关键字段对不对:规范化后相等,或作为整体出现在回答里(两侧不是紧挨着的字母数字 / 小数点)。"""
    want, got = normalize(key), normalize(answer)
    if not want:
        return False
    if got == want:
        return True
    start = got.find(want)
    while start != -1:
        before = got[start - 1] if start > 0 else ""
        after = got[start + len(want)] if start + len(want) < len(got) else ""
        if not _glued(before, want[0]) and not _glued(after, want[-1]):
            return True
        start = got.find(want, start + 1)
    return False


def _glued(neighbour: str, edge: str) -> bool:
    """紧挨着的是同一类(数字 / 小数点挨数字,字母挨字母)才算粘连:``169.1`` 里没有 ``69.1``,
    ``69.1kg`` 里有。"""

    def kind(ch: str) -> str:
        if ch.isascii() and (ch.isdigit() or ch == "."):
            return "number"
        if ch.isascii() and ch.isalpha():
            return "letter"
        return ""

    return bool(neighbour) and kind(neighbour) != "" and kind(neighbour) == kind(edge)


def strip_notes(content: str) -> str:
    """去掉工具追加在回答后面的说明(转过 / 判不出 / 建议用 region),只留看图模型的回答。"""
    for note in (UNDETERMINED_NOTE, REGION_HINT, *(ROTATED_NOTE.format(degrees=d) for d in TURNS)):
        content = content.replace(note, "")
    return content.strip()


def orientation_of(record: Mapping[str, Any]) -> tuple[str, int, str | None]:
    """(结论, 角度, 第 1 步的回答)。工具结果里有 meta 就用它;工具出错没有 meta 时按调用记录
    用同一套规则推出来(第 2 步三个角度靠副本路径里的 ``r<角度>`` 分辨)。"""
    meta = record.get("orientation")
    if isinstance(meta, Mapping):
        first_answer = (meta.get("answers") or {}).get("0")
        return str(meta["conclusion"]), int(meta["degrees"]), first_answer
    answers: dict[int, Any] = {}
    for call in record.get("calls", []):
        if call.get("kind") == "orientation":
            answers[int(call.get("turn", 0))] = call.get("answer")
    first = answers.get(0)
    if first is None:
        return "not_asked", 0, None
    conclusion = first_conclusion(first)
    if conclusion is not None:
        return conclusion, 0, first
    if not all(turn in answers for turn in TURNS):
        return "not_asked", 0, first
    turned, degrees = turned_conclusion({turn: answers[turn] for turn in TURNS})
    return turned, degrees, first


def _latency_stats(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"median_s": None, "p95_s": None}
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]
    return {"median_s": round(statistics.median(ordered), 2), "p95_s": round(p95, 2)}


def _read_right(record: Mapping[str, Any] | None, key: str) -> bool:
    """这一条读出了关键字段(工具出错的一律算没读出)。"""
    if record is None or record.get("status") != "ok":
        return False
    return field_matches(key, str(record.get("answer", "")))


def _classify(upright_ok: int, on_ok: int, control_ok: int) -> str:
    if upright_ok < L2_PREMISE_MIN:
        return "模型本来就读不好(不计入)"
    if on_ok <= control_ok - WORSE_BY:
        return "变差"
    if control_ok >= L2_CONTROL_HIGH:
        return "无收益"
    if control_ok <= L2_CONTROL_LOW and on_ok >= L2_MIN:
        return "有收益"
    return "介于之间"


def model_report(
    model: str, records: Sequence[Mapping[str, Any]], image_set: Sequence[images_mod.SetImage]
) -> dict[str, Any]:
    """一个体检模型的 §9.2 各线与结论。"""
    on = {r["image_id"]: r for r in records if r["model"] == model and r["mode"] == "on"}
    control = {r["image_id"]: r for r in records if r["model"] == model and r["mode"] == "control"}
    upright_ids = [i.image_id for i in image_set if i.upright]
    rotated = [i for i in image_set if not i.upright]
    missing = len(upright_ids) + len(rotated) - len(on) + len(rotated) - len(control)

    l1_ok, l1_by_class = 0, dict[str, int]()
    for image in rotated:
        record = on.get(image.image_id)
        hit = record is not None and orientation_of(record)[:2] == ("rotated", image.rotation)
        l1_ok += hit
        l1_by_class[image.image_class] = l1_by_class.get(image.image_class, 0) + hit
    l1_pass = l1_ok >= L1_MIN and all(v >= L1_PER_CLASS_MIN for v in l1_by_class.values())

    turned, undetermined, first_no = [], [], 0
    for image_id in upright_ids:
        record = on.get(image_id)
        if record is None:
            continue
        conclusion, _degrees, first = orientation_of(record)
        turned += [image_id] if conclusion == "rotated" else []
        undetermined += [image_id] if conclusion == "undetermined" else []
        first_no += first == "no"
    l1p_pass = not turned and len(undetermined) <= L1P_UNDETERMINED_MAX

    classes: dict[str, dict[str, Any]] = {}
    for image in image_set:
        if image.key is None:
            continue
        c = classes.setdefault(image.image_class, {"upright": 0, "on": 0, "control": 0})
        if _read_right(on.get(image.image_id), image.key):
            c["upright" if image.upright else "on"] += 1
        if _read_right(control.get(image.image_id), image.key):
            c["control"] += 1
    for c in classes.values():
        c["l2_pass"] = c["upright"] < L2_PREMISE_MIN or c["on"] >= L2_MIN
        c["result"] = _classify(c["upright"], c["on"], c["control"])

    calls = [
        call
        for r in on.values()
        for call in r.get("calls", [])
        if call.get("kind") == "orientation"
    ]
    timeouts = sum(call.get("outcome") == "timeout" for call in calls)
    errors = sum(call.get("outcome") == "error" for call in calls)
    off_topic = sum(
        call.get("outcome") == "ok" and call.get("answer") == "off_topic" for call in calls
    )
    bad_rate = (timeouts + off_topic + errors) / len(calls) if calls else 1.0
    latency = _latency_stats([c["latency_s"] for c in calls if c.get("outcome") == "ok"])
    l6_pass = bool(calls) and bad_rate <= L6_MAX_RATE

    complete = missing == 0
    why: list[str] = []
    if not complete:
        why.append(f"数据不齐:还差 {missing} 条")
    if turned:
        why.append(f"L1' 正放的图被转了 {len(turned)} 张:{', '.join(turned)}")
    if len(undetermined) > L1P_UNDETERMINED_MAX:
        why.append(f"L1' 「判不出」{len(undetermined)} 张 > {L1P_UNDETERMINED_MAX}")
    if not l6_pass:
        why.append(f"L6 问方向没问成的比例 {bad_rate:.1%} > {L6_MAX_RATE:.0%}")
    verdict = "不完整" if not complete else ("通过" if l1p_pass and l6_pass else "不通过")
    worse = [name for name, c in classes.items() if c["result"] == "变差"]
    return {
        "model": model,
        "verdict": verdict,
        "why": why,
        "needs_user": [f"L2「变差」:{', '.join(worse)}"] if worse else [],
        "L1": {"ok": l1_ok, "of": len(rotated), "by_class": l1_by_class, "pass": l1_pass},
        "L1'": {
            "turned": turned,
            "undetermined": undetermined,
            "first_step_no": first_no,
            "of": len(upright_ids),
            "pass": l1p_pass,
        },
        "L2": classes,
        "L6": {
            "calls": len(calls),
            "timeouts": timeouts,
            "off_topic": off_topic,
            "errors": errors,
            "bad_rate": round(bad_rate, 4),
            **latency,
            "pass": l6_pass,
        },
        "spent_yuan": round(
            sum(float(r.get("cost_yuan", 0.0)) for r in records if r["model"] == model), 4
        ),
    }


def probe_report(
    model: str, records: Sequence[Mapping[str, Any]], image_set: Sequence[images_mod.SetImage]
) -> dict[str, Any]:
    """§8.1:正放答 yes(或无文字答 none)、转过的答 no 的张数;没答成的另计。"""
    rotation = {i.image_id: (i.rotation, i.image_class) for i in image_set}
    rows = [r for r in records if r["model"] == model and r["mode"] == "probe"]
    counts = {
        "upright_yes": 0,
        "upright": 0,
        "rotated_no": 0,
        "rotated": 0,
        "no_text_none": 0,
        "no_text": 0,
    }
    unanswered = 0
    for r in rows:
        degrees, image_class = rotation[r["image_id"]]
        answer = r.get("probe_answer")
        unanswered += answer not in ("yes", "no", "none")
        if image_class == images_mod.NO_TEXT_CLASS:
            counts["no_text"] += 1
            counts["no_text_none"] += answer == "none"
        elif degrees == 0:
            counts["upright"] += 1
            counts["upright_yes"] += answer == "yes"
        else:
            counts["rotated"] += 1
            counts["rotated_no"] += answer == "no"
    latency = _latency_stats(
        [c["latency_s"] for r in rows for c in r.get("calls", []) if c.get("outcome") == "ok"]
    )
    return {"model": model, **counts, "unanswered": unanswered, **latency}


def read_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    return rows


def report(
    records: Sequence[Mapping[str, Any]], image_set: Sequence[images_mod.SetImage] | None = None
) -> dict[str, Any]:
    image_set = images_mod.build_set() if image_set is None else image_set
    health = sorted({r["model"] for r in records if r["mode"] in ("on", "control")})
    probe = sorted({r["model"] for r in records if r["mode"] == "probe"})
    return {
        "health": [model_report(m, records, image_set) for m in health],
        "probe": [probe_report(m, records, image_set) for m in probe],
        "spent_yuan": round(sum(float(r.get("cost_yuan", 0.0)) for r in records), 4),
    }


def _mark(passed: bool) -> str:
    return "过" if passed else "没过"


def render_report(result: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for m in result["health"]:
        lines.append(f"== {m['model']}:{m['verdict']}")
        lines.extend(f"   - {why}" for why in m["why"])
        lines.extend(f"   - 交用户定:{item}" for item in m["needs_user"])
        l1, l1p, l6 = m["L1"], m["L1'"], m["L6"]
        lines.append(
            f"   L1  转过的 {l1['ok']}/{l1['of']}"
            f"(及格 ≥{L1_MIN},每类 ≥{L1_PER_CLASS_MIN}/9):{_mark(l1['pass'])}"
        )
        lines.append("       每类:" + ", ".join(f"{k} {v}/9" for k, v in l1["by_class"].items()))
        lines.append(
            f"   L1' 正放 + 无文字 {l1p['of']} 张:被转 {len(l1p['turned'])}、"
            f"判不出 {len(l1p['undetermined'])}、第 1 步答 no {l1p['first_step_no']}:"
            f"{_mark(l1p['pass'])}"
        )
        lines.append("   L2  每类(正放 /3、开 /9、对照 /9 → 结论):")
        for name, c in m["L2"].items():
            lines.append(
                f"       {name}: {c['upright']}/3, {c['on']}/9, {c['control']}/9 → {c['result']}"
            )
        lines.append(
            f"   L6  问方向 {l6['calls']} 次:超时 {l6['timeouts']}、答非所问 {l6['off_topic']}、"
            f"出错 {l6['errors']}({l6['bad_rate']:.1%});"
            f"耗时中位 {l6['median_s']} s、p95 {l6['p95_s']} s:{_mark(l6['pass'])}"
        )
        lines.append(f"   花费 ¥{m['spent_yuan']}")
    for p in result["probe"]:
        lines.append(
            f"== §8.1 {p['model']}:正放答 yes {p['upright_yes']}/{p['upright']}、"
            f"转过答 no {p['rotated_no']}/{p['rotated']}、"
            f"无文字答 none {p['no_text_none']}/{p['no_text']}、没答成 {p['unanswered']};"
            f"耗时中位 {p['median_s']} s"
        )
    lines.append(f"合计花费 ¥{result['spent_yuan']}")
    return "\n".join(lines)


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
    run_parser.add_argument("--env-file", type=Path, default=Path.home() / ".helix-eval.env")
    report_parser = sub.add_parser("report", help="offline report")
    report_parser.add_argument("--results", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "report":
        sys.stdout.write(render_report(report(read_records(args.results))) + "\n")
        return 0
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

    try:
        summary = asyncio.run(
            runner.run_from_quote(
                q, models=models, probe_models=probe, results=args.results, env_file=args.env_file
            )
        )
    except runner.RunRefusedError as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(asdict(summary), ensure_ascii=False, indent=2) + "\n")
    return 0 if summary.stopped_reason is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
