"""Stream 9.5 — RunQueueWorker drains the distributed run queue.

Drives the real :class:`RunQueueWorker` over a real :class:`InMemoryRunStore`
seeded with a ``queued`` run (via ``RunManager.enqueue``). ``run_agent`` is
monkeypatched to a recording no-op — the seam under test is
scan → claim CAS (exactly-once) → rebuild input → adopt → start, which is
model-agnostic.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from control_plane import run_queue_worker as worker_module
from control_plane.agent_disable_status import AgentDisableService
from control_plane.audit import build_default_audit_logger
from control_plane.run_queue_worker import RunQueueWorker
from control_plane.tenant_status import TenantStatusService
from expert_work.persistence import InMemoryAgentDisableStore, InMemoryTenantConfigStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.runtime.runs import InMemoryRunStore, RunManager, RunStatus


class _FakeThreads:
    def __init__(self, *, has_agent: bool = True) -> None:
        self._has_agent = has_agent

    async def get(self, _thread_id, *, tenant_id):
        del tenant_id
        if not self._has_agent:
            return SimpleNamespace(agent_name=None, agent_version=None, user_id=None)
        return SimpleNamespace(agent_name="a", agent_version="1.0.0", user_id=None)


class _FakeAgents:
    async def get(self, *, tenant_id, name, version):
        del tenant_id, name, version
        return SimpleNamespace(spec=SimpleNamespace())


class _FakeRuntime:
    def __init__(self, run_store: InMemoryRunStore, *, instance_id: str = "worker-1") -> None:
        self.run_manager = RunManager(run_store, instance_id=instance_id, lease_ttl_s=30.0)
        self.stream_bridge = object()
        self.run_event_store = None
        self.skill_run_usage_recorder = None
        self.trajectory_recorder = None
        self.thread_stats_recorder = None

    async def get_agent(self, **_kw):
        return SimpleNamespace(
            graph=object(),
            bound_distilled_skills=(),
            tool_replay_safe=None,
            run_deadline_s=0,
            system_prompt="you are a test agent",
            supports_vision=False,
            spotlight_nonce=None,
            trajectory_recording=True,
            token_budget=0,
            max_steps=8,
            max_no_progress=0,
        )

    async def new_worker_spawn_budget(self):
        return None

    def delegation_gate(self):
        return None


def _worker(store, runtime, **kw) -> RunQueueWorker:
    return RunQueueWorker(
        run_store=store,
        thread_store=kw.pop("threads", _FakeThreads()),
        agent_spec_store=_FakeAgents(),
        runtime=runtime,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        approval_store=object(),
        **kw,
    )


async def _enqueue(mgr: RunManager, *, text: str = "hello") -> tuple:
    run_id, tenant, thread = uuid4(), uuid4(), uuid4()
    await mgr.enqueue(
        run_id=run_id,
        thread_id=thread,
        tenant_id=tenant,
        enqueued_input={"input": text, "image_refs": [], "untrusted_content": []},
    )
    return run_id, tenant


@pytest.mark.asyncio
async def test_suspended_tenant_queued_run_not_claimed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stream RT-4 — a suspended tenant's queued run is not claimed (the RLS-scope
    fix keeps the ``is_suspended`` read alive inside the worker's tenant scope)."""
    spawns: list[dict] = []
    monkeypatch.setattr(worker_module, "run_agent", lambda **kw: spawns.append(kw))
    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    run_id, tenant = await _enqueue(runtime.run_manager)
    tcs = InMemoryTenantConfigStore()
    await tcs.create(tenant_id=tenant, display_name="t", actor_id="seed")
    await tcs.set_status(tenant_id=tenant, status="suspended", actor_id="admin")
    worker = _worker(store, runtime, tenant_status_service=TenantStatusService(store=tcs))

    # Direct gate assertion — the suspend queue-gate would be silently dead
    # without the tenant-scope wrapper.
    run_info = await store.get(run_id=run_id, tenant_id=tenant)
    assert run_info is not None
    assert await worker._is_killed(run_info) is True

    started = await worker.run_once()
    assert started == 0
    assert spawns == []
    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None and row.status is RunStatus.QUEUED


