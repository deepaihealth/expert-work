"""B-285 —— 彻底删除带走对象存储里的轨迹对象(每个 run 一份整段对话)。

* 共用的 ``delete_conversation_trajectories``:按 run 推出的日期,只删这条对话自己的对象和
  它名下子智能体的对象;同一天别的对话、别的租户里同一个对话 id 的不动;有删不掉的抛错,
  其它照删,重跑删完。
* 删单个对话(``:purge``):删掉,兄弟对话不动;删不掉 → 503、对话留着、重跑删完;删除过程中
  还在收尾的 run 写的那一份,第二遍删掉。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.purge.trajectories import (
    TrajectoriesNotDeletedError,
    TrajectoryStore,
    delete_conversation_trajectories,
    run_days,
)
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol.trajectory_keys import trajectory_key
from expert_work.runtime.runs import DisconnectMode, RunInfo, RunStatus
from expert_work.runtime.storage import InMemoryObjectStore
from orchestrator.trajectory import TrajectoryRecorder
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier, make_test_jwt

_TENANT = DEFAULT_DEV_TENANT_ID
_AGENT = "code-reviewer"
_AGENT_YAML = f"""\
apiVersion: expert_work.io/v1
kind: Agent
metadata:
  name: {_AGENT}
  version: "1.0.0"
  tenant: platform-eng
spec:
  tenant_config: {{}}
  model:
    provider: anthropic
    name: claude-sonnet-4-6
  system_prompt:
    template: "you are a reviewer"
  sandbox:
    resources: {{ cpu: "1.0", memory: "1Gi" }}
    network:
      egress: proxy
      allowlist: ["api.anthropic.com"]
    filesystem:
      readonly_root: true
      writable: ["/workspace"]
