"""Stream 9.4 (HA failover) — OrphanSweep recovery + hot-handoff.

Drives the real :class:`OrphanSweep` over a real :class:`InMemoryRunStore`
seeded with an expired-lease running run (a crashed owner). ``run_agent`` is
monkeypatched to a recording no-op so no real graph/streaming is needed — the
seam under test is detect → reclaim CAS → adopt → respawn (or, past the cap /
with auto-reclaim off, mark errored), which is model-agnostic.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from control_plane import orphan_sweep as sweep_module
from control_plane.agent_disable_status import AgentDisableService
from control_plane.audit import build_default_audit_logger
from control_plane.orphan_sweep import OrphanSweep
from control_plane.tenant_status import TenantStatusService
from expert_work.persistence import InMemoryAgentDisableStore, InMemoryTenantConfigStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.platform_agent_template import compute_spec_sha256
from expert_work.protocol import AgentSpec
from expert_work.runtime.runs import InMemoryRunStore, RunInfo, RunManager, RunStatus
from expert_work.runtime.runs.schemas import DisconnectMode


def _run_info(*, run_id, tenant, thread, status=RunStatus.RUNNING, created_at=None) -> RunInfo:
    now = created_at or datetime.now(UTC)
    return RunInfo(
        run_id=run_id,
        tenant_id=tenant,
        thread_id=thread,
        user_id=None,
        status=status,
        on_disconnect=DisconnectMode.CANCEL,
        is_resume=False,
        error=None,
        created_at=now,
        updated_at=now,
        finished_at=None,
    )


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
        "metadata": {"name": "a", "version": "1.0.0", "tenant": "t"},
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
        # A real AgentSpec, not a stand-in: the sweep now hashes what it
        # rebuilt from, and a stand-in would make that hashing untestable here.
        return SimpleNamespace(spec=_SPEC)


class _FakeRuntime:
    """Minimal AgentRuntime surface the sweep touches."""

    def __init__(self, run_store: InMemoryRunStore) -> None:
        self.run_manager = RunManager(run_store, instance_id="sweeper", lease_ttl_s=30.0)
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
            trajectory_recording=True,
            token_budget=0,
            worker_max_concurrent=None,
            worker_max_per_run=None,
        )

    async def new_worker_spawn_budget(self, **_kw: object):
        return None

    def delegation_gate(self):
        return None


async def _seed_orphan(store: InMemoryRunStore, *, expired: bool):
    run_id, tenant, thread = uuid4(), uuid4(), uuid4()
    await store.create(_run_info(run_id=run_id, tenant=tenant, thread=thread))
    now = datetime.now(UTC)
    lease = now - timedelta(seconds=5) if expired else now + timedelta(seconds=60)
    await store.claim(
        run_id=run_id,
        tenant_id=tenant,
        claimed_by="dead-instance",
        lease_until=lease,
        heartbeat_at=now - timedelta(seconds=40),
    )
    return run_id, tenant


def _sweep(store, runtime, **kw) -> OrphanSweep:
    return OrphanSweep(
        run_store=store,
        thread_store=kw.pop("threads", _FakeThreads()),
        agent_spec_store=_FakeAgents(),
        runtime=runtime,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        approval_store=object(),
        **kw,
    )


@pytest.mark.asyncio
async def test_reclaims_and_respawns_expired_orphan(monkeypatch: pytest.MonkeyPatch) -> None:
    spawns: list[object] = []

    async def _fake_run_agent(**kw):
        spawns.append(kw)

    monkeypatch.setattr(sweep_module, "run_agent", _fake_run_agent)

    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    run_id, tenant = await _seed_orphan(store, expired=True)

    handled = await _sweep(store, runtime).run_once()
    await asyncio.sleep(0)  # let the spawned task body run

    assert handled == 1
    assert len(spawns) == 1  # respawned exactly once
    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None
    assert row.claimed_by == "sweeper"  # reclaimed by this instance
    assert row.reclaim_count == 1
    assert row.status is RunStatus.RUNNING  # still running (resumed)


@pytest.mark.asyncio
async def test_skips_fresh_lease_run(monkeypatch: pytest.MonkeyPatch) -> None:
    spawns: list[object] = []
    monkeypatch.setattr(sweep_module, "run_agent", lambda **kw: spawns.append(kw))
    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    await _seed_orphan(store, expired=False)  # lease still valid → not an orphan
    handled = await _sweep(store, runtime).run_once()
    assert handled == 0
    assert spawns == []


@pytest.mark.asyncio
async def test_marks_errored_past_reclaim_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sweep_module, "run_agent", lambda **kw: None)
    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    run_id, tenant = await _seed_orphan(store, expired=True)
    # Burn the reclaim budget (reclaim_count starts 0; cap=2 → already at cap).
    for _ in range(2):
        await store.reclaim(
            run_id=run_id,
            new_owner="x",
            lease_until=datetime.now(UTC) - timedelta(seconds=1),
            heartbeat_at=datetime.now(UTC),
            now=datetime.now(UTC),
        )
    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None and row.reclaim_count == 2

    await _sweep(store, runtime, max_reclaims=2).run_once()
    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None
    assert row.status is RunStatus.ERROR  # not respawned — errored


@pytest.mark.asyncio
async def test_conservative_mode_marks_errored(monkeypatch: pytest.MonkeyPatch) -> None:
    spawns: list[object] = []
    monkeypatch.setattr(sweep_module, "run_agent", lambda **kw: spawns.append(kw))
    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    run_id, tenant = await _seed_orphan(store, expired=True)
    await _sweep(store, runtime, auto_reclaim=False).run_once()
    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None
    assert row.status is RunStatus.ERROR
    assert spawns == []  # never respawned in conservative mode


@pytest.mark.asyncio
async def test_skips_respawn_for_disabled_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stream RT-4 — a reclaimed run whose agent was disabled is terminated,
    not resumed."""
    spawns: list[object] = []
    monkeypatch.setattr(sweep_module, "run_agent", lambda **kw: spawns.append(kw))
    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    run_id, tenant = await _seed_orphan(store, expired=True)
    disable_store = InMemoryAgentDisableStore()
    await disable_store.set_disabled(
        tenant_id=tenant, agent_name="a", disabled=True, reason=None, disabled_by="admin"
    )
    sweep = _sweep(store, runtime, agent_disable_service=AgentDisableService(store=disable_store))
    handled = await sweep.run_once()
    assert handled == 1  # reclaimed + handled (terminated)
    assert spawns == []  # NOT respawned
    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None
    assert row.status is RunStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_skips_respawn_for_suspended_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stream RT-4 — a reclaimed run in a suspended tenant is terminated."""
    spawns: list[object] = []
    monkeypatch.setattr(sweep_module, "run_agent", lambda **kw: spawns.append(kw))
    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    run_id, tenant = await _seed_orphan(store, expired=True)
    tcs = InMemoryTenantConfigStore()
    await tcs.create(tenant_id=tenant, display_name="t", actor_id="seed")
    await tcs.set_status(tenant_id=tenant, status="suspended", actor_id="admin")
    sweep = _sweep(store, runtime, tenant_status_service=TenantStatusService(store=tcs))
    handled = await sweep.run_once()
    assert handled == 1
    assert spawns == []
    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None
    assert row.status is RunStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_fail_orphan_second_call_skips_audit_and_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W1 PR1 task 4 — two replicas racing the same orphan's terminal
    transition (both scanned it while it was still RUNNING/expired-lease)
    must not double-audit or double-count. The CAS guard (``fail_if_active``)
    lets only the first ``_fail_orphan`` call win; the loser is a no-op."""
    monkeypatch.setattr(sweep_module, "run_agent", lambda **kw: None)
    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    run_id, tenant = await _seed_orphan(store, expired=True)
    sweep = _sweep(store, runtime, auto_reclaim=False)

    audit_calls: list[str] = []

    async def _fake_emit_audit(_orphan, *, result, reason):
        del result
        audit_calls.append(reason)

    monkeypatch.setattr(sweep, "_emit_audit", _fake_emit_audit)

    counter_calls: list[dict[str, str]] = []
    real_labels = sweep_module._failed_total.labels

    def _spying_labels(**kw):
        counter_calls.append(kw)
        return real_labels(**kw)

    monkeypatch.setattr(sweep_module._failed_total, "labels", _spying_labels)

    now = datetime.now(UTC)
    # Both replicas' scan phase observed the same still-RUNNING snapshot.
    orphan = (await store.list_orphans(now=now, limit=10))[0]
    await sweep._fail_orphan(orphan, now=now, reason="auto_reclaim_off")
    await sweep._fail_orphan(orphan, now=now, reason="auto_reclaim_off")  # loses the CAS

    assert audit_calls == ["auto_reclaim_off"]
    assert counter_calls == [{"reason": "auto_reclaim_off"}]
    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None and row.status is RunStatus.ERROR


