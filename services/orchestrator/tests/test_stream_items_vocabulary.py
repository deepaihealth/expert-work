"""对外事件名的词表闸(对话条目 program PR3,spec §十一)。

**在这条测试之前,全仓没有任何测试断言对外事件名的全集。** 名字最像的那个对外
契约测试只有一条 SSE 断言(``"event: end" in body``),不校验集合。而条目模式下
有一批事件要原样透传 —— 少写一个 = **静默丢帧且不会红**。

做法照 ``test_run_event_stream.py::test_end_status_vocabulary_round_trip`` 那条
现成套路:AST 扫源码里实际发布过的字面量事件名,与 ``stream_items`` 的常量表
对齐。扫描器解析不了的表达式一律**当场报错**,不静默跳过 —— 一个「扫不到就当
没有」的扫描器会让整条闸空转。
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

from expert_work.protocol import EventType
from orchestrator.sse import DEFAULT_STREAM_MODE, format_sse
from orchestrator.stream_items import (
    CONNECTION_EVENTS,
    CONVERTED_EVENTS,
    ITEM_EVENTS,
    ITEMS_WIRE_EVENTS,
    PASSTHROUGH_EVENTS,
    PUBLISHED_EVENTS,
)

#: ``_publish_frame`` 的第一个实参写成裸变量时的解析表。模块级常量直接从模块
#: 取(改名会当场报错);``stream_mode`` 是 ``run_agent`` 的形参,默认值就是这
#: 个常量 —— 改默认值这里跟着变,所以不是硬编码。
_NAME_FALLBACK = {"stream_mode": DEFAULT_STREAM_MODE}


def _sse_source() -> str:
    # 用 from-import 进来的函数对象定位源文件,而不是再 ``import orchestrator.sse``
    # —— 同一模块两种导入方式并存会被 CodeQL 判为缺陷并卡住合并。
    return Path(inspect.getsourcefile(format_sse) or "").read_text(encoding="utf-8")


def _resolve(node: ast.expr, *, where: str) -> str:
    """一个事件名表达式 → 它的字面值。解析不了就 fail,不返回 ``None``。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    # ``EventType.PLAN.value``
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "value"
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "EventType"
    ):
        member: str = getattr(EventType, node.value.attr).value
        return member
    if isinstance(node, ast.Name):
        module = sys.modules[format_sse.__module__]
        value = getattr(module, node.id, None) or _NAME_FALLBACK.get(node.id)
        if isinstance(value, str):
            return value
    raise AssertionError(
        f"{where}:{node.lineno} 的事件名表达式 {ast.dump(node)} 解析不了 —— "
        "扫描器必须认识 sse.py 里每一种事件名写法,否则这道闸会静默漏掉一种帧"
    )


def _published_event_names() -> set[str]:
    """``sse.py`` 里 ``_publish_frame`` / ``publish_ephemeral`` 发出的全部事件名。"""
    names: set[str] = set()
    for node in ast.walk(ast.parse(_sse_source())):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "_publish_frame":
            names.add(_resolve(node.args[0], where="sse.py _publish_frame"))
        elif isinstance(func, ast.Attribute) and func.attr == "publish_ephemeral":
            # ``bridge.publish_ephemeral(run_id, "token", frame)`` —— 事件名是
            # 第二个实参。
            names.add(_resolve(node.args[1], where="sse.py publish_ephemeral"))
    return names