"""


def _run(tenant: UUID, thread: UUID, *, at: datetime, finished: bool = True) -> RunInfo:
    return RunInfo(
        run_id=uuid4(),
        tenant_id=tenant,
        thread_id=thread,
        user_id=None,
        status=RunStatus.SUCCESS if finished else RunStatus.RUNNING,
        on_disconnect=DisconnectMode.CANCEL,
        is_resume=False,
        error=None,
        created_at=at - timedelta(minutes=3),
        updated_at=at,
        finished_at=at if finished else None,
    )


async def _put(
    store: InMemoryObjectStore,
    tenant: UUID,
    thread: UUID,
    *,
    at: datetime,
    outcome: str = "success",
    conversation: UUID | None = None,
) -> str:
    key = trajectory_key(
        tenant_id=tenant, outcome=outcome, at=at, thread_id=thread, conversation_id=conversation
    )
    await store.put(key, b"{}")
    return key


async def _left(store: InMemoryObjectStore) -> list[str]:
    return await store.list_prefix("trajectories/")


# --------------------------------------------------------------------------- helper


_DAY1 = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
_DAY2 = datetime(2026, 11, 5, 0, 10, tzinfo=UTC)  # near midnight: day 4 and 5


@pytest.mark.asyncio
async def test_only_this_conversations_objects_and_its_sub_agents_go() -> None:
    store = InMemoryObjectStore()
    tenant, other_tenant, mine, sibling, sub = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    runs = [_run(tenant, mine, at=_DAY1), _run(tenant, mine, at=_DAY2)]
    doomed = [
        await _put(store, tenant, mine, at=_DAY1),
        await _put(store, tenant, mine, at=_DAY2, outcome="failed"),
        await _put(store, tenant, mine, at=_DAY2 - timedelta(minutes=30), outcome="cancelled"),
        await _put(store, tenant, sub, at=_DAY1, outcome="max_steps", conversation=mine),
    ]
    kept = [
        await _put(store, tenant, sibling, at=_DAY1),
        await _put(store, tenant, sub, at=_DAY1, conversation=sibling),
        await _put(store, other_tenant, mine, at=_DAY1),
    ]

    removed = await delete_conversation_trajectories(
        TrajectoryStore(store), tenant_id=tenant, thread_id=mine, days=run_days(runs)
    )

    assert removed == len(doomed)
    assert sorted(await _left(store)) == sorted(kept)


def test_an_unfinished_run_is_dated_by_its_last_update() -> None:
    at = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    assert [d.day for d in run_days([_run(uuid4(), uuid4(), at=at, finished=False)])] == [6]


class _OneDeleteFails(InMemoryObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.bad: str | None = None

    async def delete(self, key: str) -> None:
        if key == self.bad:
            raise TimeoutError("oss")
        await super().delete(key)


@pytest.mark.asyncio
async def test_a_failure_is_raised_after_the_rest_went_and_a_rerun_finishes() -> None:
    store = _OneDeleteFails()
    tenant, mine = uuid4(), uuid4()
    first = await _put(store, tenant, mine, at=_DAY1)
    second = await _put(store, tenant, mine, at=_DAY1, outcome="failed")
    store.bad = first
    days = run_days([_run(tenant, mine, at=_DAY1)])

    with pytest.raises(TrajectoriesNotDeletedError):
        await delete_conversation_trajectories(
            TrajectoryStore(store), tenant_id=tenant, thread_id=mine, days=days
        )
    assert await _left(store) == [first]
    assert second not in await _left(store)

    store.bad = None
    assert (
        await delete_conversation_trajectories(
            TrajectoryStore(store), tenant_id=tenant, thread_id=mine, days=days
        )
        == 1
    )
    assert await _left(store) == []


@pytest.mark.asyncio
async def test_no_store_means_nothing_to_delete() -> None:
    assert (
        await delete_conversation_trajectories(None, tenant_id=uuid4(), thread_id=uuid4(), days=[])
        == 0
    )


# --------------------------------------------------------------------------- session purge


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app(
        settings=Settings(
            env="dev",
            auth_mode="dev",
            rate_limit_burst=10_000,
            rate_limit_per_second=10_000.0,
            oidc_issuer=TEST_ISSUER,
            oidc_audience=[TEST_AUDIENCE],
        ),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    app.state.agent_runtime.trajectory_recorder = TrajectoryRecorder(object_store=_OneDeleteFails())
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT)}"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://cp.test", headers=headers
    ) as c:
        resp = await c.post("/v1/agents", json={"manifest_yaml": _AGENT_YAML})
        assert resp.status_code in (200, 201), resp.text
        yield c


def _app(client: AsyncClient) -> FastAPI:
    return client._transport.app  # type: ignore[attr-defined,no-any-return]


def _objects(app: FastAPI) -> _OneDeleteFails:
    store = app.state.agent_runtime.trajectory_recorder.object_store
    assert isinstance(store, _OneDeleteFails)
    return store


async def _conversation(client: AsyncClient, *, at: datetime) -> tuple[UUID, list[str]]:
    resp = await client.post("/v1/sessions", json={"agent_name": _AGENT, "agent_version": "1.0.0"})
    assert resp.status_code == 201, resp.text
    thread = UUID(resp.json()["data"]["thread_id"])
    app = _app(client)
    await app.state.run_store.create(_run(_TENANT, thread, at=at))
    store = _objects(app)
    keys = [
        await _put(store, _TENANT, thread, at=at),
        await _put(store, _TENANT, uuid4(), at=at, conversation=thread),
    ]
    return thread, keys


@pytest.mark.asyncio
async def test_session_purge_deletes_the_conversations_trajectories(client: AsyncClient) -> None:
    at = datetime.now(UTC) - timedelta(days=2)
    doomed, _ = await _conversation(client, at=at)
    sibling, sibling_keys = await _conversation(client, at=at)

    resp = await client.post(f"/v1/sessions/{doomed}:purge")

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["trajectories"] == 2
    assert sorted(await _left(_objects(_app(client)))) == sorted(sibling_keys)
    assert (await client.get(f"/v1/sessions/{sibling}")).status_code == 200


@pytest.mark.asyncio
async def test_a_trajectory_that_will_not_go_keeps_the_conversation(client: AsyncClient) -> None:
    at = datetime.now(UTC) - timedelta(days=2)
    doomed, keys = await _conversation(client, at=at)
    store = _objects(_app(client))
    store.bad = keys[0]

    failed = await client.post(f"/v1/sessions/{doomed}:purge")
    assert failed.status_code == 503, failed.text
    assert (await client.get(f"/v1/sessions/{doomed}")).status_code == 200
    assert await _left(store) == [keys[0]]

    store.bad = None
    retried = await client.post(f"/v1/sessions/{doomed}:purge")
    assert retried.status_code == 200, retried.text
    assert await _left(store) == []


@pytest.mark.asyncio
async def test_a_trajectory_written_while_purging_goes_too(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run still finishing (the purge does not stop runs) writes its trajectory
    now; the second pass after the thread row takes today's."""
    doomed, _ = await _conversation(client, at=datetime.now(UTC) - timedelta(days=3))
    app = _app(client)
    store = _objects(app)
    real_delete = app.state.thread_meta_repo.delete

    async def _late_write_then_delete(thread_id: UUID, *, tenant_id: UUID) -> bool:
        await _put(store, _TENANT, thread_id, at=datetime.now(UTC), outcome="cancelled")
        removed: bool = await real_delete(thread_id, tenant_id=tenant_id)
        return removed

    monkeypatch.setattr(app.state.thread_meta_repo, "delete", _late_write_then_delete)
    resp = await client.post(f"/v1/sessions/{doomed}:purge")

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["trajectories"] == 3
    assert await _left(store) == []


def test_the_agent_purge_wires_the_trajectory_store() -> None:
    from types import SimpleNamespace

    from control_plane.api.agent_lifecycle import build_agent_purge_deps

    app = create_app(
        settings=Settings(
            env="dev", auth_mode="dev", oidc_issuer=TEST_ISSUER, oidc_audience=[TEST_AUDIENCE]
        ),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    objects = InMemoryObjectStore()
    app.state.agent_runtime.trajectory_recorder = TrajectoryRecorder(object_store=objects)
    deps = build_agent_purge_deps(SimpleNamespace(app=app))  # type: ignore[arg-type]
    assert deps.trajectories is not None
    assert deps.trajectories.object_store is objects