@pytest.mark.asyncio
async def test_kill_switch_second_call_skips_audit_and_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W1-PR2 Task 5 — two replicas racing the same reclaimed run's
    kill-switch termination (both resumed the same disabled-agent orphan in
    the same sweep cycle) must not double-audit or double-count. The CAS
    guard (``request_cancel``) lets only the first ``_respawn`` call's
    kill-switch branch win; the loser is a no-op — same shape as
    ``test_fail_orphan_second_call_skips_audit_and_counter`` above."""
    monkeypatch.setattr(sweep_module, "run_agent", lambda **kw: None)
    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    run_id, tenant = await _seed_orphan(store, expired=True)
    disable_store = InMemoryAgentDisableStore()
    await disable_store.set_disabled(
        tenant_id=tenant, agent_name="a", disabled=True, reason=None, disabled_by="admin"
    )
    sweep = _sweep(store, runtime, agent_disable_service=AgentDisableService(store=disable_store))

    audit_calls: list[str] = []

    async def _fake_emit_audit(_orphan, *, result, reason):
        del result
        audit_calls.append(reason)

    monkeypatch.setattr(sweep, "_emit_audit", _fake_emit_audit)

    counter_calls: list[dict[str, str]] = []
    real_labels = sweep_module._failed_total.labels

    def _spying_labels(**kw):
        counter_calls.append(kw)
        return real_labels(**kw)

    monkeypatch.setattr(sweep_module._failed_total, "labels", _spying_labels)

    now = datetime.now(UTC)
    # Both replicas' scan phase reclaimed the same orphan under this
    # instance (mirrors run_once()'s reclaim step) so the row is RUNNING
    # when _respawn's kill-switch branch fires for each racing call.
    await store.reclaim(
        run_id=run_id,
        new_owner="sweeper",
        lease_until=now + timedelta(seconds=30),
        heartbeat_at=now,
        now=now,
    )
    orphan = await store.get(run_id=run_id, tenant_id=tenant)
    assert orphan is not None

    await sweep._respawn(orphan)
    await sweep._respawn(orphan)  # loses the CAS — row already INTERRUPTED

    assert audit_calls == ["agent_disabled"]
    assert counter_calls == [{"reason": "agent_disabled"}]
    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None and row.status is RunStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_pending_sweep_fails_stale_pending_and_spares_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W1-PR3 Task 1 — a PENDING run whose owner crashed in the
    create→RUNNING window (before ever stamping a lease) never shows up in
    ``list_orphans`` (that scan only looks at running + expired lease);
    ``run_once`` must also sweep stale PENDING rows via
    ``list_stale_pending``, reusing ``_fail_orphan``'s CAS + audit."""
    monkeypatch.setattr(sweep_module, "run_agent", lambda **kw: None)
    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    sweep = _sweep(store, runtime)

    now = datetime.now(UTC)
    stale_id, stale_tenant, stale_thread = uuid4(), uuid4(), uuid4()
    await store.create(
        _run_info(
            run_id=stale_id,
            tenant=stale_tenant,
            thread=stale_thread,
            status=RunStatus.PENDING,
            created_at=now - timedelta(seconds=sweep_module._PENDING_STALE_S + 60),
        )
    )
    fresh_id, fresh_tenant, fresh_thread = uuid4(), uuid4(), uuid4()
    await store.create(
        _run_info(
            run_id=fresh_id,
            tenant=fresh_tenant,
            thread=fresh_thread,
            status=RunStatus.PENDING,
            created_at=now,
        )
    )

    handled = await sweep.run_once()
    assert handled == 1

    stale_row = await store.get(run_id=stale_id, tenant_id=stale_tenant)
    assert stale_row is not None
    assert stale_row.status is RunStatus.ERROR
    assert stale_row.error == "orphaned run failover: stale_pending"

    fresh_row = await store.get(run_id=fresh_id, tenant_id=fresh_tenant)
    assert fresh_row is not None
    assert fresh_row.status is RunStatus.PENDING  # untouched — inside the normal window

    # A second sweep pass sees the (now ERROR) row filtered out by the
    # status='pending' predicate — no re-processing, no duplicate audit.
    handled_again = await sweep.run_once()
    assert handled_again == 0