@pytest.mark.asyncio
async def test_disabled_agent_queued_run_not_claimed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stream RT-4 — a disabled agent's queued run is not claimed."""
    spawns: list[dict] = []
    monkeypatch.setattr(worker_module, "run_agent", lambda **kw: spawns.append(kw))
    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    run_id, tenant = await _enqueue(runtime.run_manager)
    disable_store = InMemoryAgentDisableStore()
    await disable_store.set_disabled(
        tenant_id=tenant, agent_name="a", disabled=True, reason=None, disabled_by="admin"
    )
    worker = _worker(store, runtime, agent_disable_service=AgentDisableService(store=disable_store))
    started = await worker.run_once()
    assert started == 0
    assert spawns == []
    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None and row.status is RunStatus.QUEUED


@pytest.mark.asyncio
async def test_claims_and_starts_queued_run(monkeypatch: pytest.MonkeyPatch) -> None:
    spawns: list[dict] = []

    async def _fake_run_agent(**kw):
        spawns.append(kw)

    monkeypatch.setattr(worker_module, "run_agent", _fake_run_agent)

    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    run_id, tenant = await _enqueue(runtime.run_manager, text="do the thing")

    started = await _worker(store, runtime).run_once()
    await asyncio.sleep(0)  # let the spawned task body run

    assert started == 1
    assert len(spawns) == 1
    # graph_input was rebuilt from the persisted input (not None).
    assert spawns[0]["graph_input"]["messages"][1].content == "do the thing"
    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None
    assert row.status is RunStatus.RUNNING
    assert row.claimed_by == "worker-1"


@pytest.mark.asyncio
async def test_claimed_run_carries_document_names_into_graph_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """修复轮 1(原顾虑 2)—— P2 块 1(Task 11)加了 ``document_names`` 但漏了
    这一处回读:``enqueued_input`` 里存了它,``_execute`` 重放时却没有读
    回来喂给 ``build_run_graph_input``,queue 模式下的文档附件会静默消失
    (只是被正确持久化,从未真正到达 agent)。

    与既有的 ``test_claims_and_starts_queued_run`` 同一套装置(``run_agent``
    monkeypatch 成录制型假函数),但断言的是 ``_execute`` 真正构造、准备喂
    给 graph 的 ``HumanMessage`` —— 不是只断言 ``enqueued_input`` 里存着了
    (那条在 ``test_external_run_files.py`` 已经测过,证明的是"持久化对不
    对";这条证明的是"重放时有没有读回来喂给 agent",是两件不同的事——
    这也正是本次要修的 bug 唯一会漏出来的地方)。"""
    spawns: list[dict] = []

    async def _fake_run_agent(**kw):
        spawns.append(kw)

    monkeypatch.setattr(worker_module, "run_agent", _fake_run_agent)

    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    run_id, tenant, thread = uuid4(), uuid4(), uuid4()
    await runtime.run_manager.enqueue(
        run_id=run_id,
        thread_id=thread,
        tenant_id=tenant,
        enqueued_input={
            "input": "总结这份文件",
            "image_refs": [],
            "untrusted_content": [],
            "document_names": ["uploads/report.pdf"],
        },
    )

    started = await _worker(store, runtime).run_once()
    await asyncio.sleep(0)  # let the spawned task body run

    assert started == 1
    assert len(spawns) == 1
    human_message_content = spawns[0]["graph_input"]["messages"][1].content
    assert "[file attached: uploads/report.pdf]" in human_message_content


