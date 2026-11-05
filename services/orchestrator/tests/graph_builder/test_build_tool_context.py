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


def test_turn_documents_lifted_from_state() -> None:
    ctx = _build_tool_context(
        {"configurable": {}}, turn_documents=["uploads/a.docx", "uploads/b.pptx"]
    )
    assert ctx.turn_documents == ("uploads/a.docx", "uploads/b.pptx")


def test_turn_image_refs_lifted_from_state() -> None:
    ctx = _build_tool_context({"configurable": {}}, turn_image_refs=["expert_work://image/x"])
    assert ctx.turn_image_refs == ("expert_work://image/x",)


def test_turn_documents_default_empty() -> None:
    assert _build_tool_context({"configurable": {}}).turn_documents == ()


def test_turn_documents_drops_non_string_entries() -> None:
    """这个值一路从 HTTP 载荷传下来,不是进程内对象 —— 非字符串项丢弃,
    免得一条脏数据顺着种子消息喂进子代的 prompt。"""
    ctx = _build_tool_context(
        {"configurable": {}},
        turn_documents=["uploads/a.docx", None, 7, "", {"x": 1}],  # type: ignore[list-item]
    )
    assert ctx.turn_documents == ("uploads/a.docx",)


def test_turn_documents_ignores_a_non_list_value() -> None:
    ctx = _build_tool_context({"configurable": {}}, turn_documents="uploads/a.docx")  # type: ignore[arg-type]
    assert ctx.turn_documents == ()


def test_turn_document_original_names_lifted_from_state() -> None:
    """B-275 —— 原名表与附件清单同走 state;只留本轮附件的、键值都是非空字符串的条目
    (这个值一路从 HTTP 载荷传下来,不是进程内对象)。"""
    ctx = _build_tool_context(
        {"configurable": {}},
        turn_documents=["uploads/a-1a2b3c4d.pdf", "uploads/b.docx"],
        turn_document_original_names={
            "uploads/a-1a2b3c4d.pdf": "a.pdf",
            "uploads/not-this-turn.pdf": "x.pdf",
            "uploads/b.docx": 7,  # type: ignore[dict-item]
        },
    )
    assert dict(ctx.turn_document_original_names) == {"uploads/a-1a2b3c4d.pdf": "a.pdf"}


def test_turn_document_original_names_default_empty() -> None:
    assert dict(_build_tool_context({"configurable": {}}).turn_document_original_names) == {}
    ctx = _build_tool_context(
        {"configurable": {}},
        turn_documents=["uploads/a.pdf"],
        turn_document_original_names=["uploads/a.pdf"],  # type: ignore[arg-type]
    )
    assert dict(ctx.turn_document_original_names) == {}
