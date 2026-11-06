"""B-285 —— 彻底删除带走对象存储里的轨迹对象(每个 run 一份整段对话)。

* 共用的 ``delete_conversation_trajectories``:按 run 推出的日期,只删这条对话自己的对象和
  它名下子智能体的对象;同一天别的对话、别的租户里同一个对话 id 的不动;有删不掉的抛错,
  其它照删,重跑删完。
* 删单个对话(``:purge``):删掉,兄弟对话不动;删不掉 → 503、对话留着、重跑删完;删除过程中
  还在收尾的 run 写的那一份,第二遍删掉。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.purge.trajectories import (
    CONCURRENCY,
    TrajectoriesNotDeletedError,
    TrajectoryStore,
    conversation_days,
    delete_conversation_trajectories,
    run_days,
    today,
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

    days = run_days(runs)
    removed = await delete_conversation_trajectories(
        TrajectoryStore(store), tenant_id=tenant, thread_id=mine, days=days
    )

    # Own keys are cleared directly (absent ones too), sub-agents' as listed:
    # 3 days x 4 outcomes + 1 sub-agent object.
    assert len(days) == 3
    assert removed == 3 * 4 + 1
    assert sorted(await _left(store)) == sorted(kept)
    assert not set(doomed) & set(await _left(store))


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

    with pytest.raises(TrajectoriesNotDeletedError) as info:
        await delete_conversation_trajectories(
            TrajectoryStore(store), tenant_id=tenant, thread_id=mine, days=days
        )
    assert await _left(store) == [first]
    assert second not in await _left(store)
    # The 3 own keys of that day that did clear are counted, the failed one is not.
    assert info.value.removed == 3

    store.bad = None
    assert (
        await delete_conversation_trajectories(
            TrajectoryStore(store), tenant_id=tenant, thread_id=mine, days=days
        )
        == 4
    )
    assert await _left(store) == []


class _Recording(InMemoryObjectStore):
    """Records LIST prefixes and the most deletes in flight at once."""

    def __init__(self) -> None:
        super().__init__()
        self.listed: list[str] = []
        self.in_flight = 0
        self.peak = 0

    async def list_prefix(self, prefix: str) -> list[str]:
        self.listed.append(prefix)
        return await super().list_prefix(prefix)

    async def delete(self, key: str) -> None:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        await asyncio.sleep(0.005)
        self.in_flight -= 1
        await super().delete(key)


@pytest.mark.asyncio
async def test_own_keys_are_deleted_without_listing_only_sub_agent_prefixes_are_listed() -> None:
    store = _Recording()
    tenant, mine = uuid4(), uuid4()
    own = await _put(store, tenant, mine, at=_DAY1, outcome="max_steps")
    days = run_days([_run(tenant, mine, at=_DAY1)])

    await delete_conversation_trajectories(
        TrajectoryStore(store), tenant_id=tenant, thread_id=mine, days=days
    )

    listed = list(store.listed)
    assert len(listed) == 4 * len(days)
    assert all(p.endswith(f"/{mine}/") for p in listed)
    assert own not in await _left(store)


@pytest.mark.asyncio
async def test_units_run_concurrently_but_bounded() -> None:
    store = _Recording()
    tenant, mine = uuid4(), uuid4()
    days = [date(2026, 10, d) for d in range(1, 11)]  # 40 (day, outcome) units

    await delete_conversation_trajectories(
        TrajectoryStore(store), tenant_id=tenant, thread_id=mine, days=days
    )

    assert 1 < store.peak <= CONCURRENCY


def test_a_conversation_without_runs_is_dated_by_its_own_row() -> None:
    created = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    meta = SimpleNamespace(created_at=created, updated_at=created + timedelta(days=1))
    dated = conversation_days([], meta)
    assert not dated.undatable
    assert [d.day for d in dated.days] == [2, 3]
    assert conversation_days([], SimpleNamespace(created_at=None, updated_at=None)).undatable
    assert conversation_days([], None).undatable
    # Runs win over the row.
    assert conversation_days([_run(uuid4(), uuid4(), at=_DAY1)], meta).days == run_days(
        [_run(uuid4(), uuid4(), at=_DAY1)]
    )


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
    days = run_days([_run(_TENANT, doomed, at=at)])
    late = today()
    assert resp.json()["data"]["trajectories"] == 4 * len(days) + 1 + 4 * len(late)
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
    at = datetime.now(UTC) - timedelta(days=3)
    doomed, _ = await _conversation(client, at=at)
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
    days = run_days([_run(_TENANT, doomed, at=at)])
    assert resp.json()["data"]["trajectories"] == 4 * len(days) + 1 + 4 * len(today())
    assert await _left(store) == []


@pytest.mark.asyncio
async def test_a_conversation_without_run_rows_still_loses_its_trajectories(
    client: AsyncClient,
) -> None:
    """Medium-4 —— no run rows (e.g. removed by an older partial purge): the days
    come from the conversation row itself, not "nothing to do"."""
    resp = await client.post("/v1/sessions", json={"agent_name": _AGENT, "agent_version": "1.0.0"})
    thread = UUID(resp.json()["data"]["thread_id"])
    store = _objects(_app(client))
    key = await _put(store, _TENANT, thread, at=datetime.now(UTC))

    purged = await client.post(f"/v1/sessions/{thread}:purge")

    assert purged.status_code == 200, purged.text
    assert purged.json()["data"]["trajectory_undatable"] == 0
    assert key not in await _left(store)


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
