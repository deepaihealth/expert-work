"""B-330 —— 审批(T7a~c)、委派子代(T8)、回滚兼容(T16)。脚手架见 wiring 模块。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypedDict, get_type_hints
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from prometheus_client import REGISTRY

from expert_work.protocol import SubAgentSpec, canonical_args_digest
from orchestrator import (
    AgentState,
    GraphRunner,
    ToolRegistry,
    ToolSpec,
    build_react_graph,
    pending_request_binding,
)
from orchestrator.built_agent import BuiltAgent
from orchestrator.graph_builder.link_registry_node import make_link_registry_node
from orchestrator.tools import SubAgentTool
from orchestrator.tools.link_registry import NOT_RUN_CONTENT

from .test_b330_link_registry_wiring import (
    FORM,
    OTHER,
    SIGNED,
    VARIABLES,
    Conversation,
    EchoMCPClient,
    ScriptedLLM,
    _no_registered_url_in_logs,  # noqa: F401 —— T14:本模块的日志同样不许有登记链接
    first_id,
    form_call,
    mcp_registry,
    opened,
    tc,
    tool_messages,
    url_forms,
)

pytestmark = pytest.mark.asyncio

GATED = frozenset({FORM})


async def _resume(
    conv: Conversation, *, modified_args: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """照审批端点的做法给出裁决(批准 / 修改),再续跑。"""
    cfg = conv.config(None)
    values = (await conv.compiled.aget_state(cfg)).values
    request = values["pending_approval"]
    if modified_args is None:
        verdict: dict[str, Any] = {"decision": "approve", "binding_digest": request.binding_digest}
    else:
        verdict = {
            "decision": "modify",
            "modified_args": dict(modified_args),
            "binding_digest": canonical_args_digest(dict(modified_args)),
        }
    binding = pending_request_binding(values)
    assert binding is not None
    await conv.compiled.aupdate_state(
        cfg, {"pending_approval": None, "approval_resume": {**verdict, **binding}}, as_node="agent"
    )
    return dict(await conv.compiled.ainvoke(None, config=cfg))


def _after_approval_blocked() -> float:
    return (
        REGISTRY.get_sample_value(
            "expert_work_tools_literal_guard_blocked_after_approval_total", {"tool": "mcp:shenhu"}
        )
        or 0.0
    )


# ---------------------------------------------------------------------------
# T7a —— 审批单上是编号,批准后才换回
# ---------------------------------------------------------------------------


async def test_approval_shows_the_id_and_the_approved_call_gets_the_url() -> None:
    client = EchoMCPClient()
    llm = ScriptedLLM([AIMessage(content="收到")])
    async with opened(llm, mcp_registry(client), approval_required_tools=GATED) as conv:
        await conv.turn("t1", inputs={"attachment_urls": [SIGNED]})
        link = first_id(conv)
        llm.responses += [form_call(link), AIMessage(content="已保存")]
        paused = await conv.turn("t2")
        request = paused["pending_approval"]
        assert request is not None
        assert client.calls == []
        dumped = json.dumps(request.model_dump(mode="json"), ensure_ascii=False)
        assert link in dumped
        assert SIGNED not in dumped
        state = await _resume(conv)

    # RT-6 摘要校验通过(没有完整性否决),假 MCP 收到真链接
    assert state.get("approval_outcome") != "rejected"
    assert client.calls == [
        {"fields": [{"field_code": "field_PE_report_attachment", "value": SIGNED}]}
    ]


# ---------------------------------------------------------------------------
# T7b —— 审批「修改」
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seq, dispatched", [(2, OTHER), (9, None)], ids=["other-id", "unknown"])
async def test_modify_to_another_id_is_rewritten_and_an_unknown_one_is_refused(
    seq: int, dispatched: str | None
) -> None:
    client = EchoMCPClient()
    llm = ScriptedLLM([AIMessage(content="收到")])
    async with opened(llm, mcp_registry(client), approval_required_tools=GATED) as conv:
        await conv.turn("t1", inputs={"attachment_urls": [SIGNED, OTHER]})
        llm.responses += [form_call(first_id(conv, 1)), AIMessage(content="ok")]
        await conv.turn("t2")
        before = _after_approval_blocked()
        modified = {
            "fields": [{"field_code": "field_PE_report_attachment", "value": first_id(conv, seq)}]
        }
        state = await _resume(conv, modified_args=modified)

    if dispatched is None:
        assert client.calls == []
        assert "本对话没有这个编号" in str(tool_messages(state)["tc-1"].content)
        assert _after_approval_blocked() == before + 1
    else:
        assert [c["fields"][0]["value"] for c in client.calls] == [dispatched]
        assert _after_approval_blocked() == before


# ---------------------------------------------------------------------------
# T7c —— 同一批两个要审批的调用,第一个编号错
# ---------------------------------------------------------------------------


async def test_a_bad_id_in_a_gated_batch_raises_no_approval_and_the_resend_works() -> None:
    client = EchoMCPClient()
    llm = ScriptedLLM([AIMessage(content="收到")])
    async with opened(llm, mcp_registry(client), approval_required_tools=GATED) as conv:
        await conv.turn("t1", inputs={"attachment_urls": [SIGNED]})
        good = {"fields": [{"field_code": "field_PE_report_attachment", "value": first_id(conv)}]}
        bad = {"fields": [{"field_code": "field_PE_report_attachment", "value": first_id(conv, 7)}]}
        llm.responses += [
            AIMessage(content="", tool_calls=[tc(bad, "tc-bad"), tc(good, "tc-good")]),
            AIMessage(content="", tool_calls=[tc(good, "tc-again")]),
            AIMessage(content="已保存"),
        ]
        paused = await conv.turn("t2")
        msgs = tool_messages(paused)
        # 这一步没有挂审批:坏的得到编号错误、另一条「没有执行」;下一步模型只重发好的那条
        assert "本对话没有这个编号" in str(msgs["tc-bad"].content)
        assert msgs["tc-good"].content == NOT_RUN_CONTENT
        assert paused["pending_approval"] is not None
        assert paused["pending_approval"].tool_call_id == "tc-again"
        state = await _resume(conv)

    assert state.get("approval_outcome") != "rejected"
    assert client.calls == [
        {"fields": [{"field_code": "field_PE_report_attachment", "value": SIGNED}]}
    ]


# ---------------------------------------------------------------------------
# T8 —— 委派子代、孙代
# ---------------------------------------------------------------------------


@dataclass
class _Builder:
    built: BuiltAgent

    async def __call__(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        depth: int,
        oauth_user_id: str | None = None,
        token_usage_kind: str = "conversation",  # noqa: S107
    ) -> BuiltAgent:
        del tenant_id, name, version, depth, oauth_user_id, token_usage_kind
        return self.built


@dataclass
class _ChildLLM:
    script: list[AIMessage]
    seen: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec], **_: Any
    ) -> AIMessage:
        self.seen.append(list(messages))
        return self.script[len(self.seen) - 1]


def _child_graph(llm: _ChildLLM, registry: ToolRegistry, saver: InMemorySaver) -> BuiltAgent:
    graph = GraphRunner(checkpointer=saver).compile(
        build_react_graph(
            llm_caller=llm,
            tool_registry=registry,
            link_registry_node=make_link_registry_node(variables=VARIABLES),
        )
    )
    return BuiltAgent(graph=graph, system_prompt="child", max_steps=6)


def _delegate(name: str, task: str, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[tc({"task": task}, call_id, name)])


async def test_children_and_grandchildren_rewrite_the_parent_conversations_ids() -> None:
    client = EchoMCPClient()
    grand_llm = _ChildLLM([])
    child_llm = _ChildLLM([])
    grand_saver, child_saver = InMemorySaver(), InMemorySaver()
    grand = _child_graph(grand_llm, mcp_registry(client), grand_saver)
    child_registry = mcp_registry(client)
    grand_spec = SubAgentSpec(name="grand", agent_ref="grand@1.0.0", description="grand")
    child_registry.register(
        SubAgentTool(subagent=grand_spec, builder=_Builder(grand), child_depth=2)
    )
    child = _child_graph(child_llm, child_registry, child_saver)
    child_spec = SubAgentSpec(name="child", agent_ref="child@1.0.0", description="child")

    parent_registry = mcp_registry(client)
    parent_registry.register(
        SubAgentTool(subagent=child_spec, builder=_Builder(child), child_depth=1)
    )
    llm = ScriptedLLM([AIMessage(content="收到")])
    async with opened(llm, parent_registry) as conv:
        await conv.turn("t1", inputs={"attachment_urls": [SIGNED, OTHER]})
        one, two = first_id(conv, 1), first_id(conv, 2)
        child_llm.script += [
            form_call(two, "c-1"),
            _delegate("grand", "存第一份", "c-2"),
            AIMessage(content="child done"),
        ]
        grand_llm.script += [form_call(one, "g-1"), AIMessage(content="grand done")]
        llm.responses += [_delegate("child", "存两份附件", "p-1"), AIMessage(content="完成")]
        await conv.turn("t2")

    assert [c["fields"][0]["value"] for c in client.calls] == [OTHER, SIGNED]
    for seen in (child_llm.seen, grand_llm.seen):
        tail = str(seen[0][-1].content)
        assert one in tail and two in tail
        for request in seen:
            assert SIGNED not in json.dumps([m.model_dump() for m in request], ensure_ascii=False)
    # 审查 M3:子代、孙代的检查点(按子线程 id 存,会话清除删不到)里没有原链接 —— 登记走 config
    # 往下传,不种进子代状态。
    for saver in (child_saver, grand_saver):
        stored = repr(vars(saver))
        assert one in stored or two in stored  # 检查点确实写了(编号版的参数)
        for url in (SIGNED, OTHER):
            for form in url_forms(url):
                assert form not in stored, form


# ---------------------------------------------------------------------------
# T16 —— 回滚兼容:不认识这个通道的图读写一轮,登记还在
# ---------------------------------------------------------------------------


_OLD_FIELDS = {
    name: hint
    for name, hint in get_type_hints(AgentState, include_extras=True).items()
    if name != "conversation_links"
}
OldAgentState = TypedDict("OldAgentState", _OLD_FIELDS, total=False)  # type: ignore[misc]


async def _rollback_roundtrip(dsn: str | None) -> None:
    client = EchoMCPClient()
    llm = ScriptedLLM([AIMessage(content="收到")])
    async with opened(llm, mcp_registry(client), dsn=dsn) as conv:
        await conv.turn("t1", inputs={"attachment_urls": [SIGNED]})

        async def _old_agent(state: Any) -> dict[str, Any]:
            del state
            return {"messages": [AIMessage(content="old build")]}

        old: StateGraph[Any] = StateGraph(OldAgentState)
        old.add_node("agent", _old_agent)
        old.add_edge(START, "agent")
        old.add_edge("agent", END)
        old_compiled = old.compile(checkpointer=conv.cp)
        cfg = conv.config(None)
        await old_compiled.ainvoke({"messages": [HumanMessage(content="旧版本这一轮")]}, config=cfg)
        assert "conversation_links" not in (await old_compiled.aget_state(cfg)).values

        llm.responses += [form_call(first_id(conv)), AIMessage(content="ok")]
        state = await conv.turn("回到新版本")

    assert [e["url"] for e in state["conversation_links"]] == [SIGNED]
    assert client.calls[0]["fields"][0]["value"] == SIGNED


async def test_an_older_graph_without_the_channel_keeps_the_registry_intact() -> None:
    await _rollback_roundtrip(None)


@pytest.mark.integration
async def test_an_older_graph_keeps_the_registry_with_the_postgres_saver(
    postgres_container: Any,
) -> None:
    """同上,用生产用的 Postgres 检查点保存器(``make_checkpointer("postgres")``)。"""
    await _rollback_roundtrip(str(postgres_container.get_connection_url()).replace("+psycopg2", ""))
