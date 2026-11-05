"""B-278 review —— a failed purge must leave something a re-run can find, and nothing
may change the name while a purge is under way.

* A child step that fails keeps its parent (conversation / trigger / webhook /
  artifact registry / user anchor), so the re-run finds the leftovers and only
  reports ok once they are gone.
* ``begin_purge`` marks every version ``purging`` before anything is deleted:
  restore and create are refused for the whole purge (and after a failed one).
* The final delete re-checks live versions and subagent referrers in its own
  transaction.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.purge.agent_purge import agent_key_for, purge_agent
from expert_work.persistence.agent_spec import AgentSpecPurgingError
from expert_work.protocol import AgentSpecStatus, WebhookEndpointRecord
from tests.test_agent_purge import _NOW, _X, _counts, _spec, _World, _world


def _fail_once(obj: object, method: str) -> list[int]:
    """Make ``obj.method`` raise ``OSError`` on its first call only; returns a call counter."""
    original: Callable[..., Awaitable[Any]] = getattr(obj, method)
    calls = [0]

    async def _flaky(*args: Any, **kwargs: Any) -> Any:
        calls[0] += 1
        if calls[0] == 1:
            raise OSError(5, "transient")
        return await original(*args, **kwargs)

    setattr(obj, method, _flaky)
    return calls


async def _status(world: _World, name: str = _X) -> set[AgentSpecStatus]:
    return {r.status for r in await world.specs.list_by_tenant(tenant_id=world.t1, name=name)}


async def _assert_rerun_finishes(world: _World, threads: list[UUID]) -> None:
    rerun = await purge_agent(world.deps(), tenant_id=world.t1, name=_X)
    assert rerun.ok, rerun.as_dict()
    after = await _counts(world, world.t1, _X)
    assert all(v == 0 for v in after.values()), after
    left_approvals = await world.approvals.count_for_threads(tenant_id=world.t1, thread_ids=threads)
    assert left_approvals == 0


@pytest.mark.parametrize(
    ("store_attr", "method", "kept_table"),
    [
        ("approvals", "delete_for_threads", "thread_meta"),
        ("feedback", "delete_for_threads", "thread_meta"),
        ("dlq", "delete_for_threads", "thread_meta"),
        ("uploads", "delete_for_threads", "thread_meta"),
        ("trigger_runs", "delete_for_triggers", "agent_trigger"),
        ("deliveries", "delete_for_endpoints", "webhook_endpoint"),
        ("snapshots", "delete", "artifact"),
        ("jobs", "delete_for_agent", "memory_item"),
        ("candidates", "delete_for_agent", "eval_dataset"),
    ],
)
@pytest.mark.asyncio
async def test_a_failed_child_step_keeps_its_parent_and_a_rerun_finishes(
    store_attr: str, method: str, kept_table: str
) -> None:
    world, threads = await _world()
    before = await _counts(world, world.t1, _X)
    _fail_once(getattr(world, store_attr), method)

    first = await purge_agent(world.deps(), tenant_id=world.t1, name=_X)

    assert first.failures and not first.ok
    assert kept_table in first.skipped
    assert kept_table not in first.deleted or first.deleted[kept_table] == 0
    got_1 = await _status(world)
    assert got_1 == {AgentSpecStatus.PURGING}
    middle = await _counts(world, world.t1, _X)
    parent_count = {
        "thread_meta": "conversations",
        "agent_trigger": "triggers",
        "webhook_endpoint": "webhooks",
        "artifact": "artifacts",
        "memory_item": "memories",
        "eval_dataset": "eval_datasets",
    }[kept_table]
    assert middle[parent_count] == before[parent_count]
    await _assert_rerun_finishes(world, threads)


@pytest.mark.asyncio
async def test_a_failed_checkpoint_keeps_that_conversation() -> None:
    world, threads = await _world()

    class _Checkpointer:
        def __init__(self) -> None:
            self.deleted: list[str] = []
            self.failed = False

        async def adelete_thread(self, thread_id: str) -> None:
            if not self.failed:
                self.failed = True
                raise OSError(5, "transient")
            self.deleted.append(thread_id)

    saver = _Checkpointer()
    deps = world.deps()
    deps.runtime.durable_checkpointer = saver  # type: ignore[assignment]
    first = await purge_agent(deps, tenant_id=world.t1, name=_X)

    assert first.failures == {"checkpoint": "OSError"}
    assert first.deleted.get("thread_meta") == len(threads) - 1  # the other one went
    left = await world.threads.list_by_tenant(world.t1, agent_name=_X, include_archived=True)
    assert len(left) == 1
    rerun = await purge_agent(deps, tenant_id=world.t1, name=_X)
    assert rerun.ok
    assert sorted(saver.deleted) == sorted(str(t) for t in threads)


@pytest.mark.asyncio
async def test_restore_and_create_are_refused_while_the_purge_runs() -> None:
    world, _ = await _world()
    seen: dict[str, object] = {}
    original = world.jobs.delete_for_agent

    async def _meanwhile(*, tenant_id: UUID, agent_name: str) -> int:
        # Another admin clicks "restore", and someone creates a new version, mid-purge.
        seen["restore"] = await world.specs.restore(
            tenant_id=tenant_id, name=agent_name, version="1.0.0"
        )
        try:
            await world.specs.create(
                tenant_id=tenant_id,
                spec=_spec(agent_name, "3.0.0"),
                spec_sha256="a" * 64,
                created_by="u",
            )
        except AgentSpecPurgingError as exc:
            seen["create"] = exc
        return await original(tenant_id=tenant_id, agent_name=agent_name)

    world.jobs.delete_for_agent = _meanwhile  # type: ignore[method-assign]
    summary = await purge_agent(world.deps(), tenant_id=world.t1, name=_X)

    assert seen["restore"] is None
    assert isinstance(seen["create"], AgentSpecPurgingError)
    assert summary.ok, summary.as_dict()
    got_2 = await world.specs.list_by_tenant(tenant_id=world.t1, name=_X)
    assert got_2 == []


@pytest.mark.asyncio
async def test_a_failed_purge_cannot_be_restored() -> None:
    world, _ = await _world()
    _fail_once(world.scores, "delete_for_agent")
    first = await purge_agent(world.deps(), tenant_id=world.t1, name=_X)
    assert not first.ok

    restored = await world.specs.restore(tenant_id=world.t1, name=_X, version="1.0.0")

    assert restored is None
    got_3 = await _status(world)
    assert got_3 == {AgentSpecStatus.PURGING}


@pytest.mark.asyncio
async def test_a_referrer_added_mid_purge_blocks_the_final_delete() -> None:
    world, _ = await _world()
    original = world.allowlist.delete_for_agent

    async def _meanwhile(*, tenant_id: UUID, agent_name: str) -> int:
        await world.specs.create(
            tenant_id=tenant_id,
            spec=_spec("late-parent", subagent_of=f"{agent_name}@1.0.0"),
            spec_sha256="a" * 64,
            created_by="u",
        )
        return await original(tenant_id=tenant_id, agent_name=agent_name)

    world.allowlist.delete_for_agent = _meanwhile  # type: ignore[method-assign]
    summary = await purge_agent(world.deps(), tenant_id=world.t1, name=_X)

    assert not summary.agent_spec_deleted
    assert summary.blocked == {"active_versions": [], "referenced_by": ["late-parent@1.0.0"]}
    got_4 = await _status(world)
    assert got_4 == {AgentSpecStatus.PURGING}


@pytest.mark.asyncio
async def test_all_agent_webhooks_and_prefix_similar_names_are_untouched() -> None:
    world, _ = await _world()
    shared = WebhookEndpointRecord(
        id=uuid4(),
        tenant_id=world.t1,
        name="all-agents",
        url="https://example.invalid/all",
        event_types=("run.completed",),
        agent_name=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    await world.endpoints.create(shared)
    similar = f"{_X}-2"
    await world.seed_agent(world.t1, similar, deleted=True)
    similar_before = await _counts(world, world.t1, similar)

    summary = await purge_agent(world.deps(), tenant_id=world.t1, name=_X)

    assert summary.ok
    kept = await world.endpoints.get(endpoint_id=shared.id, tenant_id=world.t1)
    assert kept is not None
    got_5 = await _counts(world, world.t1, similar)
    assert got_5 == similar_before
    assert agent_key_for(similar) != agent_key_for(_X)
    got_6 = await _status(world, similar)
    assert got_6 == {AgentSpecStatus.DELETED}


@pytest.mark.asyncio
async def test_a_symlinked_agents_dir_is_skipped_not_failed(tmp_path: Path) -> None:
    from dataclasses import replace

    from orchestrator.tools.nas_workspace_store import NasWorkspaceStore

    world, _ = await _world()
    user = uuid4()
    outside = tmp_path / "outside"
    (outside / agent_key_for(_X)).mkdir(parents=True)
    keep = outside / agent_key_for(_X) / "keep.md"
    keep.write_text("not the agent's")
    user_root = tmp_path / str(world.t1) / str(user)
    user_root.mkdir(parents=True)
    (user_root / "agents").symlink_to(outside, target_is_directory=True)

    summary = await purge_agent(
        replace(
            world.deps(),
            workspace_store=NasWorkspaceStore(root=str(tmp_path)),
            workspace_root=str(tmp_path),
            artifact_snapshots=None,
        ),
        tenant_id=world.t1,
        name=_X,
    )

    assert summary.ok, summary.as_dict()
    assert "nas_symlink" in summary.skipped
    assert summary.nas_symlinks_skipped == 1
    assert keep.exists()


@pytest.mark.asyncio
async def test_a_stale_delete_cannot_reopen_a_purging_name() -> None:
    world, _ = await _world()
    _fail_once(world.scores, "delete_for_agent")
    first = await purge_agent(world.deps(), tenant_id=world.t1, name=_X)
    assert not first.ok

    stale = await world.specs.update_status(
        tenant_id=world.t1, name=_X, version="1.0.0", status=AgentSpecStatus.DELETED
    )

    assert stale is None
    status = await _status(world)
    assert status == {AgentSpecStatus.PURGING}
