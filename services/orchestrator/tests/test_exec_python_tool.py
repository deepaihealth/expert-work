"""Unit tests for the ``exec_python`` tool — Stream F.4b (test matrix #46).

The Sandbox Supervisor is faked via :class:`RecordingSandboxRuntime`,
so these run in the plain ``pytest`` job — no Docker, no supervisor.
The old sandbox-exec call denylist was removed (audit over blocking — the
gVisor sandbox is the real boundary); submitted code is now recorded into the
tool audit (see ``_emit_tool_audit`` in ``graph_builder/builder.py`` +
docs/design/sandbox-audit-evaluation.md).
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from expert_work.protocol import BuiltinToolSpec
from orchestrator.errors import AgentFactoryError
from orchestrator.tools import (
    DEFAULT_OUTPUT_CHAR_CAP,
    ExecPythonTool,
    RecordingSandboxRuntime,
    SandboxOutcome,
    ToolBlockedError,
    ToolContext,
    ToolEnv,
    build_tool_registry,
)


def _ctx(*, user_id: UUID | None = None) -> ToolContext:
    return ToolContext(tenant_id=uuid4(), run_id=uuid4(), user_id=user_id)


# ---------------------------------------------------------------------------
# the tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_python_runs_code_and_returns_output() -> None:
    client = RecordingSandboxRuntime(
        outcome=SandboxOutcome(stdout="42\n", stderr="", exit_code=0, timed_out=False)
    )
    tool = ExecPythonTool(client=client)

    result = await tool.call({"code": "print(6 * 7)"}, ctx=_ctx())

    assert "42" in result.content
    assert "exit_code: 0" in result.content
    assert result.meta["exit_code"] == 0
    # Un-truncated output carries no overflow payload (Stream CM-5).
    assert result.full_content is None
    # acquire → exec → release, all once; a clean run never force-destroys.
    assert len(client.acquired) == 1
    assert len(client.execs) == 1
    assert len(client.released) == 1
    assert client.destroyed == []


@pytest.mark.asyncio
async def test_exec_python_passes_run_id_to_exec() -> None:
    """B-61 §4.4 —— ``run_in_sandbox`` 把 ``ctx.run_id`` 原样带给
    ``client.exec``,这是唯一的生产调用点(``exec_python``/``bash``/
    ``read_file``/``write_file`` 等共用)。忘了传的话两个真实后端都拿不到
    ``EXPERT_WORK_INPUTS`` 该有的 ``run_id`` —— 这条钉住这一层不被静默丢掉。"""
    client = RecordingSandboxRuntime()
    ctx = _ctx()

    await ExecPythonTool(client=client).call({"code": "print(1)"}, ctx=ctx)

    assert client.exec_run_ids == [ctx.run_id]


@pytest.mark.asyncio
async def test_a_child_exec_points_at_the_parents_inputs_file() -> None:
    """终审 finding 4 —— 委派出的子代每次都新铸一个 ``sub_run_id``,而 inputs 节点
    **故意不为子 run 写文件**:不把父的 run id 带下去,子代的 ``EXPERT_WORK_INPUTS``
    就是 ``inputs/<sub_run_id>/inputs.json`` 这条悬空路径,而工具描述告诉模型文件在
    —— 模型读不到就退回从上文手抄 URL,正是 B-61 要消灭的那个失败(委派型 agent
    如 ai-health-plan 把写 PPT 的活交给 worker,真实场景踩的就是这条)。

    钉的是整条链:``_child_config`` → ``_build_tool_context`` → ``run_in_sandbox``
    → ``agent_key_envs``,任一环丢掉都红。
    """
    from orchestrator.graph_builder.builder import _build_tool_context
    from orchestrator.tools._child_run import _child_config
    from orchestrator.tools.inputs_doc import inputs_abs_path
    from orchestrator.tools.sandbox import agent_key_envs

    parent_ctx = ToolContext(tenant_id=uuid4(), run_id=uuid4(), agent_key="ai-health-plan-3081")
    sub_run_id = uuid4()
    child_ctx = _build_tool_context(
        _child_config(parent_ctx, sub_thread_id=uuid4(), sub_run_id=sub_run_id)
    )
    assert child_ctx.run_id == sub_run_id, "子 run 仍有自己的 run_id(审计/检查点靠它)"

    client = RecordingSandboxRuntime()
    await ExecPythonTool(client=client).call({"code": "print(1)"}, ctx=child_ctx)

    assert client.exec_run_ids == [parent_ctx.run_id]
    envs = agent_key_envs(child_ctx.agent_key, run_id=client.exec_run_ids[0])
    assert envs["EXPERT_WORK_INPUTS"] == inputs_abs_path(parent_ctx.run_id)


def test_a_grandchild_still_points_at_the_top_run() -> None:
    """再深一层(worker 又派 worker)不能指回中间那个子 run —— 文件只有最上面那个
    run 有。``_child_config`` 取的是 ``ctx.inputs_run_id or ctx.run_id``。"""
    from orchestrator.graph_builder.builder import _build_tool_context
    from orchestrator.tools._child_run import _child_config

    parent_ctx = ToolContext(tenant_id=uuid4(), run_id=uuid4())
    child_ctx = _build_tool_context(
        _child_config(parent_ctx, sub_thread_id=uuid4(), sub_run_id=uuid4())
    )
    grandchild_ctx = _build_tool_context(
        _child_config(child_ctx, sub_thread_id=uuid4(), sub_run_id=uuid4())
    )

    assert grandchild_ctx.inputs_run_id == parent_ctx.run_id


@pytest.mark.asyncio
async def test_exec_python_passes_skill_seed_files_to_acquire() -> None:
    # skill-runtime §5.1 — the build-bound skill seed set reaches acquire so the
    # supervisor materializes /opt/skills/<agent_key>/<name>/ before the code
    # runs (sandbox migration wave 2). This test only checks ExecPythonTool
    # forwards whatever relpath it was given — the agent_key prefix itself is
    # build_skill_seed_files's job (see test_skill_seed.py).
    client = RecordingSandboxRuntime(
        outcome=SandboxOutcome(stdout="", stderr="", exit_code=0, timed_out=False)
    )
    seed = (("agent-1/pptx/SKILL.md", b"---\nname: pptx\n---\n"),)
    tool = ExecPythonTool(client=client, skill_seed_files=seed)

    await tool.call({"code": "pass"}, ctx=_ctx())

    assert client.acquired[0][3] == seed  # 4th acquire tuple slot = seed_files


@pytest.mark.asyncio
async def test_exec_python_truncates_oversized_output() -> None:
    # The supervisor returns 50k chars; the tool caps each stream at 20k.
    client = RecordingSandboxRuntime(
        outcome=SandboxOutcome(stdout="x" * 50_000, stderr="", exit_code=0, timed_out=False)
    )
    tool = ExecPythonTool(client=client)

    result = await tool.call({"code": "print('x' * 50000)"}, ctx=_ctx())

    assert result.meta["truncated"] is True
    # content = "stdout:\n" + capped-stdout + marker + exit-code line.
    assert len(result.content) < DEFAULT_OUTPUT_CHAR_CAP + 200
    assert "[truncated]" in result.content
    # Stream CM-5: the complete rendering rides along for externalization.
    assert result.full_content is not None
    assert "x" * 50_000 in result.full_content
    assert "exit_code: 0" in result.full_content
    assert "[truncated]" not in result.full_content


@pytest.mark.asyncio
async def test_exec_python_meta_carries_streams() -> None:
    # PR-D — the debug console reads stdout/stderr from the structured
    # ``meta`` (→ ToolMessage.artifact) because the rendered ``content``
    # is spotlight-datamarked (newlines destroyed) on the wire.
    client = RecordingSandboxRuntime(
        outcome=SandboxOutcome(stdout="42\n", stderr="boom\n", exit_code=3, timed_out=False)
    )
    tool = ExecPythonTool(client=client)

    result = await tool.call({"code": "print(6 * 7)"}, ctx=_ctx())

    assert result.meta["stdout"] == "42\n"
    assert result.meta["stderr"] == "boom\n"
    assert result.meta["exit_code"] == 3


@pytest.mark.asyncio
async def test_exec_python_meta_streams_are_capped() -> None:
    # meta rides the SSE / audit / trace path — it carries the same
    # head-truncated streams the rendered content shows, never the raw 1MB.
    client = RecordingSandboxRuntime(
        outcome=SandboxOutcome(stdout="x" * 50_000, stderr="", exit_code=0, timed_out=False)
    )
    tool = ExecPythonTool(client=client)

    result = await tool.call({"code": "print('x' * 50000)"}, ctx=_ctx())

    assert result.meta["truncated"] is True
    assert result.meta["stdout"].endswith("...[truncated]")
    assert len(result.meta["stdout"]) == DEFAULT_OUTPUT_CHAR_CAP + len("...[truncated]")
    assert result.meta["stderr"] == ""


@pytest.mark.asyncio
async def test_exec_python_reports_timeout() -> None:
    client = RecordingSandboxRuntime(
        outcome=SandboxOutcome(stdout="", stderr="", exit_code=-1, timed_out=True)
    )
    tool = ExecPythonTool(client=client)

    result = await tool.call({"code": "while True: pass"}, ctx=_ctx())

    assert result.meta["timed_out"] is True
    assert "timed out" in result.content
    # Actionable recovery hint so the model retries with a larger budget
    # instead of stalling (e.g. a slow ``pip install``).
    assert "timeout_s" in result.content


@pytest.mark.asyncio
async def test_exec_python_requires_tenant_binding() -> None:
    tool = ExecPythonTool(client=RecordingSandboxRuntime())
    with pytest.raises(ToolBlockedError, match="tenant binding"):
        await tool.call({"code": "print(1)"}, ctx=ToolContext(tenant_id=None))


@pytest.mark.asyncio
async def test_exec_python_requires_code() -> None:
    tool = ExecPythonTool(client=RecordingSandboxRuntime())
    with pytest.raises(ValueError, match="non-empty 'code'"):
        await tool.call({"code": "   "}, ctx=_ctx())


@pytest.mark.asyncio
async def test_exec_python_releases_sandbox_even_on_exec_error() -> None:
    client = RecordingSandboxRuntime(exec_error=RuntimeError("runner died"))
    tool = ExecPythonTool(client=client)

    with pytest.raises(RuntimeError, match="runner died"):
        await tool.call({"code": "print(1)"}, ctx=_ctx())
    # An ordinary error is a graceful release — not a forced destroy.
    assert len(client.released) == 1
    assert client.destroyed == []


@pytest.mark.asyncio
async def test_exec_python_passes_timeout_through() -> None:
    client = RecordingSandboxRuntime()
    tool = ExecPythonTool(client=client)

    await tool.call({"code": "print(1)", "timeout_s": 15}, ctx=_ctx())
    # The supervisor was acquired with a thread label derived from run_id.
    assert client.execs[0][1] == "print(1)"


def test_exec_python_spec_advertises_code_param() -> None:
    spec = ExecPythonTool(client=RecordingSandboxRuntime()).spec
    assert spec.name == "exec_python"
    assert "code" in spec.parameters["required"]


# ---------------------------------------------------------------------------
# workspace durability — automatic for user-scoped runs (no manifest flag)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_python_user_run_passes_user_id_without_flag() -> None:
    # Durability is automatic: a user-scoped run acquires against the user's
    # persistent workspace volume even though no manifest flag is set.
    client = RecordingSandboxRuntime()
    tool = ExecPythonTool(client=client)  # no persistent_workspace knob anymore
    user_id = uuid4()

    await tool.call({"code": "print(1)"}, ctx=_ctx(user_id=user_id))

    assert client.acquired[0][2] == user_id


@pytest.mark.asyncio
async def test_exec_python_without_user_falls_back_to_tmpfs() -> None:
    # No user binding → no volume mount (ephemeral tmpfs).
    client = RecordingSandboxRuntime()
    tool = ExecPythonTool(client=client)

    await tool.call({"code": "print(1)"}, ctx=_ctx())

    assert client.acquired[0][2] is None


# ---------------------------------------------------------------------------
# cancellation — Stream F.7 (test matrix #58)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_python_destroys_sandbox_on_cancellation() -> None:
    """A cancelled run force-destroys the sandbox, never a graceful release."""
    # E.15 cancels the dispatch task → CancelledError on the exec ``await``.
    client = RecordingSandboxRuntime(exec_error=asyncio.CancelledError())
    tool = ExecPythonTool(client=client)

    with pytest.raises(asyncio.CancelledError):
        await tool.call({"code": "while True: pass"}, ctx=_ctx())

    assert client.released == []
    assert len(client.destroyed) == 1
    # The supervisor sees reason="cancelled" → SIGKILL + force-destroy audit.
    assert client.destroyed[0][1] == "cancelled"


@pytest.mark.asyncio
async def test_exec_python_cancellation_destroy_failure_is_swallowed() -> None:
    """A failed destroy must not mask the cancellation (TTL reaper backstops)."""
    client = RecordingSandboxRuntime(
        exec_error=asyncio.CancelledError(),
        destroy_error=RuntimeError("supervisor unreachable"),
    )
    tool = ExecPythonTool(client=client)

    # The CancelledError still propagates — the destroy error is swallowed.
    with pytest.raises(asyncio.CancelledError):
        await tool.call({"code": "while True: pass"}, ctx=_ctx())


# ---------------------------------------------------------------------------
# assembly — the exec_python builtin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_python_builtin_assembled_when_supervisor_present() -> None:
    env = ToolEnv(sandbox_runtime=RecordingSandboxRuntime())
    registry = await build_tool_registry([BuiltinToolSpec(name="exec_python")], tool_env=env)
    assert registry.get("exec_python") is not None


@pytest.mark.asyncio
async def test_exec_python_builtin_missing_supervisor_raises() -> None:
    with pytest.raises(AgentFactoryError, match="sandbox runtime"):
        await build_tool_registry([BuiltinToolSpec(name="exec_python")], tool_env=ToolEnv())


@pytest.mark.asyncio
async def test_exec_python_builtin_durability_is_automatic() -> None:
    # No manifest flag is threaded; the assembled tool relies on ctx.user_id at
    # call time for durability. Acquire carries the run's user.
    env = ToolEnv(sandbox_runtime=RecordingSandboxRuntime())
    registry = await build_tool_registry([BuiltinToolSpec(name="exec_python")], tool_env=env)
    tool = registry.get("exec_python")
    assert isinstance(tool, ExecPythonTool)

    user_id = uuid4()
    await tool.call({"code": "print(1)"}, ctx=_ctx(user_id=user_id))
    client = env.sandbox_runtime
    assert isinstance(client, RecordingSandboxRuntime)
    assert client.acquired[0][2] == user_id


def _inputs_clause(description: str) -> str:
    """工具描述里 B-61 那一段(从「本轮如果有输入变量」起到结尾)。"""
    marker = "本轮如果有输入变量"
    assert marker in description, "B-61 注入变量那段不见了"
    return description[description.index(marker) :]


def test_both_sandbox_tool_descriptions_state_the_local_path_fallback() -> None:
    """``exec_python`` 与 ``bash`` 的描述都要带上 ``local_path`` 失效时的回退。

    描述就是模型据以行动的东西,所以它必须与保留期契约一致:缓存 7 天、run 目录 30 天
    (见 ``control_plane.workspace_janitor``),于是一个挂起很久才续跑的轮次**可能**看到
    一个非空但指向已回收文件的 ``local_path``。描述如果还硬说「非空 = 文件在,直接用」,
    模型就会在这个功能本该救场的那一刻做错事。

    **常见情形必须排在前面**:绝大多数时候文件就在,直接用、不要联网 —— 一段让每次读取
    都听起来有风险的描述会把模型推回「自己下载」,而那正是本项目消灭掉的行为。

    三条断言各有分工:① 常见路径还在(没被改写成防御性措辞);② 回退子句在;
    ③ **两个文件的这一段逐字节相同** —— 只改一个文件就红,这是本条最主要的防漂移作用
    (在此之前这段措辞没有任何测试覆盖,所以它才会悄悄过期)。
    """
    from orchestrator.tools.bash import BashTool

    exec_clause = _inputs_clause(ExecPythonTool(client=RecordingSandboxRuntime()).spec.description)
    bash_clause = _inputs_clause(BashTool(client=RecordingSandboxRuntime()).spec.description)

    for clause in (exec_clause, bash_clause):
        assert "local_path 非空表示平台已把该文件下载到本地，直接用它，不必再联网下载" in clause  # noqa: RUF001
        assert "该文件已被清理，这时改用同一项里的原始 URL 自己下载" in clause  # noqa: RUF001
    assert exec_clause == bash_clause, "两个工具的 B-61 段落必须逐字节相同"


def test_description_tells_the_model_about_the_inputs_dir() -> None:
    """B-67 —— 描述里提 $EXPERT_WORK_INPUTS_DIR、按变量名的文件与列表项的 local_path 位置;
    平台文本,不写任何租户的变量名。"""
    description = ExecPythonTool(client=RecordingSandboxRuntime()).spec.description
    assert "$EXPERT_WORK_INPUTS_DIR" in description
    assert "<变量名>" in description
    assert "value_parsed[i].local_path" in description
    for tenant_name in ("org_logo", "materials", "ai-health-plan"):
        assert tenant_name not in description
