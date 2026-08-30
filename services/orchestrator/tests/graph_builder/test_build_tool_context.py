from __future__ import annotations

from uuid import uuid4

from orchestrator.graph_builder.builder import _build_tool_context


def test_lifts_thread_id() -> None:
    thread_id = uuid4()
    ctx = _build_tool_context(
        {"configurable": {"thread_id": str(thread_id), "tenant_id": str(uuid4())}}
    )
    assert ctx.thread_id == thread_id


def test_trigger_origin_defaults_false() -> None:
    ctx = _build_tool_context({"configurable": {"thread_id": str(uuid4())}})
    assert ctx.trigger_origin is False


def test_trigger_origin_true_when_flagged() -> None:
    ctx = _build_tool_context({"configurable": {"thread_id": str(uuid4()), "trigger_origin": True}})
    assert ctx.trigger_origin is True


def test_missing_thread_id_is_none() -> None:
    ctx = _build_tool_context({"configurable": {"tenant_id": str(uuid4())}})
    assert ctx.thread_id is None


def test_turn_documents_lifted_from_config() -> None:
    ctx = _build_tool_context(
        {"configurable": {"turn_documents": ["uploads/a.docx", "uploads/b.pptx"]}}
    )
    assert ctx.turn_documents == ("uploads/a.docx", "uploads/b.pptx")


def test_turn_documents_default_empty() -> None:
    assert _build_tool_context({"configurable": {}}).turn_documents == ()


def test_turn_documents_drops_non_string_entries() -> None:
    """这个值一路从 HTTP 载荷传下来,不是进程内对象 —— 非字符串项丢弃,
    免得一条脏数据顺着种子消息喂进子代的 prompt。"""
    ctx = _build_tool_context(
        {"configurable": {"turn_documents": ["uploads/a.docx", None, 7, "", {"x": 1}]}}
    )
    assert ctx.turn_documents == ("uploads/a.docx",)


def test_turn_documents_ignores_a_non_list_value() -> None:
    assert (
        _build_tool_context({"configurable": {"turn_documents": "uploads/a.docx"}}).turn_documents
        == ()
    )