@pytest.mark.asyncio
async def test_pending_sweep_cas_loser_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two replicas' sweep loops racing the same stale-PENDING row (both
    scanned it while it was still PENDING) must not double-audit or
    double-count — same CAS shape as
    ``test_fail_orphan_second_call_skips_audit_and_counter``, exercised via
    the ``stale_pending`` reason."""
    monkeypatch.setattr(sweep_module, "run_agent", lambda **kw: None)
    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    sweep = _sweep(store, runtime)

    now = datetime.now(UTC)
    run_id, tenant, thread = uuid4(), uuid4(), uuid4()
    await store.create(
        _run_info(
            run_id=run_id,
            tenant=tenant,
            thread=thread,
            status=RunStatus.PENDING,
            created_at=now - timedelta(seconds=sweep_module._PENDING_STALE_S + 60),
        )
    )

    audit_calls: list[str] = []

    async def _fake_emit_audit(_orphan, *, result, reason):
        del result
        audit_calls.append(reason)

    monkeypatch.setattr(sweep, "_emit_audit", _fake_emit_audit)

    counter_calls: list[dict[str, str]] = []
    real_labels = sweep_module._failed_total.labels

    def _spying_labels(**kw):
        counter_calls.append(kw)
        return real_labels(**kw)

    monkeypatch.setattr(sweep_module._failed_total, "labels", _spying_labels)

    # Both replicas' scan phase observed the same still-PENDING snapshot.
    pending = (await store.list_stale_pending(cutoff=now, limit=10))[0]
    await sweep._fail_orphan(pending, now=now, reason="stale_pending")
    await sweep._fail_orphan(pending, now=now, reason="stale_pending")  # loses the CAS

    assert audit_calls == ["stale_pending"]
    assert counter_calls == [{"reason": "stale_pending"}]
    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None and row.status is RunStatus.ERROR


@pytest.mark.asyncio
async def test_no_agent_meta_marks_errored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sweep_module, "run_agent", lambda **kw: None)
    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    run_id, tenant = await _seed_orphan(store, expired=True)
    sweep = _sweep(store, runtime, threads=_FakeThreads(has_agent=False))
    await sweep.run_once()
    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None
    assert row.status is RunStatus.ERROR  # reclaimed then errored (unrecoverable)


@pytest.mark.asyncio
async def test_stop_is_bounded_when_a_sweep_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    """卡死的 sweep 不能把关机拖到 SIGKILL —— stop() 等一小会儿就取消它。

    lifespan 顺序 await 每个 worker 的 stop();这里少了上界,一个卡在
    reclaim+恢复 run 的 sweep 就能把整个进程的关机挂到 K8s 优雅期耗尽。
    """
    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    sweep = _sweep(store, runtime, interval_s=0.01)
    entered = asyncio.Event()

    async def _never_returns() -> int:
        entered.set()
        await asyncio.sleep(3600)
        return 0

    monkeypatch.setattr(sweep, "run_once", _never_returns)
    monkeypatch.setattr(sweep_module, "_STOP_TIMEOUT_S", 0.05, raising=False)

    sweep.start()
    await asyncio.wait_for(entered.wait(), timeout=2)

    # 修复前:stop() 永远等下去,这里超时失败。
    await asyncio.wait_for(sweep.stop(), timeout=2)
    assert sweep._task is None


@pytest.mark.asyncio
async def test_respawn_rewrites_trace_id_to_the_executing_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """被回收重跑的 run 必须把 ``trace_id`` 换成**续跑那一段**的 trace。

    ``agent_run.trace_id`` 记的是上一次执行的 trace。原主实例崩掉后,续跑
    发生在这里 —— sweep 是个后台轮询循环,``run_agent`` 从它的 context 里
    ``create_task`` 出来,``expert_work.session.run`` 因此另起一个 trace。
    续跑段的每次 LLM 调用都把 ``token_usage`` 记在那条新 trace 下,而
    ``token_usage`` 没有 ``run_id`` 列 —— ``totals_by_trace_ids`` 全靠 trace
    连接两张表。行里还是旧 trace,续跑段的用量就查不回来。

    这和 ``run_queue_worker`` 那条(#1373)是同一个病:执行入口有三个,
    ``RunStore.set_trace_id`` 当时只接了排队 worker 一个。
    """
    spawns: list[object] = []

    async def _fake_run_agent(**kw):
        spawns.append(kw)

    monkeypatch.setattr(sweep_module, "run_agent", _fake_run_agent)
    # 测试进程没有 init_tracing,``expert_work_span`` 开出来是 no-op span,
    # ``current_trace_id_hex()`` 恒 None。这里验的是接线 —— 拿到 trace 之后
    # 有没有真写下去。
    monkeypatch.setattr(sweep_module, "current_trace_id_hex", lambda: "b2" * 16)

    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    run_id, tenant = await _seed_orphan(store, expired=True)
    # 上一次执行留下的 trace —— 崩溃前那条,续跑后必须不再是它。
    await store.set_trace_id(run_id=run_id, tenant_id=tenant, trace_id="a1" * 16)

    assert await _sweep(store, runtime).run_once() == 1
    await asyncio.sleep(0)

    assert len(spawns) == 1
    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None
    assert row.trace_id == "b2" * 16, (
        "trace_id 还是崩溃前那条 —— 续跑段的 token_usage 关联不回这个 run"
    )


@pytest.mark.asyncio
async def test_respawn_keeps_trace_id_when_no_span_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有活跃 span 时保留原 trace,不能把 ``None`` 写下去。

    OTel 未初始化时 ``current_trace_id_hex()`` 返回 ``None``。那时上一次执行
    的 trace 是**唯一**已知的关联,擦掉只会更糟。
    """

    async def _fake_run_agent(**kw):
        return None

    monkeypatch.setattr(sweep_module, "run_agent", _fake_run_agent)
    monkeypatch.setattr(sweep_module, "current_trace_id_hex", lambda: None)

    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    run_id, tenant = await _seed_orphan(store, expired=True)
    await store.set_trace_id(run_id=run_id, tenant_id=tenant, trace_id="c3" * 16)

    assert await _sweep(store, runtime).run_once() == 1
    await asyncio.sleep(0)

    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None
    assert row.trace_id == "c3" * 16


@pytest.mark.asyncio
async def test_respawn_records_the_manifest_version_it_rebuilt_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """续跑必须记下**这次续跑**读到的那一版 manifest。

    原 run 可能是几分钟前建的行,期间配置被编辑过很正常;续跑重建 agent 用的
    是当前那一版,记的就必须是它,不是崩掉那次用的。
    """

    async def _fake_run_agent(**_kw):
        """Swallow the spawn — 这条用例只关心行里记下了什么。"""

    monkeypatch.setattr(sweep_module, "run_agent", _fake_run_agent)
    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    run_id, tenant = await _seed_orphan(store, expired=True)

    assert await _sweep(store, runtime).run_once() == 1
    await asyncio.sleep(0)

    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None
    assert row.agent_spec_sha256 == compute_spec_sha256(_SPEC)
