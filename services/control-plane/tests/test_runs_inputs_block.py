"""B-67 §6.2 —— 隐藏「本轮输入」段贴在用户消息之后;replay 同源。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from control_plane.api.runs import build_run_graph_input, replay_graph_input
from control_plane.inputs_block import inputs_block_message, is_inputs_block
from control_plane.supersede import _replay_originals
from expert_work.common.conversation_channel import is_hidden
from expert_work.common.message_stamp import STAMP_RUN_ID
from expert_work.protocol import ArgBindingSpec, PromptVariableSpec

LOGO = "https://x/l.png"


def _jinja_built() -> Any:
    return SimpleNamespace(
        supports_vision=False,
        spotlight_nonce=None,
        max_steps=10,
        max_no_progress=3,
        system_prompt="sys {{ org_logo }}",
        prompt_jinja=True,
        prompt_base="sys {{ org_logo }}",
        prompt_suffix="",
        prompt_variables=(PromptVariableSpec(name="org_logo", description="机构 LOGO"),),
        arg_bindings=(),
    )


def test_jinja_run_gets_a_hidden_stamped_inputs_block_after_the_user_message() -> None:
    rid = uuid4()
    gi = build_run_graph_input(
        _jinja_built(),
        input_text="做方案",
        image_refs=[],
        untrusted_content=None,
        inputs={"org_logo": LOGO},
        run_id=rid,
    )
    system, human, block = gi["messages"]
    assert isinstance(system, SystemMessage)
    assert human.content == "做方案" and not is_hidden(human)
    assert is_inputs_block(block) and is_hidden(block)
    assert block.additional_kwargs[STAMP_RUN_ID] == str(rid)
    assert "org_logo（机构 LOGO）" in block.content  # noqa: RUF001
    assert LOGO not in block.content
    assert LOGO not in system.content  # B:URL 不进提示词
    assert gi["turn_documents"] == [] and gi["turn_image_refs"] == []


def test_bound_variable_line_carries_the_binding_note() -> None:
    """``build_run_graph_input`` 把 ``bound_variable_names(built)`` 接进段落:被某条
    ``arg_bindings`` 引用的变量,行尾带绑定说明。"""
    built = _jinja_built()
    built.arg_bindings = (
        ArgBindingSpec(server="crm", tool="upload", args={"logo_url": "org_logo"}),
    )
    gi = build_run_graph_input(
        built,
        input_text="x",
        image_refs=[],
        untrusted_content=None,
        inputs={"org_logo": LOGO},
        run_id=uuid4(),
    )
    line = next(ln for ln in gi["messages"][2].content.splitlines() if ln.startswith("- org_logo"))
    assert line.endswith("；绑定了它的工具由平台自动填，不用手抄")  # noqa: RUF001


def test_jinja_run_without_run_id_still_appends_the_block_unstamped() -> None:
    gi = build_run_graph_input(
        _jinja_built(), input_text="x", image_refs=[], untrusted_content=None, inputs={}
    )
    assert len(gi["messages"]) == 3
    assert STAMP_RUN_ID not in gi["messages"][2].additional_kwargs
    assert "本轮未提供" in gi["messages"][2].content


def test_non_jinja_run_is_byte_identical_two_messages() -> None:
    built = SimpleNamespace(
        supports_vision=False,
        spotlight_nonce=None,
        max_steps=10,
        max_no_progress=3,
        system_prompt="sys",
    )
    gi = build_run_graph_input(
        built, input_text="hi", image_refs=[], untrusted_content=None, run_id=uuid4()
    )
    assert [m.content for m in gi["messages"]] == ["sys", "hi"]


def test_non_jinja_agent_with_declared_variables_gets_no_block() -> None:
    """裁定 8 —— 段落只在 ``prompt_jinja`` 为真时生成;声明了变量也不行。"""
    built = _jinja_built()
    built.prompt_jinja = False
    built.system_prompt = "sys"
    gi = build_run_graph_input(
        built, input_text="hi", image_refs=[], untrusted_content=None, run_id=uuid4()
    )
    assert [m.content for m in gi["messages"]] == ["sys", "hi"]


def test_replay_keeps_the_inputs_block_with_fresh_id_and_new_stamp() -> None:
    old, new = uuid4(), uuid4()
    gi = build_run_graph_input(
        _jinja_built(),
        input_text="做方案",
        image_refs=[],
        untrusted_content=None,
        inputs={"org_logo": LOGO},
        run_id=old,
    )
    out = replay_graph_input(
        SimpleNamespace(max_steps=5, max_no_progress=0), gi["messages"], run_id=new
    )
    assert len(out["messages"]) == 3
    human, block = out["messages"][1], out["messages"][2]
    assert human.additional_kwargs[STAMP_RUN_ID] == str(new)
    assert is_inputs_block(block) and block.additional_kwargs[STAMP_RUN_ID] == str(new)
    assert block.id not in (None, gi["messages"][2].id)
    assert "turn_documents" not in out


def test_replay_of_a_two_message_turn_is_unchanged() -> None:
    out = replay_graph_input(
        SimpleNamespace(max_steps=5, max_no_progress=0),
        [SystemMessage(content="s"), HumanMessage(content="u")],
        run_id=uuid4(),
    )
    assert len(out["messages"]) == 2


@pytest.mark.parametrize("count", [1, 4])
def test_replay_rejects_a_turn_that_is_not_two_or_three_messages(count: int) -> None:
    msgs = [SystemMessage(content="s"), *[HumanMessage(content="u") for _ in range(count - 1)]]
    with pytest.raises(ValueError, match="2 or 3"):
        replay_graph_input(SimpleNamespace(max_steps=5, max_no_progress=0), msgs, run_id=uuid4())


def test_replay_originals_take_the_inputs_block_but_not_other_hidden_messages() -> None:
    system, human = SystemMessage(content="s"), HumanMessage(content="u")
    block = inputs_block_message("[本轮输入]")
    other_hidden = HumanMessage(
        content="[system reminder]", additional_kwargs={"expert_work_hide_from_ui": True}
    )
    assert _replay_originals([system, human, block, AIMessage(content="a")]) == (
        system,
        human,
        block,
    )
    assert _replay_originals([system, human, other_hidden]) == (system, human)
    assert _replay_originals([system, human]) == (system, human)
    assert _replay_originals([system, other_hidden]) is None
    assert _replay_originals([system]) is None
