"""Stream 13.2 — full-flow idempotency seam for ``apply_approval_decision``.

The endpoint tests in ``test_runs_api`` seed an already-decided approval row to
exercise the replay branch. This drives the REAL winner path instead: a pending
approval → a genuine decide (the CAS winner persists ``continuation_run_id`` via
``mark_decided``) → a retry with the same key replays it WITHOUT spawning a
second continuation worker. ``run_agent`` is monkeypatched to a recording no-op
so no streaming / real graph is needed — the seam under test is the
store-then-replay data flow + spawn-exactly-once, which is model-agnostic.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from control_plane.agent_disable_status import AgentDisableService
from control_plane.api import runs as runs_module
from control_plane.api.runs import _workspace_drift, apply_approval_decision
from control_plane.audit import build_default_audit_logger
from control_plane.tenant_status import TenantStatusService
from expert_work.persistence import (
    InMemoryAgentDisableStore,
    InMemoryApprovalStore,
    InMemoryUserWorkspaceStore,
)
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.workspace import workspace_volume_name
from expert_work.protocol import (
    AgentSpecStatus,
    ApprovalRecord,
    ApprovalStatus,
    AuditAction,
    Principal,
    UserWorkspace,
    canonical_args_digest,
)

_TENANT = uuid4()


def _request(
    *,
    tenant_status: TenantStatusService | None = None,
    agent_disable: AgentDisableService | None = None,
    workspace_store: object | None = None,
) -> SimpleNamespace:
    # A service principal owns no per-user instance (resolve_caller_user_id →
    # None) and an unowned thread (meta.user_id=None) passes caller_owns_thread.
    # ``app.state`` carries the RT-4 kill-switch services the resume gate reads
    # (both ``None`` here = fail-open, as in an unwired deployment).
    principal = Principal(subject_id=str(uuid4()), subject_type="service", tenant_id=_TENANT)
    app = SimpleNamespace(
        state=SimpleNamespace(
            tenant_status_service=tenant_status,
            agent_disable_service=agent_disable,
            user_workspace_store=workspace_store,
        )
    )
    return SimpleNamespace(
        app=app,
        state=SimpleNamespace(tenant_id=_TENANT, actor_id="svc", principal=principal),
    )


class _FakeGraph:
    async def aupdate_state(self, *_a: object, **_k: object) -> None:
        return None


class _CapturingGraph:
    """Records the ``approval_resume`` payload written into the checkpoint."""

    def __init__(self) -> None:
        self.resume_payloads: list[dict[str, object]] = []

    async def aupdate_state(self, _config: object, state: dict[str, object], **_k: object) -> None:
        resume = state.get("approval_resume")
        if isinstance(resume, dict):
            self.resume_payloads.append(resume)


class _FakeRunManager:
    def __init__(self) -> None:
        self.created: list[object] = []

    async def create(self, **kw: object) -> SimpleNamespace:
        rec = SimpleNamespace(**kw, bound_distilled_skills=(), seed_artifacts=None)
        self.created.append(rec)
        return rec

    async def persisted_artifacts(self, run_id: object, *, tenant_id: object) -> None:
        # 产物清单契约 —— decide 端点给 continuation 播父 run 清单;本 fake
        # 的场景不涉产物,恒 None(= 无记录)。
        del run_id, tenant_id
        return None

    async def attach_task(self, _run_id: object, _task: object) -> bool:
        return True


class _FakeRuntime:
    def __init__(self, graph: object | None = None) -> None:
        self.run_manager = _FakeRunManager()
        self.stream_bridge = object()
        self.run_event_store = None
        self.skill_run_usage_recorder = None
        self.trajectory_recorder = None
        self.thread_stats_recorder = None
        self._graph = graph if graph is not None else _FakeGraph()

    async def get_agent(self, **_kw: object) -> SimpleNamespace:
        return SimpleNamespace(
            graph=self._graph,
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


def _pending(run_id: object, thread_id: object) -> ApprovalRecord:
    now = datetime.now(UTC)
    return ApprovalRecord(
        id=uuid4(),
        tenant_id=_TENANT,
        run_id=run_id,  # type: ignore[arg-type]
        thread_id=thread_id,  # type: ignore[arg-type]
        request_id="approval:flow",
        node="tools",
        reason_kind="policy_gate",
        action_summary="approval-gated tool 'http'",
        proposed_args={},
        requested_at=now,
        timeout_at=now + timedelta(hours=24),
        status=ApprovalStatus.PENDING,
    )


@pytest.mark.asyncio
async def test_winner_stores_continuation_then_retry_replays_without_respawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawns: list[dict[str, object]] = []

    async def _fake_run_agent(**kw: object) -> None:
        spawns.append(kw)

    monkeypatch.setattr(runs_module, "run_agent", _fake_run_agent)

    approvals = InMemoryApprovalStore()
    run_id, thread_id = uuid4(), uuid4()
    await approvals.create(_pending(run_id, thread_id))

    runtime = _FakeRuntime()
    common = {
        "thread_id": thread_id,
        "run_id": run_id,
        "decision": "approve",
        "modified_args": None,
        "reason": None,
        "threads": _FakeThreads(),
        "users": object(),
        "agent_repo": _FakeAgentRepo(),
        "runtime": runtime,
        "approvals": approvals,
        "audit": build_default_audit_logger(InMemoryAuditLogStore()),
        "idempotency_key": "flow-key",
    }

    # 1) Winner decide — persists continuation_run_id via the CAS, spawns once.
    _record, continuation, replayed = await apply_approval_decision(request=_request(), **common)
    await asyncio.sleep(0)  # let the spawned task body run
    assert replayed is False
    assert len(spawns) == 1
    stored = await approvals.get_by_run(run_id=run_id, tenant_id=_TENANT)
    assert stored is not None
    assert stored.status is ApprovalStatus.APPROVED
    assert stored.continuation_run_id == continuation
    assert stored.idempotency_key == "flow-key"

    # 2) Retry with the SAME key — idempotent replay returns the same id, NO
    #    second worker spawned.
    record2, continuation2, replayed2 = await apply_approval_decision(request=_request(), **common)
    await asyncio.sleep(0)
    assert replayed2 is True
    assert record2 is None
    assert continuation2 == continuation
    assert len(spawns) == 1  # still exactly one — replay never re-spawns


@pytest.mark.asyncio
async def test_disabled_agent_resume_is_blocked_and_spawns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stream RT-4 — resume mints a fresh run id no front-door gate sees; a
    disabled agent must not resume an approved (possibly dangerous) tool call."""
    spawns: list[dict[str, object]] = []
    monkeypatch.setattr(runs_module, "run_agent", lambda **kw: spawns.append(kw))

    approvals = InMemoryApprovalStore()
    run_id, thread_id = uuid4(), uuid4()
    await approvals.create(_pending(run_id, thread_id))

    disable_store = InMemoryAgentDisableStore()
    await disable_store.set_disabled(
        tenant_id=_TENANT, agent_name="agent", disabled=True, reason=None, disabled_by="admin"
    )

    common = {
        "thread_id": thread_id,
        "run_id": run_id,
        "decision": "approve",
        "modified_args": None,
        "reason": None,
        "threads": _FakeThreads(),
        "users": object(),
        "agent_repo": _FakeAgentRepo(),
        "runtime": _FakeRuntime(),
        "approvals": approvals,
        "audit": build_default_audit_logger(InMemoryAuditLogStore()),
        "idempotency_key": "flow-key",
    }
    with pytest.raises(HTTPException) as exc:
        await apply_approval_decision(
            request=_request(agent_disable=AgentDisableService(store=disable_store)),
            **common,
        )
    assert exc.value.status_code == 403
    await asyncio.sleep(0)
    assert spawns == []  # no continuation worker spawned


