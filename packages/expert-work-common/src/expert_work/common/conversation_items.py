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

    def _payload(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "name": self.name,
            "status": self.status,
            "content": self.content,
            "artifact": self.artifact,
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
    """

    TYPE: ClassVar[str] = "approval"

    status: str
    tool: str
    args: Mapping[str, Any] = field(default_factory=dict)

    def _payload(self) -> dict[str, Any]:
        return {"status": self.status, "tool": self.tool, "args": dict(self.args)}


@dataclass(frozen=True, slots=True)
class ErrorItem(_ItemBase):
    """这一轮的失败原因。

    与 ``runs[].error`` 同源。放进 items 是为了让它出现在时间线的正确位置 ——
    一轮跑了一半才失败时,错误应当排在已产出的内容之后。
    """

    TYPE: ClassVar[str] = "error"

    message: str

    def _payload(self) -> dict[str, Any]:
        return {"message": self.message}


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
    "ConversationItem",
    "ErrorItem",
    "PlanItem",
    "ToolCallItem",
    "ToolResultItem",
    "UserMessageItem",
]
