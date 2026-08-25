"""legacy SSE 帧 → 对话条目生命周期事件 —— **消费端**转换器。

设计见 ``docs/superpowers/specs/2026-08-25-conversation-items-design.md`` §四 / §五。

为什么转换发生在消费端而不是生产端:``_publish_frame``(``orchestrator/sse.py``)
是所有持久帧的唯一收口,而事件库是**所有连接共享的一份**;``stream_format``
却是**每条连接**的选择。所以事件库永远只存 legacy 帧一份,实时在转发时转换、
回放在读取时转换,同一个转换器服务两条路径。

为什么放在 orchestrator 而不是 common:这是 SSE **帧级**逻辑,与
``format_sse`` / ``end_frame_data`` / ``EXTERNAL_HIDDEN_EVENTS`` 同层 —— 那三个
也住在 ``orchestrator.sse`` 并由 control-plane 直接 import。条目的**形状**与
**推导**才在 common(``conversation_items`` / ``conversation_derive``),本模块
一行推导都不重写,只负责编号、生命周期与状态机。

本模块不 import ``orchestrator.sse``(那边要 import 本模块),所以帧名在这里
以字面量给出;两边不漂移由 ``tests/test_stream_items_vocabulary.py`` 的 AST 闸
钉住。

条目 ``id`` 的派生规则(spec §四「修正 —— item id 必须确定性派生」)
--------------------------------------------------------------------
**绝不能用自增计数器。** ``_encode`` 的调用路径里有三条会毁掉自增状态:补洞
重发是乱序的、回放截断把一条流切成两条连接(客户端带 ``since_seq`` 重连,计数
器归零)、live 接合会重放缓冲区里的陈旧帧。所以每个 ``id`` 都是**帧内容的纯
函数**:

============= ==========================================================
条目           ``id``
============= ==========================================================
助手消息       ``{run}:step:{step_count}`` —— ``step_count`` 同时出现在
               token 帧(``TokenSink`` 用 ``step_count + 1`` 构造)与 agent
               节点的 ``updates`` 值里,所以打字机预览与权威条目**天然同
               号**,不需要任何连接级配对状态
工具调用       ``{run}:call:{call_id}`` —— ``call_id`` 同时在 ``tool_args``
               token 帧(PR #1278 补的)与 ``AIMessage.tool_calls[].id`` 上
工具结果       ``{run}:result:{call_id}``
计划           ``{run}:plan`` —— 计划帧是整份快照,一轮只该有一条计划条目,
               后来的帧 upsert 掉前一份(与 legacy ``plan`` 帧同语义)
审批           ``{run}:approval:{request_id}``
其余           ``{run}:{seq}:{消息下标}[:{调用下标}]``
============= ==========================================================

``{seq}`` 取自帧的 SSE ``id:``(``{created_at_ms}-{seq}``)的后半段。**只能取
seq、不能取整个 id**:``created_at_ms`` 在实时(bridge ``publish`` 的时钟)与
回放(``run_event.created_at_ms``)是两次独立的 ``time.time()``,毫秒级会差;
拿它进 ``id`` 会让同一条目在断线重连前后编号不同,客户端 upsert 变成重复渲染。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from expert_work.common.conversation_channel import (
    CHANNEL_COMMENTARY,
    CHANNEL_FINAL,
    has_tool_calls,
)
from expert_work.common.conversation_derive import derive_run_items
from expert_work.common.conversation_items import (
    AssistantMessageItem,
    AuxFrame,
    ToolCallItem,
    ToolResultItem,
)

__all__ = [
    "CONNECTION_EVENTS",
    "CONVERTED_EVENTS",
    "ITEMS_WIRE_EVENTS",
    "ITEM_ADDED",
    "ITEM_DELTA",
    "ITEM_DONE",
    "ITEM_EVENTS",
    "PASSTHROUGH_EVENTS",
    "PUBLISHED_EVENTS",
    "STREAM_FORMATS",
    "STREAM_FORMAT_ITEMS",
    "STREAM_FORMAT_LEGACY",
    "ItemStreamConverter",
]

#: ``stream_format`` 的取值。``legacy`` 是默认 —— 已在对接的第三方零感知。
STREAM_FORMAT_LEGACY = "legacy"
STREAM_FORMAT_ITEMS = "items"
STREAM_FORMATS: frozenset[str] = frozenset({STREAM_FORMAT_LEGACY, STREAM_FORMAT_ITEMS})

#: 条目生命周期事件。``item.done`` 客户端必须按 **upsert** 处理(允许对一个
#: 从没 ``added`` 过的 id 直接 done)—— 回放路径里根本没有 token 帧,所以那条
#: 路径上只有 ``item.done``。
ITEM_ADDED = "item.added"
ITEM_DELTA = "item.delta"
ITEM_DONE = "item.done"
ITEM_EVENTS: frozenset[str] = frozenset({ITEM_ADDED, ITEM_DELTA, ITEM_DONE})

#: ``orchestrator/sse.py`` 能发布的全部帧名(``_publish_frame`` 的字面量 +
#: ``publish_ephemeral`` 的 ``token``)。**这张表是闸的一半** —— 另一半是
#: ``tests/test_stream_items_vocabulary.py`` 里 AST 扫 ``sse.py`` 得到的实际
#: 集合,两者必须相等。新增一种帧却忘了在下面二选一归类,那条测试就红,而不是
#: 在 items 模式下静默丢帧。
PUBLISHED_EVENTS: frozenset[str] = frozenset(
    {
        "metadata",
        "system_prompt",
        "updates",
        "plan",
        "compaction",
        "worker",
        "guard",
        "retry",
        "approval",
        "error",
        "token",
    }
)

#: items 模式下被转换成条目的帧。
CONVERTED_EVENTS: frozenset[str] = frozenset({"updates", "token", "plan", "approval", "error"})

#: items 模式下原样透传的帧。``worker`` **有意留在这里**(spec §五 拍板):
#: 转成 ``ToolCallItem.worker`` 就得等子任务的 ``end`` 帧才能发工具卡的
#: ``item.done``,时机语义会很别扭。这是 items 与历史唯一不完全同构处,对外
#: 文档诚实标注。
PASSTHROUGH_EVENTS: frozenset[str] = PUBLISHED_EVENTS - CONVERTED_EVENTS

#: 由 API 层直接 ``format_sse`` 出去、从不经过转换器的连接级帧。它们描述的是
#: **这条连接**的状况,不是 run 的事件。
CONNECTION_EVENTS: frozenset[str] = frozenset({"end", "gap", "truncated"})

#: items 模式下第三方能在 wire 上看到的全部事件。``system_prompt`` 对外恒隐藏
#: (``EXTERNAL_HIDDEN_EVENTS``),所以不在其中。
ITEMS_WIRE_EVENTS: frozenset[str] = (
    (PASSTHROUGH_EVENTS - {"system_prompt"}) | ITEM_EVENTS | CONNECTION_EVENTS
)

#: ``item.delta`` 的 ``field`` 取值。工具参数今天根本不流式,所以没有 ``args``
#: 频道(``streaming_redact.py`` 明确写了这一点)。
_DELTA_FIELDS: frozenset[str] = frozenset({"content", "reasoning"})


def _int_or_none(value: Any) -> int | None:
    """``bool`` 是 ``int`` 的子类,显式挡掉。"""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _seq_token(event_id: str | None) -> str:
    """帧 SSE ``id:``(``{created_at_ms}-{seq}``)里的 ``seq`` 部分。

    只取 seq:``created_at_ms`` 在实时与回放是两次独立取样,拿它编号会让同一
    条目在断线重连前后换号(见模块 docstring)。取不到时给 ``-``,那种帧
    (只有 token)本来也走不到位置回退分支。
    """
    if not event_id:
        return "-"
    return event_id.rsplit("-", 1)[-1]


def _frame_created_at(event_id: str | None) -> str | None:
    """帧 SSE ``id:`` 前半段的毫秒时刻 → ISO8601。

    ``plan`` / ``approval`` / ``error`` 三种帧的 ``data`` 里都不含时刻(spec
    §八),时刻只在 ``id:`` 上,所以只能从这里取。取不到给 ``None``,绝不编。
    """
    if not event_id:
        return None
    head, sep, _ = event_id.partition("-")
    if not sep:
        return None
    try:
        return datetime.fromtimestamp(int(head) / 1000.0, tz=UTC).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


class ItemStreamConverter:
    """一条连接上的 legacy 帧 → 条目事件转换器。

    **状态的生命周期恰好等于一条连接** —— 实时是 ``sse_consumer`` 这个 async
    generator 的局部变量,回放是 ``build_event_producer`` 闭包里的局部变量。
    状态只有三样,每一样都只是「少发一帧」的优化或对外契约的补丁,**没有一样
    参与 ``id`` 的计算**:乱序补洞 / 跨页重连 / live 重放三条路径因此污染不到
    编号(spec §十一「``_encode`` 从无状态变有状态」)。
    """

    def __init__(self, *, run_id: UUID | str) -> None:
        self._run = str(run_id)
        #: 已经收到权威 ``updates`` 帧的 step。**硬约束 (d) 与 (e) 共用这一个
        #: 机制**:
        #:
        #: * (d) live 接合会重放陈旧 token —— ``_stream_live`` 订阅时不传
        #:   ``last_event_id``,bridge 从缓冲区最早一条重放;带 seq 的帧被去重
        #:   挡掉,但 token 帧 ``seq is None``,**无条件放行**。legacy 下只是多
        #:   看到几段陈旧打字机文本,items 下会给一个**已经 done 的条目重开
        #:   ``item.added``**。
        #: * (e) ``token.step`` 在瞬时重试后会重复 —— 重试以 ``graph_input=None``
        #:   从 checkpoint 重入,重跑的 agent 节点再次产出同一个
        #:   ``step_count + 1``。
        #:
        #: 两种情形的判据是同一句话:**这个 step 的权威帧已经到过了,后面再来的
        #: token 都是预览的回声,丢掉**。重试那次的权威 ``updates`` 帧仍会在新的
        #: seq 上到达,以同一个 ``id`` upsert 出最终文本,所以内容不会丢。
        self._done_steps: set[int] = set()
        #: 本连接已经发过 ``item.added`` 的 id —— 只用来省掉重复的 added。
        self._added: set[str] = set()
        #: 「run 结束时该改判 ``final`` 的那条助手消息」= (id, 已发出的 payload)。
        self._final_candidate: tuple[str, dict[str, Any]] | None = None

    # -- 对外 ---------------------------------------------------------------

    def convert(self, event_name: str, data: Any, *, event_id: str | None) -> list[tuple[str, Any]]:
        """一帧 legacy → 0..N 帧 items。返回 ``(event_name, data)`` 列表。

        不认识的帧名一律**原样透传** —— 静默丢帧是本设计最怕的失败方式,而
        「该转却漏转」由词表闸(:data:`PUBLISHED_EVENTS`)在测试期抓,不靠
        运行期丢弃。
        """
        if event_name == "updates":
            return self._on_updates(data, event_id=event_id)
        if event_name == "token":
            return self._on_token(data)
        if event_name == "plan":
            return self._on_aux(data, event_id=event_id, kind="plan")
        if event_name == "approval":
            return self._on_aux(data, event_id=event_id, kind="approval")
        if event_name == "error":
            return self._on_aux(data, event_id=event_id, kind="error")
        return [(event_name, data)]

    def finalize(self) -> list[tuple[str, Any]]:
        """run 结束时补发的 ``channel="final"`` 改判(spec §五 硬约束 (c))。

        ``final`` 的判定要**向后看一条消息**(``visible_turns`` 的
        ``nxt = collected[i+1]``),流式时那条还不存在。所以实时一律先发
        ``commentary``,run 结束时对最后一条符合 final 条件的助手消息补发一个
        ``item.done`` 把 channel 改过来。客户端 reducer 本来就必须把
        ``item.done`` 当 upsert 处理,所以不引入新机制。

        **必须排在 ``end`` 帧之前**,而截断(``truncated``)时**不能发** ——
        那条流并没有结束,下一页还会来。

        自清空,重复调用无害。
        """
        if self._final_candidate is None:
            return []
        _item_id, payload = self._final_candidate
        self._final_candidate = None
        return [(ITEM_DONE, {**payload, "channel": CHANNEL_FINAL})]

    # -- updates ------------------------------------------------------------

    def _on_updates(self, chunk: Any, *, event_id: str | None) -> list[tuple[str, Any]]:
        """``{node: {channel: value}}`` → 每条消息的 ``item.done``。

        逐条消息调 ``derive_run_items``(而不是整帧一次)—— 这样才知道每个条目
        来自哪条消息,才能按消息下标编号。对单条消息而言两种调法**除 channel
        外完全等价**(``visible_turns`` 的段落判定只在多条消息之间起作用),而
        channel 本来就要被 :meth:`finalize` 那条规则覆写成 ``commentary``。
        """
        if not isinstance(chunk, Mapping):
            return []
        seq = _seq_token(event_id)
        out: list[tuple[str, Any]] = []
        index = 0
        for node_val in chunk.values():
            if not isinstance(node_val, Mapping):
                continue
            step = _int_or_none(node_val.get("step_count"))
            messages = node_val.get("messages")
            if isinstance(messages, Sequence) and not isinstance(messages, str | bytes):
                for msg in messages:
                    out.extend(self._message_events(msg, step=step, seq=seq, index=index))
                    index += 1
            if step is not None:
                # 权威帧到过了 —— 这个 step 后面再来的 token 都是回声。放在
                # 消息循环**之外**:即便这一帧一条可见消息都没产出(整条被
                # 隐藏 / 正文空白),抑制照样要生效。
                self._done_steps.add(step)
        return out

    def _message_events(
        self, msg: Any, *, step: int | None, seq: str, index: int
    ) -> list[tuple[str, Any]]:
        events: list[tuple[str, Any]] = []
        call_n = 0
        for item in derive_run_items(run_id=self._run, messages=[msg]):
            if isinstance(item, AssistantMessageItem):
                item = self._identify_assistant(item, msg=msg, step=step, seq=seq, index=index)
            elif isinstance(item, ToolCallItem):
                item = replace(
                    item,
                    id=(
                        f"{self._run}:call:{item.call_id}"
                        if item.call_id
                        else f"{self._run}:{seq}:{index}:{call_n}"
                    ),
                )
                call_n += 1
            elif isinstance(item, ToolResultItem):
                item = replace(
                    item,
                    id=(
                        f"{self._run}:result:{item.call_id}"
                        if item.call_id
                        else f"{self._run}:{seq}:{index}"
                    ),
                )
            else:
                item = replace(item, id=f"{self._run}:{seq}:{index}")
            self._added.add(item.id)
            events.append((ITEM_DONE, item.to_wire()))
        return events

    def _identify_assistant(
        self, item: AssistantMessageItem, *, msg: Any, step: int | None, seq: str, index: int
    ) -> AssistantMessageItem:
        item_id = f"{self._run}:step:{step}" if step is not None else f"{self._run}:{seq}:{index}"
        # 实时判不出 final(见 :meth:`finalize`)—— 一律先 commentary。
        settled = replace(item, id=item_id, channel=CHANNEL_COMMENTARY)
        # 带 tool_calls 的助手消息永远是 commentary(它还没说完),所以它不但
        # 自己不是候选,还把前一条候选顶掉 —— 前一条不再是段尾。
        self._final_candidate = None if has_tool_calls(msg) else (item_id, settled.to_wire())
        return settled

    # -- token --------------------------------------------------------------

    def _on_token(self, data: Any) -> list[tuple[str, Any]]:
        if not isinstance(data, Mapping):
            return []
        step = _int_or_none(data.get("step"))
        if step is None or step in self._done_steps:
            return []
        channel = data.get("channel")
        if channel in _DELTA_FIELDS:
            return self._text_delta(step, str(channel), data.get("text"))
        if channel == "tool_args":
            return self._tool_preview(data)
        return []

    def _text_delta(self, step: int, field: str, text: Any) -> list[tuple[str, Any]]:
        if not isinstance(text, str) or not text:
            return []
        item_id = f"{self._run}:step:{step}"
        out: list[tuple[str, Any]] = []
        if item_id not in self._added:
            self._added.add(item_id)
            out.append(
                (
                    ITEM_ADDED,
                    AssistantMessageItem(
                        id=item_id,
                        run_id=self._run,
                        created_at=None,
                        content="",
                        channel=CHANNEL_COMMENTARY,
                    ).to_wire(),
                )
            )
        # **不带 seq**(spec §五 硬约束 (b))—— token 帧是 ephemeral 的,不落库、
        # 不占号。这里发的 ``item.delta`` 只带 id,调用方也不会给它挂 ``id:``
        # 行:让不可回放的帧占用 seq,客户端解析出的续传位点就会跑到
        # ``since_seq`` 实际能回放的范围之外,断线重连**静默漏事件**。
        out.append((ITEM_DELTA, {"id": item_id, "field": field, "text": text}))
        return out

    def _tool_preview(self, data: Mapping[str, Any]) -> list[tuple[str, Any]]:
        """``tool_args`` token → 工具卡的 ``item.added``。

        键用 **``call_id``**,不用 ``tool_index``:后者在 Anthropic 路径下是
        **内容块**下标(text / thinking 块也占号),不是 ``tool_calls[]`` 的数组
        下标,拿它配对会配到错的那个工具(PR #1278)。``call_id`` 与
        ``AIMessage.tool_calls[].id`` 同值,所以这张预览卡与随后权威帧发出的
        ``item.done`` **天然同号**。
        """
        call_id = data.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return []
        item_id = f"{self._run}:call:{call_id}"
        if item_id in self._added:
            return []
        self._added.add(item_id)
        name = data.get("name")
        return [
            (
                ITEM_ADDED,
                ToolCallItem(
                    id=item_id,
                    run_id=self._run,
                    created_at=None,
                    call_id=call_id,
                    name=name if isinstance(name, str) else "",
                    # 参数今天不流式,预览卡只有名字。
                    args={},
                ).to_wire(),
            )
        ]

    # -- plan / approval / error --------------------------------------------

    def _on_aux(self, data: Any, *, event_id: str | None, kind: str) -> list[tuple[str, Any]]:
        """三种「一帧就是一条完整条目」的辅助信号 → 单个 ``item.done``。

        推导仍走 ``derive_run_items`` —— 字段取法(``approval`` 的
        ``requested_at`` 回退、``error`` 的 ``name``、``plan`` 的 steps 净化)
        只有那一份实现,历史路径与这里必须字节同义。
        """
        if not isinstance(data, Mapping):
            return []
        frame = AuxFrame(data=data, created_at=_frame_created_at(event_id))
        if kind == "plan":
            items = derive_run_items(run_id=self._run, messages=(), plan=frame)
            item_id = f"{self._run}:plan"
        elif kind == "approval":
            items = derive_run_items(run_id=self._run, messages=(), approvals=[frame])
            request_id = data.get("request_id")
            token = (
                request_id if isinstance(request_id, str) and request_id else _seq_token(event_id)
            )
            item_id = f"{self._run}:approval:{token}"
        else:
            items = derive_run_items(run_id=self._run, messages=(), error=frame)
            item_id = f"{self._run}:error:{_seq_token(event_id)}"
        if not items:
            return []
        self._added.add(item_id)
        return [(ITEM_DONE, replace(items[0], id=item_id).to_wire())]
