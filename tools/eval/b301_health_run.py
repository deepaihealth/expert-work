"""B-301 PR 2 体检的真跑部分:凭据、看图调用的计量包装、按条驱动真的 ``AskImageTool``。

入口是 ``b301_health.py run``(先核 ``quote_id``)。这里:

* 止损与账本在 :mod:`b301_spend`:每次调用**之前**判止损线、按单次上限预留并落账(fsync),调用后
  结算;没有单价的模型、用量不可信的回答、单次输入超上限、厂商返回超时以外的错误 → 停,不再发调用
  (:class:`~b301_spend.SpendStopError` 不是 ``Exception``,工具里「问方向出错就按没问成」的兜底
  接不住它,这一条作废、续跑时重做;钱已经在账上)。超时 / 被取消的调用按单次上限结算。
* 续跑时止损线从**账本合计**起算(作废的、出错的、崩溃时在飞的调用都算);同一个结果文件同一时间
  只许一个 run(``flock``)。
* :class:`MeteredCaller` —— 包在看图路由外面:预留 / 结算、计时、把每次调用记进当前这一条的调用表。
* 工具按生产的装法建(:func:`real_callers_factory` 用 ``build_llm_router``,快看关思考、深入看补
  默认档位、不配备用链、不挂重试中间件,所以一次逻辑调用就是一次厂商请求),直接构造
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
import b301_report as report_mod
import b301_spend as spend
from b301_spend import SpendGuard, SpendLedger, SpendStopError
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

__all__ = ["SpendGuard", "SpendLedger", "SpendStopError"]

#: 每家厂商的凭据变量名:先查环境变量,再按顺序查各个 ``--env-file``。默认凭据文件
#: ``~/.kube/eval-llm.env`` 里有 ``ARK_API_KEY``(豆包)、``GLM_API_KEY``、``DEEPSEEK_API_KEY``;
#: 通义的 ``HELIX_EVAL_LLM_API_KEY`` 在 ``~/.helix-eval.env``(要用就再加一个 ``--env-file``)。
KEY_VARIABLES: Mapping[str, tuple[str, ...]] = {
    "doubao": ("ARK_API_KEY",),
    "qwen": ("HELIX_EVAL_LLM_API_KEY",),
    "glm": ("GLM_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
}
DEFAULT_ENV_FILE = Path.home() / ".kube" / "eval-llm.env"
_TENANT = uuid.UUID("b3010000-0000-4000-8000-000000000001")
_USER = uuid.UUID("b3010000-0000-4000-8000-000000000002")
_THREAD = uuid.UUID("b3010000-0000-4000-8000-000000000003")
_AGENT_KEY = "b301-health"
REPLY_CHARS = 300
EVAL_RATE_LIMIT_RPM = 600


class RunRefusedError(RuntimeError):
    """开跑前的前提不成立(凭据缺、结果文件属于别的计划、另一个 run 正在用……)。"""


#: 当前这一条(一次 ``ask_image`` / 一次 §8.1 提问)的调用表与条目编号;工具内部 gather 出来的
#: 任务继承同一份。
_CALLS: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "b301_calls", default=None
)
_ITEM: contextvars.ContextVar[str] = contextvars.ContextVar("b301_item", default="")


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
    """看图路由外面的一层:预留(过止损线、落账)→ 发 → 结算;计时;记调用表。"""

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
        calls, item = _CALLS.get(), _ITEM.get()
        call_id = self.guard.reserve(self.model, item=item, phase=kind)
        started = self.clock()
        try:
            response = await self.inner(messages=messages, tools=tools)
        except asyncio.CancelledError:
            self._failed(call, calls, call_id, item, "timeout", started)
            raise
        except Exception as exc:
            outcome = "timeout" if _timeout_like(exc) else "error"
            self._failed(call, calls, call_id, item, outcome, started, error=type(exc).__name__)
            if outcome == "error":
                self.guard.block(
                    f"{self.model}: provider error {type(exc).__name__} (unknown status)"
                )
            raise
        latency = self.clock() - started
        usage = dict(response.usage_metadata) if response.usage_metadata else None
        cost, tokens_in, tokens_out = self.guard.settle_usage(
            call_id, self.model, usage, item=item, phase=kind
        )
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
        call_id: str,
        item: str,
        outcome: str,
        started: float,
        *,
        error: str | None = None,
    ) -> None:
        reason = "cap_timeout" if outcome == "timeout" else "cap_error"
        cost = self.guard.settle_cap(call_id, self.model, reason, item=item, phase=call["kind"])
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


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip().removeprefix("export ").strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            name, value = stripped.split("=", 1)
            values[name.strip()] = value.strip().strip("'\"")
    return values


def load_keys(providers: Sequence[str], env_files: Sequence[Path]) -> dict[str, str]:
    """每家一个 key:先环境变量,再按顺序查 ``env_files``(进程内解析)。缺的只报变量名。"""
    from_files: dict[str, str] = {}
    for path in reversed(env_files):  # 排在前面的文件优先
        from_files.update(_parse_env_file(path))
    keys: dict[str, str] = {}
    missing: list[str] = []
    for provider in providers:
        names = KEY_VARIABLES[provider]
        found = next((v for n in names if (v := os.environ.get(n) or from_files.get(n))), None)
        if found:
            keys[provider] = found
        else:
            missing.append(f"{provider}: {' or '.join(names)}")
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
    spent_at_start_yuan: float
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


@dataclass
class _Runner:
    """一次 run 的工具 / 路由与按条执行。"""

    guard: SpendGuard
    refs: Mapping[str, str]
    by_id: Mapping[str, images_mod.SetImage]
    stamp: Mapping[str, str]
    tools: dict[tuple[str, str], AskImageTool] = field(default_factory=dict)
    probes: dict[str, MeteredCaller] = field(default_factory=dict)

    async def add_model(
        self,
        model: str,
        factory: CallerFactory,
        resolver: DispatchingImageResolver,
        store: NasWorkspaceStore,
    ) -> None:
        callers = await factory(model, health.PROVIDERS[model], resolver)

        def wrap(inner: LLMCallable | None) -> MeteredCaller | None:
            return None if inner is None else MeteredCaller(inner, model, self.guard)

        vl = MeteredCaller(callers.vl, model, self.guard)
        quick, plain = wrap(callers.quick), wrap(callers.plain)
        self.probes[model] = quick or vl
        for mode, on in (("on", True), ("control", False)):
            self.tools[(model, mode)] = AskImageTool(
                vl_caller=vl,
                quick_vl_caller=quick,
                plain_vl_caller=plain,
                image_resolver=resolver,
                workspace_store=store,
                ask_orientation=on,
                vl_model_name=model,
            )

    async def one(self, item: health.Item) -> dict[str, Any] | None:
        """跑一条;作废(止损、厂商出错)时返回 ``None`` —— 钱已经在账上,续跑时重做。"""
        image = self.by_id[item.image_id]
        calls: list[dict[str, Any]] = []
        tokens = (_CALLS.set(calls), _ITEM.set(item.key))
        record: dict[str, Any] = {
            **self.stamp,
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
                if not await self._probe(item, image, record):
                    return None
            else:
                await self._ask(item, image, record)
        except SpendStopError:
            return None
        finally:
            _CALLS.reset(tokens[0])
            _ITEM.reset(tokens[1])
        if self.guard.blocked is not None:
            return None
        record["calls"] = calls
        record["cost_yuan"] = round(sum(float(c.get("cost_yuan", 0.0)) for c in calls), 6)
        return record

    async def _probe(
        self, item: health.Item, image: images_mod.SetImage, record: dict[str, Any]
    ) -> bool:
        """§8.1 只问方向。超时记成答案;厂商出错(已在计量层停下)这一条作废。"""
        messages = _messages(self.refs[image.image_id], ORIENTATION_QUESTION)
        try:
            async with asyncio.timeout(ORIENTATION_TIMEOUT_S):
                response = await self.probes[item.model](messages=messages, tools=[])
        except Exception as exc:
            if not _timeout_like(exc):
                return False
            record["probe_answer"] = "timeout"
            return True
        word = parse_orientation_reply(str(response.content))
        record["probe_answer"] = word or "off_topic"
        return True

    async def _ask(
        self, item: health.Item, image: images_mod.SetImage, record: dict[str, Any]
    ) -> None:
        tool = self.tools[(item.model, item.mode)]
        args = {"image_ref": self.refs[image.image_id], "question": image.question}
        try:
            result = await tool.call(args, ctx=_ctx())
        except Exception as exc:
            record.update(status="tool_error", error=type(exc).__name__, answer="")
            return
        record["content"] = result.content[:2000]
        record["answer"] = report_mod.strip_notes(result.content)[:2000]
        record["orientation"] = result.meta.get("orientation")


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
    spent_at_start = guard.spent
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
    runner = _Runner(guard, refs, {image.image_id: image for image in image_set}, stamp)
    for model in dict.fromkeys(item.model for item in todo):
        await runner.add_model(model, factory, resolver, store)
    queues: dict[str, deque[health.Item]] = {}
    for item in todo:
        queues.setdefault(item.provider, deque()).append(item)
    counts = {"written": 0, "discarded": 0}

    async def worker(queue: deque[health.Item]) -> None:
        while queue and guard.blocked is None:
            record = await runner.one(queue.popleft())
            if record is None:
                counts["discarded"] += 1
                continue
            with results.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            counts["written"] += 1

    await asyncio.gather(*(worker(q) for q in queues.values() for _ in range(concurrency)))
    return RunSummary(
        done=counts["written"],
        skipped=len(items) - len(todo),
        discarded=counts["discarded"],
        spent_at_start_yuan=round(spent_at_start, 6),
        spent_yuan=round(guard.spent, 6),
        stopped_reason=guard.blocked,
    )


def _prices(q: Mapping[str, Any]) -> dict[str, health.Price]:
    return {
        model: health.Price(
            entry["price_yuan_per_mtok"]["input"],
            entry["price_yuan_per_mtok"]["output"],
            0.0,
            entry["price_source"],
        )
        for model, entry in q["models"].items()
    }


def _same_plan(rows: Sequence[Mapping[str, Any]], plan_id: str, path: Path) -> None:
    other = {str(r.get("plan_id")) for r in rows} - {plan_id}
    if other:
        raise RunRefusedError(f"{path} holds rows of another plan {sorted(other)}")


async def run_from_quote(
    q: Mapping[str, Any],
    *,
    models: Sequence[str],
    probe_models: Sequence[str],
    results: Path,
    env_files: Sequence[Path],
    factory: CallerFactory | None = None,
    image_set: Sequence[images_mod.SetImage] | None = None,
    items: Sequence[health.Item] | None = None,
) -> RunSummary:
    """``run`` 子命令:独占结果文件、核计划、取凭据、止损线从账本合计起算,然后跑。
    ``items`` 只给测试用(默认按计划跑全部)。"""
    image_set = images_mod.build_set() if image_set is None else image_set
    plan_id = str(q["plan_id"])
    try:
        with spend.exclusive(results):
            existing = health.read_records(results)
            ledger_file = spend.ledger_path(results)
            ledger_rows = spend.read_ledger(ledger_file)
            _same_plan(existing, plan_id, results)
            _same_plan(ledger_rows, plan_id, ledger_file)
            providers = sorted({health.PROVIDERS[m] for m in [*models, *probe_models]})
            if factory is None:
                factory = real_callers_factory(load_keys(providers, env_files))
            guard = SpendGuard(
                stop_line=float(q["stop_line_yuan"]),
                prices=_prices(q),
                ledger=SpendLedger(ledger_file, plan_id),
                settled=spend.ledger_total(ledger_rows),
            )
            planned = health.plan_items(models, probe_models, image_set) if items is None else items
            stamp = {"plan_id": plan_id, "quote_id": str(q["quote_id"])}
            with tempfile.TemporaryDirectory(prefix="b301-health-") as workdir:
                return await run_items(
                    planned,
                    image_set=image_set,
                    guard=guard,
                    factory=factory,
                    results=results,
                    stamp=stamp,
                    workdir=Path(workdir),
                )
    except spend.LockedError as exc:
        raise RunRefusedError(str(exc)) from exc
