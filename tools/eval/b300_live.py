"""B-300 评测会碰网络 / 调模型的那一段:A 组提炼(本机进程内)、B / C 组对话(对外平面)、判官。

**钱的规矩**(``b300_spend.Guard``):每一次模型调用、每一次运行发出前先按上限预留并落账,结束后按
用量结算。说不清花了多少的按上限记并止损。这里的每个入口都要求调用方传入 ``Guard``,没有不记账的
路径。

* **A 组 / §8.5 对比**(:func:`run_extraction`):在本机进程里直接调生产的写回入口
  ``orchestrator.graph_builder.memory.flush_messages_with_outcome``(提炼 + 去重合并 + 写入),
  用户消息由生产的唯一构造点 ``control_plane.api.runs._build_human_message`` 拼出来(PR 1 的白名单
  标记由它写,所以评测与线上一致);记忆库是进程内的 ``InMemoryMemoryStore``;模型走各家 OpenAI
  兼容接口(:class:`GuardedCaller`),凭据只从环境变量读:``EXPERT_WORK_EVAL_<PROVIDER>_BASE_URL`` /
  ``EXPERT_WORK_EVAL_<PROVIDER>_API_KEY``。跑基线就在基线提交的检出目录(另开一个工作区)里跑
  同一份脚本。
* **B / C 组**(:func:`run_conversation`):对外平面 ``/v1/agents/{code}/...``(凭据
  ``EXPERT_WORK_EVAL_API_KEY``),目标是当时测试环境上线的那个版本;版本号由调用方记进结果。
* **判官**(:class:`Judge`):B-206 尺子,``deepseek-v4-pro`` 三票取多数,凭据
  ``EXPERT_WORK_EVAL_JUDGE_BASE_URL`` / ``EXPERT_WORK_EVAL_JUDGE_API_KEY``(同 ``kb_agent_eval``)。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import b300_scenarios as sc  # noqa: E402
from b300_spend import Guard, SpendStopError, call_cap_yuan, usage_cost  # noqa: E402
from b301_plan import Price  # noqa: E402
from b302_live import TurnCollector  # noqa: E402
from behavior_client import SseParser, content_type_for  # noqa: E402

#: 单次调用的输入上限(token)—— 只用来算预留;我们自己拼的提示词远小于它,超了结算时会止损。
MAX_CALL_INPUT_TOKENS = 40_000
EXTRACT_MAX_TOKENS = 4_000
JUDGE_MAX_TOKENS = 2_000
#: 评测智能体每次运行的 token 预算(``policies.token_budget``);一次运行的预留 = 它 x 最贵的输出价。
RUN_TOKEN_BUDGET = 150_000
RUN_MAX_ITERATIONS = 8
RUN_TIMEOUT_S = 900.0
CONCURRENCY_PER_VENDOR = 2
#: 后台写回不在运行的用量里(记为平台开销,设计 §4.4);对外看不到,按上限结算。
WRITEBACK_CALLS_CAP = 3
EVAL_AGENT_PREFIX = "eval-b300-"
#: A 组进程内记忆库的租户 / 用户(合成)。
A_TENANT = UUID("00000000-0000-4000-8000-0000000b3000")
A_AGENT = "eval-b300-extraction"
IMAGE_REF = "expert_work://uploads/b300-synthetic.png"

#: 生产两段提炼指令里各自独有的一句(设计 §4.2 最终英文原文),用来认出这是第几次提炼。
PASS1_MARK = "The other side of the conversation is not shown."
PASS2_MARK = "Every line starts with a tag:"
OLD_EXTRACT_MARK = "You are a memory extraction module. From the conversation, extract"
RECONCILE_MARK = "You reconcile newly extracted memories"


#: 睡眠的间接层(测试换掉,不碰全局的 ``asyncio.sleep``;同 ``b302_live``)。
_sleep = asyncio.sleep


def classify_call(system_text: str) -> str:
    if PASS1_MARK in system_text:
        return "pass1"
    if PASS2_MARK in system_text:
        return "pass2"
    if OLD_EXTRACT_MARK in system_text:
        return "extract_old"
    if RECONCILE_MARK in system_text:
        return "reconcile"
    return "other"


def vendor_env(provider: str) -> tuple[str, str]:
    """``(base_url, api_key)`` 只从环境变量读;缺了就拒,不在命令行收凭据。"""
    prefix = f"EXPERT_WORK_EVAL_{provider.upper()}_"
    base_url, key = os.environ.get(prefix + "BASE_URL"), os.environ.get(prefix + "API_KEY")
    if not base_url or not key:
        raise RuntimeError(f"set {prefix}BASE_URL and {prefix}API_KEY for {provider}")
    return base_url, key


def openai_usage_cost(usage: Mapping[str, Any], price: Price) -> float:
    """OpenAI 兼容回包的 ``usage`` → 元(缓存读按缓存价;DeepSeek 闲时半价不算,偏贵)。"""
    prompt = int(usage.get("prompt_tokens") or 0)
    cached = int(
        (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        or usage.get("prompt_cache_hit_tokens")
        or 0
    )
    out = int(usage.get("completion_tokens") or 0)
    return ((prompt - cached) * price.input + cached * price.cache_read + out * price.output) / 1e6


@dataclass
class ChatTransport:
    """一个 OpenAI 兼容 ``/chat/completions`` 端点;每次调用先预留后发送。"""

    model: str
    base_url: str
    api_key: str
    guard: Guard
    http: httpx.AsyncClient
    max_tokens: int
    #: 厂商(glm / deepseek / qwen / doubao):决定关思考与输出上限的字段(审查 H2)。
    provider: str = ""

    async def complete(
        self, messages: list[dict[str, str]], *, item: str, phase: str, json_mode: bool = False
    ) -> tuple[str, Mapping[str, Any]]:
        price = self.guard.prices[self.model]
        cap = call_cap_yuan(price, MAX_CALL_INPUT_TOKENS, self.max_tokens)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.0,
            **request_fields(self.provider, self.model, max_tokens=self.max_tokens),
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        for attempt in range(4):
            call_id = self.guard.reserve(item=item, model=self.model, cap=cap, phase=phase)
            try:
                resp = await self.http.post(
                    f"{self.base_url.rstrip('/')}/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=180.0,
                )
            except BaseException:
                self.guard.settle_cap(
                    call_id, item=item, model=self.model, phase=phase, reason="cap_error"
                )
                self.guard.block(f"{item}: transport error on {self.model}")
                raise
            if resp.status_code == 429 and attempt < 3:  # 限流:没花钱,等一下再试
                self.guard.settle(
                    call_id,
                    item=item,
                    model=self.model,
                    phase=phase,
                    cost=0.0,
                    reason="rate_limited",
                )
                await _sleep(5.0 * (attempt + 1))
                continue
            if resp.status_code != 200:
                self.guard.settle_cap(
                    call_id, item=item, model=self.model, phase=phase, reason="cap_error"
                )
                self.guard.block(f"{item}: {self.model} returned HTTP {resp.status_code}")
                raise SpendStopError(self.guard.blocked)
            data = resp.json()
            usage = data.get("usage")
            if not isinstance(usage, dict) or not usage.get("prompt_tokens"):
                self.guard.settle_cap(
                    call_id, item=item, model=self.model, phase=phase, reason="cap_no_usage"
                )
                self.guard.block(f"{item}: {self.model} reply carried no usage")
                raise SpendStopError(self.guard.blocked)
            self.guard.settle(
                call_id,
                item=item,
                model=self.model,
                phase=phase,
                cost=openai_usage_cost(usage, price),
            )
            choices = data.get("choices") or [{}]
            content = (choices[0].get("message") or {}).get("content")
            return (content if isinstance(content, str) else ""), usage
        raise SpendStopError(f"{item}: still rate limited after retries")


def request_fields(provider: str, model: str, *, max_tokens: int) -> dict[str, Any]:
    """与产品同一套「关思考 + 输出上限」字段(``agent_factory.thinking_off_request_fields``:
    记忆调用关思考,B-168;qwen / 豆包的上限字段是 ``max_completion_tokens``)。目录里没有思考
    开关的型号(如 glm-5.3-flash 始终思考)按目录给的最低档。不认识的厂商不发。"""
    if not provider:
        raise ValueError(f"no provider for {model}: the request fields depend on it")
    from orchestrator.agent_factory import thinking_off_request_fields

    fields = dict(thinking_off_request_fields(provider, model, max_tokens=max_tokens))
    if "max_tokens" not in fields and "max_completion_tokens" not in fields:
        fields["max_tokens"] = max_tokens
    return fields


# --------------------------------------------------------------------------- A 组提炼


@dataclass
class GuardedCaller:
    """生产 ``LLMCaller`` 的形状;每次调用记下是第几次提炼 / 去重合并、原样的回答。"""

    transport: ChatTransport
    item: str
    phase: str
    calls: list[dict[str, Any]] = field(default_factory=list)
    #: 失败的调用(审查 M1):产品的写回入口会把提炼 / 去重合并里的异常吞掉、照常返回,这里记下,
    #: 由 :func:`run_extraction` 把这一条判为出错。
    failures: list[str] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[Any], tools: Sequence[Any] = (), **_: Any
    ) -> Any:
        from langchain_core.messages import AIMessage
        from longmem.openai_client import render_chat_payload

        del tools
        payload = render_chat_payload(
            messages, model=self.transport.model, max_tokens=self.transport.max_tokens
        )
        system = "\n".join(m["content"] for m in payload["messages"] if m["role"] == "system")
        kind = classify_call(system)
        try:
            text, usage = await self.transport.complete(
                payload["messages"], item=f"{self.item}|{kind}", phase=self.phase
            )
        except BaseException as exc:
            self.failures.append(f"{kind}: {exc!r}")
            raise
        self.calls.append(
            {
                "kind": kind,
                "reply": text,
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
            }
        )
        return AIMessage(
            content=text,
            usage_metadata={
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("prompt_tokens") or 0)
                + int(usage.get("completion_tokens") or 0),
            },
        )


def turn_messages(turn: sc.Turn, index: int) -> list[Any]:
    """一轮 → 生产形状的消息:用户消息走 ``_build_human_message``(外部内容走它的
    ``untrusted_content``,图走 ``image_refs``),工具调用 / 结果、最终回答照常。"""
    from langchain_core.messages import AIMessage, ToolMessage

    from control_plane.api.runs import _build_human_message

    human = _build_human_message(
        input_text=turn.user,
        image_refs=[IMAGE_REF] * turn.images,
        supports_vision=True,
        untrusted_content=list(turn.external) or None,
        spotlight_nonce="b300eval" if turn.external else None,
        document_names=[name for name, _text in turn.documents] or None,
    )
    out: list[Any] = [human]
    for n, (tool, result) in enumerate(turn.tool_calls):
        call_id = f"call_b300_{index}_{n}"
        out.append(AIMessage(content="", tool_calls=[{"name": tool, "args": {}, "id": call_id}]))
        out.append(ToolMessage(content=result, tool_call_id=call_id, name=tool))
    if turn.reply:
        out.append(AIMessage(content=turn.reply))
    return out


def _live(item: Any) -> bool:
    return item.deleted_at is None and item.invalid_at is None and item.superseded_by is None


async def _seed_store(
    store: Any, embedder: Any, scenario: sc.Scenario, user_id: UUID
) -> dict[str, str]:
    from expert_work.protocol import MemoryItem

    if not scenario.seeds:
        return {}
    vectors = await embedder.embed([s.content for s in scenario.seeds], tenant_id=A_TENANT)
    ids: dict[str, str] = {}
    items = []
    for seed, vec in zip(scenario.seeds, vectors, strict=True):
        fields: dict[str, Any] = {
            "id": uuid4(),
            "tenant_id": A_TENANT,
            "user_id": user_id,
            "kind": seed.kind,
            "content": seed.content,
            "embedding": tuple(float(v) for v in vec),
            "confidence": seed.confidence,
            "importance": 0.7,
        }
        if seed.origin is not None and "origin" in MemoryItem.model_fields:
            fields["origin"] = seed.origin
        item = MemoryItem(**fields)
        items.append(item)
        ids[seed.key] = str(item.id)
    await store.write(items)
    return ids


async def run_extraction(
    scenario: sc.Scenario,
    *,
    transport: ChatTransport,
    embedder: Any,
    item: str,
    phase: str,
) -> dict[str, Any]:
    """一个 A 组(或 §8.5 对比)场景的一次:按 ``writebacks`` 在对应轮之后各写回一次。"""
    from expert_work.persistence import InMemoryMemoryStore
    from expert_work.runtime.cancellation import CancellationToken
    from orchestrator.graph_builder.memory import flush_messages_with_outcome

    store = InMemoryMemoryStore()
    user_id = uuid4()
    seeded = await _seed_store(store, embedder, scenario, user_id)
    messages: list[Any] = []
    rounds: list[dict[str, Any]] = []
    thread = uuid4()
    for index, turn in enumerate(scenario.turns):
        messages += turn_messages(turn, index)
        if index not in scenario.writebacks:
            continue
        caller = GuardedCaller(transport, f"{item}|wb{index}", phase)
        outcome = await flush_messages_with_outcome(
            list(messages),
            memory_store=store,
            embedder=embedder,
            llm_caller=caller,
            tenant_id=A_TENANT,
            user_id=user_id,
            thread_id=thread,
            token=CancellationToken(),
            reconcile=True,
            agent_name=A_AGENT,
        )
        if caller.failures or transport.guard.blocked:
            if transport.guard.blocked:
                raise SpendStopError(transport.guard.blocked)
            raise RuntimeError(f"model call failed inside the writeback: {caller.failures}")
        rounds.append(
            {
                "after_turn": index,
                "written": outcome.written,
                "failed": outcome.failed,
                "calls": caller.calls,
            }
        )
    everything = await store.list_for_user(tenant_id=A_TENANT, user_id=user_id, limit=500)
    return {
        "seeded": seeded,
        "rounds": rounds,
        "memories": [
            {
                "id": str(m.id),
                "kind": m.kind,
                "content": m.content,
                "confidence": m.confidence,
                "origin": getattr(m, "origin", None),
                "live": _live(m),
            }
            for m in everything
        ],
    }


def parse_extraction(reply: str) -> list[dict[str, Any]]:
    """提炼回答里的 ``memories``(容忍代码围栏);解析不了 = 空。"""
    text = reply.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") :] if "{" in text else text
    try:
        data = json.loads(text[text.find("{") : text.rfind("}") + 1])
    except (ValueError, TypeError):
        return []
    memories = data.get("memories") if isinstance(data, dict) else None
    return [m for m in memories or [] if isinstance(m, dict) and isinstance(m.get("content"), str)]


# --------------------------------------------------------------------------- B / C 组对话


def never_started(exc: BaseException, run_id: str | None) -> bool:
    """运行确定没建起来:``POST /runs`` 回 4xx,或连不上(连 run id 都没有)。5xx 不算 —— 网关
    502 / 504 时上游可能已经建好了(同 ``b302_live._never_started``)。"""
    if isinstance(exc, httpx.HTTPStatusError):
        return 400 <= exc.response.status_code < 500
    return run_id is None and isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)


def mark_run(exc: BaseException, *, run_id: str | None, started: bool | None) -> None:
    """把「有没有建起运行」挂在异常上,写回预留据此结算(审查第三轮 LOW)。"""
    try:
        exc.b300_run_id = run_id  # type: ignore[attr-defined]
        exc.b300_run_started = started  # type: ignore[attr-defined]
    except AttributeError:
        pass


def run_cap_yuan(prices: Mapping[str, Price], models: Sequence[str]) -> float:
    """一次运行的上限:预算内的 token 全按其中最贵的输出价算。"""
    return RUN_TOKEN_BUDGET * max(prices[m].output for m in models) / 1e6


def writeback_cap_yuan(price: Price) -> float:
    return WRITEBACK_CALLS_CAP * call_cap_yuan(price, MAX_CALL_INPUT_TOKENS, EXTRACT_MAX_TOKENS)


async def _usage_endpoint(
    http: Any, agent: str, run_id: str, user: str
) -> tuple[str | None, list[Any] | None]:
    resp = await http.get(f"/v1/agents/{agent}/runs/{run_id}/usage", params={"user_id": user})
    if resp.status_code != 200:
        return None, None
    body = resp.json()
    usage = body.get("usage_by_model")
    return body.get("run_status"), usage if isinstance(usage, list) else None


async def _cancel(http: Any, agent: str, run_id: str, user: str) -> None:
    await http.post(f"/v1/agents/{agent}/runs/{run_id}:cancel", json={"user_id": user})


async def run_conversation(
    http: Any,
    *,
    agent: str,
    user: str,
    turn: sc.Turn,
    guard: Guard,
    models: Sequence[str],
    item: str,
    phase: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """对外平面跑一轮。先预留一次运行的上限;按 ``end`` 帧的 ``usage_by_model`` 结算;没有
    ``end`` 帧就查用量接口,还在跑就取消并按上限结算、止损。"""
    model = models[0]
    uploads: list[str] = []
    try:
        for name, text in turn.documents:
            resp = await http.post(
                f"/v1/agents/{agent}/uploads",
                data={"user_id": user, **({"session_id": session_id} if session_id else {})},
                files={"file": (name, text.encode("utf-8"), content_type_for(name))},
            )
            resp.raise_for_status()
            body = resp.json()["data"]
            uploads.append(str(body["upload_id"]))
            session_id = str(body["session_id"])
    except BaseException as exc:
        mark_run(exc, run_id=None, started=False)  # 还没发 POST /runs:一定没有运行
        raise
    payload: dict[str, Any] = {"user_id": user, "input": turn.user, "mode": "stream"}
    if session_id:
        payload["session_id"] = session_id
    if uploads:
        payload["files"] = [{"upload_id": u} for u in uploads]
    call_id = guard.reserve(
        item=item, model=model, cap=run_cap_yuan(guard.prices, models), phase=phase
    )
    collector, parser = TurnCollector(), SseParser()
    started = time.monotonic()
    try:
        async with http.stream("POST", f"/v1/agents/{agent}/runs", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                frame = parser.feed(line)
                if frame is not None:
                    collector.on_frame(*frame)
                    if frame[0] == "metadata" and session_id is None:
                        data = json.loads(frame[1])
                        session_id = data.get("thread_id") or session_id
        tail = parser.flush()
        if tail is not None:
            collector.on_frame(*tail)
    except BaseException as exc:
        mark_run(exc, run_id=collector.run_id, started=not never_started(exc, collector.run_id))
        if collector.run_id:
            try:
                await _cancel(http, agent, collector.run_id, user)
            except Exception:  # noqa: S110 - 下一行照样止损
                pass
        guard.settle_cap(call_id, item=item, model=model, phase=phase, reason="cap_broken_stream")
        guard.block(f"{item}: stream broke (run {collector.run_id})")
        raise
    usage = (collector.end or {}).get("usage_by_model") if collector.end is not None else None
    if usage is None and collector.run_id:
        status, usage = await _usage_endpoint(http, agent, collector.run_id, user)
        if status in {"pending", "queued", "running"}:
            await _cancel(http, agent, collector.run_id, user)
            usage = None
    guard.settle_usage(call_id, item=item, model=model, phase=phase, usage_by_model=usage)
    final = ""
    for step in collector.steps:
        if step.reply.strip() and not step.calls:
            final = step.reply
    return {
        "run_id": collector.run_id,
        "session_id": session_id,
        "final_text": final,
        "status": (collector.end or {}).get("status"),
        "usage_by_model": usage,
        # 试点量 token 用:每步的输入 token(``usage_metadata``)与步数。
        "steps": len(collector.steps),
        "step_input_tokens": list(collector.input_tokens),
        "wall_s": round(time.monotonic() - started, 1),
    }


def reserve_writeback(guard: Guard, *, item: str, model: str, phase: str) -> str:
    """带写回的智能体每跑一次,先按上限给后台写回预留(对外看不到它的用量,§4.4),**不结算**;
    之后由 pod 里 ``writeback_usage`` 读出的实际用量结算(:func:`settle_writebacks`)。"""
    cap = writeback_cap_yuan(guard.prices[model])
    return guard.reserve(item=f"{item}|writeback", model=model, cap=cap, phase=phase)


def settle_writebacks(
    guard: Guard,
    call_ids: Sequence[str],
    usage_by_model: Sequence[Mapping[str, Any]] | None,
    *,
    phase: str,
) -> float:
    """一个评测用户的全部写回预留按 ``token_usage`` 里的实际用量结算(平台开销行,只有数字),
    按各笔上限依次分摊。读不到(``None`` 或空)就**不结算、留着预留**(账本里按上限算;审查 HIGH:
    从不按 0 结)。调用方只在这个用户的写回任务都结束之后才调。"""
    if not call_ids or not usage_by_model:
        return 0.0
    try:
        remaining = usage_cost(usage_by_model, guard.prices)
    except KeyError as exc:
        guard.block(f"writeback usage has a model without a price: {exc}")
        return sum(
            guard.settle_cap(c, item="writeback", model="", phase=phase, reason="cap_no_price")
            for c in call_ids
        )
    total = 0.0
    for n, call_id in enumerate(call_ids):  # 按各笔上限依次分摊,最后一笔拿余数
        last = n == len(call_ids) - 1
        cost = remaining if last else min(remaining, guard.open_cap(call_id))
        remaining -= cost
        total += guard.settle(call_id, item="writeback", model="", phase=phase, cost=cost)
    return total


# --------------------------------------------------------------------------- 判官

JUDGE_SYSTEM = (
    "You grade one output of an AI assistant's long-term memory feature against one rule. "
    "Read the material, apply the rule literally, and answer with ONLY a JSON object: "
    '{"answer": "yes" | "no", "reason": "<one short sentence>"}.'
)


@dataclass
class Judge:
    transport: ChatTransport
    votes: int

    async def vote(self, prompt: str, *, item: str, phase: str) -> list[bool | None]:
        """``votes`` 次独立判;每票 ``True`` / ``False``,解析不了记 ``None``。"""
        return [v for v, _u in await self.vote_with_usage(prompt, item=item, phase=phase)]

    async def vote_with_usage(
        self, prompt: str, *, item: str, phase: str
    ) -> list[tuple[bool | None, dict[str, int]]]:
        """同 :meth:`vote`,另带每票的 token(试点量 token 用)。"""
        out: list[tuple[bool | None, dict[str, int]]] = []
        for n in range(self.votes):
            text, usage = await self.transport.complete(
                [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": prompt}],
                item=f"{item}|v{n}",
                phase=phase,
                json_mode=True,
            )
            tokens = {
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
            }
            out.append((parse_vote(text), tokens))
        return out


def parse_vote(text: str) -> bool | None:
    try:
        data = json.loads(text[text.find("{") : text.rfind("}") + 1])
    except (ValueError, TypeError):
        return None
    answer = str(data.get("answer", "")).strip().lower() if isinstance(data, dict) else ""
    return {"yes": True, "no": False}.get(answer)


def majority(votes: Sequence[bool | None]) -> bool | None:
    """严格多数;没有严格多数(含解析不了的票)= ``None``,记「判分分歧」单列。"""
    yes = sum(v is True for v in votes)
    no = sum(v is False for v in votes)
    if yes * 2 > len(votes):
        return True
    if no * 2 > len(votes):
        return False
    return None