@pytest.mark.asyncio
async def test_approve_threads_mint_digest_into_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RT-6 Tier A — approve carries the row's mint digest into approval_resume."""

    async def _noop_run_agent(**_kw: object) -> None:
        return None

    monkeypatch.setattr(runs_module, "run_agent", _noop_run_agent)

    approvals = InMemoryApprovalStore()
    run_id, thread_id = uuid4(), uuid4()
    await approvals.create(
        _pending(run_id, thread_id).model_copy(update={"binding_digest": "mint-digest"})
    )
    graph = _CapturingGraph()
    runtime = _FakeRuntime(graph=graph)

    await apply_approval_decision(
        request=_request(),
        thread_id=thread_id,
        run_id=run_id,
        decision="approve",
        modified_args=None,
        reason=None,
        threads=_FakeThreads(),
        users=object(),
        agent_repo=_FakeAgentRepo(),
        runtime=runtime,
        approvals=approvals,
        audit=build_default_audit_logger(InMemoryAuditLogStore()),
        idempotency_key="k-approve",
    )
    assert graph.resume_payloads == [
        {
            "decision": "approve",
            "modified_args": None,
            "reason": None,
            "binding_digest": "mint-digest",
        }
    ]
    # approve keeps the mint digest (no re-bind).
    stored = await approvals.get_by_run(run_id=run_id, tenant_id=_TENANT)
    assert stored is not None
    assert stored.binding_digest == "mint-digest"


