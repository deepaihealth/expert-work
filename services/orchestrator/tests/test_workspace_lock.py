"""Stream TE-8 — per-workspace write lock wiring (orchestrator side).

Verifies the tool layer holds the :class:`WorkspaceLock` around the *write*
exec (``write_file`` / ``bash``) and that reads are lock-free. The real
cross-replica behaviour (PG advisory lock serialises concurrent writers) is
covered by the control-plane integration test; here the lock is a recording
double and a probe client asserts the exec ran while the lock was held.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, fields
from uuid import UUID, uuid4

import pytest

from orchestrator.tools import (
    BashTool,
    EditFileTool,
    ListDirTool,
    NullWorkspaceLock,
    ReadFileTool,
    RecordingSandboxRuntime,
    RecordingWorkspaceLock,
    SandboxOutcome,
    ToolContext,
    WriteFileTool,
)

_OK_WRITE = json.dumps({"ok": True, "content_hash": "h", "size": 1, "path": "a.txt"})


def _ctx() -> ToolContext:
    return ToolContext(tenant_id=uuid4(), run_id=uuid4(), user_id=uuid4())


@dataclass
class _LockProbeClient:
    """Minimal SandboxRuntime that records the lock depth seen at exec time."""

    lock: RecordingWorkspaceLock
    envelope: str
    active_at_exec: int = -1
    released: list[UUID] = field(default_factory=list)

    async def acquire(
        self,
        *,
        tenant_id: UUID,
        thread_id: str,
        user_id: UUID | None = None,
        seed_files: tuple[tuple[str, bytes], ...] = (),
    ) -> UUID:
        return uuid4()

    async def exec(self, *, sandbox_id: UUID, code: str, timeout_s: int | None) -> SandboxOutcome:
        self.active_at_exec = self.lock.active
        return SandboxOutcome(stdout=self.envelope, stderr="", exit_code=0, timed_out=False)

    async def release(self, *, sandbox_id: UUID) -> None:
        self.released.append(sandbox_id)

    async def destroy(self, *, sandbox_id: UUID, reason: str) -> None:
        return None

    async def read_workspace_file(self, *, tenant_id: UUID, user_id: UUID, path: str) -> bytes:
        return b""

    async def reap(self, *, force: bool) -> int:
        return 0


async def test_null_lock_is_noop() -> None:
    async with NullWorkspaceLock().acquire(tenant_id=uuid4(), user_id=uuid4()):
        pass  # no error, no guarantee


async def test_recording_lock_tracks_depth() -> None:
    lock = RecordingWorkspaceLock()
    tenant, user = uuid4(), uuid4()
    assert lock.active == 0
    async with lock.acquire(tenant_id=tenant, user_id=user):
        assert lock.active == 1
    assert lock.active == 0
    assert lock.acquired == [(tenant, user)]


async def test_write_file_holds_lock_during_exec() -> None:
    lock = RecordingWorkspaceLock()
    client = _LockProbeClient(lock=lock, envelope=_OK_WRITE)
    ctx = _ctx()
    tool = WriteFileTool(client=client, workspace_lock=lock)
    await tool.call({"path": "a.txt", "content": "x"}, ctx=ctx)
    assert client.active_at_exec == 1  # exec ran inside the lock
    assert lock.acquired == [(ctx.tenant_id, ctx.user_id)]
    assert lock.active == 0  # released on exit


async def test_write_file_user_less_locks_with_none_user() -> None:
    # A user-less (ephemeral) run isn't shared; the lock key uses user_id=None
    # (the PG impl then treats it as a no-op).
    lock = RecordingWorkspaceLock()
    client = _LockProbeClient(lock=lock, envelope=_OK_WRITE)
    ctx = ToolContext(tenant_id=uuid4(), run_id=uuid4(), user_id=None)
    tool = WriteFileTool(client=client, workspace_lock=lock)
    await tool.call({"path": "a.txt", "content": "x"}, ctx=ctx)
    assert lock.acquired == [(ctx.tenant_id, None)]


async def test_bash_holds_lock_during_exec() -> None:
    lock = RecordingWorkspaceLock()
    client = _LockProbeClient(lock=lock, envelope="done")
    ctx = _ctx()
    tool = BashTool(client=client, workspace_lock=lock)
    await tool.call({"command": "echo hi"}, ctx=ctx)
    assert client.active_at_exec == 1
    assert lock.acquired == [(ctx.tenant_id, ctx.user_id)]
    assert lock.active == 0


async def test_write_file_releases_lock_on_cancel() -> None:
    # A cancellation mid-exec must unwind through the lock context manager so
    # the lock is released (and, for the PG impl, its transaction rolled back).
    lock = RecordingWorkspaceLock()
    client = RecordingSandboxRuntime()
    client.exec_error = asyncio.CancelledError()
    tool = WriteFileTool(client=client, workspace_lock=lock)
    with pytest.raises(asyncio.CancelledError):
        await tool.call({"path": "a.txt", "content": "x"}, ctx=_ctx())
    assert lock.acquired  # the lock was taken
    assert lock.active == 0  # ...and released despite the cancel


async def test_edit_file_holds_lock_during_exec() -> None:
    lock = RecordingWorkspaceLock()
    client = _LockProbeClient(lock=lock, envelope=_OK_WRITE)
    ctx = _ctx()
    tool = EditFileTool(client=client, workspace_lock=lock)
    await tool.call({"path": "a.txt", "old_string": "a", "new_string": "b"}, ctx=ctx)
    assert client.active_at_exec == 1
    assert lock.acquired == [(ctx.tenant_id, ctx.user_id)]
    assert lock.active == 0


async def test_edit_file_user_less_locks_with_none_user() -> None:
    lock = RecordingWorkspaceLock()
    client = _LockProbeClient(lock=lock, envelope=_OK_WRITE)
    ctx = ToolContext(tenant_id=uuid4(), run_id=uuid4(), user_id=None)
    tool = EditFileTool(client=client, workspace_lock=lock)
    await tool.call({"path": "a.txt", "old_string": "a", "new_string": "b"}, ctx=ctx)
    assert lock.acquired == [(ctx.tenant_id, None)]


def test_reads_are_lock_free() -> None:
    # read_file / list_dir declare no workspace_lock field — reads never
    # serialise; only the write tools carry the lock.
    assert "workspace_lock" not in {f.name for f in fields(ReadFileTool)}
    assert "workspace_lock" not in {f.name for f in fields(ListDirTool)}
    assert "workspace_lock" in {f.name for f in fields(WriteFileTool)}
    assert "workspace_lock" in {f.name for f in fields(EditFileTool)}
    assert "workspace_lock" in {f.name for f in fields(BashTool)}