def _format_sse_literals(source: str, *, where: str) -> set[str]:
    """一份源码里 ``format_sse`` 被直接喂进去的**字面量**事件名。

    转发已有名字的调用(``format_sse(entry.event, ...)`` / 转换器返回的
    ``name``)不造新名字,跳过;除此以外解析不了的表达式一律 fail —— 拼出来的
    动态事件名会绕过整道闸。
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "format_sse":
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.add(first.value)
            elif not isinstance(first, ast.Name | ast.Attribute):
                raise AssertionError(f"{where}:{node.lineno} 的 format_sse 事件名解析不了")
    return names


def test_published_event_vocabulary_round_trip() -> None:
    """``sse.py`` 实际发布的事件名集合 == :data:`PUBLISHED_EVENTS`。

    这是整道闸的地基。新增一种帧却忘了归类,这条先红 —— 而不是在条目模式下
    悄悄丢掉它。
    """
    scanned = _published_event_names()
    assert scanned, "一个事件名都没扫到 —— 扫描器失效,本断言已空转"
    assert scanned == set(PUBLISHED_EVENTS), (
        f"sse.py 发布的事件名与 PUBLISHED_EVENTS 不闭合:"
        f"漏登记 {sorted(scanned - PUBLISHED_EVENTS)},多登记 "
        f"{sorted(PUBLISHED_EVENTS - scanned)}"
    )


def test_every_published_event_is_either_converted_or_passed_through() -> None:
    """每种帧必须二选一,不能既不转换也不透传(= 条目模式下静默丢帧)。"""
    assert CONVERTED_EVENTS <= PUBLISHED_EVENTS, sorted(CONVERTED_EVENTS - PUBLISHED_EVENTS)
    assert not (CONVERTED_EVENTS & PASSTHROUGH_EVENTS), "同一种帧不能既转换又透传"
    assert CONVERTED_EVENTS | PASSTHROUGH_EVENTS == set(PUBLISHED_EVENTS)


def test_connection_events_are_minted_outside_the_converter() -> None:
    """``end`` / ``gap`` / ``truncated`` 由两个 API 模块直接编码,不经转换器。

    它们描述的是这条连接的状况,不是 run 的事件,所以既不在 ``PUBLISHED_EVENTS``
    里,也不该被转换。这条钉住「两边都只发这三个」。
    """
    from control_plane.api import _run_event_stream

    sse_names = _format_sse_literals(_sse_source(), where="sse.py")
    stream_names = _format_sse_literals(
        Path(inspect.getsourcefile(_run_event_stream) or "").read_text(encoding="utf-8"),
        where="_run_event_stream.py",
    )
    assert sse_names, "sse.py 里一个 format_sse 字面量都没扫到 —— 断言已空转"
    assert stream_names, "_run_event_stream.py 里一个都没扫到 —— 断言已空转"
    assert sse_names <= CONNECTION_EVENTS, sorted(sse_names - CONNECTION_EVENTS)
    assert stream_names <= CONNECTION_EVENTS, sorted(stream_names - CONNECTION_EVENTS)
    assert not (CONNECTION_EVENTS & PUBLISHED_EVENTS)


def test_items_wire_vocabulary_is_closed() -> None:
    """条目模式下第三方能看到的事件集合 —— 对外文档就照这张表写。

    ``system_prompt`` 对外恒隐藏,所以不在其中;``worker`` 在其中(拍板:条目
    模式下仍作为独立事件发,不转成 ``tool_call.worker``)。
    """
    assert ITEMS_WIRE_EVENTS == {
        "item.added",
        "item.delta",
        "item.done",
        "metadata",
        "end",
        "gap",
        "truncated",
        "guard",
        "compaction",
        "retry",
        "worker",
    }
    assert ITEM_EVENTS <= ITEMS_WIRE_EVENTS
    assert not (CONVERTED_EVENTS & ITEMS_WIRE_EVENTS), (
        "被转换掉的帧名不该还出现在条目模式的 wire 词表里:"
        f"{sorted(CONVERTED_EVENTS & ITEMS_WIRE_EVENTS)}"
    )
    assert "system_prompt" not in ITEMS_WIRE_EVENTS


def test_converter_handles_every_converted_event() -> None:
    """``CONVERTED_EVENTS`` 里每一种都真的被转换,没有一种落进透传分支。

    光有常量表不够 —— 表里写了「要转」而 ``convert`` 的分支忘了加,帧会原样
    透传出去,条目模式下混进一个 legacy 事件。
    """
    from uuid import uuid4

    from orchestrator.stream_items import ItemStreamConverter

    conv = ItemStreamConverter(run_id=uuid4())
    for name in sorted(CONVERTED_EVENTS):
        out = conv.convert(name, {}, event_id="1700000000000-1")
        assert all(emitted in ITEM_EVENTS for emitted, _ in out), f"{name} 落进了透传分支:{out}"
