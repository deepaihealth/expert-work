"""B-301 PR 2 体检的真跑部分:止损、凭据、看图调用的计量包装、按条驱动真的 ``AskImageTool``。

入口是 ``b301_health.py run``(先核 ``quote_id``)。这里:

* :class:`SpendGuard` —— 每次调用**之前**判止损线;没有单价的模型、没有用量的回答、单次输入超上限、
  厂商返回超时以外的错误 → 记下原因、此后一次调用都不再发(:class:`SpendStopError` 不是
  ``Exception``,工具里「问方向出错就按没问成」的兜底接不住它,这一条结果整条作废、续跑时重做)。
  超时 / 被取消的调用按单次上限计费(请求可能已经在厂商那边算钱)。
* :class:`MeteredCaller` —— 包在看图路由外面:计费、计时、把每次调用记进当前这一条的调用表。
* 工具按生产的装法建(:func:`real_callers` 用 ``build_llm_router``,快看关思考、深入看补默认档位、
  不配备用链、不挂重试中间件,所以一次逻辑调用就是一次厂商请求),直接构造
  ``AskImageTool(ask_orientation=True / False)``;上传图走内存解析器,裁图 / 候选副本写进本地
  临时目录充当工作区。
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import tempfile
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import b301_health as health
import b301_images as images_mod
from langchain_core.messages import AIMessage, BaseMessage

from expert_work.protocol.multimodal import ImageRef
from expert_work.runtime.middleware.llm_error_handling import LLMStreamStaleError
from orchestrator.multimodal import (
    DispatchingImageResolver,
    InMemoryImageResolver,
    NasWorkspaceImageResolver,
    ResolvedImage,
)
from orchestrator.tools.image_orientation import (
    ORIENTATION_QUESTION,
    ORIENTATION_TIMEOUT_S,
    TURNS,
    parse_orientation_reply,
)
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from orchestrator.tools.registry import ToolContext, ToolSpec
from orchestrator.tools.vision import AskImageTool, _messages

if TYPE_CHECKING:
    from expert_work.protocol import StructuredOutputSpec
    from orchestrator.llm.providers._streaming import LLMDelta

#: 每家厂商的凭据变量名(先查环境变量,再查 ``--env-file``,按顺序取第一个有值的)。
#: ``~/.helix-eval.env`` 的 ``HELIX_EVAL_LLM_API_KEY`` 是 DashScope(通义)的 key。
KEY_VARIABLES: Mapping[str, tuple[str, ...]] = {
    "doubao": ("EXPERT_WORK_EVAL_DOUBAO_API_KEY",),
    "qwen": ("EXPERT_WORK_EVAL_QWEN_API_KEY", "HELIX_EVAL_LLM_API_KEY"),
    "glm": ("EXPERT_WORK_EVAL_GLM_API_KEY", "GLM_API_KEY"),
    "deepseek": ("EXPERT_WORK_EVAL_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
}
_TENANT = uuid.UUID("b3010000-0000-4000-8000-000000000001")
_USER = uuid.UUID("b3010000-0000-4000-8000-000000000002")
_THREAD = uuid.UUID("b3010000-0000-4000-8000-000000000003")
_AGENT_KEY = "b301-health"
REPLY_CHARS = 300
EVAL_RATE_LIMIT_RPM = 600


class RunRefusedError(RuntimeError):
    """开跑前的前提不成立(凭据缺、结果文件属于别的计划……)。"""


class SpendStopError(BaseException):
    """止损:不再发任何调用。故意不是 ``Exception`` —— 工具里的兜底接不住,整条作废。"""


@dataclass
class SpendGuard:
    stop_line: float
    prices: Mapping[str, health.Price]
    spent: float = 0.0
    blocked: str | None = None

    def block(self, reason: str) -> None:
        if self.blocked is None:
            self.blocked = reason

    def check(self, model: str) -> None:
        """每次调用之前:已停 / 没单价 / 到线 → 停。"""
        if self.blocked is None and model not in self.prices:
            self.block(f"no validated price for {model}")
        if self.blocked is None and self.spent >= self.stop_line:
            self.block(f"stop line ¥{self.stop_line} reached (spent ¥{self.spent:.4f})")
        if self.blocked is not None:
            raise SpendStopError(self.blocked)

    def charge(self, model: str, usage: Mapping[str, Any] | None) -> tuple[float, int, int]:
        """按回答里的用量计费;用量缺 / 不是整数 → 按单次上限计费并停;输入超上限 → 照实计费并停。"""
        price = self.prices[model]
        tokens_in = usage.get("input_tokens") if usage else None
        tokens_out = usage.get("output_tokens") if usage else None
        if not isinstance(tokens_in, int) or not isinstance(tokens_out, int):
            cost = self.charge_cap(model)
            self.block(f"{model}: a response carried no usable token usage")
            return cost, -1, -1
        cost = (tokens_in * price.input + tokens_out * price.output) / 1e6
        self.spent += cost
        if tokens_in > health.MAX_INPUT_TOKENS_PER_CALL:
            limit = health.MAX_INPUT_TOKENS_PER_CALL
            self.block(f"{model}: one call used {tokens_in} input tokens > {limit}")
        return cost, tokens_in, tokens_out

    def charge_cap(self, model: str) -> float:
        cost = health.per_call_cap_yuan(self.prices[model])
        self.spent += cost
        return cost


#: 当前这一条(一次 ``ask_image`` / 一次 §8.1 提问)的调用表;工具内部 gather 出来的任务继承同一张表。
_CALLS: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "b301_calls", default=None
)


def _question_and_ref(messages: Sequence[BaseMessage]) -> tuple[str, str]:
    content = messages[-1].content
    text, ref = "", ""
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text", ""))
            elif isinstance(block, dict) and "ref" in block:
                ref = str(block["ref"])
    return text, ref


def _turn_of(ref: str) -> int:
    """问方向的副本放在 ``…/r<角度>/`` 里;0 度在裁图目录本身。"""
    for turn in TURNS:
        if f"/r{turn}/" in ref:
            return turn
    return 0


def _timeout_like(exc: BaseException) -> bool:
    seen: BaseException | None = exc
    for _ in range(6):
        if seen is None:
            return False
        if isinstance(seen, (TimeoutError, LLMStreamStaleError)):
            return True
        seen = getattr(seen, "last_exc", None) or seen.__cause__
    return False


LLMCallable = Callable[..., Awaitable[AIMessage]]


@dataclass
class MeteredCaller:
    """看图路由外面的一层:先过止损、计时、计费、记调用表。"""

    inner: LLMCallable
    model: str
    guard: SpendGuard
    clock: Callable[[], float] = time.monotonic

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
        on_delta: Callable[[LLMDelta], Awaitable[None]] | None = None,
    ) -> AIMessage:
        """``LLMCaller`` 的形状;``ask_image`` 只传 ``messages`` / ``tools``,另两个不转发。"""
        del output_schema, on_delta
        question, ref = _question_and_ref(messages)
        kind = "orientation" if question == ORIENTATION_QUESTION else "read"
        call: dict[str, Any] = {
            "kind": kind,
            "turn": _turn_of(ref) if kind == "orientation" else None,
        }
        calls = _CALLS.get()
        self.guard.check(self.model)
        started = self.clock()
        try:
            response = await self.inner(messages=messages, tools=tools)
        except asyncio.CancelledError:
            self._failed(call, calls, "timeout", started)
            raise
        except SpendStopError:
            raise
        except Exception as exc:
            outcome = "timeout" if _timeout_like(exc) else "error"
            self._failed(call, calls, outcome, started, error=type(exc).__name__)
            if outcome == "error":
                self.guard.block(
                    f"{self.model}: provider error {type(exc).__name__} (unknown status)"
                )
            raise
        latency = self.clock() - started
        usage = dict(response.usage_metadata) if response.usage_metadata else None
        cost, tokens_in, tokens_out = self.guard.charge(self.model, usage)
        text = (
            response.content if isinstance(response.content, str) else json.dumps(response.content)
        )
        answer = parse_orientation_reply(text) if kind == "orientation" else None
        call.update(
            outcome="ok",
            latency_s=round(latency, 3),
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cost_yuan=round(cost, 6),
            reply=text[:REPLY_CHARS],
            answer=(answer or "off_topic") if kind == "orientation" else None,
        )
        if calls is not None:
            calls.append(call)
        if self.guard.blocked is not None:
            raise SpendStopError(self.guard.blocked)
        return response

    def _failed(
        self,
        call: dict[str, Any],
        calls: list[dict[str, Any]] | None,
        outcome: str,
        started: float,
        *,
        error: str | None = None,
    ) -> None:
        cost = self.guard.charge_cap(self.model)
        call.update(
            outcome=outcome,
            latency_s=round(self.clock() - started, 3),
            cost_yuan=round(cost, 6),
            error=error,
            answer=outcome if call["kind"] == "orientation" else None,
        )
        if calls is not None:
            calls.append(call)


@dataclass(frozen=True)
class Callers:
    """一个看图模型的三条路由(与生产装配同形):深入看、快看(关思考)、配置原样。"""

    vl: LLMCallable
    quick: LLMCallable | None = None
    plain: LLMCallable | None = None


CallerFactory = Callable[[str, str, DispatchingImageResolver], Awaitable[Callers]]


@dataclass
class _KeyStore:
    """进程内的凭据(``SecretStore`` 形状);值不打印、不落盘。"""

    keys: Mapping[str, str] = field(repr=False)

    async def get(self, name: str, *, version: str | None = None) -> str:
        del version
        return self.keys[name.rsplit("/", 1)[-1]]

    async def put(self, name: str, value: str) -> None:
        raise NotImplementedError

    async def list_versions(self, name: str) -> list[str]:
        return ["1"]

    async def delete(self, name: str) -> None:
        raise NotImplementedError


def load_keys(providers: Sequence[str], env_file: Path | None) -> dict[str, str]:
    """每家一个 key:先环境变量,再 ``env_file``(``KEY=VALUE`` 行,进程内解析)。缺的报变量名。"""
    from_file: dict[str, str] = {}
    if env_file is not None and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip().removeprefix("export ").strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                name, value = stripped.split("=", 1)
                from_file[name.strip()] = value.strip().strip("'\"")
    keys: dict[str, str] = {}
    missing: list[str] = []
    for provider in providers:
        names = KEY_VARIABLES[provider]
        found: str | None = next(
            (
                os.environ.get(n) or from_file.get(n)
                for n in names
                if os.environ.get(n) or from_file.get(n)
            ),
            None,
        )
        if found:
            keys[provider] = found
        else:
            missing.append(f"{provider}: one of {', '.join(names)}")
    if missing:
        raise RunRefusedError("no credential for " + "; ".join(missing))
    return keys


def real_callers_factory(keys: Mapping[str, str]) -> CallerFactory:
    """生产同形的三条路由(``agent_factory`` 的看图装配),单个模型、无备用链、无重试中间件。"""

    async def build(model: str, provider: str, resolver: DispatchingImageResolver) -> Callers:
        from expert_work.protocol import ModelSpec
        from orchestrator.agent_factory import (
            VL_FIRST_TOKEN_TIMEOUT_S,
            _deep_vision_model,
            _thinking_off,
            build_llm_router,
        )

        store = _KeyStore(keys)

        async def key_refs(name: str) -> list[str]:
            return [f"secret://b301-health/{name}"]

        async def router(spec: ModelSpec) -> LLMCallable:
            return await build_llm_router(
                spec,
                secret_store=store,
                image_resolver=resolver,
                first_token_timeout_s=float(VL_FIRST_TOKEN_TIMEOUT_S),
                provider_timeout_s=float(VL_FIRST_TOKEN_TIMEOUT_S),
                provider_key_resolver=key_refs,
                ignore_api_key_ref=True,
            )

        # 客户端令牌桶放宽到 600 次 / 分:真正的限流是「每家 2 个 ask_image」,桶不该在量到的耗时
        # 里加排队(默认 60 次 / 分在一家 6 路并行时会排队,把 L6 的耗时和超时量歪)。
        spec = ModelSpec.model_validate(
            {
                "provider": provider,
                "name": model,
                "max_tokens": health.MAX_OUTPUT_TOKENS,
                "rate_limit_rpm": EVAL_RATE_LIMIT_RPM,
            }
        )
        deep, quick = _deep_vision_model(spec), _thinking_off(spec)
        return Callers(
            vl=await router(deep),
            quick=await router(quick) if quick != spec else None,
            plain=await router(spec) if deep != spec else None,
        )

    return build


@dataclass
class RunSummary:
    done: int
    skipped: int
    discarded: int
    spent_yuan: float
    stopped_reason: str | None


def _ref(image: images_mod.SetImage) -> str:
    image_id = uuid.uuid5(uuid.NAMESPACE_URL, f"b301-health/{image.image_id}")
    return ImageRef(tenant_id=_TENANT, thread_id=_THREAD, image_id=image_id, ext=".jpg").to_uri()


def _ctx() -> ToolContext:
    return ToolContext(
        tenant_id=_TENANT,
        run_id=uuid.uuid4(),
        user_id=_USER,
        thread_id=_THREAD,
        agent_key=_AGENT_KEY,
        workspace_layout="conversation",
    )


async def run_items(
    items: Sequence[health.Item],
    *,
    image_set: Sequence[images_mod.SetImage],
    guard: SpendGuard,
    factory: CallerFactory,
    results: Path,
    stamp: Mapping[str, str],
    workdir: Path,
    concurrency: int = health.CONCURRENCY_PER_PROVIDER,
) -> RunSummary:
    """按条跑 ``items``(已在 ``results`` 里的跳过),每家 ``concurrency`` 路。每条结束就追加一行。"""
    by_id = {image.image_id: image for image in image_set}
    refs = {image.image_id: _ref(image) for image in image_set}
    (workdir / str(_TENANT) / str(_USER)).mkdir(parents=True, exist_ok=True)
    resolver = DispatchingImageResolver(
        uploads=InMemoryImageResolver(
            images={refs[i.image_id]: ResolvedImage(i.media_type, i.data) for i in image_set}
        ),
        workspace=NasWorkspaceImageResolver(root=workdir),
    )
    store = NasWorkspaceStore(root=str(workdir))
    done = {f"{r['model']}|{r['mode']}|{r['image_id']}" for r in health.read_records(results)}
    todo = [item for item in items if item.key not in done]
    tools: dict[tuple[str, str], AskImageTool] = {}
    probes: dict[str, MeteredCaller] = {}
    for model in dict.fromkeys(item.model for item in todo):
        provider = health.PROVIDERS[model]
        callers = await factory(model, provider, resolver)

        def wrap(inner: LLMCallable | None, model: str = model) -> MeteredCaller | None:
            return None if inner is None else MeteredCaller(inner, model, guard)

        vl = MeteredCaller(callers.vl, model, guard)
        quick, plain = wrap(callers.quick), wrap(callers.plain)
        probes[model] = quick or vl
        for mode, on in (("on", True), ("control", False)):
            tools[(model, mode)] = AskImageTool(
                vl_caller=vl,
                quick_vl_caller=quick,
                plain_vl_caller=plain,
                image_resolver=resolver,
                workspace_store=store,
                ask_orientation=on,
                vl_model_name=model,
            )
    discarded = 0

    async def one(item: health.Item) -> dict[str, Any] | None:
        image = by_id[item.image_id]
        calls: list[dict[str, Any]] = []
        token = _CALLS.set(calls)
        record: dict[str, Any] = {
            **stamp,
            "model": item.model,
            "mode": item.mode,
            "image_id": image.image_id,
            "image_class": image.image_class,
            "rotation": image.rotation,
            "key": image.key,
            "status": "ok",
        }
        try:
            if item.mode == "probe":
                try:
                    async with asyncio.timeout(ORIENTATION_TIMEOUT_S):
                        response = await probes[item.model](
                            messages=_messages(refs[image.image_id], ORIENTATION_QUESTION), tools=[]
                        )
                    word = parse_orientation_reply(str(response.content))
                    record["probe_answer"] = word or "off_topic"
                except TimeoutError:
                    record["probe_answer"] = "timeout"
            else:
                tool = tools[(item.model, item.mode)]
                try:
                    result = await tool.call(
                        {"image_ref": refs[image.image_id], "question": image.question}, ctx=_ctx()
                    )
                    record["content"] = result.content[:2000]
                    record["answer"] = health.strip_notes(result.content)[:2000]
                    record["orientation"] = result.meta.get("orientation")
                except Exception as exc:
                    record.update(status="tool_error", error=type(exc).__name__, answer="")
        except SpendStopError:
            return None
        finally:
            _CALLS.reset(token)
        if guard.blocked is not None:
            return None
        record["calls"] = calls
        record["cost_yuan"] = round(sum(float(c.get("cost_yuan", 0.0)) for c in calls), 6)
        return record

    queues: dict[str, deque[health.Item]] = {}
    for item in todo:
        queues.setdefault(item.provider, deque()).append(item)
    written = 0

    async def worker(queue: deque[health.Item]) -> None:
        nonlocal written, discarded
        while queue and guard.blocked is None:
            record = await one(queue.popleft())
            if record is None:
                discarded += 1
                continue
            with results.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1

    await asyncio.gather(*(worker(q) for q in queues.values() for _ in range(concurrency)))
    return RunSummary(
        done=written,
        skipped=len(items) - len(todo),
        discarded=discarded,
        spent_yuan=round(guard.spent, 4),
        stopped_reason=guard.blocked,
    )


async def run_from_quote(
    q: Mapping[str, Any],
    *,
    models: Sequence[str],
    probe_models: Sequence[str],
    results: Path,
    env_file: Path | None,
    factory: CallerFactory | None = None,
    image_set: Sequence[images_mod.SetImage] | None = None,
) -> RunSummary:
    """``run`` 子命令:核结果文件属于同一个计划、取凭据、按已花的钱起算止损,然后跑。"""
    image_set = images_mod.build_set() if image_set is None else image_set
    results.parent.mkdir(parents=True, exist_ok=True)
    existing = health.read_records(results)
    other = {r.get("plan_id") for r in existing} - {q["plan_id"]}
    if other:
        raise RunRefusedError(f"{results} holds results of another plan {sorted(map(str, other))}")
    prices = {
        model: health.Price(
            entry["price_yuan_per_mtok"]["input"],
            entry["price_yuan_per_mtok"]["output"],
            0.0,
            entry["price_source"],
        )
        for model, entry in q["models"].items()
    }
    providers = sorted({health.PROVIDERS[m] for m in [*models, *probe_models]})
    if factory is None:
        factory = real_callers_factory(load_keys(providers, env_file))
    guard = SpendGuard(stop_line=float(q["stop_line_yuan"]), prices=prices)
    guard.spent = sum(float(r.get("cost_yuan", 0.0)) for r in existing)
    items = health.plan_items(models, probe_models, image_set)
    stamp = {"plan_id": str(q["plan_id"]), "quote_id": str(q["quote_id"])}
    with tempfile.TemporaryDirectory(prefix="b301-health-") as workdir:
        return await run_items(
            items,
            image_set=image_set,
            guard=guard,
            factory=factory,
            results=results,
            stamp=stamp,
            workdir=Path(workdir),
        )