@pytest.mark.asyncio
async def test_modify_rebinds_digest_and_threads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RT-6 Tier A — modify re-binds to digest(modified_args), stored + threaded."""

    async def _noop_run_agent(**_kw: object) -> None:
        return None

    monkeypatch.setattr(runs_module, "run_agent", _noop_run_agent)

    approvals = InMemoryApprovalStore()
    run_id, thread_id = uuid4(), uuid4()
    await approvals.create(
        _pending(run_id, thread_id).model_copy(update={"binding_digest": "mint-digest"})
    )
    graph = _CapturingGraph()
    runtime = _FakeRuntime(graph=graph)
    modified = {"url": "https://safe.example.com", "method": "GET"}
    expected = canonical_args_digest(modified)

    await apply_approval_decision(
        request=_request(),
        thread_id=thread_id,
        run_id=run_id,
        decision="modify",
        modified_args=modified,
        reason=None,
        threads=_FakeThreads(),
        users=object(),
        agent_repo=_FakeAgentRepo(),
        runtime=runtime,
        approvals=approvals,
        audit=build_default_audit_logger(InMemoryAuditLogStore()),
        idempotency_key="k-modify",
    )
    # Threaded into the resume payload...
    assert graph.resume_payloads[0]["binding_digest"] == expected
    # ...and persisted atomically on the row (overwriting the mint digest).
    stored = await approvals.get_by_run(run_id=run_id, tenant_id=_TENANT)
    assert stored is not None
    assert stored.binding_digest == expected


# ---------------------------------------------------------------------------
# RT-6 Tier B — workspace-drift signal on the decision audit (RT-ADR-20)
# ---------------------------------------------------------------------------


def _seed_workspace(user_id: UUID, *, last_write_at: datetime | None) -> InMemoryUserWorkspaceStore:
    store = InMemoryUserWorkspaceStore()
    store._rows[(_TENANT, user_id)] = UserWorkspace(
        id=uuid4(),
        tenant_id=_TENANT,
        user_id=user_id,
        volume_name=workspace_volume_name(_TENANT, user_id),
        last_write_at=last_write_at,
    )
    return store


def _decided_drift(audit_store: InMemoryAuditLogStore) -> object:
    for entry in audit_store._rows.values():
        if entry.action is AuditAction.APPROVAL_DECIDED:
            return entry.details.get("workspace_drift")
    raise AssertionError("no APPROVAL_DECIDED audit written")


async def _decide_with_workspace(
    monkeypatch: pytest.MonkeyPatch,
    *,
    user_id: UUID,
    workspace_store: InMemoryUserWorkspaceStore | None,
) -> InMemoryAuditLogStore:
    async def _noop_run_agent(**_kw: object) -> None:
        return None

    monkeypatch.setattr(runs_module, "run_agent", _noop_run_agent)

    approvals = InMemoryApprovalStore()
    run_id, thread_id = uuid4(), uuid4()
    # policy_gate + a user binding are the two preconditions for the drift check.
    await approvals.create(_pending(run_id, thread_id).model_copy(update={"user_id": user_id}))
    audit_store = InMemoryAuditLogStore()
    await apply_approval_decision(
        request=_request(workspace_store=workspace_store),
        thread_id=thread_id,
        run_id=run_id,
        decision="approve",
        modified_args=None,
        reason=None,
        threads=_FakeThreads(),
        users=object(),
        agent_repo=_FakeAgentRepo(),
        runtime=_FakeRuntime(),
        approvals=approvals,
        audit=build_default_audit_logger(audit_store),
        idempotency_key=None,
    )
    return audit_store


@pytest.mark.asyncio
async def test_workspace_drift_true_when_write_after_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workspace write AFTER the approval was requested → drift=True."""
    user_id = uuid4()
    store = _seed_workspace(user_id, last_write_at=datetime.now(UTC) + timedelta(minutes=5))
    audit_store = await _decide_with_workspace(monkeypatch, user_id=user_id, workspace_store=store)
    assert _decided_drift(audit_store) is True