@pytest.mark.asyncio
async def test_claimed_run_passes_prompt_inputs_to_run_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BUG-16 —— ``enqueued_input`` 里的 Jinja ``inputs`` 要原样穿给
    ``run_agent(prompt_inputs=...)``,queue 路径的 system_prompt 帧才带原始 k/v。"""
    spawns: list[dict] = []

    async def _fake_run_agent(**kw):
        spawns.append(kw)

    monkeypatch.setattr(worker_module, "run_agent", _fake_run_agent)

    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    run_id, tenant, thread = uuid4(), uuid4(), uuid4()
    await runtime.run_manager.enqueue(
        run_id=run_id,
        thread_id=thread,
        tenant_id=tenant,
        enqueued_input={
            "input": "hi",
            "image_refs": [],
            "untrusted_content": [],
            "inputs": {"project_code": "P1", "employee_code": "E9"},
        },
    )

    started = await _worker(store, runtime).run_once()
    await asyncio.sleep(0)  # let the spawned task body run

    assert started == 1
    assert len(spawns) == 1
    assert spawns[0]["prompt_inputs"] == {"project_code": "P1", "employee_code": "E9"}


@pytest.mark.asyncio
async def test_exactly_one_worker_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_run_agent(**kw):
        return None

    monkeypatch.setattr(worker_module, "run_agent", _fake_run_agent)

    store = InMemoryRunStore()
    runtime_a = _FakeRuntime(store, instance_id="worker-a")
    runtime_b = _FakeRuntime(store, instance_id="worker-b")
    run_id, tenant = await _enqueue(runtime_a.run_manager)

    # Two workers race the same queued run; the claim CAS lets exactly one win.
    started_a, started_b = await asyncio.gather(
        _worker(store, runtime_a).run_once(),
        _worker(store, runtime_b).run_once(),
    )

    assert started_a + started_b == 1
    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None
    assert row.status is RunStatus.RUNNING
    assert row.claimed_by in {"worker-a", "worker-b"}


@pytest.mark.asyncio
async def test_skips_already_claimed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    spawns: list[dict] = []
    monkeypatch.setattr(worker_module, "run_agent", lambda **kw: spawns.append(kw))

    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    run_id, _tenant = await _enqueue(runtime.run_manager)
    # A peer already claimed it (status flipped out of queued).
    await store.claim_queued(
        run_id=run_id,
        new_owner="peer",
        lease_until=datetime.now(UTC) + timedelta(seconds=30),
        heartbeat_at=datetime.now(UTC),
    )

    started = await _worker(store, runtime).run_once()
    assert started == 0
    assert spawns == []


@pytest.mark.asyncio
async def test_no_agent_meta_marks_errored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module, "run_agent", lambda **kw: None)

    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    run_id, tenant = await _enqueue(runtime.run_manager)

    worker = _worker(store, runtime, threads=_FakeThreads(has_agent=False))
    await worker.run_once()

    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None
    assert row.status is RunStatus.ERROR  # claimed then errored (unrecoverable)


@pytest.mark.asyncio
async def test_stop_is_bounded_when_a_sweep_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    """卡死的 sweep 不能把关机拖到 SIGKILL —— stop() 等一小会儿就取消它。

    lifespan 顺序 await 每个 worker 的 stop();这里少了上界,一个卡在
    claim+启动 run 的 sweep 就能把整个进程的关机挂到 K8s 优雅期耗尽。
    """
    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    worker = _worker(store, runtime, interval_s=0.01)
    entered = asyncio.Event()

    async def _never_returns() -> int:
        entered.set()
        await asyncio.sleep(3600)
        return 0

    monkeypatch.setattr(worker, "run_once", _never_returns)
    monkeypatch.setattr(worker_module, "_STOP_TIMEOUT_S", 0.05, raising=False)

    worker.start()
    await asyncio.wait_for(entered.wait(), timeout=2)

    # 修复前:stop() 永远等下去,这里超时失败。
    await asyncio.wait_for(worker.stop(), timeout=2)
    assert worker._task is None
