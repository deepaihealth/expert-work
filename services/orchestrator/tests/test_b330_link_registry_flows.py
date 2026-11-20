"""B-330 —— 链接登记的图级用例 T2~T6、T9~T15、T18(脚手架见 ``test_b330_link_registry_wiring``)。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from prometheus_client import REGISTRY

from orchestrator import ToolRegistry, ToolResult, ToolSpec
from orchestrator.graph_builder.link_registry_node import make_link_registry_node
from orchestrator.tools.arg_bindings import ToolArgBindings

from .test_b330_link_registry_wiring import (
    A_FIRST,
    A_SECOND,
    B_ONLY,
    FORM,
    OTHER,
    SIGNED,
    UPLOAD,
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

NOTE_HEAD = "[本对话收到的链接]"


def _use(event: str) -> float:
    return REGISTRY.get_sample_value("expert_work_link_use_total", {"event": event}) or 0.0


def _blocked_reasons(conv: Conversation) -> list[str | None]:
    return [e.reason for e in conv.audit.entries if e.action.value == "tool:blocked"]


# ---------------------------------------------------------------------------
# T2 —— 嵌套与 JSON 字符串参数
# ---------------------------------------------------------------------------


async def test_nested_ids_and_json_string_arguments_are_all_rewritten() -> None:
    client = EchoMCPClient()
    llm = ScriptedLLM([AIMessage(content="收到")])
    async with opened(llm, mcp_registry(client)) as conv:
        await conv.turn("t1", inputs={"attachment_urls": [SIGNED, OTHER]})
        fields = [
            {"field_code": "a", "value": first_id(conv, 1)},
            {"field_code": "b", "value": first_id(conv, 2)},
        ]
        llm.responses += [
            AIMessage(
                content="",
                tool_calls=[
                    tc({"fields": fields}, "tc-nested"),
                    # 模型把数组写成了 JSON 字符串(B-272 在校验时还原)
                    tc({"fields": json.dumps(fields, ensure_ascii=False)}, "tc-json"),
                ],
            ),
            AIMessage(content="ok"),
        ]
        await conv.turn("t2")

    expected = [{"field_code": "a", "value": SIGNED}, {"field_code": "b", "value": OTHER}]
    assert [c["fields"] for c in client.calls] == [expected, expected]


# ---------------------------------------------------------------------------
# T3 —— 不存在的编号 / 登记为空
# ---------------------------------------------------------------------------


async def test_unknown_id_is_refused_without_dispatch_or_approval() -> None:
    client = EchoMCPClient()
    llm = ScriptedLLM([AIMessage(content="收到")])
    gated = frozenset({FORM})
    async with opened(llm, mcp_registry(client), approval_required_tools=gated) as conv:
        await conv.turn("t1", inputs={"attachment_urls": [SIGNED]})
        llm.responses += [form_call(first_id(conv, 9)), AIMessage(content="改口")]
        state = await conv.turn("t2")

    assert client.calls == []
    assert state.get("pending_approval") is None
    content = str(tool_messages(state)["tc-1"].content)
    assert "本对话没有这个编号" in content
    assert first_id(conv, 1) in content  # 列出最近的编号说明
    assert "attachment_urls 第 1 项" in content
    assert _blocked_reasons(conv) == ["link_id_unknown"]
    # 被拒的那条是欠账(B-226 ⑤,与动作筛查 block 同一做法)
    assert [f.tool_name for f in state.get("unresolved_failures") or []] == [FORM]
    rows = [e for e in conv.audit.entries if e.action.value == "tool:blocked"]
    assert rows[0].details["link_id"] == first_id(conv, 9)


async def test_an_id_in_a_conversation_without_links_gets_the_empty_sentence() -> None:
    client = EchoMCPClient()
    llm = ScriptedLLM([form_call("expert_work://link/0badc0de-1"), AIMessage(content="改口")])
    async with opened(llm, mcp_registry(client)) as conv:
        state = await conv.turn("t1")
    assert client.calls == []
    assert "本对话没有收到过链接" in str(tool_messages(state)["tc-1"].content)
    assert _blocked_reasons(conv) == ["link_id_empty"]


# ---------------------------------------------------------------------------
# T4 —— 跨对话
# ---------------------------------------------------------------------------


async def test_another_conversations_id_is_refused_not_swapped_for_a_local_link() -> None:
    client = EchoMCPClient()
    llm = ScriptedLLM([AIMessage(content="A 收到")])
    async with opened(llm, mcp_registry(client)) as conv_a:
        await conv_a.turn("a1", inputs={"attachment_urls": [A_FIRST, A_SECOND]})
        conv_b = Conversation(llm=llm, registry=conv_a.registry, cp=conv_a.cp)
        conv_b.compiled = conv_a.compiled
        llm.responses += [AIMessage(content="B 收到")]
        await conv_b.turn("b1", inputs={"note_url": B_ONLY})
        llm.responses += [form_call(first_id(conv_a, 1), "tc-foreign"), AIMessage(content="改口")]
        await conv_b.turn("b2")
        llm.responses += [form_call(first_id(conv_b, 1), "tc-own"), AIMessage(content="ok")]
        state_b = await conv_b.turn("b3")

    # B 写 A 的编号:拒绝(foreign),不会换成 B 的第 1 条
    msgs = tool_messages(state_b)
    assert "不是本对话收到的链接编号" in str(msgs["tc-foreign"].content)
    assert _blocked_reasons(conv_b) == ["link_id_foreign"]
    # B 写自己的编号 → B 的链接
    assert client.calls == [
        {"fields": [{"field_code": "field_PE_report_attachment", "value": B_ONLY}]}
    ]
    # A 的链接从没出现在 B 的任何出站参数、消息、错误文字、模型请求里
    b_text = json.dumps(
        [m.model_dump() for m in state_b["messages"]] + [client.calls], ensure_ascii=False
    )
    b_requests = json.dumps(
        [m.model_dump() for request in llm.seen[2:] for m in request], ensure_ascii=False
    )
    for url in (A_FIRST, A_SECOND):
        assert url not in b_text
        assert url not in b_requests


# ---------------------------------------------------------------------------
# T5 —— 原链接不进上下文
# ---------------------------------------------------------------------------


def _all_echo_forms(args: Mapping[str, Any]) -> str:
    """回显收到的链接的各种写法。"""
    url = str(args["fields"][0]["value"])
    return "\n".join(
        [
            "saved " + "x" * 150,  # 让截断边界(cap=400 时 200 字处)落在第一条原样链接中间
            url,
            json.dumps({"echo": url}),
            json.dumps({"echo": url, "备注": "报告"}, ensure_ascii=True),  # ``\u`` 写法
            json.dumps({"echo": url}).replace("/", "\\/"),  # ``\/`` 写法
            quote(url, safe=""),
            quote(url),
            f'<a href="{url.replace("&", "&amp;")}">附件</a>',
            url.replace("&", "\\u0026"),  # Go encoding/json 的默认写法
            url.replace("/", "\\/").replace("&", "&amp;"),
            unquote(url),  # 服务端把百分号解码后回显
            re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), url),
            "y" * 300,
        ]
    )


def _platform_written_text(seen: Sequence[Sequence[BaseMessage]]) -> str:
    """每一次发给模型的请求里,系统提示词之外的全部文字(平台段、工具结果、错误文字……)。"""
    return "\n".join(
        json.dumps(m.model_dump(), ensure_ascii=False)
        for request in seen
        for m in request
        if not isinstance(m, SystemMessage)
    )


@pytest.mark.parametrize("cap", [100_000, 400], ids=["no-trim", "trim-mid-url"])
async def test_original_url_never_reaches_the_model(cap: int) -> None:
    client = EchoMCPClient(reply=_all_echo_forms)
    llm = ScriptedLLM([AIMessage(content="这份报告是您的吗？")])  # noqa: RUF001
    async with opened(llm, mcp_registry(client, cap=cap)) as conv:
        # 租户模板用 render: raw 引用了这个变量:链接在系统提示词里(这是租户的选择)
        await conv.compiled.ainvoke(
            {
                "messages": [
                    SystemMessage(content=f"附件:{SIGNED}"),
                    HumanMessage(content=f"[file attached: {UPLOAD}]"),
                ],
                "step_count": 0,
                "max_steps": 6,
                "turn_documents": [UPLOAD],
                "turn_image_refs": [],
            },
            config=conv.config({"attachment_urls": json.dumps([SIGNED])}),
        )
        link = first_id(conv)
        llm.responses += [form_call(link), AIMessage(content="已保存")]
        await conv.turn("是我的")
        snapshot = await conv.compiled.aget_state(conv.config(None))

    assert client.calls[0]["fields"][0]["value"] == SIGNED
    platform_text = _platform_written_text(llm.seen)
    for form in url_forms(SIGNED):
        assert form not in platform_text, form
    # 截断边界落在链接中间时漏出的是链接的一段:任何 24 个字符的片段都不许出现
    for start in range(len(SIGNED) - 24):
        assert SIGNED[start : start + 24] not in platform_text, SIGNED[start : start + 24]
    assert link in platform_text
    # 检查点里 AIMessage 的参数是编号
    ai_args = [
        call["args"]
        for m in snapshot.values["messages"]
        if isinstance(m, AIMessage)
        for call in m.tool_calls
    ]
    assert ai_args == [{"fields": [{"field_code": "field_PE_report_attachment", "value": link}]}]
    # 系统提示词里的链接是租户模板的,平台不动它
    assert SIGNED in str(llm.seen[0][0].content)


async def test_server_error_echoing_the_url_is_masked_in_result_and_span_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses: list[str] = []

    def _spy_status(span: Any, outcome: tuple[Any, ...]) -> None:
        del span
        statuses.append(str(getattr(outcome[3], "summary", "")))

    monkeypatch.setattr("orchestrator.graph_builder.builder._record_tool_error", _spy_status)
    client = EchoMCPClient(raise_with=RuntimeError(f"链接无效:{SIGNED}"))
    llm = ScriptedLLM([AIMessage(content="收到")])
    async with opened(llm, mcp_registry(client)) as conv:
        await conv.turn("t1", inputs={"attachment_urls": [SIGNED]})
        llm.responses += [form_call(first_id(conv)), AIMessage(content="失败了")]
        state = await conv.turn("t2")

    assert first_id(conv) in str(tool_messages(state)["tc-1"].content)
    assert statuses and first_id(conv) in statuses[0]
    leaked = _platform_written_text(llm.seen) + "\n".join(statuses)
    leaked += json.dumps(state.get("tool_failures") or [], default=str)
    leaked += json.dumps(state.get("unresolved_failures") or [], default=str)
    for form in url_forms(SIGNED):
        assert form not in leaked, form


# ---------------------------------------------------------------------------
# T6 —— 与绑定(``attachments ← attachment_urls``)
# ---------------------------------------------------------------------------


def _bound_registry(client: EchoMCPClient) -> ToolRegistry:
    registry = mcp_registry(client)
    registry.bind_tool_args(FORM, ToolArgBindings(args={"attachments": "attachment_urls"}))
    return registry


async def test_without_binding_both_turns_write_the_id_and_the_server_gets_the_url() -> None:
    client = EchoMCPClient()
    llm = ScriptedLLM([])
    async with opened(llm, mcp_registry(client)) as conv:
        link = first_id(conv)
        llm.responses += [form_call(link, "tc-a"), AIMessage(content="ok")]
        await conv.turn("t1", inputs={"attachment_urls": [SIGNED]})
        llm.responses += [form_call(link, "tc-b"), AIMessage(content="ok")]
        await conv.turn("t2")
    assert [c["fields"][0]["value"] for c in client.calls] == [SIGNED, SIGNED]


async def test_with_binding_the_bound_param_is_never_touched() -> None:
    client = EchoMCPClient()
    llm = ScriptedLLM([])
    async with opened(llm, _bound_registry(client)) as conv:
        link = first_id(conv)
        llm.responses += [form_call(link, "tc-a"), AIMessage(content="ok")]
        await conv.turn("t1", inputs={"attachment_urls": json.dumps([SIGNED])})
        llm.responses += [form_call(link, "tc-b"), AIMessage(content="ok")]
        await conv.turn("t2")
        # 对接方的变量值恰好是编号字面量:被绑参数原样不动
        llm.responses += [form_call(link, "tc-c"), AIMessage(content="ok")]
        await conv.turn("t3", inputs={"attachment_urls": link})

    first, second, third = client.calls
    assert first["attachments"] == json.dumps([SIGNED])  # 绑定填真值
    assert first["fields"][0]["value"] == SIGNED  # 模型写的编号也换回(两处都有)
    assert "attachments" not in second  # 本轮没传 → 绑定照旧删掉
    assert second["fields"][0]["value"] == SIGNED
    assert third["attachments"] == link
    assert third["fields"][0]["value"] == SIGNED


# ---------------------------------------------------------------------------
# T9 —— 去重与稳定
# ---------------------------------------------------------------------------


async def test_same_link_keeps_its_id_across_turns_and_replays() -> None:
    client = EchoMCPClient()
    llm = ScriptedLLM([AIMessage(content="1"), AIMessage(content="2"), AIMessage(content="3")])
    node = make_link_registry_node(variables=VARIABLES)
    async with opened(llm, mcp_registry(client)) as conv:
        await conv.turn("t1", inputs={"attachment_urls": [SIGNED]})
        await conv.turn("t2", inputs={"note_url": OTHER})
        # 第 3 轮又收到第 1 轮那条(重新生成 = 同一份 inputs 重放,同理)
        await conv.turn("t3", inputs={"attachment_urls": [SIGNED, OTHER]})
        values = (await conv.compiled.aget_state(conv.config(None))).values
        # 孤儿复活:开头节点对同一份 inputs 再跑一次,登记不变
        replay = await node(values, conv.config({"attachment_urls": [SIGNED, OTHER]}))

    assert [(e["id"], e["url"]) for e in values["conversation_links"]] == [
        (first_id(conv, 1), SIGNED),
        (first_id(conv, 2), OTHER),
    ]
    assert replay == {}


# ---------------------------------------------------------------------------
# T10 —— 平台段出现的条件
# ---------------------------------------------------------------------------


def _tail_text(request: Sequence[BaseMessage]) -> str:
    return str(request[-1].content)


async def test_note_appears_only_with_links_and_an_mcp_tool() -> None:
    llm = ScriptedLLM([AIMessage(content="1"), AIMessage(content="2"), AIMessage(content="3")])
    async with opened(llm, mcp_registry(EchoMCPClient())) as conv:
        await conv.turn("t1")
        await conv.turn("t2", inputs={"attachment_urls": [SIGNED]})
        await conv.turn("t3")
    assert NOTE_HEAD not in _tail_text(llm.seen[0])
    assert NOTE_HEAD in _tail_text(llm.seen[1])
    assert first_id(conv) in _tail_text(llm.seen[1])
    # 之后的轮次没传变量,平台段照样在(每一步按登记重新生成)
    assert NOTE_HEAD in _tail_text(llm.seen[2])

    no_mcp = ScriptedLLM([AIMessage(content="1")])
    async with opened(no_mcp, ToolRegistry()) as conv2:
        await conv2.turn("t1", inputs={"attachment_urls": [SIGNED]})
    assert NOTE_HEAD not in _tail_text(no_mcp.seen[0])


# ---------------------------------------------------------------------------
# T11 / T12 —— 拼在长串里 / http 工具 / 平台工具
# ---------------------------------------------------------------------------


@dataclass
class PlainTool:
    name: str
    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description="d", is_read_only=False)

    async def call(self, args: Mapping[str, Any], *, ctx: Any) -> ToolResult:
        del ctx
        self.calls.append(dict(args))
        return ToolResult(content="done")


async def test_embedded_and_http_ids_are_refused_platform_tools_untouched() -> None:
    client = EchoMCPClient()
    http_tool, write_tool = PlainTool("http"), PlainTool("spawn_worker")
    llm = ScriptedLLM([AIMessage(content="1")])
    async with opened(llm, mcp_registry(client, extra=[http_tool, write_tool])) as conv:
        await conv.turn("t1", inputs={"attachment_urls": [SIGNED, OTHER]})
        one, two = first_id(conv, 1), first_id(conv, 2)
        llm.responses += [
            AIMessage(
                content="", tool_calls=[tc({"task": f"把 {one} 存进表单"}, "tc-w", "spawn_worker")]
            ),
            AIMessage(content="", tool_calls=[tc({"url": one}, "tc-h", "http")]),
            form_call(f"{one};{two}", "tc-e"),
            AIMessage(content="改口"),
        ]
        state = await conv.turn("t2")

    msgs = tool_messages(state)
    assert write_tool.calls == [{"task": f"把 {one} 存进表单"}]
    assert http_tool.calls == []
    assert "http 工具收到的就是这串字" in str(msgs["tc-h"].content)
    assert client.calls == []
    assert "请写成数组" in str(msgs["tc-e"].content)
    assert _blocked_reasons(conv) == ["link_id_http", "link_id_embedded"]


# ---------------------------------------------------------------------------
# T13 —— 模型自己写原链接:只计数
# ---------------------------------------------------------------------------


async def test_verbatim_and_near_copies_are_dispatched_and_only_counted() -> None:
    client = EchoMCPClient()
    llm = ScriptedLLM([AIMessage(content="1")])
    near = SIGNED.replace("abc%2F", "abd%2F")
    before = (_use("verbatim"), _use("near_copy"))
    async with opened(llm, mcp_registry(client)) as conv:
        await conv.turn("t1", inputs={"attachment_urls": [SIGNED]})
        llm.responses += [
            form_call(SIGNED, "tc-v"),
            form_call(near, "tc-n"),
            AIMessage(content="ok"),
        ]
        await conv.turn("t2")
    assert [c["fields"][0]["value"] for c in client.calls] == [SIGNED, near]
    assert _use("verbatim") == before[0] + 1
    assert _use("near_copy") == before[1] + 1


# ---------------------------------------------------------------------------
# T14 —— 轨迹记编号版参数与遮过的结果
# ---------------------------------------------------------------------------


async def test_span_records_the_id_version_of_args_and_the_masked_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[tuple[dict[str, Any], str]] = []

    def _spy(span: Any, args: Mapping[str, Any], result: Any) -> None:
        del span
        recorded.append((json.loads(json.dumps(dict(args))), str(result)))

    monkeypatch.setattr("orchestrator.graph_builder.builder._record_tool_io", _spy)
    client = EchoMCPClient(reply=lambda args: f"saved {args['fields'][0]['value']}")
    llm = ScriptedLLM([AIMessage(content="1")])
    async with opened(llm, mcp_registry(client)) as conv:
        await conv.turn("t1", inputs={"attachment_urls": [SIGNED]})
        link = first_id(conv)
        llm.responses += [form_call(link), AIMessage(content="ok")]
        await conv.turn("t2")

    value = {"fields": [{"field_code": "field_PE_report_attachment", "value": link}]}
    assert recorded == [(value, f"saved {link}")]
    rows = [e for e in conv.audit.entries if e.action.value == "tool:call"]
    assert rows[0].details["rewritten_links"] == [link]
    assert SIGNED not in json.dumps([r.details for r in conv.audit.entries], default=str)


# ---------------------------------------------------------------------------
# T15 —— 字面量守卫的提示给编号
# ---------------------------------------------------------------------------


async def test_literal_guard_hint_names_the_ids_when_the_variable_has_links() -> None:
    client = EchoMCPClient()
    llm = ScriptedLLM([AIMessage(content="1")])
    declared = frozenset({"attachment_urls"})
    async with opened(llm, mcp_registry(client), declared_input_names=declared) as conv:
        await conv.turn("t1", inputs={"attachment_urls": [SIGNED]})
        llm.responses += [
            AIMessage(
                content="",
                tool_calls=[
                    tc({"fields": [{"field_code": "f", "value": "$attachment_urls"}]}, "tc-var"),
                    tc(
                        {
                            "fields": [
                                {
                                    "field_code": "f",
                                    "value": "inputs/current/attachment_urls/0-x.pdf",
                                }
                            ]
                        },
                        "tc-path",
                    ),
                ],
            ),
            AIMessage(content="ok"),
        ]
        state = await conv.turn("t2", inputs={"attachment_urls": [SIGNED]})

    msgs = tool_messages(state)
    for key in ("tc-var", "tc-path"):
        text = str(msgs[key].content)
        assert first_id(conv) in text, text
        assert "原始链接" not in text and "实际内容" not in text
    assert client.calls == []


# ---------------------------------------------------------------------------
# T18 —— 最终回复里的编号只计数
# ---------------------------------------------------------------------------


async def test_an_id_in_the_final_reply_is_counted_and_left_alone() -> None:
    llm = ScriptedLLM([AIMessage(content="1")])
    before = _use("id_in_reply")
    async with opened(llm, mcp_registry(EchoMCPClient())) as conv:
        await conv.turn("t1", inputs={"attachment_urls": [SIGNED]})
        reply = f"附件见 {first_id(conv)}"
        llm.responses += [AIMessage(content=reply)]
        state = await conv.turn("t2")
    assert state["messages"][-1].content == reply
    assert _use("id_in_reply") == before + 1


# ---------------------------------------------------------------------------
# T5 / T14 补充 —— 直播帧与 run_event:``updates`` 帧过平台的剥离后不含原链接
# ---------------------------------------------------------------------------


def _frame_text(chunk: Any) -> str:
    return json.dumps(
        chunk,
        ensure_ascii=False,
        default=lambda o: o.model_dump() if hasattr(o, "model_dump") else str(o),
    )


async def test_published_update_frames_never_carry_a_registered_url() -> None:
    from orchestrator.sse import DEFAULT_STREAM_MODE, _strip_internal_state_keys

    client = EchoMCPClient(reply=lambda args: f"saved {args['fields'][0]['value']}")
    llm = ScriptedLLM([AIMessage(content="收到")])
    frames: list[str] = []
    async with opened(llm, mcp_registry(client)) as conv:
        link = first_id(conv)
        llm.responses += [form_call(link), AIMessage(content="ok")]
        for text, inputs in (("t1", {"attachment_urls": [SIGNED]}), ("t2", None)):
            graph_input = {
                "messages": [HumanMessage(content=text)],
                "step_count": 0,
                "max_steps": 6,
                "turn_documents": [],
                "turn_image_refs": [],
            }
            async for chunk in conv.compiled.astream(
                graph_input, config=conv.config(inputs), stream_mode=DEFAULT_STREAM_MODE
            ):
                frames.append(_frame_text(_strip_internal_state_keys(chunk)))

    assert client.calls[0]["fields"][0]["value"] == SIGNED
    published = "\n".join(frames)
    assert link in published  # 帧照发,只是没有原链接
    for form in url_forms(SIGNED):
        assert form not in published, form
