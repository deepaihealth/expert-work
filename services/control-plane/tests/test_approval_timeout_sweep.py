"""Stream 9.5 — ApprovalTimeoutSweep auto-rejects expired pending approvals.

Drives the worker against in-memory stores with ``run_agent`` monkeypatched to
a no-op (the continuation spawn is model-agnostic — the seam under test is the
``list_expired`` → ``mark_decided`` CAS → continuation path). Mirrors the fakes
in ``test_resume_idempotency_flow`` so the worker exercises the SAME
``resolve_approval_decision`` core the human endpoints use.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from control_plane.api import runs as runs_module
from control_plane.approval_timeout_sweep import ApprovalTimeoutSweep
from control_plane.audit import build_default_audit_logger
from expert_work.persistence import InMemoryApprovalStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AgentSpecStatus, ApprovalRecord, ApprovalStatus

_TENANT = uuid4()


class _FakeGraph:
    async def aupdate_state(self, *_a: object, **_k: object) -> None:
        return None


class _FakeRunManager:
    def __init__(self) -> None:
        self.created: list[object] = []

    async def create(self, **kw: object) -> SimpleNamespace:
        rec = SimpleNamespace(**kw, bound_distilled_skills=())
        self.created.append(rec)
        return rec

    async def attach_task(self, _run_id: object, _task: object) -> bool:
        return True


class _FakeRuntime:
    def __init__(self) -> None:
        self.run_manager = _FakeRunManager()
        self.stream_bridge = object()
        self.run_event_store = None
        self.skill_run_usage_recorder = None
        self.trajectory_recorder = None

    async def get_agent(self, **_kw: object) -> SimpleNamespace:
        return SimpleNamespace(
            graph=_FakeGraph(),
            bound_distilled_skills=(),
            tool_replay_safe=None,
            trajectory_recording=True,
            token_budget=0,
        )

    async def new_worker_spawn_budget(self) -> None:
        return None

    def delegation_gate(self) -> None:
        return None


class _FakeThreads:
    async def get(self, _thread_id: object, *, tenant_id: object) -> SimpleNamespace:
        del tenant_id
        return SimpleNamespace(agent_name="agent", agent_version="1.0.0", user_id=None)


class _FakeAgentRepo:
    async def get(
        self,
        *,
        tenant_id: object,
        name: object,
        version: object,
        include_deleted: bool = False,
    ) -> SimpleNamespace:
        del tenant_id, name, version, include_deleted
        return SimpleNamespace(spec=SimpleNamespace(), status=AgentSpecStatus.ACTIVE)


def _approval(run_id: object, thread_id: object, *, timeout_at: datetime) -> ApprovalRecord:
    now = datetime.now(UTC)
    return ApprovalRecord(
        id=uuid4(),
        tenant_id=_TENANT,
        run_id=run_id,  # type: ignore[arg-type]
        thread_id=thread_id,  # type: ignore[arg-type]
        request_id="approval:sweep",
        node="tools",
        reason_kind="policy_gate",
        action_summary="approval-gated tool 'http'",
        proposed_args={},
        requested_at=now,
        timeout_at=timeout_at,
        status=ApprovalStatus.PENDING,
    )


def _sweep(approvals: InMemoryApprovalStore, runtime: _FakeRuntime) -> ApprovalTimeoutSweep:
    return ApprovalTimeoutSweep(
        approval_store=approvals,
        thread_store=_FakeThreads(),  # type: ignore[arg-type]
        agent_spec_store=_FakeAgentRepo(),  # type: ignore[arg-type]
        runtime=runtime,  # type: ignore[arg-type]
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        interval_s=60,
    )


@pytest.fixture(autouse=True)
def _no_op_run_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_run_agent(**_kw: object) -> None:
        return None

    monkeypatch.setattr(runs_module, "run_agent", _fake_run_agent)


@pytest.mark.asyncio
async def test_sweep_times_out_expired_approval() -> None:
    approvals = InMemoryApprovalStore()
    run_id, thread_id = uuid4(), uuid4()
    past = datetime.now(UTC) - timedelta(minutes=1)
    await approvals.create(_approval(run_id, thread_id, timeout_at=past))

    runtime = _FakeRuntime()
    swept = await _sweep(approvals, runtime).run_once()

    assert swept == 1
    row = await approvals.get_by_run(run_id=run_id, tenant_id=_TENANT)
    assert row is not None
    assert row.status is ApprovalStatus.TIMEOUT
    assert row.decided_by == "approval_timeout_sweep"
    # A continuation run was spawned to resume the (now rejected) paused run.
    assert len(runtime.run_manager.created) == 1


@pytest.mark.asyncio
async def test_sweep_does_not_resume_a_disabled_agent() -> None:
    # RT-4 (RT-ADR-16) — the timeout sweep is a run-spawn path too; a disabled
    # agent must not be resumed by it. The approval stays PENDING (reversible).
    from control_plane.agent_disable_status import AgentDisableService
    from expert_work.persistence.agent_disable import InMemoryAgentDisableStore

    approvals = InMemoryApprovalStore()
    run_id, thread_id = uuid4(), uuid4()
    past = datetime.now(UTC) - timedelta(minutes=1)
    await approvals.create(_approval(run_id, thread_id, timeout_at=past))

    disable_store = InMemoryAgentDisableStore()
    await disable_store.set_disabled(
        tenant_id=_TENANT, agent_name="agent", disabled=True, reason="stop", disabled_by="op"
    )

    runtime = _FakeRuntime()
    sweep = ApprovalTimeoutSweep(
        approval_store=approvals,
        thread_store=_FakeThreads(),  # type: ignore[arg-type]
        agent_spec_store=_FakeAgentRepo(),  # type: ignore[arg-type]
        runtime=runtime,  # type: ignore[arg-type]
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        interval_s=60,
        agent_disable_service=AgentDisableService(store=disable_store),
    )
    await sweep.run_once()

    # No continuation was spawned, and the approval is left untouched (PENDING)
    # so re-enabling the agent lets it time out / be decided normally.
    assert len(runtime.run_manager.created) == 0
    row = await approvals.get_by_run(run_id=run_id, tenant_id=_TENANT)
    assert row is not None and row.status is ApprovalStatus.PENDING


@pytest.mark.asyncio
async def test_sweep_skips_not_yet_expired() -> None:
    approvals = InMemoryApprovalStore()
    run_id, thread_id = uuid4(), uuid4()
    future = datetime.now(UTC) + timedelta(hours=1)
    await approvals.create(_approval(run_id, thread_id, timeout_at=future))

    runtime = _FakeRuntime()
    swept = await _sweep(approvals, runtime).run_once()

    assert swept == 0
    row = await approvals.get_by_run(run_id=run_id, tenant_id=_TENANT)
    assert row is not None
    assert row.status is ApprovalStatus.PENDING  # untouched — deadline not reached
    assert runtime.run_manager.created == []


@pytest.mark.asyncio
async def test_two_instances_time_out_exactly_once() -> None:
    """Blue + green both sweep the same expired approval; the ``mark_decided``
    CAS lets exactly one auto-reject it + spawn the continuation."""
    approvals = InMemoryApprovalStore()
    run_id, thread_id = uuid4(), uuid4()
    past = datetime.now(UTC) - timedelta(minutes=1)
    await approvals.create(_approval(run_id, thread_id, timeout_at=past))

    blue_rt, green_rt = _FakeRuntime(), _FakeRuntime()
    counts = await asyncio.gather(
        _sweep(approvals, blue_rt).run_once(),
        _sweep(approvals, green_rt).run_once(),
    )

    assert sum(counts) == 1  # exactly one instance won the CAS
    spawned = len(blue_rt.run_manager.created) + len(green_rt.run_manager.created)
    assert spawned == 1  # the continuation was spawned once, no double-resume
    row = await approvals.get_by_run(run_id=run_id, tenant_id=_TENANT)
    assert row is not None
    assert row.status is ApprovalStatus.TIMEOUT
