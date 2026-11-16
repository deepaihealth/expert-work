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
import inspect
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

import b300_k_platform as kp  # noqa: E402
import b300_scenarios as sc  # noqa: E402
from b300_spend import Guard, SpendStopError, call_cap_yuan, usage_cost  # noqa: E402
from b301_plan import Price  # noqa: E402
from b302_live import TurnCollector  # noqa: E402
from behavior_client import SseParser, content_type_for  # noqa: E402

#: 单次调用的输入上限(token)—— 只用来算预留;我们自己拼的提示词远小于它,超了结算时会止损。
MAX_CALL_INPUT_TOKENS = 40_000
EXTRACT_MAX_TOKENS = 4_000
#: 判官的输出上限(重做设计 §11.8 ④:判词是一个 JSON、一句理由,按需要设,单次最坏值随之变小)。
JUDGE_MAX_TOKENS = 400
#: 判官同时在飞的笔数上限(实际笔数还随剩余额度收缩,``b300_spend.RoomGate``)。
JUDGE_CONCURRENCY = 4
#: 判官一次调用的输入上界 = 系统提示词与题目的 UTF-8 字节数(字节级分词每个 token 至少一个字节)
#: + 对话模板的特殊 token 余量。超了结算时会按「超过上限」止损,不会悄悄多花。
JUDGE_TEMPLATE_OVERHEAD_TOKENS = 64
#: 判官一次调用输入上界的下限:题目很短时也至少按这么多预留(防厂商计数比字节上界多出一点,
#: 比如另加的系统前缀)。仍远小于 :data:`MAX_CALL_INPUT_TOKENS`。
JUDGE_MIN_INPUT_TOKENS = 2_048
#: 每次调用的采样温度与 JSON 模式的回答格式(判官措辞摘要也算它们,改了要重新校准)。
TEMPERATURE = 0.0
JSON_RESPONSE_FORMAT: Mapping[str, str] = {"type": "json_object"}
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
#: 重做(设计 2026-11-15 附录 A / B)换了第二次与去重合并指令的开头;第一次的那句(附录 C)不变。
REDESIGN_PASS2_MARK = "Each line starts with its number in brackets and a tag:"
REDESIGN_RECONCILE_MARK = "You compare newly extracted memories"
#: 切片起点(重做设计 §8.1:同一对话前一个任务的 ``message_count``)传给写回入口的参数名 —— PR S 的
#: ``flush_messages_with_outcome(segment=SegmentStart(message_index=...))``。入口不收它(PR S 之前的
#: 代码,回归闸门的基线)时照整段重抽,A13 / A14 报 incomplete。
SLICE_START_PARAM = "segment"


#: 睡眠的间接层(测试换掉,不碰全局的 ``asyncio.sleep``;同 ``b302_live``)。
_sleep = asyncio.sleep


def classify_call(system_text: str) -> str:
    if PASS1_MARK in system_text:
        return "pass1"
    if PASS2_MARK in system_text or REDESIGN_PASS2_MARK in system_text:
        return "pass2"
    if OLD_EXTRACT_MARK in system_text:
        return "extract_old"
    if RECONCILE_MARK in system_text or REDESIGN_RECONCILE_MARK in system_text:
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


#: 限流(429)之后最多再试几次(间隔 5、10、15 秒)。
RATE_LIMIT_RETRIES = 3


