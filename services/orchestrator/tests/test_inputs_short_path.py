# ruff: noqa: RUF001 — 断言的就是面向模型的中文全角标点
"""B-317 —— ``inputs/current/`` 短路径:所有工具经 ``resolve_workspace_path`` 改写到本轮的
``inputs/<run>/``,落盘写真实路径,回给模型的写法留短路径。"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from orchestrator.tools.registry import ToolContext
from orchestrator.tools.workspace_paths import (
    WorkspaceView,
    agent_relative,
    resolve_workspace_path,
)

RUN = UUID("11111111-2222-3333-4444-555555555555")
CONV = UUID("99999999-8888-7777-6666-555555555555")
KEY = "plan-aaaaaaaa"


def _legacy(inputs_run: UUID | None = RUN) -> WorkspaceView:
    return WorkspaceView(agent_key=KEY, thread_id=CONV, inputs_run=inputs_run)


def _conversation(inputs_run: UUID | None = RUN) -> WorkspaceView:
    return WorkspaceView(agent_key=KEY, conv_id=CONV, thread_id=CONV, inputs_run=inputs_run)


@pytest.mark.parametrize("view", [_legacy(), _conversation()])
@pytest.mark.parametrize(
    "written",
    [
        "inputs/current/org_logo.png",
        "/workspace/inputs/current/org_logo.png",
        "$EXPERT_WORK_INPUTS_DIR/org_logo.png",
        "${EXPERT_WORK_INPUTS_DIR}/org_logo.png",
    ],
)
def test_short_path_and_alias_resolve_to_the_runs_real_path(
    view: WorkspaceView, written: str
) -> None:
    target = resolve_workspace_path(written, view=view, tool="ask_image")
    real = f"inputs/{RUN}/org_logo.png"
    assert target.rel == real
    # H1 —— 落盘 / 跨轮用的写法是真实路径;回给模型的是短路径(别名也归一成短路径)。
    assert target.path == real
    assert target.display == "inputs/current/org_logo.png"


def test_nested_short_path_keeps_the_rest() -> None:
    target = resolve_workspace_path(
        "inputs/current/materials/0-示范视频.mp4", view=_legacy(), tool="read_document"
    )
    assert target.rel == f"inputs/{RUN}/materials/0-示范视频.mp4"


def test_conversation_view_places_the_run_dir_under_the_conversation() -> None:
    view = _conversation()
    target = resolve_workspace_path("inputs/current/x.png", view=view, tool="ask_image")
    assert agent_relative(target, view) == f"threads/{CONV}/work/inputs/{RUN}/x.png"


@pytest.mark.parametrize("view", [_legacy(None), _conversation(None)])
def test_without_inputs_this_turn_the_short_path_is_refused(view: WorkspaceView) -> None:
    with pytest.raises(ValueError, match="本轮没有输入变量") as info:
        resolve_workspace_path("inputs/current/x.png", view=view, tool="ask_image")
    assert "回执末尾的「之后的轮次请用 …」" in str(info.value)
    assert "list_dir 查看 inputs/" in str(info.value)


def test_legacy_agent_spelling_is_refused_with_the_right_spelling() -> None:
    with pytest.raises(ValueError, match=r"只在 /workspace 下有效，请写 inputs/current/<文件名>"):
        resolve_workspace_path("/agent/inputs/current/x.png", view=_legacy(), tool="read_file")


def test_conversation_agent_spelling_keeps_todays_platform_dir_refusal() -> None:
    with pytest.raises(ValueError, match="not part of this conversation's view"):
        resolve_workspace_path(
            "/agent/inputs/current/x.png", view=_conversation(), tool="read_file"
        )


@pytest.mark.parametrize(
    "written",
    [
        f"inputs/{RUN}/org_logo.png",
        "inputs/cache/abc.png",
        "inputs/currentx/a.png",
        "a/$EXPERT_WORK_INPUTS_DIR/x.png",
        "work/inputs/current/x.png",
    ],
)
def test_other_paths_resolve_exactly_as_before(written: str) -> None:
    with_run = resolve_workspace_path(written, view=_legacy(), tool="read_file")
    without = resolve_workspace_path(
        written, view=WorkspaceView(agent_key=KEY, thread_id=CONV), tool="read_file"
    )
    assert with_run == replace(without, display=with_run.display)
    assert with_run.rel == written
    assert with_run.path == with_run.display == written


def _tool_ctx(**overrides: object) -> ToolContext:
    base = ToolContext(
        tenant_id=uuid4(),
        run_id=uuid4(),
        agent_key=KEY,
        prompt_inputs={"org_logo": "https://x.invalid/a.png"},
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_tool_context_view_carries_this_turns_inputs_run() -> None:
    ctx = _tool_ctx()
    assert ctx.workspace_view.inputs_run == ctx.run_id
    first = uuid4()
    assert _tool_ctx(inputs_run_id=first).workspace_view.inputs_run == first


@pytest.mark.parametrize(
    "overrides",
    [{"prompt_inputs": None}, {"prompt_inputs": {}}, {"agent_key": ""}],
)
def test_tool_context_view_has_no_inputs_run_without_inputs_or_agent(
    overrides: dict[str, object],
) -> None:
    assert _tool_ctx(**overrides).workspace_view.inputs_run is None


def test_a_delegated_child_resolves_to_the_parents_turn() -> None:
    from orchestrator.graph_builder.builder import _build_tool_context
    from orchestrator.tools._child_run import _child_config

    parent = _tool_ctx(thread_id=uuid4())
    config = _child_config(parent, sub_thread_id=uuid4(), sub_run_id=uuid4())
    child = _build_tool_context(config)
    assert child.run_id != parent.run_id
    assert child.workspace_view.inputs_run == parent.run_id
