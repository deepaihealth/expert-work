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
async def test_exec_python_passes_skill_seed_files_to_acquire() -> None:
    # skill-runtime §5.1 — the build-bound skill seed set reaches acquire so the
    # supervisor materializes /workspace/skills/<name>/ before the code runs.
    client = RecordingSandboxRuntime(
        outcome=SandboxOutcome(stdout="", stderr="", exit_code=0, timed_out=False)
    )
    seed = (("skills/pptx/SKILL.md", b"---\nname: pptx\n---\n"),)
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
