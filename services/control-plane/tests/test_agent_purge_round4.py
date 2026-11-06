"""B-278 review round 4.

* A read-only folder on the way to the target (``chmod 555 /workspace/agents``,
  ``threads/``) must not make the entry undeletable *and* unmovable.
* The purge lock: Postgres must not kill the idle lock session (that silently
  drops the mutex), and a failing release must never replace the purge outcome.
* The holding-area sweep: odd names never crash it, one bad entry / tenant never
  stops the rest.
* Purging a user also removes that user's holding-area directory.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.purge import purge_user
from control_plane.purge.agent_purge_lock import agent_purge_lock
from expert_work.persistence import PURGED_AGENTS_DIR, purged_agents_dir
from expert_work.persistence.workspace import safe_remove
from expert_work.protocol import AuditAction, AuditQuery
from tests.test_agent_lifecycle_api import _YAML_V2, _create_and_delete, _Ctx, _purge
from tests.test_agent_lifecycle_api import ctx as ctx
from tests.test_user_purge import _LogWorld

# --------------------------------------------------------------------------- read-only parents


@pytest.mark.parametrize("mode", [0o555, 0o500, 0o100, 0o000])
def test_a_read_only_agents_folder_does_not_block_removal(tmp_path: Path, mode: int) -> None:
    tenant, user = uuid4(), uuid4()
    agents = tmp_path / str(tenant) / str(user) / "agents"
    (agents / "k-1" / "out").mkdir(parents=True)
    (agents / "k-1" / "out" / "f.txt").write_text("x")
    agents.chmod(mode)

    outcome = safe_remove.remove_entry(
        str(tmp_path), [str(tenant), str(user), "agents"], "k-1", tenant_id=tenant, owner_id=user
    )

    agents.chmod(0o700)
    assert outcome == safe_remove.REMOVED
    assert not os.path.lexists(agents / "k-1")


def test_a_read_only_threads_folder_does_not_block_removal(tmp_path: Path) -> None:
    tenant, user, thread = uuid4(), uuid4(), uuid4()
    threads = tmp_path / str(tenant) / str(user) / "threads"
    (threads / str(thread)).mkdir(parents=True)
    (threads / str(thread) / "PLAN.md").write_text("x")
    threads.chmod(0o555)

    outcome = safe_remove.remove_entry(
        str(tmp_path),
        [str(tenant), str(user), "threads"],
        str(thread),
        tenant_id=tenant,
        owner_id=user,
    )

    threads.chmod(0o700)
    assert outcome == safe_remove.REMOVED
    assert not os.path.lexists(threads / str(thread))


def test_a_read_only_parent_does_not_block_the_quarantine_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, user = uuid4(), uuid4()
    agents = tmp_path / str(tenant) / str(user) / "agents"
    (agents / "k-1").mkdir(parents=True)
    agents.chmod(0o555)

    def _foreign(dfd: int, name: str) -> bool:
        return False

    monkeypatch.setattr(safe_remove, "remove_tree_at", _foreign)

    outcome = safe_remove.remove_entry(
        str(tmp_path), [str(tenant), str(user), "agents"], "k-1", tenant_id=tenant, owner_id=user
    )

    agents.chmod(0o700)
    assert outcome == safe_remove.QUARANTINED
    assert not os.path.lexists(agents / "k-1")
    assert len(list(purged_agents_dir(str(tmp_path), tenant, user).iterdir())) == 1


# --------------------------------------------------------------------------- the purge lock


class _Result:
    def scalar_one(self) -> bool:
        return True


class _DeadOnReleaseSession:
    """The lock session after Postgres (or the network) dropped it: release raises."""

    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def execute(self, statement: Any, params: Any = None) -> _Result:
        self.log.append(str(statement))
        return _Result()

    async def rollback(self) -> None:
        raise ConnectionResetError("connection is closed")

    async def close(self) -> None:
        raise ConnectionResetError("connection is closed")


def _dead_factory(log: list[str]) -> Any:
    def _make() -> _DeadOnReleaseSession:
        return _DeadOnReleaseSession(log)

    return _make


@pytest.mark.asyncio
async def test_the_lock_session_is_never_idle_killed_and_release_never_raises() -> None:
    statements: list[str] = []
    async with agent_purge_lock(_dead_factory(statements), uuid4(), "doomed") as got:
        assert got is True
    assert any("idle_in_transaction_session_timeout = 0" in s for s in statements), statements


@pytest.mark.asyncio
async def test_a_failing_release_keeps_the_purge_outcome_and_audit(ctx: _Ctx) -> None:
    ctx.app.state.session_factory = _dead_factory([])  # type: ignore[attr-defined]
    await _create_and_delete(ctx.client)

    resp = await _purge(ctx.client)

    assert resp.status_code == 200, resp.text  # type: ignore[attr-defined]
    page = await ctx.audit.query(AuditQuery(tenant_id=ctx.tenant_id, limit=1000))
    assert [e for e in page.entries if e.action is AuditAction.MANIFEST_PURGE]


@pytest.mark.asyncio
async def test_a_failing_release_keeps_a_blocked_purge_409(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    import control_plane.api.agent_lifecycle as lifecycle
    from control_plane.purge.agent_purge import AgentPurgeBlockers

    async def _nothing_blocks(*_args: object, **_kwargs: object) -> AgentPurgeBlockers:
        return AgentPurgeBlockers(versions=[], active_versions=[], referenced_by=[])

    monkeypatch.setattr(lifecycle, "find_blockers", _nothing_blocks)
    ctx.app.state.session_factory = _dead_factory([])  # type: ignore[attr-defined]
    await _create_and_delete(ctx.client)
    live = await ctx.client.post("/v1/agents", json={"manifest_yaml": _YAML_V2})
    assert live.status_code == 201

    resp = await _purge(ctx.client)

    assert resp.status_code == 409  # type: ignore[attr-defined]
    assert resp.json()["error"]["code"] == "AGENT_PURGE_BLOCKED"  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- the sweep


def _park(root: Path, tenant: UUID, user: UUID, name: str) -> Path:
    entry = purged_agents_dir(str(root), tenant, user) / name
    (entry / "x").mkdir(parents=True)
    return entry


def test_unicode_digits_in_a_name_are_ignored_not_fatal(tmp_path: Path) -> None:
    tenant, user = uuid4(), uuid4()
    odd = _park(tmp_path, tenant, user, "k-1-²³⁴-aaaaaaaa")
    old = _park(tmp_path, tenant, user, "k-1-1000-bbbbbbbb")

    removed, failed = safe_remove.sweep_quarantine(str(tmp_path), older_than=1_500)

    assert (removed, failed) == (1, 0)
    assert odd.exists() and not old.exists()


def test_one_bad_entry_or_tenant_does_not_stop_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    t_bad, t_ok, user = uuid4(), uuid4(), uuid4()
    stuck = _park(tmp_path, t_ok, user, "k-1-1000-cccccccc")
    gone = _park(tmp_path, t_ok, user, "k-2-1000-dddddddd")
    _park(tmp_path, t_bad, user, "k-3-1000-eeeeeeee")
    real_remove = safe_remove.remove_tree_at
    real_chain = safe_remove._open_dir_chain

    def _remove(dfd: int, name: str) -> bool:
        return False if name == stuck.name else real_remove(dfd, name)

    def _chain(root: str, parts: list[str]) -> tuple[int | None, str]:
        if parts == [str(t_bad), PURGED_AGENTS_DIR]:
            raise PermissionError(13, "Permission denied")
        return real_chain(root, parts)

    monkeypatch.setattr(safe_remove, "remove_tree_at", _remove)
    monkeypatch.setattr(safe_remove, "_open_dir_chain", _chain)

    with caplog.at_level("WARNING"):
        removed, failed = safe_remove.sweep_quarantine(str(tmp_path), older_than=1_500)

    assert (removed, failed) == (1, 2)
    assert stuck.exists() and not gone.exists()
    assert any(
        "sweep_entry_failed" in r.getMessage() and stuck.name in r.getMessage()
        for r in caplog.records
    )


# --------------------------------------------------------------------------- purge_user


@pytest.mark.asyncio
async def test_purging_a_user_removes_their_holding_area(tmp_path: Path) -> None:
    world = _LogWorld()
    user = await world.seed()
    other = uuid4()
    mine = _park(tmp_path, world.t1, user, "k-1-1000-aaaaaaaa")
    theirs = _park(tmp_path, world.t1, other, "k-1-1000-bbbbbbbb")

    summary = await purge_user(
        tenant_id=world.t1,
        user_id=user,
        subject_id="subj-a",
        deps=replace(world.deps(), workspace_root=str(tmp_path)),
        actor_id="admin",
    )

    assert "purged_agents" not in summary.failures
    assert summary.deleted["purged_agents"] == 1
    assert not mine.exists()
    assert not purged_agents_dir(str(tmp_path), world.t1, user).exists()
    assert theirs.exists()


def test_the_user_purge_is_wired_with_the_nas_root() -> None:
    from types import SimpleNamespace

    from control_plane.api.agent_users import _build_purge_deps
    from control_plane.app import create_app
    from control_plane.audit import build_default_audit_logger
    from control_plane.settings import Settings
    from expert_work.persistence.audit_log import InMemoryAuditLogStore
    from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
    from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier

    app = create_app(
        settings=Settings(
            env="dev", auth_mode="dev", oidc_issuer=TEST_ISSUER, oidc_audience=[TEST_AUDIENCE]
        ),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    app.state.workspace_store = NasWorkspaceStore(root="/mnt/nas")

    deps = _build_purge_deps(SimpleNamespace(app=app))  # type: ignore[arg-type]

    assert deps.workspace_root == "/mnt/nas"