class RateLimitedError(RuntimeError):
    """连续被限流:这一项没花钱(按 0 结),只让这一项失败,续跑时再试。"""


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

    def cap_for(self, max_input_tokens: int | None = None) -> float:
        """一次调用的上限(元);``max_input_tokens`` 不给按 :data:`MAX_CALL_INPUT_TOKENS`。"""
        price = self.guard.prices[self.model]
        return call_cap_yuan(price, max_input_tokens or MAX_CALL_INPUT_TOKENS, self.max_tokens)

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        item: str,
        phase: str,
        json_mode: bool = False,
        max_input_tokens: int | None = None,
    ) -> tuple[str, Mapping[str, Any]]:
        price = self.guard.prices[self.model]
        cap = self.cap_for(max_input_tokens)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": TEMPERATURE,
            **request_fields(self.provider, self.model, max_tokens=self.max_tokens),
        }
        if json_mode:
            body["response_format"] = dict(JSON_RESPONSE_FORMAT)
        # 一次调用只预留一次,限流重试沿用同一笔预留(审查 M1:以前每次重试先结 0 再重新预留,
        # 睡的那几秒里额度被别的调用占走,重新预留就撞线、这一段整段被拒)。
        call_id = self.guard.reserve(item=item, model=self.model, cap=cap, phase=phase)
        attempt = 0
        while True:
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
            if resp.status_code == 429:  # 限流:没花钱
                if attempt < RATE_LIMIT_RETRIES:  # 留着预留、等一下再试
                    attempt += 1
                    await _sleep(5.0 * attempt)
                    continue
                # 连续限流:这一项按 0 结、只让这一项失败,不按上限结、不停整份报价(审查二)
                self.guard.settle(
                    call_id,
                    item=item,
                    model=self.model,
                    phase=phase,
                    cost=0.0,
                    reason="rate_limited",
                )
                raise RateLimitedError(f"{item}: {self.model} still rate limited after retries")
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
        # 重做 §11.4:提炼的输入原样记下(带行号的那几行),``turn`` 指得对不对由它来查。合成内容。
        rest = "\n".join(str(m["content"]) for m in payload["messages"] if m["role"] != "system")
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
                # 第 9 版:去重合并的材料也记下(模型列了哪些旧记忆、写的保证,都要按它对回去)。
                **({"input": rest} if kind in {"pass1", "pass2", "reconcile"} else {}),
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
    """有效 = 没删、没作废、没被取代,且不是已并入汇总的成员(成员召回不到,平台也不当它「已经
    在了」,审查 M3;汇总行自己算)。"""
    folded = item.status != "consolidated" and getattr(item, "consolidated_into", None) is not None
    return (
        item.deleted_at is None
        and item.invalid_at is None
        and item.superseded_by is None
        and not folded
    )


def platform_capabilities() -> dict[str, bool]:
    """被测代码有没有重做加的字段(设计 2026-11-15 §9.1 / §8.1);没有的指标报 incomplete。"""
    from expert_work.protocol import MemoryItem
    from expert_work.protocol import memory_item as protocol_item
    from orchestrator.graph_builder import memory as memory_module

    fields = MemoryItem.model_fields
    origins: frozenset[str] = getattr(protocol_item, "MEMORY_ORIGINS", frozenset())
    entry = inspect.signature(memory_module.flush_messages_with_outcome).parameters
    from expert_work.persistence import InMemoryMemoryStore

    return {
        "user_confirmed": "user_confirmed" in origins,
        "evidence_pointer": "evidence_message_id" in fields,
        "event_date": "occurred_on" in fields,
        "confirmed_at": "confirmed_at" in fields,
        "slicing": SLICE_START_PARAM in entry,
        # 第 9 版:``supersede`` 收清单、有当场重写汇总行的入口(假设都在 ``b300_k_platform``)
        **kp.capabilities(InMemoryMemoryStore),
    }


def slice_start(message_count: int) -> dict[str, Any]:
    """写回入口的切片参数:从这条消息起的整轮是新段(上一次写回结束时的消息条数;第一次是 0)。
    只在 :func:`platform_capabilities` 认出切片时调用 —— 老代码里没有 ``SegmentStart``。"""
    from orchestrator.graph_builder.memory_slice import SegmentStart

    return {SLICE_START_PARAM: SegmentStart(message_index=message_count)}


def _with_ids(messages: Sequence[Any], turn: int) -> list[Any]:
    """每条消息一个 id(证据指针指向它;没 id 的补一个)。不进提示词,不改提炼的输入。"""
    out = []
    for n, m in enumerate(messages):
        out.append(
            m if m.id else m.model_copy(update={"id": f"b300-t{turn}-m{n}-{uuid4().hex[:8]}"})
        )
    return out


