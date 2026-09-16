"""B-61 Task 7 —— 绑定的接线:模型看不到被绑参数、填值发生在审批之前。

夹具就地写在本文件里:``services/orchestrator/tests/`` 下没有 conftest.py,
这批夹具也只有本模块用得到,建一个目录级 conftest 会把它们摊给另外 174 个
测试文件。MCP 那侧用仓库自带的 ``RecordingMCPClient``(``orchestrator.tools``
导出的测试替身),不另造一套假客户端。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import ArgBindingSpec, AuditEntry, MCPToolSpec
from expert_work.runtime.audit.fallback import InMemoryAuditFallbackQueue
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.audit.redactor import DefaultSecretRedactor
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import (
    ActionVerdict,
    GraphRunner,
    ToolRegistry,
    build_react_graph,
)
from orchestrator.graph_builder._config import AUDIT_LOGGER_KEY
from orchestrator.sse import PROMPT_INPUTS_KEY
from orchestrator.tools import (
    MCPServerPool,
    MCPToolDef,
    RecordingMCPClient,
    ToolEnv,
    build_tool_registry,
    register_mcp_tools,
)

pytestmark = pytest.mark.asyncio

_SERVER = "deepcare"
_WIRE = "mcp__deepcare__t1"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _tool_def(name: str, input_schema: Mapping[str, Any]) -> MCPToolDef:
    return MCPToolDef(name=name, description=f"{name} description", input_schema=input_schema)


async def _build_registry(
    *,
    tools: Sequence[Mapping[str, Any]],
    arg_bindings: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[ToolRegistry, RecordingMCPClient]:
    """按真正的注册路径建目录 —— wire 名折叠、schema 剥离都走生产代码。"""
    defs = tuple(_tool_def(t["name"], t["input_schema"]) for t in tools)
    client = RecordingMCPClient(
        tools=defs,
        responses={d.name: f"result of {d.name}" for d in defs},
    )
    registry = ToolRegistry()
    await register_mcp_tools(
        server_name=_SERVER,
        client=client,
        registry=registry,
        arg_bindings=arg_bindings,
    )
    return registry, client


@pytest.fixture
def mcp_registry_factory():  # type: ignore[no-untyped-def]
    return _build_registry


@dataclass
class _ScriptedLLM:
    """先发一次工具调用,再收尾 —— 与 test_action_screen_wiring 同一写法。"""

    responses: list[AIMessage]
    calls: int = field(default=0)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[object]
    ) -> AIMessage:
        del messages, tools
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[idx]


@dataclass
class _RecordingJudge:
    """记录每次被判的 ``tool_args``(action screening 看到的那一份)。"""

    aligned: bool
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def judge_action(
        self, *, user_request: str, tool_name: str, tool_args: Mapping[str, Any]
    ) -> ActionVerdict:
        del user_request
        self.calls.append({"tool_name": tool_name, "tool_args": dict(tool_args)})
        return ActionVerdict(aligned=self.aligned, reason="test")


class _RecordingAuditStore(InMemoryAuditLogStore):
    """真 AuditLogger + 真脱敏器,只是把落库的行留一份给断言。"""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[AuditEntry] = []

    async def append(self, entry: AuditEntry) -> AuditEntry:
        stamped = await super().append(entry)
        self.rows.append(stamped)
        return stamped


@dataclass
class _Harness:
    registry: ToolRegistry
    client: RecordingMCPClient
    audit_store: _RecordingAuditStore
    judge: _RecordingJudge | None
    prompt_inputs: Mapping[str, Any]
    approval_required_tools: frozenset[str]
    action_screen: Literal["off", "block", "approval"]

    @property
    def audit_rows(self) -> list[AuditEntry]:
        return self.audit_store.rows

    @property
    def judge_calls(self) -> list[dict[str, Any]]:
        assert self.judge is not None, "harness built without a judge"
        return self.judge.calls

    async def run_turn_with_tool_call(self, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        llm = _ScriptedLLM(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": name, "args": dict(args), "id": "tc-1", "type": "tool_call"}
                    ],
                ),
                AIMessage(content="done"),
            ]
        )
        audit_logger = AuditLogger(
            self.audit_store, DefaultSecretRedactor(), InMemoryAuditFallbackQueue()
        )
        async with make_checkpointer("memory") as cp:
            runner = GraphRunner(checkpointer=cp)
            compiled = runner.compile(
                build_react_graph(
                    llm_caller=llm,
                    tool_registry=self.registry,
                    approval_required_tools=self.approval_required_tools,
                    action_judge=self.judge,  # type: ignore[arg-type]
                    action_screen=self.action_screen,
                )
            )
            cfg: RunnableConfig = {
                "configurable": {
                    "thread_id": str(uuid4()),
                    "run_id": str(uuid4()),
                    "tenant_id": str(uuid4()),
                    AUDIT_LOGGER_KEY: audit_logger,
                    PROMPT_INPUTS_KEY: dict(self.prompt_inputs),
                }
            }
            return await compiled.ainvoke(
                {
                    "messages": [HumanMessage(content="查一下")],
                    "step_count": 0,
                    "max_steps": 5,
                },
                config=cfg,
            )


@pytest.fixture
def graph_harness():  # type: ignore[no-untyped-def]
    async def _make(
        *,
        bindings: Mapping[str, Mapping[str, str]],
        prompt_inputs: Mapping[str, Any],
        approval_required_tools: frozenset[str] | set[str] = frozenset(),
        action_screen: Literal["off", "block", "approval"] = "off",
        tool_schema: Mapping[str, Any] | None = None,
    ) -> _Harness:
        # ``bindings`` 用 wire 名(与测试正文一致),注册那一侧收的是**裸**名。
        bare = {
            name.removeprefix(f"mcp__{_SERVER}__"): dict(bound) for name, bound in bindings.items()
        }
        schema = tool_schema or {
            "type": "object",
            "properties": {"project_code": {"type": "string"}, "keyword": {"type": "string"}},
            "required": ["project_code", "keyword"],
        }
        registry, client = await _build_registry(
            tools=[{"name": "t1", "input_schema": schema}],
            arg_bindings=bare,
        )
        judge = _RecordingJudge(aligned=action_screen == "off") if action_screen != "off" else None
        return _Harness(
            registry=registry,
            client=client,
            audit_store=_RecordingAuditStore(),
            judge=judge,
            prompt_inputs=prompt_inputs,
            approval_required_tools=frozenset(approval_required_tools),
            action_screen=action_screen,
        )

    return _make


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


async def test_the_model_never_sees_a_bound_parameter(mcp_registry_factory) -> None:
    """建完目录后,tool catalog 里那个参数连同 required 一起消失。"""
    registry, _ = await mcp_registry_factory(
        tools=[
            {
                "name": "t1",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_code": {"type": "string"},
                        "keyword": {"type": "string"},
                    },
                    "required": ["project_code", "keyword"],
                },
            }
        ],
        arg_bindings={"t1": {"project_code": "pc"}},
    )
    schema = registry.get_required(_WIRE).spec.parameters
    assert set(schema["properties"]) == {"keyword"}
    assert schema["required"] == ["keyword"]


async def test_bound_args_are_filled_before_the_approval_gate(graph_harness) -> None:
    """审批请求里带的是**真值** —— 填值在 tools_node 最前面,不在 dispatch 前那层。"""
    harness = await graph_harness(
        approval_required_tools={_WIRE},
        bindings={_WIRE: {"project_code": "pc"}},
        prompt_inputs={"pc": "PRJ001"},
    )
    update = await harness.run_turn_with_tool_call(_WIRE, {"keyword": "王"})
    approval = update["pending_approval"]
    assert approval.proposed_args.get("project_code") == "PRJ001"
    # 门真的拦住了:工具没跑。否则「审批看到真值」就可能是 dispatch 之后补的。
    assert harness.client.calls == []


async def test_bound_args_are_filled_before_action_screening(graph_harness) -> None:
    harness = await graph_harness(
        action_screen="block",
        bindings={_WIRE: {"project_code": "pc"}},
        prompt_inputs={"pc": "PRJ001"},
    )
    await harness.run_turn_with_tool_call(_WIRE, {"keyword": "王"})
    judged = harness.judge_calls[-1]
    assert judged["tool_args"].get("project_code") == "PRJ001"


async def test_audit_records_which_params_the_platform_filled(graph_harness) -> None:
    harness = await graph_harness(
        bindings={_WIRE: {"project_code": "pc"}},
        prompt_inputs={"pc": "PRJ001"},
    )
    await harness.run_turn_with_tool_call(_WIRE, {"keyword": "王"})
    row = harness.audit_rows[-1]
    assert row.details["bound_args"] == ["project_code"]
    assert "PRJ001" not in str(row.details), "只记参数名,不记值"


async def test_a_binding_whose_param_is_absent_from_the_schema_warns_but_runs(
    mcp_registry_factory, caplog
) -> None:
    caplog.set_level(logging.WARNING, logger="orchestrator.tools.mcp")
    registry, _ = await mcp_registry_factory(
        tools=[{"name": "t1", "input_schema": {"type": "object", "properties": {"keyword": {}}}}],
        arg_bindings={"t1": {"gone": "pc"}},
    )
    assert registry.get_required(_WIRE) is not None
    assert "mcp.binding_param_absent" in caplog.text


async def test_a_param_absent_from_the_schema_is_never_injected_into_the_call(
    graph_harness,
) -> None:
    """追加要求之二 —— 上游改了接口:这条绑定按未命中处理,args 里不得出现该参数。

    ``additionalProperties: false`` 的服务端会把多出来的参数当硬错拒掉,
    那比「降级成模型自己填」更糟。
    """
    harness = await graph_harness(
        bindings={_WIRE: {"project_code": "pc"}},
        prompt_inputs={"pc": "PRJ001"},
        tool_schema={
            "type": "object",
            "properties": {"keyword": {"type": "string"}},
            "required": ["keyword"],
            "additionalProperties": False,
        },
    )
    await harness.run_turn_with_tool_call(_WIRE, {"keyword": "王"})
    assert harness.client.calls, "工具应当照跑 —— 这条绑定不阻断 run"
    _, sent_args = harness.client.calls[-1]
    assert "project_code" not in sent_args
    assert sent_args["keyword"] == "王"


# ---------------------------------------------------------------------------
# §5.4 —— 「一个工具都没匹配上」要一路走到保存时的试建
# ---------------------------------------------------------------------------


async def _pool_with_t1() -> MCPServerPool:
    pool = MCPServerPool()
    await pool.add(
        _SERVER,
        RecordingMCPClient(
            tools=(
                MCPToolDef(
                    name="t1",
                    description="",
                    input_schema={"type": "object", "properties": {"project_code": {}}},
                ),
            )
        ),
    )
    return pool


async def test_a_binding_for_an_unadvertised_tool_is_reported_as_unmatched(
    mcp_registry_factory,
) -> None:
    """工具名写错 → 绑定落空。目录照建(其它工具不受连累),但得留下账。"""
    registry, _ = await mcp_registry_factory(
        tools=[{"name": "t1", "input_schema": {"type": "object", "properties": {"keyword": {}}}}],
        arg_bindings={"t9": {"project_code": "pc"}},
    )
    assert registry.get(_WIRE) is not None
    assert registry.unmatched_arg_bindings() == ((_SERVER, "t9"),)


async def test_a_binding_for_a_server_that_never_registered_is_reported_as_unmatched() -> None:
    """服务器名写错(或对方连不上)→ ``register_mcp_tools`` 根本没为它跑过一次。"""
    registry = await build_tool_registry(
        [
            MCPToolSpec(
                arg_bindings=[
                    ArgBindingSpec(server="nosuchserver", tool="t1", args={"project_code": "pc"})
                ]
            )
        ],
        tool_env=ToolEnv(mcp_pool=await _pool_with_t1()),
    )
    assert registry.unmatched_arg_bindings() == (("nosuchserver", "t1"),)


async def test_a_binding_that_matches_reports_nothing() -> None:
    """反向:这份清单不能是恒有的装饰,否则保存时的告警什么都没说。"""
    registry = await build_tool_registry(
        [
            MCPToolSpec(
                arg_bindings=[
                    ArgBindingSpec(server=_SERVER, tool="t1", args={"project_code": "pc"})
                ]
            )
        ],
        tool_env=ToolEnv(mcp_pool=await _pool_with_t1()),
    )
    assert registry.unmatched_arg_bindings() == ()
    assert registry.arg_bindings() == {_WIRE: {"project_code": "pc"}}