@pytest.mark.asyncio
async def test_workspace_drift_false_when_write_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workspace last written BEFORE the request (or never) → drift=False."""
    user_id = uuid4()
    store = _seed_workspace(user_id, last_write_at=datetime.now(UTC) - timedelta(minutes=5))
    audit_store = await _decide_with_workspace(monkeypatch, user_id=user_id, workspace_store=store)
    assert _decided_drift(audit_store) is False


@pytest.mark.asyncio
async def test_workspace_drift_false_without_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unwired workspace store (e.g. NullWorkspaceLock deployment) → drift=False."""
    audit_store = await _decide_with_workspace(monkeypatch, user_id=uuid4(), workspace_store=None)
    assert _decided_drift(audit_store) is False


@pytest.mark.asyncio
async def test_workspace_drift_read_failure_never_blocks_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising drift read (post-CAS/pre-spawn) must NOT wedge the resume."""

    class _RaisingWorkspaceStore(InMemoryUserWorkspaceStore):
        async def get(self, *, tenant_id: UUID, user_id: UUID) -> UserWorkspace | None:
            raise RuntimeError("transient DB error")

    # apply_approval_decision must still succeed and record drift=False, or the
    # already-consumed decision would leave the run permanently un-spawned.
    audit_store = await _decide_with_workspace(
        monkeypatch, user_id=uuid4(), workspace_store=_RaisingWorkspaceStore()
    )
    assert _decided_drift(audit_store) is False


@pytest.mark.asyncio
async def test_workspace_drift_swallows_comparison_error() -> None:
    """A naive/aware datetime mismatch in the comparison → False, never raised.

    Guards the extraction regression: the `>` must stay inside the try/except so
    a status poll never 500s and a post-CAS resume never wedges.
    """

    class _NaiveStore:
        async def get(self, *, tenant_id: UUID, user_id: UUID) -> object:
            # tz-naive last_write_at vs an aware requested_at → TypeError on `>`.
            return SimpleNamespace(last_write_at=datetime(2026, 5, 22, 12, 0, 0))

    drift = await _workspace_drift(
        _NaiveStore(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        reason_kind="policy_gate",
        requested_at=datetime.now(UTC),
    )
    assert drift is False