def _text(value: Any) -> Any:
    return None if value is None else (value if isinstance(value, int) else str(value))


def _dump(m: Any) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "kind": m.kind,
        "content": m.content,
        "confidence": m.confidence,
        "origin": getattr(m, "origin", None),
        "live": _live(m),
        # 重做要量的(§11.3 / §11.4):谁取代了它、证据指针、事件日期、确认日期。老代码上没有的为空。
        "superseded_by": _text(getattr(m, "superseded_by", None)),
        "evidence_message_id": _text(getattr(m, "evidence_message_id", None)),
        "evidence_message_index": _text(getattr(m, "evidence_message_index", None)),
        "occurred_on": _text(getattr(m, "occurred_on", None)),
        "confirmed_at": _text(getattr(m, "confirmed_at", None)),
        # 第 9 版的读库断言(§11.2a):连线、作废时间、汇总行与成员、重写标记。
        **kp.dump_row(m),
    }


async def _seed_store(
    store: Any, embedder: Any, scenario: sc.Scenario, user_id: UUID
) -> dict[str, str]:
    from expert_work.protocol import MemoryItem

    if not scenario.seeds:
        return {}
    if scenario.group == "K":  # 带日子、并存连线、置信度 1.0、汇总行与成员(§11.2a)
        return await kp.seed_store(
            store, embedder, scenario.seeds, tenant_id=A_TENANT, user_id=user_id
        )
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
    """一个 A 组(或 §8.5 对比、重做的各段)场景的一次:按 ``writebacks`` 在对应轮之后各写回一次。
    平台收切片起点(:data:`SLICE_START_PARAM`)时,每次写回从上一次写回结束的消息条数起(没有上一次
    就是 0)—— 同一对话里前一个任务的 ``message_count``(重做设计 §8.1)。"""
    from expert_work.persistence import InMemoryMemoryStore
    from expert_work.runtime.cancellation import CancellationToken
    from orchestrator.graph_builder import memory as memory_module

    caps = platform_capabilities()
    store = InMemoryMemoryStore()
    user_id = uuid4()
    seeded = await _seed_store(store, embedder, scenario, user_id)
    messages: list[Any] = []
    rounds: list[dict[str, Any]] = []
    user_ids: list[str | None] = []
    thread = uuid4()
    start = 0
    for index, turn in enumerate(scenario.turns):
        added = _with_ids(turn_messages(turn, index), index)
        user_ids.append(str(added[0].id) if added else None)
        messages += added
        if index not in scenario.writebacks:
            continue
        caller = GuardedCaller(transport, f"{item}|wb{index}", phase)
        slice_arg: dict[str, Any] = slice_start(start) if caps["slicing"] else {}
        with kp.writeback_errors() as swallowed:  # 写回入口吞掉的错,只记异常名(§11.1 第 2 步 ③)
            outcome = await memory_module.flush_messages_with_outcome(
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
                **slice_arg,
            )
        if caller.failures or transport.guard.blocked:
            if transport.guard.blocked:
                raise SpendStopError(transport.guard.blocked)
            raise RuntimeError(f"model call failed inside the writeback: {caller.failures}")
        rewrite: dict[str, Any] | None = None
        if scenario.cls == "K-s":  # §7.12 第 2 步:写回任务收尾时当场重写打了标的汇总行
            aux = kp.GuardedAux(transport, f"{item}|wb{index}", phase)
            rewrite = await kp.rewrite_flagged(
                store,
                embedder,
                aux,
                tenant_id=A_TENANT,
                user_id=user_id,
                model=transport.model,
                summary_id=seeded.get("summary"),
            )
            rewrite["calls"] = aux.calls
            if transport.guard.blocked:  # 重写器把模型调用的错吞成 failed:止损了照样停
                raise SpendStopError(transport.guard.blocked)
            if aux.failures:  # 评测这边的调用出错(限流用完重试等):这条记录要补跑
                raise RuntimeError(f"summary rewrite model call failed: {aux.failures}")
        now = await store.list_for_user(tenant_id=A_TENANT, user_id=user_id, limit=500)
        rounds.append(
            {
                **({"rewrite": rewrite} if rewrite is not None else {}),
                "after_turn": index,
                "written": outcome.written,
                "failed": outcome.failed,
                "errors": list(swallowed),
                "calls": caller.calls,
                "message_count": len(messages),
                "sliced": bool(slice_arg),
                "live_after": [m.content for m in now if _live(m)],
            }
        )
        start = len(messages)
    everything = await store.list_for_user(tenant_id=A_TENANT, user_id=user_id, limit=500)
    return {
        "seeded": seeded,
        "rounds": rounds,
        "memories": [_dump(m) for m in everything],
        "capabilities": caps,
        "origin_labels": kp.origin_labels(),
        "user_message_ids": user_ids,
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
        # 挂不上属性的异常:结算方读到 None = 不知道建没建起运行,预留按上限留着,不按 0 结。
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
    try:
        call_id = guard.reserve(
            item=item, model=model, cap=run_cap_yuan(guard.prices, models), phase=phase
        )
    except BaseException as exc:
        mark_run(exc, run_id=None, started=False)  # 运行本身的预留被拒:没发 POST,一定没有运行
        raise
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


def judge_input_bound(messages: Sequence[Mapping[str, str]]) -> int:
    """判官一次调用输入 token 的上界(§11.8 ④):UTF-8 字节数 + 模板余量,至少
    :data:`JUDGE_MIN_INPUT_TOKENS`。"""
    size = sum(len(str(m.get("content", "")).encode("utf-8")) for m in messages)
    return max(size + JUDGE_TEMPLATE_OVERHEAD_TOKENS, JUDGE_MIN_INPUT_TOKENS)


class NoRoomError(RuntimeError):
    """这一段剩下的钱一笔判官调用都装不下(没有在飞的可等):这一项不判,留给下次;不止损。"""


@dataclass
class Judge:
    transport: ChatTransport
    votes: int
    #: ``b300_spend.RoomGate``:给了就按剩余额度收缩并发(§11.8 ②)。
    gate: Any = None

    async def vote(self, prompt: str, *, item: str, phase: str) -> list[bool | None]:
        """``votes`` 次独立判;每票 ``True`` / ``False``,解析不了记 ``None``。"""
        return [v for v, _u in await self.vote_with_usage(prompt, item=item, phase=phase)]

    async def vote_with_usage(
        self, prompt: str, *, item: str, phase: str
    ) -> list[tuple[bool | None, dict[str, int]]]:
        """同 :meth:`vote`,另带每票的 token(试点量 token 用)。"""
        out: list[tuple[bool | None, dict[str, int]]] = []
        messages = [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        bound = judge_input_bound(messages)
        cap = self.transport.cap_for(bound)
        slot = None
        if self.gate is not None:
            # 先要到全部几票的额度再发第一票(审查 LOW:3 票时不留半截判分)
            slot = await self.gate.acquire(cap, count=self.votes)
            if slot is None:
                raise NoRoomError(f"{item}: no room left for {self.votes} judge call(s) in {phase}")
        try:
            for n in range(self.votes):
                if slot is not None:
                    self.gate.consume(slot, cap)
                text, usage = await self.transport.complete(
                    messages,
                    item=f"{item}|v{n}",
                    phase=phase,
                    json_mode=True,
                    max_input_tokens=bound,
                )
                tokens = {
                    "input_tokens": int(usage.get("prompt_tokens") or 0),
                    "output_tokens": int(usage.get("completion_tokens") or 0),
                }
                out.append((parse_vote(text), tokens))
        finally:
            if slot is not None:
                await self.gate.release(slot)
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
