"""B-67 §七 —— 守卫接线:命中的那条不派发,同批其它照常;一行 tool:blocked 审计,不记代码。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from expert_work.protocol import AgentSpec, AuditEntry
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator import (
    AgentState,
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_agent,
    build_react_graph,
)
from orchestrator.graph_builder._config import AUDIT_LOGGER_KEY
from orchestrator.sse import PROMPT_INPUTS_KEY

pytestmark = pytest.mark.asyncio

LOGO = "https://files.example.com/brand/cover-1726394851207.png"
RETYPED = LOGO.replace("1726394851207", "17263948512077")
READS_MANIFEST = "import os; print(open(os.environ['EXPERT_WORK_INPUTS']).read())"
TRUSTED_LOGO = frozenset({"org_logo"})


class _RecordingAuditLogger(AuditLogger):
    """走图的路径上 ``audit_logger_from_config`` 按 ``isinstance(AuditLogger)`` 取 sink,
    鸭子类型的假对象会被当成「没接审计」—— 所以继承,只覆盖 ``write``,记下**脱敏前**的
    原始条目(断言「不记 URL」对脱敏前的条目才不会被脱敏器掩盖)。"""

    def __init__(self) -> None:  # 不调 super():不需要 store / redactor / fallback
        self.entries: list[AuditEntry] = []

    async def write(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


@dataclass
class _CodeTool:
    """假 exec_python / bash:记下每次真正派发到的代码。"""

    name: str
    arg: str
    calls: list[str] = field(default_factory=list)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description="d", is_read_only=False)

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del ctx
        self.calls.append(str(args[self.arg]))
        return ToolResult(content="ran")


@dataclass
class _UrlTool:
    """非沙箱工具,参数里带 URL —— 守卫不该看它。"""

    calls: list[str] = field(default_factory=list)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="fetch", description="d", is_read_only=True)

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del ctx
        self.calls.append(str(args["url"]))
        return ToolResult(content="fetched")


def _tc(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


@dataclass
class _ScriptedLLM:
    responses: list[AIMessage]
    calls: int = 0

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        idx = self.calls
        self.calls += 1
        return self.responses[idx]


async def _run(
    llm: _ScriptedLLM,
    registry: ToolRegistry,
    *,
    inputs: dict[str, Any],
    audit: Any,
    trusted: frozenset[str] = frozenset(),
    extra_configurable: Mapping[str, Any] | None = None,
) -> AgentState:
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=registry, trusted_input_names=trusted)
        )
        cfg: RunnableConfig = {
            "configurable": {
                "thread_id": str(uuid4()),
                "tenant_id": str(uuid4()),
                "run_id": str(uuid4()),
                PROMPT_INPUTS_KEY: inputs,
                AUDIT_LOGGER_KEY: audit,
                **(extra_configurable or {}),
            }
        }
        return await compiled.ainvoke(
            {"messages": [HumanMessage(content="start")], "step_count": 0, "max_steps": 5},
            config=cfg,
        )


def _tool_messages(state: AgentState) -> dict[str, ToolMessage]:
    return {m.tool_call_id: m for m in state["messages"] if isinstance(m, ToolMessage)}


def _one_exec_python_call(code: str) -> tuple[_CodeTool, ToolRegistry, _ScriptedLLM]:
    tool = _CodeTool(name="exec_python", arg="code")
    registry = ToolRegistry()
    registry.register(tool)
    llm = _ScriptedLLM(
        [
            AIMessage(content="", tool_calls=[_tc("exec_python", {"code": code}, "tc-1")]),
            AIMessage(content="done"),
        ]
    )
    return tool, registry, llm


async def test_retyped_url_call_is_blocked_while_its_sibling_runs() -> None:
    tool = _CodeTool(name="exec_python", arg="code")
    registry = ToolRegistry()
    registry.register(tool)
    audit = _RecordingAuditLogger()
    llm = _ScriptedLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tc("exec_python", {"code": f"urlretrieve('{RETYPED}', 'l.png')"}, "tc-bad"),
                    _tc("exec_python", {"code": READS_MANIFEST}, "tc-ok"),
                ],
            ),
            AIMessage(content="done"),
        ]
    )

    state = await _run(llm, registry, inputs={"org_logo": LOGO}, audit=audit, trusted=TRUSTED_LOGO)

    assert tool.calls == [READS_MANIFEST]  # 只派发了没抄的那条
    msgs = _tool_messages(state)
    assert msgs["tc-bad"].status == "error"
    assert msgs["tc-bad"].content.startswith("[blocked]")
    assert "$EXPERT_WORK_INPUTS_DIR/org_logo.png" in msgs["tc-bad"].content
    assert msgs["tc-ok"].content == "ran"
    rows = [e for e in audit.entries if e.action.value == "tool:blocked"]
    assert len(rows) == 1
    row = rows[0]
    assert row.result.value == "denied"
    assert row.reason == "input_url_retyped"
    assert row.resource_id == "exec_python"
    assert row.details["input_variable"] == "org_logo"
    assert row.details["edit_distance"] == 1
    assert "code" not in row.details and "code_sha256" not in row.details
    dumped = json.dumps(row.details)
    assert "1726394851207" not in dumped and "17263948512077" not in dumped
    assert row.details["arg_keys"] == []
    # 没抄的那条照常一行 tool:call
    assert [e.action.value for e in audit.entries].count("tool:call") == 1


async def test_bash_command_is_guarded_too_and_exact_copy_is_blocked() -> None:
    tool = _CodeTool(name="bash", arg="command")
    registry = ToolRegistry()
    registry.register(tool)
    audit = _RecordingAuditLogger()
    llm = _ScriptedLLM(
        [
            AIMessage(content="", tool_calls=[_tc("bash", {"command": f"curl -O {LOGO}"}, "tc-1")]),
            AIMessage(content="done"),
        ]
    )
    state = await _run(llm, registry, inputs={"org_logo": LOGO}, audit=audit)
    assert tool.calls == []
    assert "与输入一致" in _tool_messages(state)["tc-1"].content
    assert [e.reason for e in audit.entries if e.action.value == "tool:blocked"] == [
        "input_url_retyped"
    ]


async def test_without_inputs_nothing_is_guarded() -> None:
    tool, registry, llm = _one_exec_python_call(f"get('{RETYPED}')")
    audit = _RecordingAuditLogger()
    await _run(llm, registry, inputs={}, audit=audit)
    assert tool.calls == [f"get('{RETYPED}')"]
    assert not [e for e in audit.entries if e.action.value == "tool:blocked"]


async def test_non_sandbox_tool_args_are_not_guarded() -> None:
    """MCP / http 参数走绑定面,不走守卫(spec §7.2)。"""
    tool = _UrlTool()
    registry = ToolRegistry()
    registry.register(tool)
    audit = _RecordingAuditLogger()
    llm = _ScriptedLLM(
        [
            AIMessage(content="", tool_calls=[_tc("fetch", {"url": RETYPED}, "tc-1")]),
            AIMessage(content="done"),
        ]
    )
    await _run(llm, registry, inputs={"org_logo": LOGO}, audit=audit)
    assert tool.calls == [RETYPED]
    assert not [e for e in audit.entries if e.action.value == "tool:blocked"]


# --- 信任口径:这条合成消息不过 spotlight 围栏(只有真工具输出过),链接名里的列表项
# 说明 / dict 键、模型抄的那串都带租户数据 —— 只有声明为 trusted 的变量才写出来。

MATERIAL_URL = "https://files.example.com/m/clip-1726394851207.mp4"
MATERIALS = [{"description": "示范视频", "url": MATERIAL_URL}]
MATERIAL_RETYPED = MATERIAL_URL.replace("1726394851207", "1726394851208")


async def test_untrusted_variable_message_names_neither_the_link_nor_the_written_url() -> None:
    tool, registry, llm = _one_exec_python_call(f"get('{MATERIAL_RETYPED}')")
    audit = _RecordingAuditLogger()
    state = await _run(llm, registry, inputs={"materials": MATERIALS}, audit=audit)

    assert tool.calls == []
    content = _tool_messages(state)["tc-1"].content
    assert content.startswith("[blocked]")
    assert "materials" in content
    assert "疑似抄错 1 处" in content
    assert "$EXPERT_WORK_INPUTS_DIR 下" in content
    assert "$EXPERT_WORK_INPUTS 清单" in content
    assert "示范视频" not in content
    assert "materials/0" not in content
    assert "1726394851208" not in content and "files.example.com" not in content


async def test_trusted_variable_message_names_the_link() -> None:
    """同一形态,声明为 trusted:链接名(含列表项说明)与模型写的那串照常写出。"""
    tool, registry, llm = _one_exec_python_call(f"get('{MATERIAL_RETYPED}')")
    audit = _RecordingAuditLogger()
    state = await _run(
        llm,
        registry,
        inputs={"materials": MATERIALS},
        audit=audit,
        trusted=frozenset({"materials"}),
    )

    assert tool.calls == []
    content = _tool_messages(state)["tc-1"].content
    assert "$EXPERT_WORK_INPUTS_DIR/materials/0-示范视频.mp4" in content
    assert MATERIAL_RETYPED in content


async def test_inherited_inputs_in_a_child_run_are_treated_as_untrusted() -> None:
    """子代(config 带 ``inputs_run_id``)的 inputs 是父 run 的,本图的声明管不到它们。"""
    tool, registry, llm = _one_exec_python_call(f"get('{RETYPED}')")
    audit = _RecordingAuditLogger()
    state = await _run(
        llm,
        registry,
        inputs={"org_logo": LOGO},
        audit=audit,
        trusted=TRUSTED_LOGO,
        extra_configurable={"inputs_run_id": str(uuid4())},
    )

    assert tool.calls == []
    content = _tool_messages(state)["tc-1"].content
    assert content.startswith("[blocked]")
    assert "org_logo.png" not in content
    assert RETYPED not in content


# --- 构建层:agent_factory 只把声明为 trusted 的变量名交给图。

_KEY_NAME = "expert-work/dev/llm/anthropic"


def _jinja_spec() -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": "test-agent", "version": "1.0.0", "tenant": "platform-eng"},
            "spec": {
                "tenant_config": {},
                "model": {
                    "provider": "anthropic",
                    "name": "claude-sonnet-4-6",
                    "api_key_ref": f"secret://{_KEY_NAME}",
                },
                "system_prompt": {
                    "template": "{{ org_logo }} {{ materials }}",
                    "jinja": True,
                    "variables": [
                        {"name": "org_logo"},
                        {"name": "materials", "trusted": False},
                    ],
                },
                "sandbox": {
                    "resources": {"cpu": "1.0", "memory": "1Gi"},
                    "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
                    "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
                },
            },
        }
    )


async def _key_resolver(provider: str) -> list[str]:
    del provider
    return [f"secret://{_KEY_NAME}"]


async def test_factory_hands_the_graph_only_trusted_variable_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _spy(**kwargs: Any) -> Any:
        captured["trusted_input_names"] = kwargs.get("trusted_input_names")
        return build_react_graph(**kwargs)

    monkeypatch.setattr("orchestrator.agent_factory.build_react_graph", _spy)
    async with make_checkpointer("memory") as cp:
        await build_agent(
            _jinja_spec(),
            secret_store=LocalDevSecretStore.from_mapping({_KEY_NAME: "sk-ant-test"}),
            checkpointer=cp,
            provider_key_resolver=_key_resolver,
        )
    assert captured["trusted_input_names"] == frozenset({"org_logo"})
