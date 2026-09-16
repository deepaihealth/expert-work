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
from expert_work.protocol import (
    ArgBindingSpec,
    AuditEntry,
    MCPToolSpec,
    canonical_args_digest,
)
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
from orchestrator.graph_builder.builder import _build_tool_context
from orchestrator.sse import PROMPT_INPUTS_KEY
from orchestrator.tools import (
    MCPServerPool,
    MCPToolDef,
    RecordingMCPClient,
    ToolEnv,
    build_tool_registry,
)
from orchestrator.tools._child_run import _child_config
from orchestrator.tools.registry import UnmatchedArgBinding

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
    """走**整条装配路径** —— wire 名折叠、剥 schema、落地判定全是生产代码。

    不直接调 ``register_mcp_tools``:「这条绑定落空了没有」自复评 N-1 起就不在单次
    注册里判了(单次注册答不了 —— 兄弟条目的 ``allow_tools`` 会把别人绑的工具挡在
    它那一遍之外)。绕过 ``build_tool_registry`` 的夹具于是验不到真正的判定点。
    """
    defs = tuple(_tool_def(t["name"], t["input_schema"]) for t in tools)
    client = RecordingMCPClient(
        tools=defs,
        responses={d.name: f"result of {d.name}" for d in defs},
    )
    pool = MCPServerPool()
    await pool.add(_SERVER, client)
    entry = MCPToolSpec(
        servers=[_SERVER],
        arg_bindings=[
            ArgBindingSpec(server=_SERVER, tool=tool, args=dict(args))
            for tool, args in (arg_bindings or {}).items()
        ],
    )
    registry = await build_tool_registry([entry], tool_env=ToolEnv(mcp_pool=pool))
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

    def _compiled(self, name: str, args: Mapping[str, Any], cp: Any) -> Any:
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
        return GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=llm,
                tool_registry=self.registry,
                approval_required_tools=self.approval_required_tools,
                action_judge=self.judge,  # type: ignore[arg-type]
                action_screen=self.action_screen,
            )
        )

    def _config(self) -> RunnableConfig:
        audit_logger = AuditLogger(
            self.audit_store, DefaultSecretRedactor(), InMemoryAuditFallbackQueue()
        )
        return {
            "configurable": {
                "thread_id": str(uuid4()),
                "run_id": str(uuid4()),
                "tenant_id": str(uuid4()),
                AUDIT_LOGGER_KEY: audit_logger,
                PROMPT_INPUTS_KEY: dict(self.prompt_inputs),
            }
        }

    @staticmethod
    def _seed() -> dict[str, Any]:
        return {"messages": [HumanMessage(content="查一下")], "step_count": 0, "max_steps": 5}

    async def run_turn_with_tool_call(
        self, name: str, args: Mapping[str, Any], *, config: RunnableConfig | None = None
    ) -> dict[str, Any]:
        async with make_checkpointer("memory") as cp:
            compiled = self._compiled(name, args, cp)
            return await compiled.ainvoke(self._seed(), config=config or self._config())

    async def resume_and_continue(
        self,
        name: str,
        args: Mapping[str, Any],
        *,
        modified_args: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """跑到审批暂停,照审批端点的做法给出裁决,再续跑。

        ``modified_args`` 非空 = 走 ``modify`` 档(人在审批面上改写了 args),
        摘要按改写后的那份重算 —— 与审批端点的做法一致(它把裁决与摘要原子地
        一起写下)。
        """
        async with make_checkpointer("memory") as cp:
            compiled = self._compiled(name, args, cp)
            cfg = self._config()
            paused = await compiled.ainvoke(self._seed(), config=cfg)
            request = paused["pending_approval"]
            if modified_args is None:
                resume: dict[str, Any] = {
                    "decision": "approve",
                    "binding_digest": request.binding_digest,
                }
            else:
                resume = {
                    "decision": "modify",
                    "modified_args": dict(modified_args),
                    "binding_digest": canonical_args_digest(dict(modified_args)),
                }
            await compiled.aupdate_state(
                cfg,
                {"pending_approval": None, "approval_resume": resume},
                as_node="agent",
            )
            return await compiled.ainvoke(None, config=cfg)


@pytest.fixture
def graph_harness():  # type: ignore[no-untyped-def]
    async def _make(
        *,
        bindings: Mapping[str, Mapping[str, str]],
        prompt_inputs: Mapping[str, Any],
        approval_required_tools: frozenset[str] | set[str] = frozenset(),
        action_screen: Literal["off", "block", "approval"] = "off",
        tool_schema: Mapping[str, Any] | None = None,
        extra_bindings: Mapping[str, Mapping[str, str]] | None = None,
    ) -> _Harness:
        # ``bindings`` 用 wire 名(与测试正文一致),注册那一侧收的是**裸**名。
        # ``extra_bindings`` 已经是裸名,用来造「这个工具服务器没有」的落空项。
        bare = {
            name.removeprefix(f"mcp__{_SERVER}__"): dict(bound) for name, bound in bindings.items()
        }
        bare.update({name: dict(bound) for name, bound in (extra_bindings or {}).items()})
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

    夹具**故意不带** ``additionalProperties: false``(复评 m-3):带上的话,多注入
    的参数会先被平台自己的入参校验拦下,这条用例就红在「工具压根没跑」上 —— 说的
    是另一件事。要钉住的是「服务端收到的 args 里没有这个参数」,所以让调用一路跑到
    ``RecordingMCPClient``,断言落在它真正收到的那份 args 上。
    ``additionalProperties: false`` 自己那条路由 C-1 那条用例覆盖。
    """
    harness = await graph_harness(
        bindings={_WIRE: {"project_code": "pc"}},
        prompt_inputs={"pc": "PRJ001"},
        tool_schema={
            "type": "object",
            "properties": {"keyword": {"type": "string"}},
            "required": ["keyword"],
        },
    )
    await harness.run_turn_with_tool_call(_WIRE, {"keyword": "王"})
    assert harness.client.calls, "工具应当照跑 —— 这条绑定不阻断 run"
    _, sent_args = harness.client.calls[-1]
    assert "project_code" not in sent_args
    assert sent_args["keyword"] == "王"


async def test_a_bound_param_dispatches_on_a_strict_additional_properties_tool(
    graph_harness,
) -> None:
    """C-1 —— 被绑参数**在** schema 里、且 ``additionalProperties: false``。

    剥 schema 是为了让**模型**看不见这个参数;服务端那边什么都没变 —— 它照旧声明
    这个参数、照旧把它列进 ``required``,而 ``additionalProperties: false``
    (zod / MCP TS SDK 默认、pydantic ``extra="forbid"``)会把任何它不认识的属性判
    成非法。dispatch 前的入参校验要是对着**剥过的**那份 schema 判,平台自己刚注入的
    那个参数就成了「多余属性」,一条**配置完全正确**的绑定于是一次也发不出去,
    而保存时的闸对此一无所知(``unmatched_arg_bindings`` 是空的)。
    """
    harness = await graph_harness(
        bindings={_WIRE: {"project_code": "pc"}},
        prompt_inputs={"pc": "PRJ001"},
        tool_schema={
            "type": "object",
            "properties": {
                "project_code": {"type": "string"},
                "keyword": {"type": "string"},
            },
            "required": ["project_code", "keyword"],
            "additionalProperties": False,
        },
    )
    await harness.run_turn_with_tool_call(_WIRE, {"keyword": "王"})
    assert harness.client.calls, "调用必须真的发出去 —— 绑定配得完全正确"
    _, sent_args = harness.client.calls[-1]
    assert sent_args["project_code"] == "PRJ001"
    assert sent_args["keyword"] == "王"
    # 模型那一侧仍然看不见它 —— 修 C-1 不能靠「别剥了」。
    model_schema = harness.registry.get_required(_WIRE).spec.parameters
    assert "project_code" not in model_schema["properties"]


async def test_the_platform_validator_still_judges_the_value_it_injected(
    graph_harness,
) -> None:
    """C-1 的反面:对着服务端合同判,不等于把被绑参数从校验里豁免掉。

    平台注入的值如果不合服务端声明的类型,照样要在 dispatch 前被拦下 —— 豁免的话
    这个值就没人看着了。
    """
    harness = await graph_harness(
        bindings={_WIRE: {"project_code": "pc"}},
        prompt_inputs={"pc": 12345},  # 声明的是 string
        tool_schema={
            "type": "object",
            "properties": {
                "project_code": {"type": "string"},
                "keyword": {"type": "string"},
            },
            "required": ["project_code", "keyword"],
            "additionalProperties": False,
        },
    )
    await harness.run_turn_with_tool_call(_WIRE, {"keyword": "王"})
    assert harness.client.calls == [], "类型不对的值不该发给服务端"


async def test_approving_a_paused_call_dispatches_the_bound_value(graph_harness) -> None:
    """续跑那一遍也必须重填 —— 这是填在 ``tools_node`` **最前面**的第二个理由。

    填值不落进检查点:续跑是从检查点里那条原始 AIMessage 重新取 tool_calls 的。
    要紧的不是「填得早」而是**两遍都填**:审批请求的 binding_digest 是按填完的
    args 记的,续跑那遍要是漏了填,重新算出来的摘要就对不上,RT-6 会把一次正常
    的批准判成完整性否决 —— 工具一次都跑不成。填在分支上方,两遍自然都经过。
    """
    harness = await graph_harness(
        approval_required_tools={_WIRE},
        bindings={_WIRE: {"project_code": "pc"}},
        prompt_inputs={"pc": "PRJ001"},
    )
    state = await harness.resume_and_continue(_WIRE, {"keyword": "王"})
    assert harness.client.calls, "批准之后工具应当真的跑起来(没被判成 binding drift)"
    _, sent_args = harness.client.calls[-1]
    assert sent_args["project_code"] == "PRJ001"
    assert state.get("approval_outcome") != "rejected"


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
    assert registry.unmatched_arg_bindings() == (
        UnmatchedArgBinding(server=_SERVER, tool="t9", params=("project_code",), tool_found=False),
    )


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
    assert registry.unmatched_arg_bindings() == (
        UnmatchedArgBinding(
            server="nosuchserver", tool="t1", params=("project_code",), tool_found=False
        ),
    )


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


# ---------------------------------------------------------------------------
# 复评修复轮 1
# ---------------------------------------------------------------------------


async def test_the_run_warns_when_a_binding_matched_nothing(graph_harness, caplog) -> None:
    """裁定 Z —— 运行期兜底告警(spec §5.4 / §8)。

    保存时的那道闸只在保存那一刻说话。对方服务器是**保存之后**下线 / 改名的那条路
    上,运行期是唯一还能说话的地方 —— 不说就是静默失效。
    """
    caplog.set_level(logging.WARNING, logger="orchestrator.graph_builder.builder")
    harness = await graph_harness(
        bindings={_WIRE: {"project_code": "pc"}},
        prompt_inputs={"pc": "PRJ001"},
        extra_bindings={"t9": {"employee_code": "emp"}},
    )
    await harness.run_turn_with_tool_call(_WIRE, {"keyword": "王"})
    assert "mcp.arg_binding_unmatched" in caplog.text
    assert "t9" in caplog.text
    assert "employee_code" in caplog.text
    assert "PRJ001" not in caplog.text, "只记名字,不记值"


async def test_a_drifted_param_also_reaches_the_save_time_gate(mcp_registry_factory) -> None:
    """m-4 —— 「参数不在 schema 里」也走未命中那条通道,别只留一行日志。

    ``tool_found=True`` 把它与「这个工具压根不存在」分开:后果一样(参数回到模型
    手里),但配置的人要做的事完全不同。
    """
    registry, _ = await mcp_registry_factory(
        tools=[{"name": "t1", "input_schema": {"type": "object", "properties": {"keyword": {}}}}],
        arg_bindings={"t1": {"gone": "pc"}},
    )
    assert registry.unmatched_arg_bindings() == (
        UnmatchedArgBinding(server=_SERVER, tool="t1", params=("gone",), tool_found=True),
    )


async def test_a_delegated_child_still_gets_the_bound_value(graph_harness) -> None:
    """裁定 AA —— 子 Agent / worker 的 config 必须带上父的 ``prompt_inputs``。

    子代跑的是同一个 ``tools_node``。config 里没有这一项时,``apply_arg_bindings``
    对每个被绑参数走「本轮没给值 → 删掉」那条分支,而该参数已经从子代的 schema 里
    剥掉、模型也补不上 —— 那个工具在子代身上永远缺一个必填参数。

    走的是真链条:父的 configurable → ``_build_tool_context`` → ``_child_config``
    → 子代 configurable → ``tools_node`` 填值 → ``RecordingMCPClient``。
    """
    parent_cfg: RunnableConfig = {
        "configurable": {
            "tenant_id": str(uuid4()),
            "run_id": str(uuid4()),
            PROMPT_INPUTS_KEY: {"pc": "PRJ001"},
        }
    }
    ctx = _build_tool_context(parent_cfg)
    assert ctx.prompt_inputs == {"pc": "PRJ001"}, "父侧要先把它捧上 ToolContext"

    child_cfg = _child_config(ctx, sub_thread_id=uuid4(), sub_run_id=uuid4())
    child_configurable = child_cfg["configurable"]
    # 字面量与常量的一致性钉在这里 —— ``_child_run`` 不能 import ``orchestrator.sse``
    # (会撞上半初始化的 orchestrator.context,实测 ImportError),所以键写的是字面量。
    assert child_configurable[PROMPT_INPUTS_KEY] == {"pc": "PRJ001"}

    harness = await graph_harness(
        bindings={_WIRE: {"project_code": "pc"}},
        prompt_inputs={},  # 子代自己的 config 里什么都没有,值只能来自透传
    )
    await harness.run_turn_with_tool_call(_WIRE, {"keyword": "王"}, config=child_cfg)
    assert harness.client.calls, "子代那次调用必须真发出去"
    _, sent_args = harness.client.calls[-1]
    assert sent_args["project_code"] == "PRJ001"


async def _pool(**servers: Sequence[MCPToolDef]) -> MCPServerPool:
    pool = MCPServerPool()
    for name, defs in servers.items():
        await pool.add(name, RecordingMCPClient(tools=tuple(defs)))
    return pool


async def test_a_second_mcp_entry_does_not_wipe_the_first_ones_binding() -> None:
    """裁定 AB —— 一份 manifest 两个 ``mcp`` 条目(协议层合法)。

    每个条目都会把它选中的服务器整个再注册一遍。按条目各发各的绑定表,后注册的那
    一条就会用「我这条没有绑定」把兄弟条目刚剥好的 schema 和刚记下的绑定一起抹掉,
    而 ``unmatched_arg_bindings()`` 是空的 —— 保存时的告警一声不吭。
    """
    bound = MCPToolSpec(
        servers=[_SERVER],
        arg_bindings=[ArgBindingSpec(server=_SERVER, tool="t1", args={"project_code": "pc"})],
    )
    catch_all = MCPToolSpec()  # servers 为空 = 所有服务器
    for order in ([bound, catch_all], [catch_all, bound]):
        registry = await build_tool_registry(
            list(order), tool_env=ToolEnv(mcp_pool=await _pool_with_t1())
        )
        assert registry.arg_bindings() == {_WIRE: {"project_code": "pc"}}, order
        schema = registry.get_required(_WIRE).spec.parameters
        assert "project_code" not in (schema.get("properties") or {}), order
        assert registry.unmatched_arg_bindings() == (), order


def _bound_entry(allow_tools: list[str]) -> MCPToolSpec:
    return MCPToolSpec(
        servers=[_SERVER],
        allow_tools=allow_tools,
        arg_bindings=[ArgBindingSpec(server=_SERVER, tool="t1", args={"project_code": "pc"})],
    )


async def _split_pool(*tool_names: str) -> MCPServerPool:
    return await _pool(
        **{
            _SERVER: [
                MCPToolDef(
                    name=n, description="", input_schema={"properties": {"project_code": {}}}
                )
                for n in tool_names
            ]
        }
    )


async def test_a_sibling_entrys_allow_tools_does_not_fake_an_unmatched_binding() -> None:
    """复评 N-1 —— 并表之后,「落没落地」不能再按单次注册的剩余项判。

    条目 A 绑 ``t1`` 且 ``allow_tools`` 只要 ``t1``;兄弟条目 B 只要 ``t2``。
    B 那一遍里 ``t1`` 被 ``allow_tools`` 挡在循环之外、从没被 pop —— 按那一遍的
    剩余项判就成了「目录里没有 t1」。而 ``t1`` 的绑定和剥过的 schema 其实都好好的。

    这条假警报会让保存时的告警对配置的人说谎、运行期每轮喊一次狼来了。这套机制
    加进来的全部价值就是那一条信号:会说假话的信号比没有信号更糟 —— 人会去「修」
    一条从来没坏的绑定。
    """
    other = MCPToolSpec(servers=[_SERVER], allow_tools=["t2"])
    for order in ([_bound_entry(["t1"]), other], [other, _bound_entry(["t1"])]):
        registry = await build_tool_registry(
            list(order), tool_env=ToolEnv(mcp_pool=await _split_pool("t1", "t2"))
        )
        assert registry.unmatched_arg_bindings() == (), order
        assert registry.arg_bindings() == {_WIRE: {"project_code": "pc"}}, order
        schema = registry.get_required(_WIRE).spec.parameters
        assert "project_code" not in (schema.get("properties") or {}), order


async def test_a_genuine_miss_is_still_reported_when_entries_are_split() -> None:
    """反向 —— 别把「狼来了」修成「狼来了不报」。

    同一份分家 manifest,只是服务器**真的**没有 ``t1``(名字写错 / 上游下线)。
    两种条目顺序都必须照报。
    """
    other = MCPToolSpec(servers=[_SERVER], allow_tools=["t2"])
    for order in ([_bound_entry(["t1"]), other], [other, _bound_entry(["t1"])]):
        registry = await build_tool_registry(
            list(order), tool_env=ToolEnv(mcp_pool=await _split_pool("t2"))
        )
        assert registry.unmatched_arg_bindings() == (
            UnmatchedArgBinding(
                server=_SERVER, tool="t1", params=("project_code",), tool_found=False
            ),
        ), order
        assert registry.arg_bindings() == {}, order


async def test_a_wire_name_collision_still_clears_the_loser_binding() -> None:
    """撞名清除那条语义仍然成立,而且只在**真的发生替换注册**时成立。

    wire 名把非 ``[a-zA-Z0-9_-]`` 折成 ``_``,所以 ``(a, x__t)`` 与 ``(a__x, t)``
    折出同一个名字。后注册的那个工具占住这个名字,绑定表就必须跟着换人 —— 否则
    平台会拿着给 ``a`` 那个工具配的绑定去填 ``a__x`` 的工具。
    """
    registry = await build_tool_registry(
        [MCPToolSpec(arg_bindings=[ArgBindingSpec(server="a", tool="x__t", args={"p": "pc"})])],
        tool_env=ToolEnv(
            mcp_pool=await _pool(
                a=[MCPToolDef(name="x__t", description="", input_schema={"properties": {"p": {}}})],
                a__x=[MCPToolDef(name="t", description="", input_schema={"properties": {"p": {}}})],
            )
        ),
    )
    # 后注册的 ``a__x/t`` 占住了 ``mcp__a__x__t``,它自己没有绑定。
    assert registry.get_required("mcp__a__x__t").tool_def.name == "t"
    assert registry.arg_bindings() == {}
    # 它的 schema 也没被别人的绑定剥过。
    assert "p" in registry.get_required("mcp__a__x__t").spec.parameters["properties"]


async def test_arg_bindings_is_a_copy(mcp_registry_factory) -> None:
    """m-1 —— 交出去的是拷贝,不是活字典。

    这张表活在 ``BuiltAgent`` 缓存里:谁就地改一下,之后每一个 run 拿到的都是被
    污染的绑定。与 ``catalog()`` 同一口径,别把「下游只读」留成一条靠约定成立的
    不变式 —— T6 已经为同一类问题吃过一次亏。
    """
    registry, _ = await mcp_registry_factory(
        tools=[
            {"name": "t1", "input_schema": {"type": "object", "properties": {"project_code": {}}}}
        ],
        arg_bindings={"t1": {"project_code": "pc"}},
    )
    handed_out = registry.arg_bindings()
    handed_out[_WIRE]["project_code"] = "TAMPERED"
    handed_out["mcp__deepcare__injected"] = {"x": "y"}
    assert registry.arg_bindings() == {_WIRE: {"project_code": "pc"}}


async def test_a_human_modify_cannot_overwrite_a_platform_binding(graph_harness) -> None:
    """裁定 AC —— ``modify`` 整份替换 args,绑定必须再套一遍。

    两件事:平台的值永远赢(被绑参数不是模型选的,也就不归人在审批面上改);
    审计行说的是真话(不重填的话它还声称那个参数是平台填的)。
    """
    harness = await graph_harness(
        approval_required_tools={_WIRE},
        bindings={_WIRE: {"project_code": "pc"}},
        prompt_inputs={"pc": "PRJ001"},
    )
    await harness.resume_and_continue(
        _WIRE,
        {"keyword": "王"},
        modified_args={"keyword": "李", "project_code": "PRJ-HAND-EDITED"},
    )
    assert harness.client.calls, "批准(改写)之后工具应当真的跑起来"
    _, sent_args = harness.client.calls[-1]
    assert sent_args["project_code"] == "PRJ001", "平台的值赢"
    assert sent_args["keyword"] == "李", "没被绑的参数,人改了就算数"
    row = harness.audit_rows[-1]
    assert row.details["bound_args"] == ["project_code"]
