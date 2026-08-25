"""对话条目模型 —— 对外 API 表示一段对话的唯一形状。

设计见 ``docs/superpowers/specs/2026-08-25-conversation-items-design.md``。

为什么在 ``common`` 而不在任一 service:条目要从三条路径产出 —— 实时 SSE
(orchestrator)、单 run 回放与会话历史(control-plane)。orchestrator 不能
import control-plane,所以共享的形状只能落在这里。理由与 ``message_stamp``
同款。

为什么只用标准库:这是跨服务的契约,依赖越少越不容易在某条 import 路径上炸。
``plan`` / ``approval`` 的载荷来自已经 jsonable 的 SSE 帧,原样透传即可,不必
重新绑定 ``expert_work.protocol`` 的 pydantic 模型。

**``id`` 的承诺范围**:只保证同一响应内唯一、同一查询可重复。**不保证跨接口
稳定** —— 实时产出与历史重建用的是两套推导输入,编号规则不同。这不成问题:
同一个 run 不会同时出现在历史与实时(历史排除活跃轮),所以两套 id 永远不会
落进同一个列表。客户端不要拿它跨接口比对。

**``item.delta`` 不带 seq(给 PR3 实现者的硬约束)**:实时的 ``item.delta``
将由今天的 ``token`` 帧转换而来,而 ``token`` 是 ephemeral 的 —— 它不落库、
走 ``bridge.publish_ephemeral``,因此**不占序号**(见 ``orchestrator/sse.py``
的 ``_publish_token``)。这条约束必须原样传进条目模式:一旦让不可回放的帧占
用 seq,客户端从实时流里解析出的续传位点就会跑到 ``since_seq`` 实际能回放的
范围之外,断线重连**静默漏事件**。所以 ``item.delta`` 一律无 seq,客户端的
续传位点只能取自带 seq 的帧(``item.added`` / ``item.done`` 及流控帧)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

#: 条目类型词表。**这是对外契约** —— 新增类型必须同时改这里、加对应
#: dataclass、更新文档。``ITEM_CLASSES`` 与它的一致性由契约测试钉住,防止
#: 「加了类忘了词表」或反过来(本仓库有过同一词表分散多处然后漂移的先例)。
ITEM_TYPES: frozenset[str] = frozenset(
    {
        "user_message",
        "assistant_message",
        "tool_call",
        "tool_result",
        "plan",
        "approval",
        "error",
    }
)

#: ``assistant_message.channel`` 的取值。沿用 ``control_plane.transcript``
#: 既有的结构化判定,不新造语义:``final`` 是所在段落里最后一条且不带
#: ``tool_calls`` 的助手消息,其余都是 ``commentary``。
CHANNELS: frozenset[str] = frozenset({"final", "commentary"})

#: ``tool_result.status`` 的取值,与 SSE ``updates`` 里 tool 消息同源。
TOOL_STATUSES: frozenset[str] = frozenset({"success", "error"})


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    """去掉值为 ``None`` 的键 —— 缺席与显式 null 对客户端是两回事。"""
    return {k: v for k, v in payload.items() if v is not None}


@dataclass(frozen=True, slots=True)
class AuxFrame:
    """一帧辅助信号 + 它的落库时刻。

    ``plan`` / ``approval`` / ``error`` 三种帧的 ``data`` 里都不含时刻 ——
    时刻只在 SSE 的 ``id:`` 前缀(``{created_at_ms}-{seq}``)上。调用方从
    落库记录取出时刻一并传进来,否则这三种条目的 ``created_at`` 只能是
    ``None``。

    不是条目,所以不进 :data:`ITEM_TYPES` / :data:`ITEM_CLASSES` —— 它是
    推导函数的**输入**形状。
    """

    data: Mapping[str, Any]
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class _ItemBase:
    """每种条目都有的三个字段。

    ``created_at`` 可以为 ``None``:上线前写入的老消息没盖时间戳,归不到
    时刻。这种情况给 ``None`` 而不是编一个,客户端按缺席处理。
    """

    id: str
    run_id: str
    created_at: str | None

    #: 子类覆盖。不是 dataclass 字段,所以不参与 ``__init__`` 与字段顺序。
    TYPE: ClassVar[str] = ""

    def _payload(self) -> dict[str, Any]:
        """子类专有字段。基类只管公共部分。"""
        raise NotImplementedError

    def to_wire(self) -> dict[str, Any]:
        """序列化成对外 JSON。公共字段在前,专有字段在后。"""
        return {
            "id": self.id,
            "type": self.TYPE,
            "run_id": self.run_id,
            "created_at": self.created_at,
            **_drop_none(self._payload()),
        }


@dataclass(frozen=True, slots=True)
class UserMessageItem(_ItemBase):
    """终端用户发出的那条消息。

    事件流从来不含它 —— 它是 graph 的输入,只躺在 checkpoint 里。第三方要渲染
    完整对话必须有这一条,由服务端合成正是本模型存在的直接理由之一。
    """

    TYPE: ClassVar[str] = "user_message"

    content: str
    attachments: Sequence[Mapping[str, Any]] = ()

    def _payload(self) -> dict[str, Any]:
        return {
            "content": self.content,
            # 空附件列表要给出来(``[]`` 是「这条没带附件」的正常答案),
            # 所以不走 ``_drop_none`` 的缺席语义。
            "attachments": [dict(a) for a in self.attachments],
        }


@dataclass(frozen=True, slots=True)
class AssistantMessageItem(_ItemBase):
    """Agent 的一段文本产出。

    ``channel`` 区分正文与中间说明,取值见 :data:`CHANNELS`。
    """

    TYPE: ClassVar[str] = "assistant_message"

    content: str
    channel: str

    def _payload(self) -> dict[str, Any]:
        return {"content": self.content, "channel": self.channel}


@dataclass(frozen=True, slots=True)
class ToolCallItem(_ItemBase):
    """一次工具调用的发起。

    一条 AIMessage 可能带多个 ``tool_calls``,每个产出独立一条,``id`` 带子序号。
    ``call_id`` 与 :class:`ToolResultItem` 配对 —— 客户端靠它把结果挂回卡片,
    不要靠列表相邻位置(工具是并行的,中间可能插着别的条目)。
    """

    TYPE: ClassVar[str] = "tool_call"

    call_id: str
    name: str
    args: Mapping[str, Any]
    #: 子任务委托的进度快照。平台特有,多数客户端忽略。
    worker: Mapping[str, Any] | None = None

    def _payload(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "name": self.name,
            "args": dict(self.args),
            "worker": dict(self.worker) if self.worker is not None else None,
        }


@dataclass(frozen=True, slots=True)
class ToolResultItem(_ItemBase):
    """一次工具调用的结果。

    ``content`` 是**还原后**的文本。工具结果在内部带防注入包装,直接显示是
    乱码;把内部表示翻译成产品表示正是条目层的价值所在,不要把还原步骤推给
    客户端。
    """

    TYPE: ClassVar[str] = "tool_result"

    call_id: str
    name: str
    status: str
    content: str
    #: 工具产出的结构化数据,结构随工具而定,可能缺席。
    artifact: Any = None
    #: 这次工具调用花了多久。来自 ``ToolMessage.additional_kwargs["duration_ms"]``
    #: (``builder._run_tools`` 在派发处量的墙钟)。缺席 = 这条结果没量到时长
    #: (老消息、或工具走了不经计时的分支),客户端按「不显示耗时」处理。
    duration_ms: int | None = None

    def _payload(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "name": self.name,
            "status": self.status,
            "content": self.content,
            "artifact": self.artifact,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class PlanItem(_ItemBase):
    """一轮结束时的计划快照。

    整份快照而非增量 —— 与 SSE ``plan`` 帧同语义,客户端整个替换本地副本。
    一轮里计划可能改多次,历史只保留最后一次:中间态属于执行过程,不属于
    这一轮留下的对话内容。
    """

    TYPE: ClassVar[str] = "plan"

    goal: str
    steps: Sequence[Mapping[str, Any]] = ()

    def _payload(self) -> dict[str, Any]:
        return {"goal": self.goal, "steps": [dict(s) for s in self.steps]}


@dataclass(frozen=True, slots=True)
class ApprovalItem(_ItemBase):
    """一次人工审批。

    历史里要能渲染出「这一步等过审批、结果是什么」,所以它是对话内容的一部分,
    不是纯执行过程。

    字段与 SSE ``approval`` 帧(``orchestrator/sse.py`` 的 ``approval_payload``
    = :class:`expert_work.protocol.approval.ApprovalRequest` 的 json dump)保持
    同名同义,一个都不能少 —— 客户端在**提交决策之前**要靠它们做三件事:

    * ``reason_kind`` —— 判断「拒绝会不会直接终结这次 run」的唯一依据
      (``policy_gate`` 拒绝即终止,另外四种 agent 自提的会继续跑)。取值是
      :data:`expert_work.protocol.approval.ApprovalReasonKind` 那五个,平台
      保证不出现第六个,所以这里按字符串透传、不复制一份词表来漂移。
    * ``requested_at`` / ``timeout_at`` —— 倒计时窗口的唯一依据。取不到时给
      ``None``(键直接缺席),**绝不编一个默认值**:客户端把默认值写死正是
      对外文档明令禁止的做法,服务端更不该替它写死。
    * ``request_id`` —— 界面上区分与去重多条审批。

    ``binding_digest`` 有意**不进条目**:它是平台内部的参数绑定校验值,对外
    文档写明客户端原样忽略,提交决策的请求体也不收它。放进来只会让客户端以为
    自己该校验点什么 —— 而它在客户端侧根本无从校验。

    ``decision`` = 这次审批最终的结果,取值来自
    :class:`expert_work.protocol.approval.ApprovalRecord` 的 ``status``:
    ``approved`` / ``rejected`` / ``modified`` / ``timeout``。选终态语义而不是
    人提交的那三个动词,是因为 ``timeout``(超时没人管、被后台按拒绝处理)在
    历史里必须看得出来,而动词那一套表达不了。类型仍是字符串透传 —— common
    不依赖 protocol,不在这里复制一份词表来漂移。live 发出时还没有决策,所以
    **缺席**;历史重建时的数据源由 PR2 接。
    """

    TYPE: ClassVar[str] = "approval"

    request_id: str
    node: str
    reason_kind: str
    action_summary: str
    proposed_args: Mapping[str, Any] = field(default_factory=dict)
    requested_at: str | None = None
    timeout_at: str | None = None
    decision: str | None = None

    def _payload(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "node": self.node,
            "reason_kind": self.reason_kind,
            "action_summary": self.action_summary,
            # 空参数列表要给出来(``{}`` 是「这次调用没有参数」的正常答案)。
            "proposed_args": dict(self.proposed_args),
            "requested_at": self.requested_at,
            "timeout_at": self.timeout_at,
            "decision": self.decision,
        }


@dataclass(frozen=True, slots=True)
class ErrorItem(_ItemBase):
    """这一轮的失败原因。

    与 ``runs[].error`` 同源。放进 items 是为了让它出现在时间线的正确位置 ——
    一轮跑了一半才失败时,错误应当排在已产出的内容之后。

    ``name`` 是异常类名,与 SSE ``error`` 帧的同名字段一路同源。对外文档已经
    就 ``MaxStepsExceededError`` 这个取值给出过语义承诺(撞了步数上限,不是
    平台故障),只留 ``message`` 会把这份承诺丢掉。取不到时缺席。
    """

    TYPE: ClassVar[str] = "error"

    message: str
    name: str | None = None

    def _payload(self) -> dict[str, Any]:
        return {"message": self.message, "name": self.name}


#: 全部条目类。与 :data:`ITEM_TYPES` 的一致性由契约测试钉住。
ITEM_CLASSES: tuple[type[_ItemBase], ...] = (
    UserMessageItem,
    AssistantMessageItem,
    ToolCallItem,
    ToolResultItem,
    PlanItem,
    ApprovalItem,
    ErrorItem,
)

#: 推导函数与端点的返回类型。
ConversationItem = (
    UserMessageItem
    | AssistantMessageItem
    | ToolCallItem
    | ToolResultItem
    | PlanItem
    | ApprovalItem
    | ErrorItem
)


__all__ = [
    "CHANNELS",
    "ITEM_CLASSES",
    "ITEM_TYPES",
    "TOOL_STATUSES",
    "ApprovalItem",
    "AssistantMessageItem",
    "AuxFrame",
    "ConversationItem",
    "ErrorItem",
    "PlanItem",
    "ToolCallItem",
    "ToolResultItem",
    "UserMessageItem",
]
