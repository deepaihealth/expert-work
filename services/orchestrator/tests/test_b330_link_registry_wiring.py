"""B-330 —— 链接登记接线(图级):第 1 轮记下链接,之后的轮次模型写编号,MCP 收到原链接。

假模型按脚本回固定的工具调用;假 MCP 服务记下收到的参数、可配置回显。工具形状照 B-314 的
``form_save_customer_values``(``fields[]`` 为 ``{field_code, value}`` 对象数组)。链接全是合成的。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from expert_work.protocol import PromptVariableSpec
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import GraphRunner, ToolRegistry, ToolSpec, build_react_graph
from orchestrator.graph_builder._config import AUDIT_LOGGER_KEY
from orchestrator.graph_builder.link_registry_node import make_link_registry_node
from orchestrator.sse import PROMPT_INPUTS_KEY
from orchestrator.tools.mcp import MCPCallResult, MCPTool, MCPToolDef

from .test_input_url_guard_wiring import _RecordingAuditLogger

pytestmark = pytest.mark.asyncio


def url_forms(url: str) -> set[str]:
    """一条链接在文字里可能出现的几种写法,外加它的前一半(截断边界落在链接中间时漏出的)。"""
    forms = {
        url,
        url[: len(url) // 2],
        json.dumps(url)[1:-1].replace("/", "\\/"),
        quote(url, safe=""),
        quote(url),
        url.replace("&", "&amp;"),
        url.replace("&", "\\u0026"),  # Go encoding/json
        url.replace("/", "\\/").replace("&", "&amp;"),
        unquote(url),  # 服务端把百分号解码后回显
        re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), url),
    }
    return {form for form in forms if form}


@pytest.fixture(autouse=True)
def _no_registered_url_in_logs(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """T14 —— 整套用例捕获的日志里没有任何登记链接(任何一种写法)。"""
    caplog.set_level(logging.DEBUG)
    yield
    text = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    for url in (SIGNED, OTHER, A_FIRST, A_SECOND, B_ONLY):
        for form in url_forms(url):
            assert form not in text, form


SERVER = "shenhu"
FORM_TOOL = "form_save_customer_values"
FORM = f"mcp__{SERVER}__{FORM_TOOL}"
#: 合成的带签名查询串的链接(没有任何真实地址)。
SIGNED = (
    "https://oss.example.invalid/tenant-x/wecom/2026/11/20/report-a1b2c3.pdf"
    "?Expires=1763600000&OSSAccessKeyId=SYNTHETICKEY&Signature=abc%2Fdef%3D%3D"
)
OTHER = "https://oss.example.invalid/tenant-x/wecom/2026/11/20/scan-77.jpg?Signature=zz9"
UPLOAD = "uploads/attachment-9.pdf"
A_FIRST = "https://files.example.invalid/conv-a/one.pdf?Signature=aaa111"
A_SECOND = "https://files.example.invalid/conv-a/two.png?Signature=aaa222"
B_ONLY = "https://files.example.invalid/conv-b/only.pdf?Signature=bbb111"

FORM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "customer_code": {"type": "string"},
        "form_code": {"type": "string"},
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"field_code": {"type": "string"}, "value": {"type": "string"}},
                "required": ["field_code", "value"],
            },
        },
        "attachments": {"type": "string"},
    },
    "required": ["fields"],
}

VARIABLES = (
    PromptVariableSpec(name="attachment_urls", required=False),
    PromptVariableSpec(name="note_url", required=False, trusted=False),
)


def _saved(_args: Mapping[str, Any]) -> str:
    return "saved"


@dataclass
class EchoMCPClient:
    """记下每次收到的参数;``reply`` 决定返回的文字(默认 ``saved``)。"""

    calls: list[dict[str, Any]] = field(default_factory=list)
    # 用 default_factory 放到实例上:类属性上的函数会被 CodeQL 当成方法(py/call/wrong-arguments)。
    reply: Callable[[Mapping[str, Any]], str] = field(default_factory=lambda: _saved)
    raise_with: Exception | None = None

    async def list_tools(self) -> Sequence[MCPToolDef]:
        return ()

    async def call_tool(self, name: str, args: Mapping[str, Any]) -> MCPCallResult:
        del name
        self.calls.append(json.loads(json.dumps(dict(args))))
        if self.raise_with is not None:
            raise self.raise_with
        return MCPCallResult(content=self.reply(args), is_error=False)

    async def close(self) -> None:
        return None


def mcp_registry(
    client: EchoMCPClient, *, cap: int = 100_000, extra: Sequence[Any] = ()
) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        MCPTool(
            client=client,
            tool_def=MCPToolDef(name=FORM_TOOL, description="save form", input_schema=FORM_SCHEMA),
            server_name=SERVER,
            content_char_cap=cap,
        )
    )
    for tool in extra:
        registry.register(tool)
    return registry


@dataclass
class ScriptedLLM:
    """按脚本回消息;``seen`` 记下每一次请求的完整消息列表。"""

    responses: list[AIMessage]
    seen: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec], **_: Any
    ) -> AIMessage:
        self.seen.append(list(messages))
        return self.responses[len(self.seen) - 1]


def tc(args: dict[str, Any], call_id: str, name: str = FORM) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def form_call(value: str, call_id: str = "tc-1", **extra: Any) -> AIMessage:
    args = {"fields": [{"field_code": "field_PE_report_attachment", "value": value}], **extra}
    return AIMessage(content="", tool_calls=[tc(args, call_id)])


@dataclass
class Conversation:
    """同一个检查点、同一个对话 id 上跑多轮。"""

    llm: ScriptedLLM
    registry: ToolRegistry
    thread_id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    audit: _RecordingAuditLogger = field(default_factory=_RecordingAuditLogger)
    graph_kwargs: dict[str, Any] = field(default_factory=dict)
    cp: Any = None
    compiled: Any = None

    def config(self, inputs: Mapping[str, Any] | None, **extra: Any) -> RunnableConfig:
        configurable: dict[str, Any] = {
            "thread_id": str(self.thread_id),
            "tenant_id": str(self.tenant_id),
            "run_id": str(uuid4()),
            AUDIT_LOGGER_KEY: self.audit,
            **extra,
        }
        if inputs is not None:
            configurable[PROMPT_INPUTS_KEY] = dict(inputs)
        return {"configurable": configurable}

    async def turn(
        self,
        text: str,
        *,
        inputs: Mapping[str, Any] | None = None,
        documents: Sequence[str] = (),
        **extra: Any,
    ) -> dict[str, Any]:
        cfg = self.config(inputs, **extra)
        return dict(
            await self.compiled.ainvoke(
                {
                    "messages": [HumanMessage(content=text)],
                    "step_count": 0,
                    "max_steps": 6,
                    "turn_documents": list(documents),
                    "turn_image_refs": [],
                },
                config=cfg,
            )
        )


class _Opened:
    def __init__(self, conv: Conversation, dsn: str | None = None) -> None:
        self.conv = conv
        self._cm: Any = None
        self._dsn = dsn

    async def __aenter__(self) -> Conversation:
        self._cm = (
            make_checkpointer("postgres", self._dsn)
            if self._dsn is not None
            else make_checkpointer("memory")
        )
        self.conv.cp = await self._cm.__aenter__()
        self.conv.compiled = GraphRunner(checkpointer=self.conv.cp).compile(
            build_react_graph(
                llm_caller=self.conv.llm,
                tool_registry=self.conv.registry,
                link_registry_node=make_link_registry_node(variables=VARIABLES),
                **self.conv.graph_kwargs,
            )
        )
        return self.conv

    async def __aexit__(self, *exc: object) -> None:
        await self._cm.__aexit__(*exc)


def opened(
    llm: ScriptedLLM, registry: ToolRegistry, *, dsn: str | None = None, **graph_kwargs: Any
) -> _Opened:
    """``dsn`` 给了就用我们的 Postgres 检查点保存器,否则用内存版(同一个工厂、同一套 serde)。"""
    return _Opened(Conversation(llm=llm, registry=registry, graph_kwargs=graph_kwargs), dsn)


def tool_messages(state: Mapping[str, Any]) -> dict[str, ToolMessage]:
    return {m.tool_call_id: m for m in state["messages"] if isinstance(m, ToolMessage)}


def first_id(conv: Conversation, seq: int = 1) -> str:
    return f"expert_work://link/{conv.thread_id.hex[:8]}-{seq}"


# ---------------------------------------------------------------------------
# T1 / T1b —— 事故形状
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attachment_urls",
    [json.dumps([SIGNED]), [SIGNED]],
    ids=["T1-json-array-string", "T1b-real-list"],
)
async def test_incident_shape_next_turn_id_reaches_the_server_as_the_original_url(
    attachment_urls: Any,
) -> None:
    client = EchoMCPClient()
    llm = ScriptedLLM([AIMessage(content="这份报告是您的吗？")])  # noqa: RUF001
    async with opened(llm, mcp_registry(client)) as conv:
        await conv.turn(
            f"[file attached: {UPLOAD}]",
            inputs={"attachment_urls": attachment_urls},
            documents=[UPLOAD],
        )
        link = first_id(conv)
        llm.responses += [form_call(link), AIMessage(content="已保存")]
        await conv.turn("是我的，存进去")  # noqa: RUF001

    assert len(client.calls) == 1
    assert client.calls[0]["fields"][0]["value"] == SIGNED


# ---------------------------------------------------------------------------
# 构建层:声明了变量就装节点(不要求接沙箱);没声明不装
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("declared", [True, False])
async def test_factory_installs_the_node_iff_variables_are_declared(
    monkeypatch: pytest.MonkeyPatch, declared: bool
) -> None:
    from expert_work.runtime.secret_store import LocalDevSecretStore
    from orchestrator import build_agent

    from .test_input_url_guard_wiring import _KEY_NAME, _jinja_spec, _key_resolver

    captured: dict[str, Any] = {}

    def _spy(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return build_react_graph(**kwargs)

    spec = _jinja_spec()
    if not declared:
        prompt = spec.spec.system_prompt.model_copy(
            update={"template": "plain", "jinja": False, "variables": []}
        )
        spec = spec.model_copy(
            update={"spec": spec.spec.model_copy(update={"system_prompt": prompt})}
        )
    monkeypatch.setattr("orchestrator.agent_factory.build_react_graph", _spy)
    async with make_checkpointer("memory") as cp:
        await build_agent(
            spec,
            secret_store=LocalDevSecretStore.from_mapping({_KEY_NAME: "sk-ant-test"}),
            checkpointer=cp,
            provider_key_resolver=_key_resolver,
        )
    # 单测里没接沙箱:inputs 节点不装,链接登记照装
    assert captured["inputs_node"] is None
    assert (captured["link_registry_node"] is not None) is declared
