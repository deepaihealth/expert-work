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
from expert_work.persistence.platform_agent_template import compute_spec_sha256
from expert_work.protocol import AgentSpec
from expert_work.runtime.runs import InMemoryRunStore, RunManager, RunStatus


class _FakeThreads:
    def __init__(self, *, has_agent: bool = True) -> None:
        self._has_agent = has_agent

    async def get(self, _thread_id, *, tenant_id):
        del tenant_id
        if not self._has_agent:
            return SimpleNamespace(agent_name=None, agent_version=None, user_id=None)
        return SimpleNamespace(agent_name="a", agent_version="1.0.0", user_id=None)


_SPEC = AgentSpec.model_validate(
    {
        "apiVersion": "expert_work.io/v1",
        "kind": "Agent",
        "metadata": {"name": "queued", "version": "1.0.0", "tenant": "t"},
        "spec": {
            "tenant_config": {},
            "model": {"provider": "anthropic", "name": "claude-haiku-4-5"},
            "system_prompt": {"template": "you help"},
            "sandbox": {
                "resources": {"cpu": "1", "memory": "1Gi"},
                "network": {"egress": "none", "allowlist": []},
                "filesystem": {"readonly_root": True, "writable": []},
            },
        },
    }
)


class _FakeAgents:
    async def get(self, *, tenant_id, name, version):
        del tenant_id, name, version
        # A real AgentSpec, not a stand-in: the worker now hashes what it built
        # from, and a stand-in would make that hashing untestable here.
        return SimpleNamespace(spec=_SPEC)


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
            worker_max_concurrent=None,
            worker_max_per_run=None,
            max_steps=8,
            max_no_progress=0,
        )

    async def new_worker_spawn_budget(self, **_kw: object):
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


@pytest.mark.asyncio
async def test_execute_rewrites_trace_id_to_the_executing_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """排队执行的 run 必须把 ``trace_id`` 回写成**执行时**的 trace。

    ``agent_run.trace_id`` 由 API handler 在建行那一刻用
    ``current_trace_id_hex()`` 写下。``mode: "stream"`` 下那就是 HTTP 请求
    的 span,而 ``sse.py`` 的 ``expert_work.session.run`` 根 span 挂在它下
    面,父子同 trace,``token_usage``(LLM 调用时取同一个 current span)
    自然对得上。

    ``mode: "queue"`` 不是:HTTP 立刻 202 返回,真正执行发生在这个 worker
    的后台任务里,``session.run`` 在那儿另起一个 trace。于是 ``trace_id``
    指向一个早已结束的 202 请求,而这一轮所有 token 记在别的 trace 下。
    ``token_usage`` 没有 ``run_id`` 列——``totals_by_trace_ids`` 全靠 trace
    连接 ``agent_run ↔ token_usage``,连接一断,Runs 列表/详情的 token 就
    是空的(测试环境实测:stream 模式的 run 匹配得到,queue 模式 5 个探针
    run 全部 0 行)。

    ``RunStore.set_trace_id`` 早就存在(abstract + 两个实现 + 自己的测试),
    docstring 还写着「if the worker observes its own trace_id, the second
    call wins」——设计意图一直是让执行方回写,只是这条线从没接上。
    """
    spawns: list[dict] = []

    # 这条用例必须真的走到 ``asyncio.create_task(run_agent(...))``(回写就在
    # 那一带),所以 fake 得是真协程——别的用例用同步 lambda 是因为它们在
    # gate 处就被拦住,走不到这里。
    async def _fake_run_agent(**kw: object) -> None:
        spawns.append(dict(kw))

    monkeypatch.setattr(worker_module, "run_agent", _fake_run_agent)
    # 测试进程没有 init_tracing,``expert_work_span`` 开出来的是 no-op span,
    # ``current_trace_id_hex()`` 恒 None(那时不回写是正确行为,见下一条用例)。
    # 这里要验的是**接线**——拿到 trace 之后有没有真的写下去。
    monkeypatch.setattr(worker_module, "current_trace_id_hex", lambda: "b2" * 16)
    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    run_id, tenant = await _enqueue(runtime.run_manager)
    # 建行时的 trace(那个 202 请求的),执行后必须不再是它。
    await store.set_trace_id(run_id=run_id, tenant_id=tenant, trace_id="a1" * 16)

    worker = _worker(store, runtime)
    assert await worker.run_once() == 1

    info = await store.get(run_id=run_id, tenant_id=tenant)
    assert info is not None
    assert info.trace_id is not None
    assert info.trace_id != "a1" * 16, (
        "trace_id 仍是建行时那个 —— queue 模式下它指向已结束的 202 请求,token_usage 关联不上"
    )
    # 32 位小写 hex:与 ``current_trace_id_hex()`` 的契约一致。
    assert len(info.trace_id) == 32
    assert info.trace_id == info.trace_id.lower()


