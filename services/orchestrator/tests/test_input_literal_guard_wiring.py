"""B-317 —— 字面量守卫接线:命中的那条不派发,同批其它照常;一行 tool:blocked 审计只记名字。"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from prometheus_client import REGISTRY

from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import (
    AgentState,
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_agent,
    build_react_graph,
    pending_request_binding,
)
from orchestrator.graph_builder._config import AUDIT_LOGGER_KEY
from orchestrator.sse import PROMPT_INPUTS_KEY
from orchestrator.tools.arg_bindings import ToolArgBindings

from .test_input_url_guard_wiring import (
    _KEY_NAME,
    _jinja_spec,
    _key_resolver,
    _RecordingAuditLogger,
    _ScriptedLLM,
    _tc,
    _tool_messages,
)

pytestmark = pytest.mark.asyncio

MCP = "mcp__shenhu__form_save"
SECRET_URL = "https://oss.example.com/a/lab-1.jpg?Signature=QWERTYUIOP"


@dataclass
class _Remote:
    name: str = MCP
    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description="d", is_read_only=False)

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del ctx
        self.calls.append(dict(args))
        return ToolResult(content="saved")


def _registry(tool: _Remote) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(tool)
    registry.bind_tool_args(
        MCP, ToolArgBindings(args={"attachments": "attachment_urls", "owner_code": "customer_code"})
    )
    return registry


def _config(inputs: Mapping[str, Any], audit: Any, **extra: Any) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": str(uuid4()),
            "tenant_id": str(uuid4()),
            "run_id": str(uuid4()),
            PROMPT_INPUTS_KEY: dict(inputs),
            AUDIT_LOGGER_KEY: audit,
            "agent_key": "health-aaaaaaaa",
            **extra,
        }
    }


async def _run(
    llm: _ScriptedLLM,
    registry: ToolRegistry,
    *,
    inputs: Mapping[str, Any],
    audit: Any,
    declared: frozenset[str] = frozenset(),
    **extra: Any,
) -> AgentState:
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=registry, declared_input_names=declared)
        )
        return await compiled.ainvoke(
            {"messages": [HumanMessage(content="start")], "step_count": 0, "max_steps": 5},
            config=_config(inputs, audit, **extra),
        )


_INPUTS = {"attachment_urls": [SECRET_URL], "customer_code": "C-001"}
_DECLARED = frozenset({"attachment_urls", "customer_code", "is_first_contact"})


async def test_a_literal_variable_in_an_mcp_call_is_blocked_while_its_sibling_runs() -> None:
    tool = _Remote()
    audit = _RecordingAuditLogger()
    llm = _ScriptedLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tc(MCP, {"summary": "附件 $attachment_urls"}, "tc-bad"),
                    _tc(MCP, {"summary": "血糖 9.1"}, "tc-ok"),
                ],
            ),
            AIMessage(content="done"),
        ]
    )

    state = await _run(llm, _registry(tool), inputs=_INPUTS, audit=audit, declared=_DECLARED)

    assert [c["summary"] for c in tool.calls] == ["血糖 9.1"]
    # 那条照常派发的调用,平台绑定照填
    assert tool.calls[0]["attachments"] == [SECRET_URL]
    msgs = _tool_messages(state)
    assert msgs["tc-bad"].status == "error"
    assert msgs["tc-bad"].content.startswith("[blocked] 参数 summary 里的 $attachment_urls")
    assert "本工具的 attachments 参数" in msgs["tc-bad"].content
    assert msgs["tc-ok"].content == "saved"
    rows = [e for e in audit.entries if e.action.value == "tool:blocked"]
    assert len(rows) == 1
    row = rows[0]
    assert row.reason == "input_variable_literal"
    assert row.details["input_variable"] == "attachment_urls"
    assert row.details["argument"] == "summary"
    dumped = json.dumps(row.details, ensure_ascii=False)
    assert "QWERTYUIOP" not in dumped and "C-001" not in dumped


async def test_the_bound_value_itself_is_never_checked() -> None:
    """G6 —— 绑定填进去的值(租户数据)里出现 ``$customer_code`` 字样也不拦。"""
    tool = _Remote()
    llm = _ScriptedLLM(
        [
            AIMessage(content="", tool_calls=[_tc(MCP, {"summary": "ok"}, "tc-1")]),
            AIMessage(content="done"),
        ]
    )
    inputs = {"attachment_urls": ["$customer_code", "inputs/current/x.png"], "customer_code": "C"}
    await _run(
        llm, _registry(tool), inputs=inputs, audit=_RecordingAuditLogger(), declared=_DECLARED
    )
    assert len(tool.calls) == 1


async def test_a_local_path_handed_to_a_remote_tool_is_blocked() -> None:
    tool = _Remote()
    audit = _RecordingAuditLogger()
    llm = _ScriptedLLM(
        [
            AIMessage(
                content="", tool_calls=[_tc(MCP, {"pics": ["inputs/current/lab.jpg"]}, "tc-1")]
            ),
            AIMessage(content="done"),
        ]
    )
    state = await _run(llm, _registry(tool), inputs=_INPUTS, audit=audit, declared=_DECLARED)
    assert tool.calls == []
    assert "对方服务读不到沙箱里的文件" in _tool_messages(state)["tc-1"].content
    rows = [e for e in audit.entries if e.action.value == "tool:blocked"]
    assert [r.reason for r in rows] == ["input_local_path_to_remote"]


async def test_a_delegated_child_knows_the_parents_variables_from_its_inputs() -> None:
    """G9 —— 子代的图里没有声明(``declared`` 为空),变量名从 ``prompt_inputs`` 的键来。"""
    tool = _Remote()
    llm = _ScriptedLLM(
        [
            AIMessage(content="", tool_calls=[_tc(MCP, {"summary": "$customer_code"}, "tc-1")]),
            AIMessage(content="done"),
        ]
    )
    await _run(
        llm,
        _registry(tool),
        inputs=_INPUTS,
        audit=_RecordingAuditLogger(),
        child_run=True,
    )
    assert tool.calls == []


async def test_a_guard_failure_fails_open_and_logs_the_type_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """G11 —— 判定出任何意外:整批放行,只记异常类型。"""

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(SECRET_URL)

    monkeypatch.setattr("orchestrator.graph_builder.builder.find_literal", _boom)
    tool = _Remote()
    llm = _ScriptedLLM(
        [
            AIMessage(content="", tool_calls=[_tc(MCP, {"summary": "$customer_code"}, "tc-1")]),
            AIMessage(content="done"),
        ]
    )
    with caplog.at_level(logging.WARNING):
        await _run(
            llm, _registry(tool), inputs=_INPUTS, audit=_RecordingAuditLogger(), declared=_DECLARED
        )
    assert len(tool.calls) == 1
    assert "tools.literal_guard_skipped err=RuntimeError" in caplog.text
    assert "QWERTYUIOP" not in caplog.text


def _after_approval_count(tool: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "expert_work_tools_literal_guard_blocked_after_approval_total", labels={"tool": tool}
        )
        or 0.0
    )


@pytest.mark.parametrize("approved", [True, False])
async def test_an_approved_call_blocked_by_the_guard_is_counted(approved: bool) -> None:
    """G12 —— 守卫在审批门之后:人批准了,派发前仍拦下,并记一次「批准后被拦」。"""
    tool = _Remote()
    audit = _RecordingAuditLogger()
    llm = _ScriptedLLM(
        [
            AIMessage(content="", tool_calls=[_tc(MCP, {"summary": "$customer_code"}, "tc-1")]),
            AIMessage(content="done"),
        ]
    )
    label = "mcp:shenhu"
    before = _after_approval_count(label)
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=llm,
                tool_registry=_registry(tool),
                approval_required_tools=frozenset({MCP}) if approved else frozenset(),
                declared_input_names=_DECLARED,
            )
        )
        cfg = _config(_INPUTS, audit)
        state = await compiled.ainvoke(
            {"messages": [HumanMessage(content="start")], "step_count": 0, "max_steps": 5},
            config=cfg,
        )
        if approved:
            assert state.get("pending_approval") is not None
            binding = pending_request_binding((await compiled.aget_state(cfg)).values)
            assert binding is not None
            verdict = {
                "decision": "approve",
                "binding_digest": state["pending_approval"].binding_digest,
                **binding,
            }
            await compiled.aupdate_state(
                cfg, {"pending_approval": None, "approval_resume": verdict}, as_node="agent"
            )
            state = await compiled.ainvoke(None, config=cfg)

    assert tool.calls == []
    assert _tool_messages(state)["tc-1"].content.startswith("[blocked]")
    assert _after_approval_count(label) == before + (1 if approved else 0)


async def test_factory_hands_the_graph_every_declared_variable_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from expert_work.runtime.secret_store import LocalDevSecretStore

    captured: dict[str, Any] = {}

    def _spy(**kwargs: Any) -> Any:
        captured["declared_input_names"] = kwargs.get("declared_input_names")
        return build_react_graph(**kwargs)

    monkeypatch.setattr("orchestrator.agent_factory.build_react_graph", _spy)
    async with make_checkpointer("memory") as cp:
        await build_agent(
            _jinja_spec(),
            secret_store=LocalDevSecretStore.from_mapping({_KEY_NAME: "sk-ant-test"}),
            checkpointer=cp,
            provider_key_resolver=_key_resolver,
        )
    assert captured["declared_input_names"] == frozenset({"org_logo", "materials"})


async def test_without_inputs_a_remote_services_own_inputs_path_passes() -> None:
    """L1 —— 本轮没有输入时规则 3 不查:别的服务的 ``inputs/…`` 是它自己的路径。"""
    tool = _Remote()
    llm = _ScriptedLLM(
        [
            AIMessage(
                content="", tool_calls=[_tc(MCP, {"pics": ["inputs/current/lab.jpg"]}, "tc-1")]
            ),
            AIMessage(content="done"),
        ]
    )
    await _run(llm, _registry(tool), inputs={}, audit=_RecordingAuditLogger(), declared=_DECLARED)
    assert len(tool.calls) == 1


@dataclass
class _FileTool:
    name: str = "edit_file"
    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description="d", is_read_only=False)

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del ctx
        self.calls.append(dict(args))
        return ToolResult(content="edited")


async def test_a_blocked_file_call_stays_out_of_the_file_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """守卫拦下、没执行的 ``edit_file`` 不进 B-302 的文件账(与 ``ran_calls`` 同一个条件)。"""
    from orchestrator.graph_builder import builder

    seen: list[str] = []
    real = builder._file_tool_event

    def _spy(tool_call: dict[str, Any], tool_message: ToolMessage) -> Any:
        seen.append(str(tool_call.get("id")))
        return real(tool_call, tool_message)

    monkeypatch.setattr(builder, "_file_tool_event", _spy)
    tool = _FileTool()
    registry = ToolRegistry()
    registry.register(tool)
    llm = _ScriptedLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tc("edit_file", {"path": "$customer_code", "old_string": "a"}, "tc-bad"),
                    _tc("edit_file", {"path": "notes.md", "old_string": "a"}, "tc-ok"),
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    await _run(llm, registry, inputs=_INPUTS, audit=_RecordingAuditLogger(), declared=_DECLARED)
    assert [c["path"] for c in tool.calls] == ["notes.md"]
    assert seen == ["tc-ok"]
