"""extract_turns —— transcript 抽取的纯函数形态(P2 Task 1)。"""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from control_plane.transcript import extract_turns


def test_extract_turns_keeps_only_human_and_ai_text() -> None:
    raw = [
        SystemMessage(content="你是助手"),
        HumanMessage(content="你好"),
        AIMessage(content="", tool_calls=[{"id": "1", "name": "t", "args": {}}]),
        ToolMessage(content="结果", tool_call_id="1"),
        AIMessage(content="答案"),
    ]
    turns = extract_turns(raw, include_hidden=False)
    assert [(t.seq, t.role, t.content) for t in turns] == [
        (1, "user", "你好"),
        (4, "assistant", "答案"),
    ]
    assert turns[1].channel == "final"


def test_extract_turns_hidden_filter() -> None:
    raw = [
        HumanMessage(content="你好"),
        HumanMessage(
            content="<recovery-advisory>", additional_kwargs={"expert_work_hide_from_ui": True}
        ),
        AIMessage(content="答案"),
    ]
    assert len(extract_turns(raw, include_hidden=True)) == 3
    assert len(extract_turns(raw, include_hidden=False)) == 2