@pytest.mark.asyncio
async def test_execute_keeps_trace_id_when_no_span_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有活跃 span 时保留原 trace,不能把 ``None`` 写下去。

    OTel 未初始化(测试、部分 job 进程)时 ``current_trace_id_hex()`` 返回
    ``None``。那时建行时的 trace 是**唯一**已知的关联,擦掉它只会更糟。
    """
    spawns: list[dict] = []

    async def _fake_run_agent(**kw: object) -> None:
        spawns.append(dict(kw))

    monkeypatch.setattr(worker_module, "run_agent", _fake_run_agent)
    monkeypatch.setattr(worker_module, "current_trace_id_hex", lambda: None)
    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    run_id, tenant = await _enqueue(runtime.run_manager)
    await store.set_trace_id(run_id=run_id, tenant_id=tenant, trace_id="c3" * 16)

    worker = _worker(store, runtime)
    assert await worker.run_once() == 1

    info = await store.get(run_id=run_id, tenant_id=tenant)
    assert info is not None
    assert info.trace_id == "c3" * 16


@pytest.mark.asyncio
async def test_execute_records_the_manifest_version_it_actually_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """排队执行的 run 必须记下**执行时**读到的那一版 manifest。

    配置页对 manifest 是原地编辑,``thread_meta`` 上的 ``agent_name`` /
    ``agent_version`` 编辑前后一模一样。排队的 run 建行时还没构建过,worker
    真去执行时读到的可能已经是编辑后的版本 —— 所以这一列只能在这里写,
    建行时写会写错。
    """

    async def _fake_run_agent(**_kw: object) -> None:
        """Swallow the spawn — 这条用例只关心行里记下了什么。"""

    monkeypatch.setattr(worker_module, "run_agent", _fake_run_agent)
    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    run_id, tenant = await _enqueue(runtime.run_manager)
    before = await store.get(run_id=run_id, tenant_id=tenant)
    assert before is not None
    assert before.agent_spec_sha256 is None, "入队时还没构建过,不该有版本"

    worker = _worker(store, runtime)
    assert await worker.run_once() == 1

    info = await store.get(run_id=run_id, tenant_id=tenant)
    assert info is not None
    assert info.agent_spec_sha256 == compute_spec_sha256(_SPEC)


@pytest.mark.asyncio
async def test_claimed_run_carries_document_names_into_delegation_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """委派出去的子代不继承对话,看不到 ``[file attached: …]``,所以本轮附件必须
    另走 ``config["configurable"]``。

    与上一条是两件事:上一条证明附件到达了**本 run 的 HumanMessage**,这条证明
    它也到达了**子代的种子消息**。queue 这一处此前已经漏过一次同源的静默回读
    (见上),而漏这一处同样不报错 —— 子代只是"少知道一件事"。"""
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
    await asyncio.sleep(0)

    assert started == 1
    assert spawns[0]["config"]["configurable"]["turn_attachments"] == ["uploads/report.pdf"]


@pytest.mark.asyncio
async def test_queued_run_without_attachments_leaves_the_key_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有附件就不要往 config 里塞空列表 —— 空值和"这一轮没有附件"是同一件事,
    多一个键只会让下游多一条要判空的路径。"""
    spawns: list[dict] = []

    async def _fake_run_agent(**kw):
        spawns.append(kw)

    monkeypatch.setattr(worker_module, "run_agent", _fake_run_agent)

    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    await runtime.run_manager.enqueue(
        run_id=uuid4(),
        thread_id=uuid4(),
        tenant_id=uuid4(),
        enqueued_input={"input": "在吗", "image_refs": [], "untrusted_content": []},
    )

    await _worker(store, runtime).run_once()
    await asyncio.sleep(0)

    assert "turn_attachments" not in spawns[0]["config"]["configurable"]
