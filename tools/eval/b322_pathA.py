"""B-322 PR ① —— §6.2 E 组:主模型自己「问方向」的体检(设计 §6.2 / §6.3 E 线)。

``docs/superpowers/specs/2026-11-19-b322-pathA-vision-parity-design.md``。体检的是**要设
``orientation_verified`` 的主模型**(默认 ``qwen/qwen4-max``):B-301 的合成图集(正放 33 + 无文字 4 +
转过 99 = 136 张),每张走**本分支** PR ① 的完整两步(``ImageOrienter._judge_one``:同一句问题、
同一张约 150 万像素的候选、同一个两步判定、同一套按回答归类),记判不出、转错角度、问方向耗时、
输出 token。

本机跑、不连集群、不经测试环境部署。这一份只做 E 组(合并 PR ① 之前要的那一笔,§8 第 12 条);
A / B / C / D / P 组在 PR ① 合并后另行报价(A / B 要用户授权的真实照片,不在仓库里)。

子命令(只有 ``run`` 花钱):

    python tools/eval/b322_pathA.py quote [--model qwen/qwen4-max ...] [--stop-line 元]
    python tools/eval/b322_pathA.py run --quote-id <quote 打印的编号> --results OUT.jsonl
                                        [与 quote 相同的 --model] [--env-file F ...]
    python tools/eval/b322_pathA.py report --results OUT.jsonl

* ``quote`` 离线:按 §4.1.10 的单次口径与价目表迁移报价,打印调用数、金额、止损线、``quote_id``。
  价目表里查不到的模型直接拒。
* ``run`` 必须带 ``quote`` 打印的 ``quote_id``(同一台机器、同样参数现算;图集 / 模型 / 单价 / 止损线
  任一变了编号就变)。凭据只从环境变量或 ``--env-file`` 取(变量名同 B-301,
  ``b301_health_run.KEY_VARIABLES``;不 echo、不进 argv)。每次调用之前判止损线、按单次上限预留并落账
  (B-301 的 :mod:`b301_spend`:账本按计划放在 ``~/.cache/b322-pathA/``,不能删);过线、用量不可信、
  厂商返回超时以外的错误 → 停。
* ``report``:按 §6.3 的 E 线出结论:**S3 正放 + 无文字 37 张 0 张被转**(硬线)、**W 转过的 99 张
  0 张转错角度**(硬线)、S3 判不出 ≤ 3、**H 超时 + 答非所问 ≤ 5%**;另报转对数、判不出数、
  问方向耗时中位 / p95、输出 token。三条全过才可以在模型目录里给这个模型设 ``orientation_verified``
  (注释写体检日期与结果出处)。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import contextvars
import hashlib
import json
import math
import statistics
import sys
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import b301_health_run as b301_run  # noqa: E402
import b301_images as images_mod  # noqa: E402
import b301_plan as b301_plan  # noqa: E402
import b301_spend as spend  # noqa: E402
from langchain_core.messages import AIMessage, BaseMessage  # noqa: E402

from expert_work.protocol import MODEL_CATALOG, ModelSpec  # noqa: E402
from expert_work.protocol.model_catalog import catalog_entry  # noqa: E402
from expert_work.protocol.multimodal import ImageRef  # noqa: E402
from expert_work.runtime.cancellation import CancellationToken  # noqa: E402
from orchestrator.tools.image_orientation import (  # noqa: E402
    ORIENTATION_QUESTION,
    ORIENTATION_QUESTION_VERSION,
    ORIENTATION_TIMEOUT_S,
    TURNS,
)

DEFAULT_MODELS = ("qwen/qwen4-max",)
_MIGRATIONS = Path(__file__).resolve().parents[2] / "packages/expert-work-persistence/migrations"
#: 主模型的价在这几张迁移里(qwen4-max 0192、Claude 5 四个 0193)。
RATE_CARD_MIGRATIONS = (
    _MIGRATIONS / "versions/0192_rate_card_qwen4_max.py",
    _MIGRATIONS / "versions/0193_rate_card_claude5.py",
)
#: 一次问方向的输入 token(看图系统提示 + 问题 + 一张约 150 万像素的候选)。§4.1.10 的口径:
#: qwen 按 B-301 §2 在 ask_image 路由上实测的形状(1,525)取整 1,550;别家没有数据,按 qwen 估,
#: 真跑时以账本里的实际用量为准。
CALL_INPUT_TOKENS = 1550
#: 能关思考的输出按 10 token;关不掉的(目录 ``always_thinking``)按 1,000 token 的上限估(§4.1.10)。
ANSWER_OUT_TOKENS, THINKING_OUT_TOKENS = 10, 1000
#: 每家同时在判的图(每张第 2 步三个角度并行)。
IMAGES_IN_FLIGHT = 2
STOP_LINE_FACTOR, STOP_LINE_MAX = 1.5, 200.0
DEFAULT_STATE_DIR = Path.home() / ".cache" / "b322-pathA"
DEFAULT_ENV_FILE = Path.home() / ".kube" / "eval-llm.env"

#: §6.3 E 线。
S3_UNDETERMINED_MAX = 3
H_MAX_RATE = 0.05

_TENANT = uuid.UUID("b3220000-0000-4000-8000-000000000001")
_THREAD = uuid.UUID("b3220000-0000-4000-8000-000000000003")
_USER = uuid.UUID("b3220000-0000-4000-8000-000000000002")


@dataclass(frozen=True)
class Item:
    model: str  # provider/name
    image_id: str

    @property
    def key(self) -> str:
        return f"{self.model}|{self.image_id}"

    @property
    def provider(self) -> str:
        return self.model.partition("/")[0]


# --------------------------------------------------------------------------- 计划与报价


def _split(model: str) -> tuple[str, str]:
    provider, _, name = model.partition("/")
    if not provider or not name or catalog_entry(provider, name) is None:
        raise b301_plan.PlanError(f"{model}: not a catalog model (want provider/name)")
    return provider, name


def price_of(model: str, rows: Mapping[str, b301_plan.Price] | None = None) -> b301_plan.Price:
    card: dict[str, b301_plan.Price] = {}
    if rows is None:
        for path in RATE_CARD_MIGRATIONS:
            try:
                card.update(b301_plan.rate_card_rows(path))
            except ValueError:
                # 0193 的价是按汇率现算的表达式,不是字面量:Claude 要报价时先把它算成字面价再加。
                continue
    else:
        card.update(rows)
    name = _split(model)[1]
    if name not in card:
        raise b301_plan.PlanError(
            f"{model}: no rate card row in {[p.name for p in RATE_CARD_MIGRATIONS]}"
        )
    return card[name]


def plan_items(models: Sequence[str], image_set: Sequence[images_mod.SetImage]) -> list[Item]:
    """先正放 + 无文字(S3 最要紧,止损时先保住它),再转过的。"""
    upright = [image for image in image_set if image.upright]
    rotated = [image for image in image_set if not image.upright]
    items: list[Item] = []
    for model in models:
        items.extend(Item(model, image.image_id) for image in upright)
    for model in models:
        items.extend(Item(model, image.image_id) for image in rotated)
    return items


def quote(
    *,
    models: Sequence[str],
    stop_line: float | None,
    image_set: Sequence[images_mod.SetImage] | None = None,
    rows: Mapping[str, b301_plan.Price] | None = None,
) -> dict[str, Any]:
    image_set = images_mod.build_set() if image_set is None else image_set
    rotated = sum(not image.upright for image in image_set)
    by_model: dict[str, Any] = {}
    for model in models:
        provider, name = _split(model)
        price = price_of(model, rows)
        entry = catalog_entry(provider, name)
        out = (
            THINKING_OUT_TOKENS
            if entry is not None and entry.always_thinking
            else ANSWER_OUT_TOKENS
        )
        expected_calls = len(image_set) + rotated * len(TURNS)
        worst_calls = len(image_set) * (1 + len(TURNS))
        per_call = (CALL_INPUT_TOKENS * price.input + out * price.output) / 1e6
        by_model[model] = {
            "price_yuan_per_mtok": {"input": price.input, "output": price.output},
            "price_source": price.source,
            "per_call": {"input_tokens": CALL_INPUT_TOKENS, "output_tokens": out},
            "calls": {"expected": expected_calls, "worst": worst_calls},
            "expected_yuan": round(expected_calls * per_call, 2),
            "worst_yuan": round(worst_calls * per_call, 2),
            "per_call_cap_yuan": round(b301_plan.per_call_cap_yuan(price), 4),
        }
    expected = round(sum(m["expected_yuan"] for m in by_model.values()), 2)
    worst = round(sum(m["worst_yuan"] for m in by_model.values()), 2)
    line = (
        float(math.ceil(max(worst, expected * STOP_LINE_FACTOR)))
        if stop_line is None
        else float(stop_line)
    )
    if line < expected:
        raise b301_plan.PlanError(f"stop line {line} is below the expected total {expected}")
    if line > STOP_LINE_MAX:
        raise b301_plan.PlanError(f"stop line {line} is above {STOP_LINE_MAX} — likely a typo")
    in_flight = IMAGES_IN_FLIGHT * len(TURNS)
    hard_cap = round(line + sum(in_flight * m["per_call_cap_yuan"] for m in by_model.values()), 2)
    plan_payload = {
        "set": images_mod.set_digest(image_set),
        "models": list(models),
        "question": ORIENTATION_QUESTION,
        "version": ORIENTATION_QUESTION_VERSION,
        "max_output": b301_plan.MAX_OUTPUT_TOKENS,
    }
    body: dict[str, Any] = {
        "plan_id": hashlib.sha256(json.dumps(plan_payload, sort_keys=True).encode()).hexdigest()[
            :12
        ],
        "images": len(image_set),
        "items": len(plan_items(models, image_set)),
        "models": by_model,
        "expected_total_yuan": expected,
        "worst_total_yuan": worst,
        "stop_line_yuan": line,
        "hard_cap_yuan": hard_cap,
    }
    digest = json.dumps(body, sort_keys=True).encode()
    return {**body, "quote_id": hashlib.sha256(digest).hexdigest()[:12]}


# --------------------------------------------------------------------------- 真跑

_CALLS: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "b322_calls", default=None
)
_ITEM: contextvars.ContextVar[str] = contextvars.ContextVar("b322_item", default="")
LLMCallable = Callable[..., Awaitable[AIMessage]]
CallerFactory = Callable[[str, Any], Awaitable[LLMCallable]]


def _degrees(messages: Sequence[BaseMessage]) -> int | None:
    content = messages[-1].content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("view"), dict):
                return int(block["view"].get("degrees", 0))
    return None


@dataclass
class MeteredCaller:
    """问方向路由外面的一层:预留(过止损线、落账)→ 发 → 结算;计时;记调用表。"""

    inner: LLMCallable
    model: str
    guard: spend.SpendGuard
    clock: Callable[[], float] = time.monotonic

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Any, **_: Any) -> AIMessage:
        call: dict[str, Any] = {"turn": _degrees(messages)}
        calls, item = _CALLS.get(), _ITEM.get()
        call_id = self.guard.reserve(self.model, item=item, phase="orientation")
        started = self.clock()
        try:
            response = await self.inner(messages=messages, tools=tools)
        except asyncio.CancelledError:
            self._failed(call, calls, call_id, item, "timeout", started)
            raise
        except Exception as exc:
            outcome = "timeout" if b301_run._timeout_like(exc) else "error"
            self._failed(call, calls, call_id, item, outcome, started)
            if outcome == "error":
                self.guard.block(f"{self.model}: provider error {type(exc).__name__}")
            raise
        usage = dict(response.usage_metadata) if response.usage_metadata else None
        cost, tokens_in, tokens_out = self.guard.settle_usage(
            call_id, self.model, usage, item=item, phase="orientation"
        )
        call.update(
            outcome="ok",
            latency_s=round(self.clock() - started, 3),
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cost_yuan=round(cost, 6),
            reply=str(response.content)[: b301_run.REPLY_CHARS],
        )
        if calls is not None:
            calls.append(call)
        if self.guard.blocked is not None:
            raise spend.SpendStopError(self.guard.blocked)
        return response

    def _failed(
        self,
        call: dict[str, Any],
        calls: list[dict[str, Any]] | None,
        call_id: str,
        item: str,
        outcome: str,
        started: float,
    ) -> None:
        reason = "cap_timeout" if outcome == "timeout" else "cap_error"
        cost = self.guard.settle_cap(call_id, self.model, reason, item=item, phase="orientation")
        call.update(outcome=outcome, latency_s=round(self.clock() - started, 3), cost_yuan=cost)
        if calls is not None:
            calls.append(call)


def real_caller_factory(keys: Mapping[str, str]) -> CallerFactory:
    """与 ``agent_factory`` 给 Path A 主模型建的问方向路由同形:关思考(关不掉的落最低档)、
    单个模型、不配备用链、不挂重试中间件 —— 一次逻辑调用就是一次厂商请求。"""

    async def build(model: str, resolver: Any) -> LLMCallable:
        from orchestrator.agent_factory import (
            VL_FIRST_TOKEN_TIMEOUT_S,
            _thinking_off,
            build_llm_router,
        )
        from orchestrator.llm.endpoints import ProviderKey

        provider, name = _split(model)

        async def key_refs(vendor: str) -> list[ProviderKey]:
            return [ProviderKey(f"secret://b322-pathA/{vendor}")]

        spec = ModelSpec.model_validate(
            {
                "provider": provider,
                "name": name,
                "max_tokens": b301_plan.MAX_OUTPUT_TOKENS,
                "rate_limit_rpm": b301_run.EVAL_RATE_LIMIT_RPM,
            }
        )
        return await build_llm_router(
            _thinking_off(spec),
            secret_store=b301_run._KeyStore(keys),
            image_resolver=resolver,
            first_token_timeout_s=float(VL_FIRST_TOKEN_TIMEOUT_S),
            provider_timeout_s=float(VL_FIRST_TOKEN_TIMEOUT_S),
            provider_key_resolver=key_refs,
            ignore_api_key_ref=True,
        )

    return build


@contextlib.contextmanager
def as_verified(models: Sequence[str]) -> Iterator[None]:
    """体检期间、只在本进程里把被测模型当作已核实 —— PR ① 只采信核实模型的回答,不这样
    所有回答都会被当作「没答成」。不改任何文件;跑完恢复。"""
    saved = {provider: MODEL_CATALOG[provider] for provider in {_split(m)[0] for m in models}}  # type: ignore[index]
    names = {_split(m) for m in models}
    try:
        for provider, entries in saved.items():
            MODEL_CATALOG[provider] = tuple(  # type: ignore[index]
                e.model_copy(update={"orientation_verified": True})
                if (provider, e.name) in names
                else e
                for e in entries
            )
        yield
    finally:
        for provider, entries in saved.items():
            MODEL_CATALOG[provider] = entries  # type: ignore[index]


def _ref(image: images_mod.SetImage) -> str:
    image_id = uuid.uuid5(uuid.NAMESPACE_URL, f"b322-pathA/{image.image_id}")
    return ImageRef(tenant_id=_TENANT, thread_id=_THREAD, image_id=image_id, ext=".jpg").to_uri()


@dataclass
class RunSummary:
    done: int
    skipped: int
    discarded: int
    spent_at_start_yuan: float
    spent_yuan: float
    stopped_reason: str | None


@dataclass
class _Runner:
    guard: spend.SpendGuard
    refs: Mapping[str, str]
    by_id: Mapping[str, images_mod.SetImage]
    stamp: Mapping[str, str]
    orienters: dict[str, Any] = field(default_factory=dict)

    async def one(self, item: Item) -> dict[str, Any] | None:
        from orchestrator.graph_builder.image_upright import StepRun, _ToJudge

        image = self.by_id[item.image_id]
        calls: list[dict[str, Any]] = []
        tokens = (_CALLS.set(calls), _ITEM.set(item.key))
        run = StepRun(
            run_id=str(uuid.uuid4()),
            turn=str(uuid.uuid4()),
            first_step=True,
            remaining_s=None,
            cancel=CancellationToken(),
        )
        started = time.monotonic()
        try:
            record = await self.orienters[item.model]._judge_one(
                _ToJudge(item.image_id, item.image_id, self.refs[image.image_id], None),
                run,
                started + ORIENTATION_TIMEOUT_S,
            )
        except spend.SpendStopError:
            return None
        finally:
            _CALLS.reset(tokens[0])
            _ITEM.reset(tokens[1])
        if self.guard.blocked is not None:
            return None
        return {
            **self.stamp,
            "model": item.model,
            "image_id": image.image_id,
            "image_class": image.image_class,
            "rotation": image.rotation,
            "conclusion": record["conclusion"],
            "degrees": record["degrees"],
            "answers": record["answers"],
            "models": record["models"],
            "duration_s": round(time.monotonic() - started, 3),
            "calls": calls,
            "cost_yuan": round(sum(float(c.get("cost_yuan", 0.0)) for c in calls), 6),
        }


async def run_items(
    items: Sequence[Item],
    *,
    image_set: Sequence[images_mod.SetImage],
    guard: spend.SpendGuard,
    factory: CallerFactory,
    results: Path,
    stamp: Mapping[str, str],
) -> RunSummary:
    """按条跑(已在 ``results`` 里的跳过),每家 :data:`IMAGES_IN_FLIGHT` 张同时判;
    每条结束就追加一行。"""
    from orchestrator.graph_builder.image_upright import ImageOrienter, OrientationJudge
    from orchestrator.multimodal import InMemoryImageResolver, ResolvedImage
    from orchestrator.usage_metering import UsageIdentity

    spent_at_start = guard.spent
    refs = {image.image_id: _ref(image) for image in image_set}
    resolver = InMemoryImageResolver(
        images={refs[i.image_id]: ResolvedImage(i.media_type, i.data) for i in image_set}
    )
    done = {f"{r['model']}|{r['image_id']}" for r in b301_plan.read_records(results)}
    todo = [item for item in items if item.key not in done]
    runner = _Runner(guard, refs, {image.image_id: image for image in image_set}, stamp)
    models = list(dict.fromkeys(item.model for item in todo))
    for model in models:
        provider, name = _split(model)
        spec = ModelSpec.model_validate({"provider": provider, "name": name})
        runner.orienters[model] = ImageOrienter(
            image_resolver=resolver,
            configured_model=model,
            judge=OrientationJudge(
                caller=MeteredCaller(await factory(model, resolver), model, guard),
                meter=UsageIdentity(None, "b322-pathA", "e", "eval").meter(
                    default=(provider, name)
                ),
                model=spec,
            ),
        )
    queues: dict[str, deque[Item]] = {}
    for item in todo:
        queues.setdefault(item.provider, deque()).append(item)
    counts = {"written": 0, "discarded": 0}

    async def worker(queue: deque[Item]) -> None:
        while queue and guard.blocked is None:
            record = await runner.one(queue.popleft())
            if record is None:
                counts["discarded"] += 1
                continue
            with results.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            counts["written"] += 1

    with as_verified(models):
        await asyncio.gather(*(worker(q) for q in queues.values() for _ in range(IMAGES_IN_FLIGHT)))
    return RunSummary(
        done=counts["written"],
        skipped=len(items) - len(todo),
        discarded=counts["discarded"],
        spent_at_start_yuan=round(spent_at_start, 6),
        spent_yuan=round(guard.spent, 6),
        stopped_reason=guard.blocked,
    )


async def run_from_quote(
    q: Mapping[str, Any],
    *,
    models: Sequence[str],
    results: Path,
    env_files: Sequence[Path],
    state_dir: Path = DEFAULT_STATE_DIR,
    factory: CallerFactory | None = None,
    image_set: Sequence[images_mod.SetImage] | None = None,
    items: Sequence[Item] | None = None,
) -> RunSummary:
    """``run``:独占这个计划、止损线从这个计划的账本合计起算,然后跑。``items`` 只给测试用。"""
    image_set = images_mod.build_set() if image_set is None else image_set
    plan_id = str(q["plan_id"])
    prices = {
        model: b301_plan.Price(
            entry["price_yuan_per_mtok"]["input"],
            entry["price_yuan_per_mtok"]["output"],
            0.0,
            entry["price_source"],
        )
        for model, entry in q["models"].items()
    }
    broken_line_cost = max(b301_plan.per_call_cap_yuan(p) for p in prices.values())
    try:
        with spend.exclusive(state_dir, plan_id):
            results.parent.mkdir(parents=True, exist_ok=True)
            existing = b301_plan.read_records(results)
            ledger_file = spend.ledger_path(state_dir, plan_id)
            ledger_rows = spend.read_ledger(ledger_file, broken_line_cost=broken_line_cost)
            b301_run._same_plan(existing, plan_id, results)
            b301_run._same_plan(ledger_rows, plan_id, ledger_file)
            b301_run._ledger_covers_records(existing, ledger_file, ledger_rows, state_dir)
            if factory is None:
                providers = sorted({_split(m)[0] for m in models})
                factory = real_caller_factory(b301_run.load_keys(providers, env_files))
            guard = spend.SpendGuard(
                stop_line=float(q["stop_line_yuan"]),
                prices=prices,
                ledger=spend.SpendLedger(ledger_file, plan_id),
                settled=spend.ledger_total(ledger_rows),
            )
            planned = plan_items(models, image_set) if items is None else items
            stamp = {"plan_id": plan_id, "quote_id": str(q["quote_id"])}
            return await run_items(
                planned,
                image_set=image_set,
                guard=guard,
                factory=factory,
                results=results,
                stamp=stamp,
            )
    except spend.LockedError as exc:
        raise b301_run.RunRefusedError(str(exc)) from exc


# --------------------------------------------------------------------------- 报告


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)]


def report(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """§6.3 E 线,每个模型一份。S3 / W 是硬线;H 按所有问方向调用算。"""
    out: dict[str, Any] = {}
    for model in dict.fromkeys(str(r["model"]) for r in records):
        rows = [r for r in records if r["model"] == model]
        upright = [r for r in rows if int(r["rotation"]) == 0]
        rotated = [r for r in rows if int(r["rotation"]) != 0]
        s3_turned = [r["image_id"] for r in upright if r["conclusion"] == "rotated"]
        s3_undetermined = [
            r["image_id"]
            for r in upright
            if r["conclusion"] != "rotated" and r["conclusion"] not in ("upright", "no_text")
        ]
        wrong_angle = [
            r["image_id"]
            for r in rotated
            if r["conclusion"] == "rotated" and int(r["degrees"]) != int(r["rotation"])
        ]
        right = sum(
            r["conclusion"] == "rotated" and int(r["degrees"]) == int(r["rotation"])
            for r in rotated
        )
        calls = [c for r in rows for c in r.get("calls", [])]
        unanswered = [
            c
            for c in calls
            if c.get("outcome") != "ok"
            or b301_run.parse_orientation_reply(str(c.get("reply", ""))) is None
        ]
        h_rate = len(unanswered) / len(calls) if calls else 0.0
        durations = [float(r["duration_s"]) for r in rows]
        out_tokens = [
            int(c["output_tokens"])
            for c in calls
            if isinstance(c.get("output_tokens"), int) and c["output_tokens"] >= 0
        ]
        passed = (
            len(upright) == 37
            and len(rotated) == 99
            and not s3_turned
            and not wrong_angle
            and len(s3_undetermined) <= S3_UNDETERMINED_MAX
            and h_rate <= H_MAX_RATE
        )
        out[model] = {
            "images": {"upright_and_no_text": len(upright), "rotated": len(rotated)},
            "S3_turned": s3_turned,
            "S3_not_determined": s3_undetermined,
            "W_wrong_angle": wrong_angle,
            "rotated_right": right,
            "rotated_not_turned": len(rotated) - right - len(wrong_angle),
            "H_timeout_or_off_topic": {
                "calls": len(calls),
                "bad": len(unanswered),
                "rate": round(h_rate, 4),
            },
            "duration_s": {
                "median": statistics.median(durations) if durations else None,
                "p95": _percentile(durations, 0.95),
            },
            "output_tokens": {
                "median": statistics.median(out_tokens) if out_tokens else None,
                "max": max(out_tokens) if out_tokens else None,
            },
            "cost_yuan": round(sum(float(r.get("cost_yuan", 0.0)) for r in rows), 4),
            "E_pass": passed,
        }
    return out


# --------------------------------------------------------------------------- 命令行


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="b322_pathA.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("quote", "run"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--model", action="append", dest="models")
        cmd.add_argument("--stop-line", type=float, default=None)
    run = sub.choices["run"]
    run.add_argument("--quote-id", required=True)
    run.add_argument("--results", type=Path, required=True)
    run.add_argument("--env-file", type=Path, action="append", default=[])
    run.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    rep = sub.add_parser("report")
    rep.add_argument("--results", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "report":
        sys.stdout.write(json.dumps(report(b301_plan.read_records(args.results)), indent=2) + "\n")
        return 0
    models = tuple(args.models or DEFAULT_MODELS)
    try:
        q = quote(models=models, stop_line=args.stop_line)
    except b301_plan.PlanError as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 2
    if args.command == "quote":
        sys.stdout.write(json.dumps(q, indent=2, ensure_ascii=False) + "\n")
        return 0
    if args.quote_id != q["quote_id"]:
        sys.stderr.write(
            f"refused: --quote-id {args.quote_id} does not match this plan's quote "
            f"{q['quote_id']} (image set, models, prices or stop line changed) — re-quote\n"
        )
        return 2
    # 同 B-301:默认查 ``~/.kube/eval-llm.env``;通义的 key 在 ``~/.helix-eval.env``,要用就再加一个
    # ``--env-file``(进程内解析,不 echo、不进 argv)。
    env_files = list(args.env_file) or [DEFAULT_ENV_FILE]
    try:
        summary = asyncio.run(
            run_from_quote(
                q,
                models=models,
                results=args.results,
                env_files=env_files,
                state_dir=args.state_dir,
            )
        )
    except b301_run.RunRefusedError as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(summary.__dict__, indent=2) + "\n")
    return 0 if summary.stopped_reason is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
