"""Unit tests for the ``bash`` tool — Stream TE-5.

The Sandbox Supervisor is faked via :class:`RecordingSandboxRuntime`,
so these run in the plain ``pytest`` job (no Docker). bash rides the
``exec`` channel as a ``subprocess`` wrapper, so the assertions focus on
the wrapper that gets executed, the irreversible classification, and the
shared acquire / release / cancel flow (already covered for exec_python).
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from expert_work.protocol import BuiltinToolSpec
from orchestrator.errors import AgentFactoryError
from orchestrator.tools import (
    BashTool,
    RecordingSandboxRuntime,
    SandboxOutcome,
    ToolBlockedError,
    ToolContext,
    ToolEnv,
    build_tool_registry,
)


def _ctx(*, user_id: UUID | None = None) -> ToolContext:
    return ToolContext(tenant_id=uuid4(), run_id=uuid4(), user_id=user_id)


# --- spec / classification -------------------------------------------------


def test_bash_spec_advertises_command_and_is_irreversible() -> None:
    spec = BashTool(client=RecordingSandboxRuntime()).spec
    assert spec.name == "bash"
    assert "command" in spec.parameters["properties"]
    assert spec.parameters["required"] == ["command"]
    # TE-5 / TE-ADR-1 — bash is irreversible → forced-serial scheduling + audit
    # (the approval gate is now config-driven, not auto-applied to irreversible).
    assert spec.side_effect == "irreversible"
    assert spec.resolved_side_effect == "irreversible"


# --- wrapper: runs the command, propagates exit code -----------------------


@pytest.mark.asyncio
async def test_bash_runs_command_via_subprocess_wrapper() -> None:
    client = RecordingSandboxRuntime(
        outcome=SandboxOutcome(stdout="hello\n", stderr="", exit_code=0, timed_out=False)
    )
    tool = BashTool(client=client)
    result = await tool.call({"command": "echo hello"}, ctx=_ctx())

    assert "hello" in result.content
    assert "exit_code: 0" in result.content
    assert result.meta["exit_code"] == 0
    # The exec'd code is a subprocess wrapper carrying the command via repr().
    assert len(client.execs) == 1
    executed_code = client.execs[0][1]
    assert "subprocess.run" in executed_code
    assert "shell=True" in executed_code
    assert "'echo hello'" in executed_code
    # Signal-killed commands map to the shell convention (128 + N).
    assert "128 - rc" in executed_code
    assert len(client.released) == 1
    assert client.destroyed == []


@pytest.mark.asyncio
async def test_bash_meta_carries_streams() -> None:
    # bash shares format_sandbox_outcome with exec_python — same structured
    # meta contract (PR-D).
    client = RecordingSandboxRuntime(
        outcome=SandboxOutcome(stdout="hello\n", stderr="warn\n", exit_code=0, timed_out=False)
    )
    tool = BashTool(client=client)

    result = await tool.call({"command": "echo hello"}, ctx=_ctx())

    assert result.meta["stdout"] == "hello\n"
    assert result.meta["stderr"] == "warn\n"


@pytest.mark.asyncio
async def test_bash_wrapper_is_valid_python_even_for_nasty_command() -> None:
    # A command with quotes, newlines and backslashes must still produce a
    # compilable wrapper — repr() makes it a safe Python string literal,
    # so there is no Python-level injection.
    nasty = "echo \"a'b\"; printf 'x\\n'\n# trailing"
    client = RecordingSandboxRuntime()
    await BashTool(client=client).call({"command": nasty}, ctx=_ctx())
    executed_code = client.execs[0][1]
    # Must compile (would raise SyntaxError if the embedding were unsafe).
    compile(executed_code, "<wrapper>", "exec")


@pytest.mark.asyncio
async def test_bash_propagates_nonzero_exit_code() -> None:
    client = RecordingSandboxRuntime(
        outcome=SandboxOutcome(stdout="", stderr="boom\n", exit_code=2, timed_out=False)
    )
    result = await BashTool(client=client).call({"command": "false"}, ctx=_ctx())
    assert result.meta["exit_code"] == 2
    assert "exit_code: 2" in result.content
    assert "boom" in result.content


# --- validation / tenant scoping -------------------------------------------


@pytest.mark.asyncio
async def test_bash_requires_tenant_binding() -> None:
    tool = BashTool(client=RecordingSandboxRuntime())
    with pytest.raises(ToolBlockedError, match="tenant binding"):
        await tool.call({"command": "ls"}, ctx=ToolContext(tenant_id=None))


@pytest.mark.asyncio
async def test_bash_requires_non_empty_command() -> None:
    tool = BashTool(client=RecordingSandboxRuntime())
    with pytest.raises(ValueError, match="non-empty 'command'"):
        await tool.call({"command": "   "}, ctx=_ctx())


# --- shared sandbox flow: cancel + persistent workspace --------------------


@pytest.mark.asyncio
async def test_bash_destroys_sandbox_on_cancellation() -> None:
    client = RecordingSandboxRuntime(exec_error=asyncio.CancelledError())
    tool = BashTool(client=client)
    with pytest.raises(asyncio.CancelledError):
        await tool.call({"command": "sleep 99"}, ctx=_ctx())
    assert client.released == []
    assert len(client.destroyed) == 1
    assert client.destroyed[0][1] == "cancelled"


@pytest.mark.asyncio
async def test_bash_user_run_passes_user_id_automatically() -> None:
    # Durability is automatic: a user-scoped run mounts the user's workspace
    # volume, no manifest flag needed.
    client = RecordingSandboxRuntime()
    user_id = uuid4()
    tool = BashTool(client=client)
    await tool.call({"command": "ls"}, ctx=_ctx(user_id=user_id))
    assert client.acquired[0][2] == user_id


@pytest.mark.asyncio
async def test_bash_without_user_falls_back_to_tmpfs() -> None:
    client = RecordingSandboxRuntime()
    tool = BashTool(client=client)
    await tool.call({"command": "ls"}, ctx=_ctx())
    assert client.acquired[0][2] is None


# --- assembly wiring -------------------------------------------------------


@pytest.mark.asyncio
async def test_bash_builtin_assembled_when_supervisor_present() -> None:
    env = ToolEnv(sandbox_runtime=RecordingSandboxRuntime())
    registry = await build_tool_registry([BuiltinToolSpec(name="bash")], tool_env=env)
    spec = registry.get_required("bash").spec
    assert spec.name == "bash"
    assert spec.resolved_side_effect == "irreversible"


@pytest.mark.asyncio
async def test_bash_builtin_missing_supervisor_raises() -> None:
    with pytest.raises(AgentFactoryError, match="no sandbox runtime"):
        await build_tool_registry([BuiltinToolSpec(name="bash")], tool_env=ToolEnv())
