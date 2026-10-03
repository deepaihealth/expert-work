"""Unit tests for the J.10 trigger scheduler — Mini-ADR J-26 / J-42."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from control_plane.audit import build_default_audit_logger
from control_plane.runtime import AgentRuntime
from control_plane.scheduler import TriggerScheduler, _is_cron_due, _next_fire, _next_occurrence
from control_plane.transcript import read_turns
from expert_work.persistence import (
    InMemoryApprovalStore,
    InMemoryThreadMetaStore,
    InMemoryTriggerRunStore,
    InMemoryTriggerStore,
)
from expert_work.persistence.agent_spec import InMemoryAgentSpecStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import (
    AgentSpec,
    AuditAction,
    AuditQuery,
    TriggerRecord,
    TriggerRunRecord,
    TriggerRunStatus,
)
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.runs import DisconnectMode, InMemoryRunStore, RunInfo, RunStatus
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator.agent_factory import build_agent
from tests.agent_fixtures import stub_agent_runtime

_BASE = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
_TENANT = uuid4()

_MANIFEST: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "reporter", "version": "1.0.0", "tenant": "platform-eng"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
        "system_prompt": {"template": "you report"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}

# PR3 D1 — delivery tests build a real graph (``build_agent``) instead of the
# stub runtime, so ``inject_delivery`` has an actual checkpoint to write into.
# Copied from ``test_agent_factory.py``'s ``_secret_store`` / ``_platform_resolver``
# (Stream Y-2: agent builds resolve every model's key through the platform
# resolver — the manifest-pinned ``api_key_ref`` is ignored).
_ANTHROPIC_KEY_NAME = "expert-work/dev/llm/anthropic"
_OPENAI_KEY_NAME = "expert-work/dev/llm/openai"
_KIMI_KEY_NAME = "expert-work/dev/llm/kimi"


def _secret_store() -> LocalDevSecretStore:
    return LocalDevSecretStore.from_mapping(
        {
            _ANTHROPIC_KEY_NAME: "sk-ant-test",
            _OPENAI_KEY_NAME: "sk-openai-test",
            _KIMI_KEY_NAME: "sk-kimi-test",
        }
    )


_PROVIDER_KEY_NAMES = {
    "anthropic": _ANTHROPIC_KEY_NAME,
    "openai": _OPENAI_KEY_NAME,
    "kimi": _KIMI_KEY_NAME,
    "self-hosted": _OPENAI_KEY_NAME,
    "azure": _OPENAI_KEY_NAME,
    "qwen": _OPENAI_KEY_NAME,
}


async def _platform_resolver(provider: str) -> list[str]:
    return [f"secret://{_PROVIDER_KEY_NAMES[provider]}"]


def _trigger(
    *,
    name: str = "nightly",
    expr: str = "0 9 * * *",
    last_fired_at: datetime | None = None,
    created_at: datetime = _BASE,
) -> TriggerRecord:
    return TriggerRecord(
        id=uuid4(),
        tenant_id=_TENANT,
        agent_name="reporter",
        agent_version="1.0.0",
        name=name,
        kind="cron",
        config={"expr": expr, "seed_input": "go"},
        enabled=True,
        source="api",
        last_fired_at=last_fired_at,
        created_at=created_at,
        updated_at=created_at,
    )


async def _build_scheduler(
    *,
    trigger_store: InMemoryTriggerStore,
    trigger_run_store: InMemoryTriggerRunStore,
    run_store: InMemoryRunStore | None = None,
    seed_agent: bool = True,
    audit_store: InMemoryAuditLogStore | None = None,
    share_run_store: bool = False,
) -> tuple[TriggerScheduler, AgentRuntime]:
    agents = InMemoryAgentSpecStore()
    if seed_agent:
        await agents.create(
            tenant_id=_TENANT,
            spec=AgentSpec.model_validate(_MANIFEST),
            spec_sha256="a" * 64,
            created_by="test",
        )
    run_store = run_store or InMemoryRunStore()
    # B-151 —— 生产里 runtime 的 run 库与 scheduler 的是同一张表;投递要靠它判断
    # 原会话忙不忙时让两边共用一份。
    runtime = stub_agent_runtime(run_store=run_store if share_run_store else None)
    store = audit_store or InMemoryAuditLogStore()
    scheduler = TriggerScheduler(
        trigger_store=trigger_store,
        trigger_run_store=trigger_run_store,
        run_store=run_store,
        agent_spec_store=agents,
        thread_store=InMemoryThreadMetaStore(),
        runtime=runtime,
        audit_logger=build_default_audit_logger(store),
        approval_store=InMemoryApprovalStore(),
        interval_s=60,
    )
    return scheduler, runtime


def _run_info(
    run_id: UUID,
    *,
    status: RunStatus,
    error: str | None = None,
    thread_id: UUID | None = None,
) -> RunInfo:
    return RunInfo(
        run_id=run_id,
        tenant_id=_TENANT,
        thread_id=thread_id or uuid4(),
        user_id=None,
        status=status,
        on_disconnect=DisconnectMode.CANCEL,
        is_resume=False,
        error=error,
        created_at=_BASE,
        updated_at=_BASE,
        finished_at=_BASE,
    )


def _fired_run(*, trigger_id: UUID, run_id: UUID, attempt: int = 1) -> TriggerRunRecord:
    return TriggerRunRecord(
        id=uuid4(),
        tenant_id=_TENANT,
        trigger_id=trigger_id,
        run_id=run_id,
        status=TriggerRunStatus.FIRED,
        attempt=attempt,
        triggered_at=_BASE,
    )


# --- cron math ------------------------------------------------------------


def test_next_fire_computes_following_slot() -> None:
    after = datetime(2026, 5, 22, 8, 0, 0, tzinfo=UTC)
    assert _next_fire("0 9 * * *", after) == datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)


def test_is_cron_due_true_when_slot_passed() -> None:
    trig = _trigger(expr="0 9 * * *", created_at=datetime(2026, 5, 22, 8, 0, tzinfo=UTC))
    assert _is_cron_due(trig, now=datetime(2026, 5, 22, 10, 0, tzinfo=UTC)) is True


def test_is_cron_due_false_before_slot() -> None:
    trig = _trigger(expr="0 9 * * *", created_at=datetime(2026, 5, 22, 8, 0, tzinfo=UTC))
    assert _is_cron_due(trig, now=datetime(2026, 5, 22, 8, 30, tzinfo=UTC)) is False


def test_is_cron_due_false_right_after_last_fire() -> None:
    """A daily trigger that just fired is not due again until tomorrow."""
    fired = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    trig = _trigger(expr="0 9 * * *", last_fired_at=fired)
    assert _is_cron_due(trig, now=fired + timedelta(minutes=30)) is False


# --- run_once -------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_once_fires_due_trigger() -> None:
    triggers = InMemoryTriggerStore()
    trigger_runs = InMemoryTriggerRunStore()
    # created_at far in the past + a daily slot → due now.
    trig = await triggers.create(
        _trigger(expr="0 9 * * *", created_at=datetime(2026, 5, 1, 9, 0, tzinfo=UTC))
    )
    scheduler, runtime = await _build_scheduler(
        trigger_store=triggers, trigger_run_store=trigger_runs
    )

    fired = await scheduler.run_once()
    assert fired == 1

    runs = await trigger_runs.list_by_trigger(trigger_id=trig.id, tenant_id=_TENANT)
    assert len(runs) == 1
    assert runs[0].status is TriggerRunStatus.FIRED
    assert runs[0].run_id is not None

    refreshed = await triggers.get(trigger_id=trig.id, tenant_id=_TENANT)
    assert refreshed is not None
    assert refreshed.last_fired_at is not None  # stamped by the fire

    # Drain the spawned run worker so the loop has no dangling task.
    record = runtime.run_manager.get(runs[0].run_id)
    assert record is not None and record.task is not None
    await record.task


@pytest.mark.asyncio
async def test_run_once_skips_not_due_trigger() -> None:
    triggers = InMemoryTriggerStore()
    trigger_runs = InMemoryTriggerRunStore()
    # Fired moments ago — a daily trigger is not due again.
    trig = await triggers.create(_trigger(expr="0 9 * * *", last_fired_at=datetime.now(UTC)))
    scheduler, _ = await _build_scheduler(trigger_store=triggers, trigger_run_store=trigger_runs)

    fired = await scheduler.run_once()
    assert fired == 0
    runs = await trigger_runs.list_by_trigger(trigger_id=trig.id, tenant_id=_TENANT)
    assert runs == []


@pytest.mark.asyncio
async def test_run_once_skips_when_agent_missing() -> None:
    """A due trigger whose agent is gone fires nothing — and does not crash."""
    triggers = InMemoryTriggerStore()
    trigger_runs = InMemoryTriggerRunStore()
    await triggers.create(
        _trigger(expr="0 9 * * *", created_at=datetime(2026, 5, 1, 9, 0, tzinfo=UTC))
    )
    scheduler, _ = await _build_scheduler(
        trigger_store=triggers, trigger_run_store=trigger_runs, seed_agent=False
    )

    fired = await scheduler.run_once()
    assert fired == 0


@pytest.mark.asyncio
async def test_run_once_survives_malformed_cron() -> None:
    """A bad cron expr fails its own trigger, not the whole sweep."""
    triggers = InMemoryTriggerStore()
    trigger_runs = InMemoryTriggerRunStore()
    await triggers.create(_trigger(name="bad", expr="not-a-cron"))
    scheduler, _ = await _build_scheduler(trigger_store=triggers, trigger_run_store=trigger_runs)

    fired = await scheduler.run_once()  # must not raise
    assert fired == 0


@pytest.mark.asyncio
async def test_start_stop_is_idempotent() -> None:
    scheduler, _ = await _build_scheduler(
        trigger_store=InMemoryTriggerStore(),
        trigger_run_store=InMemoryTriggerRunStore(),
    )
    assert scheduler.is_running is False
    scheduler.start()
    scheduler.start()  # idempotent
    assert scheduler.is_running is True
    await scheduler.stop()
    assert scheduler.is_running is False


# --- DLQ: reconcile + retry -----------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_marks_succeeded() -> None:
    trigger_runs = InMemoryTriggerRunStore()
    run_store = InMemoryRunStore()
    run_id, trigger_id = uuid4(), uuid4()
    await run_store.create(_run_info(run_id, status=RunStatus.SUCCESS))
    fired = await trigger_runs.create(_fired_run(trigger_id=trigger_id, run_id=run_id))
    scheduler, _ = await _build_scheduler(
        trigger_store=InMemoryTriggerStore(),
        trigger_run_store=trigger_runs,
        run_store=run_store,
    )

    await scheduler._reconcile_fired()

    row = await trigger_runs.get(trigger_run_id=fired.id, tenant_id=_TENANT)
    assert row is not None
    assert row.status is TriggerRunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_reconcile_failure_schedules_retry() -> None:
    trigger_runs = InMemoryTriggerRunStore()
    run_store = InMemoryRunStore()
    run_id, trigger_id = uuid4(), uuid4()
    await run_store.create(_run_info(run_id, status=RunStatus.ERROR, error="boom"))
    fired = await trigger_runs.create(_fired_run(trigger_id=trigger_id, run_id=run_id, attempt=1))
    scheduler, _ = await _build_scheduler(
        trigger_store=InMemoryTriggerStore(),
        trigger_run_store=trigger_runs,
        run_store=run_store,
    )

    await scheduler._reconcile_fired()

    row = await trigger_runs.get(trigger_run_id=fired.id, tenant_id=_TENANT)
    assert row is not None
    assert row.status is TriggerRunStatus.RETRYING
    assert row.next_retry_at is not None
    assert row.error == "boom"


@pytest.mark.asyncio
async def test_reconcile_exhausted_budget_dead_letters() -> None:
    trigger_runs = InMemoryTriggerRunStore()
    run_store = InMemoryRunStore()
    run_id, trigger_id = uuid4(), uuid4()
    await run_store.create(_run_info(run_id, status=RunStatus.ERROR))
    fired = await trigger_runs.create(_fired_run(trigger_id=trigger_id, run_id=run_id, attempt=5))
    scheduler, _ = await _build_scheduler(
        trigger_store=InMemoryTriggerStore(),
        trigger_run_store=trigger_runs,
        run_store=run_store,
    )

    await scheduler._reconcile_fired()

    row = await trigger_runs.get(trigger_run_id=fired.id, tenant_id=_TENANT)
    assert row is not None
    assert row.status is TriggerRunStatus.DEAD_LETTER


@pytest.mark.asyncio
async def test_reconcile_dead_letter_emits_trigger_failed() -> None:
    """FU3 — a dead-lettered firing (retry budget exhausted) emits exactly one
    TRIGGER_FAILED audit entry carrying the run id. Merges the DEAD_LETTER
    status coverage of ``test_reconcile_exhausted_budget_dead_letters`` above
    (which doesn't check audit) with the audit-entry shape asserted by
    ``test_reconcile_interrupted_emits_trigger_failed`` below (which goes
    through the INTERRUPTED path, not DLQ exhaustion)."""
    audit = InMemoryAuditLogStore()
    triggers = InMemoryTriggerStore()
    trigger_runs = InMemoryTriggerRunStore()
    run_store = InMemoryRunStore()
    trig = await triggers.create(_trigger(name="t"))
    run_id = uuid4()
    await trigger_runs.create(_fired_run(trigger_id=trig.id, run_id=run_id, attempt=5))
    await run_store.create(_run_info(run_id, status=RunStatus.ERROR, error="boom"))
    scheduler, _ = await _build_scheduler(
        trigger_store=triggers,
        trigger_run_store=trigger_runs,
        run_store=run_store,
        audit_store=audit,
    )

    await scheduler._reconcile_fired()

    rows = await trigger_runs.list_by_trigger(trigger_id=trig.id, tenant_id=_TENANT)
    assert rows[0].status is TriggerRunStatus.DEAD_LETTER
    page = await audit.query(AuditQuery(tenant_id=_TENANT, action=AuditAction.TRIGGER_FAILED))
    assert len(page.entries) == 1
    assert page.entries[0].details["run_id"] == str(run_id)


# --- PR3 D1 — reconcile delivers result + trigger lifecycle audit ---------


def _reuse_thread_trigger(*, originating_thread_id: UUID) -> TriggerRecord:
    return TriggerRecord(
        id=uuid4(),
        tenant_id=_TENANT,
        agent_name="reporter",
        agent_version="1.0.0",
        name="nightly",
        kind="cron",
        config={
            "rrule": "FREQ=DAILY;BYHOUR=3;BYMINUTE=0;BYSECOND=0",
            "timezone": "UTC",
            "seed_input": "go",
        },
        enabled=True,
        source="api",
        originating_thread_id=originating_thread_id,
        context_mode="reuse_thread",
        created_at=_BASE,
        updated_at=_BASE,
    )


@pytest.mark.asyncio
async def test_reconcile_delivers_result_to_originating_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reuse_thread trigger + SUCCESS run → the run's final assistant reply is
    appended to the originating conversation + TRIGGER_COMPLETED audited."""
    orig_thread, scratch_thread, run_id = uuid4(), uuid4(), uuid4()
    audit = InMemoryAuditLogStore()
    async with make_checkpointer("memory") as cp:
        built = await build_agent(
            AgentSpec.model_validate(_MANIFEST),
            secret_store=_secret_store(),
            checkpointer=cp,
            provider_key_resolver=_platform_resolver,  # required (Stream Y-2)
        )
        # seed originating conversation (has history) + the run's scratch thread (its result)
        await built.graph.aupdate_state(
            {"configurable": {"thread_id": str(orig_thread), "tenant_id": str(_TENANT)}},
            {"messages": [HumanMessage(content="make me a task"), AIMessage(content="scheduled")]},
            as_node="agent",
        )
        await built.graph.aupdate_state(
            {"configurable": {"thread_id": str(scratch_thread), "tenant_id": str(_TENANT)}},
            {"messages": [HumanMessage(content="go"), AIMessage(content="Today's AI news: X")]},
            as_node="agent",
        )
        triggers, trigger_runs, run_store = (
            InMemoryTriggerStore(),
            InMemoryTriggerRunStore(),
            InMemoryRunStore(),
        )
        trig = _reuse_thread_trigger(originating_thread_id=orig_thread)
        await triggers.create(trig)
        fired = await trigger_runs.create(_fired_run(trigger_id=trig.id, run_id=run_id))
        await run_store.create(
            _run_info(run_id, status=RunStatus.SUCCESS, thread_id=scratch_thread)
        )
        scheduler, runtime = await _build_scheduler(
            trigger_store=triggers,
            trigger_run_store=trigger_runs,
            run_store=run_store,
            audit_store=audit,  # new optional param — hold the handle for assertions
        )
        runtime.durable_checkpointer = cp

        async def _get_agent(**_kwargs: Any) -> Any:
            return built

        monkeypatch.setattr(runtime, "get_agent", _get_agent)

        await scheduler._reconcile_fired()

        turns = await read_turns(cp, orig_thread, include_hidden=False)
        assert turns[-1].role == "assistant" and turns[-1].content == "Today's AI news: X"
        row = await trigger_runs.get(trigger_run_id=fired.id, tenant_id=_TENANT)
        assert row is not None and row.status is TriggerRunStatus.SUCCEEDED
        page = await audit.query(
            AuditQuery(tenant_id=_TENANT, action=AuditAction.TRIGGER_COMPLETED)
        )
        assert page.entries and page.entries[0].details.get("delivery") == "delivered"


@pytest.mark.asyncio
async def test_reconcile_waits_for_a_busy_conversation_then_delivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-151 —— 原会话有一轮在跑:这一遍 reconcile 不写、行留在 FIRED、不记
    TRIGGER_COMPLETED;那一轮结束后的下一遍才投递并收尾。"""
    orig_thread, scratch_thread, run_id = uuid4(), uuid4(), uuid4()
    audit = InMemoryAuditLogStore()
    async with make_checkpointer("memory") as cp:
        built = await build_agent(
            AgentSpec.model_validate(_MANIFEST),
            secret_store=_secret_store(),
            checkpointer=cp,
            provider_key_resolver=_platform_resolver,
        )
        await built.graph.aupdate_state(
            {"configurable": {"thread_id": str(orig_thread), "tenant_id": str(_TENANT)}},
            {"messages": [HumanMessage(content="make me a task"), AIMessage(content="scheduled")]},
            as_node="agent",
        )
        await built.graph.aupdate_state(
            {"configurable": {"thread_id": str(scratch_thread), "tenant_id": str(_TENANT)}},
            {"messages": [HumanMessage(content="go"), AIMessage(content="Today's AI news: X")]},
            as_node="agent",
        )
        triggers, trigger_runs, run_store = (
            InMemoryTriggerStore(),
            InMemoryTriggerRunStore(),
            InMemoryRunStore(),
        )
        trig = _reuse_thread_trigger(originating_thread_id=orig_thread)
        await triggers.create(trig)
        fired = await trigger_runs.create(_fired_run(trigger_id=trig.id, run_id=run_id))
        await run_store.create(
            _run_info(run_id, status=RunStatus.SUCCESS, thread_id=scratch_thread)
        )
        busy_turn = _run_info(uuid4(), status=RunStatus.RUNNING, thread_id=orig_thread)
        await run_store.create(busy_turn)
        scheduler, runtime = await _build_scheduler(
            trigger_store=triggers,
            trigger_run_store=trigger_runs,
            run_store=run_store,
            audit_store=audit,
            share_run_store=True,
        )
        runtime.durable_checkpointer = cp

        async def _get_agent(**_kwargs: Any) -> Any:
            return built

        monkeypatch.setattr(runtime, "get_agent", _get_agent)

        await scheduler._reconcile_fired()

        turns = await read_turns(cp, orig_thread, include_hidden=False)
        assert [t.content for t in turns if t.role == "assistant"] == ["scheduled"]
        row = await trigger_runs.get(trigger_run_id=fired.id, tenant_id=_TENANT)
        assert row is not None and row.status is TriggerRunStatus.FIRED
        page = await audit.query(
            AuditQuery(tenant_id=_TENANT, action=AuditAction.TRIGGER_COMPLETED)
        )
        assert page.entries == []

        await run_store.set_status(
            run_id=busy_turn.run_id,
            tenant_id=_TENANT,
            status=RunStatus.SUCCESS,
            updated_at=_BASE,
            finished_at=_BASE,
        )
        await scheduler._reconcile_fired()

        turns = await read_turns(cp, orig_thread, include_hidden=False)
        assert turns[-1].role == "assistant" and turns[-1].content == "Today's AI news: X"
        row = await trigger_runs.get(trigger_run_id=fired.id, tenant_id=_TENANT)
        assert row is not None and row.status is TriggerRunStatus.SUCCEEDED
        page = await audit.query(
            AuditQuery(tenant_id=_TENANT, action=AuditAction.TRIGGER_COMPLETED)
        )
        assert [e.details.get("delivery") for e in page.entries] == ["delivered"]


@pytest.mark.asyncio
async def test_reconcile_fresh_thread_does_not_deliver() -> None:
    """Default context_mode (fresh_thread_per_run) → no delivery; still SUCCEEDED
    + TRIGGER_COMPLETED with delivery='skipped'. No graph/checkpointer needed —
    _deliver short-circuits on the context_mode check before touching either."""
    run_id = uuid4()
    audit = InMemoryAuditLogStore()
    triggers, trigger_runs, run_store = (
        InMemoryTriggerStore(),
        InMemoryTriggerRunStore(),
        InMemoryRunStore(),
    )
    trig = _trigger()  # default context_mode=fresh_thread_per_run, no originating_thread_id
    await triggers.create(trig)
    await trigger_runs.create(_fired_run(trigger_id=trig.id, run_id=run_id))
    await run_store.create(_run_info(run_id, status=RunStatus.SUCCESS))
    scheduler, _runtime = await _build_scheduler(
        trigger_store=triggers,
        trigger_run_store=trigger_runs,
        run_store=run_store,
        audit_store=audit,
    )
    await scheduler._reconcile_fired()
    page = await audit.query(AuditQuery(tenant_id=_TENANT, action=AuditAction.TRIGGER_COMPLETED))
    assert page.entries and page.entries[0].details.get("delivery") == "skipped"


@pytest.mark.asyncio
async def test_reconcile_interrupted_emits_trigger_failed() -> None:
    """A terminal (INTERRUPTED→FAILED) firing emits TRIGGER_FAILED."""
    run_id = uuid4()
    audit = InMemoryAuditLogStore()
    triggers, trigger_runs, run_store = (
        InMemoryTriggerStore(),
        InMemoryTriggerRunStore(),
        InMemoryRunStore(),
    )
    trig = _trigger()
    await triggers.create(trig)
    await trigger_runs.create(_fired_run(trigger_id=trig.id, run_id=run_id))
    await run_store.create(_run_info(run_id, status=RunStatus.INTERRUPTED))
    scheduler, _runtime = await _build_scheduler(
        trigger_store=triggers,
        trigger_run_store=trigger_runs,
        run_store=run_store,
        audit_store=audit,
    )
    await scheduler._reconcile_fired()
    page = await audit.query(AuditQuery(tenant_id=_TENANT, action=AuditAction.TRIGGER_FAILED))
    assert page.entries


@pytest.mark.asyncio
async def test_retry_re_fires_due_row() -> None:
    triggers = InMemoryTriggerStore()
    trigger_runs = InMemoryTriggerRunStore()
    # last_fired_at=now → pass-1 cron-fire won't also pick this up.
    trig = await triggers.create(_trigger(last_fired_at=datetime.now(UTC)))
    retrying = TriggerRunRecord(
        id=uuid4(),
        tenant_id=_TENANT,
        trigger_id=trig.id,
        run_id=uuid4(),
        status=TriggerRunStatus.RETRYING,
        attempt=1,
        next_retry_at=datetime.now(UTC) - timedelta(minutes=1),
        triggered_at=_BASE,
    )
    await trigger_runs.create(retrying)
    scheduler, runtime = await _build_scheduler(
        trigger_store=triggers, trigger_run_store=trigger_runs
    )

    fired = await scheduler._retry_due(datetime.now(UTC))
    assert fired == 1

    row = await trigger_runs.get(trigger_run_id=retrying.id, tenant_id=_TENANT)
    assert row is not None
    assert row.status is TriggerRunStatus.FIRED
    assert row.attempt == 2

    record = runtime.run_manager.get(row.run_id)
    assert record is not None and record.task is not None
    await record.task


@pytest.mark.asyncio
async def test_retry_skips_not_due_row() -> None:
    triggers = InMemoryTriggerStore()
    trigger_runs = InMemoryTriggerRunStore()
    trig = await triggers.create(_trigger(last_fired_at=datetime.now(UTC)))
    retrying = TriggerRunRecord(
        id=uuid4(),
        tenant_id=_TENANT,
        trigger_id=trig.id,
        run_id=uuid4(),
        status=TriggerRunStatus.RETRYING,
        attempt=1,
        next_retry_at=datetime.now(UTC) + timedelta(hours=1),
        triggered_at=_BASE,
    )
    await trigger_runs.create(retrying)
    scheduler, _ = await _build_scheduler(trigger_store=triggers, trigger_run_store=trigger_runs)

    fired = await scheduler._retry_due(datetime.now(UTC))
    assert fired == 0


# --- Stream 9.5 — two-instance exactly-once (CAS guards) ------------------


async def _drain(run_id: UUID, *runtimes: AgentRuntime) -> None:
    """Await the spawned run worker wherever it landed (winner's runtime)."""
    for rt in runtimes:
        record = rt.run_manager.get(run_id)
        if record is not None and record.task is not None:
            await record.task
            return


@pytest.mark.asyncio
async def test_two_instances_fire_due_cron_exactly_once() -> None:
    """Blue + green both scan the same due cron trigger; the claim_cron_fire CAS
    lets exactly one fire — no duplicate run / trigger_run."""
    triggers = InMemoryTriggerStore()
    trigger_runs = InMemoryTriggerRunStore()
    runs = InMemoryRunStore()
    trig = await triggers.create(
        _trigger(expr="0 9 * * *", created_at=datetime(2026, 5, 1, 9, 0, tzinfo=UTC))
    )
    blue, blue_rt = await _build_scheduler(
        trigger_store=triggers, trigger_run_store=trigger_runs, run_store=runs
    )
    green, green_rt = await _build_scheduler(
        trigger_store=triggers, trigger_run_store=trigger_runs, run_store=runs
    )

    counts = await asyncio.gather(blue.run_once(), green.run_once())
    assert sum(counts) == 1  # exactly one instance fired

    fired_rows = await trigger_runs.list_by_trigger(trigger_id=trig.id, tenant_id=_TENANT)
    assert len(fired_rows) == 1  # exactly one trigger_run — no double-fire
    await _drain(fired_rows[0].run_id, blue_rt, green_rt)


@pytest.mark.asyncio
async def test_two_instances_retry_exactly_once() -> None:
    """Two instances both sweep the same due retrying firing; the claim_retry CAS
    lets exactly one re-fire — attempt advances by one, not two."""
    triggers = InMemoryTriggerStore()
    trigger_runs = InMemoryTriggerRunStore()
    runs = InMemoryRunStore()
    trig = await triggers.create(_trigger(last_fired_at=datetime.now(UTC)))
    retrying = TriggerRunRecord(
        id=uuid4(),
        tenant_id=_TENANT,
        trigger_id=trig.id,
        run_id=uuid4(),
        status=TriggerRunStatus.RETRYING,
        attempt=1,
        next_retry_at=datetime.now(UTC) - timedelta(minutes=1),
        triggered_at=_BASE,
    )
    await trigger_runs.create(retrying)
    blue, blue_rt = await _build_scheduler(
        trigger_store=triggers, trigger_run_store=trigger_runs, run_store=runs
    )
    green, green_rt = await _build_scheduler(
        trigger_store=triggers, trigger_run_store=trigger_runs, run_store=runs
    )

    now = datetime.now(UTC)
    counts = await asyncio.gather(blue._retry_due(now), green._retry_due(now))
    assert sum(counts) == 1  # exactly one instance re-fired

    row = await trigger_runs.get(trigger_run_id=retrying.id, tenant_id=_TENANT)
    assert row is not None
    assert row.status is TriggerRunStatus.FIRED
    assert row.attempt == 2  # advanced once, not twice
    await _drain(row.run_id, blue_rt, green_rt)


# --- Task 2 (PR4 加固) — reconcile CAS-finalize + audit-gate ---------------
#
# ``_reconcile_one`` now routes its terminal-status transition through
# ``TriggerRunStore.claim_reconcile`` (Task 1's CAS) and only emits the
# lifecycle audit when the CAS is won. This closes the same class of race
# as the two tests above, but for the reconcile pass racing itself (a
# duplicate sweep) or the manual fire-now endpoint (Spec 1 PR4).


@pytest.mark.asyncio
async def test_reconcile_success_audits_exactly_once() -> None:
    """Two reconciles of the same FIRED row (duplicate sweep, or the sweep
    racing the fire-now endpoint) must emit exactly one TRIGGER_COMPLETED
    audit entry and leave the row SUCCEEDED — not double-audit."""
    audit = InMemoryAuditLogStore()
    trigger_runs = InMemoryTriggerRunStore()
    run_store = InMemoryRunStore()
    run_id, trigger_id = uuid4(), uuid4()
    await run_store.create(_run_info(run_id, status=RunStatus.SUCCESS))
    fired = await trigger_runs.create(_fired_run(trigger_id=trigger_id, run_id=run_id))
    scheduler, _ = await _build_scheduler(
        trigger_store=InMemoryTriggerStore(),
        trigger_run_store=trigger_runs,
        run_store=run_store,
        audit_store=audit,
    )

    now = datetime.now(UTC)
    await scheduler._reconcile_one(fired, now=now)
    await scheduler._reconcile_one(fired, now=now)  # duplicate sweep / endpoint race

    page = await audit.query(AuditQuery(tenant_id=_TENANT, action=AuditAction.TRIGGER_COMPLETED))
    assert len(page.entries) == 1
    row = await trigger_runs.get(trigger_run_id=fired.id, tenant_id=_TENANT)
    assert row is not None
    assert row.status is TriggerRunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_reconcile_failure_converges_no_double_transition() -> None:
    """The failure path's first reconcile wins the CAS and transitions
    FIRED→RETRYING; a second reconcile of the same (now-stale) row loses the
    CAS and must not re-transition or overwrite the winner's state."""
    trigger_runs = InMemoryTriggerRunStore()
    run_store = InMemoryRunStore()
    run_id, trigger_id = uuid4(), uuid4()
    await run_store.create(_run_info(run_id, status=RunStatus.ERROR, error="boom"))
    fired = await trigger_runs.create(_fired_run(trigger_id=trigger_id, run_id=run_id, attempt=1))
    scheduler, _ = await _build_scheduler(
        trigger_store=InMemoryTriggerStore(),
        trigger_run_store=trigger_runs,
        run_store=run_store,
    )

    now = datetime.now(UTC)
    await scheduler._reconcile_one(fired, now=now)  # first pass — RETRYING (attempt < max)
    got1 = await trigger_runs.get(trigger_run_id=fired.id, tenant_id=_TENANT)
    await scheduler._reconcile_one(fired, now=now)  # second pass — row no longer FIRED, CAS loses
    got2 = await trigger_runs.get(trigger_run_id=fired.id, tenant_id=_TENANT)

    assert got1 is not None
    assert got1.status is TriggerRunStatus.RETRYING
    assert got2 is not None
    assert got2.status is TriggerRunStatus.RETRYING  # not overwritten by the second pass


@pytest.mark.asyncio
async def test_reconcile_dead_letter_audits_exactly_once() -> None:
    """Two reconciles of the same FIRED row whose retry budget is already
    exhausted (attempt >= max → DEAD_LETTER) must emit exactly one
    TRIGGER_FAILED audit entry and leave the row DEAD_LETTER — not
    double-audit. This is the failure-path sibling of
    ``test_reconcile_success_audits_exactly_once`` above:
    ``test_reconcile_failure_converges_no_double_transition`` only exercises
    the RETRYING outcome, which never emits an audit, so it can't catch a
    regression to unconditional emit in the failed-run branch. DEAD_LETTER
    (like INTERRUPTED) does emit TRIGGER_FAILED, so it's the case that
    actually proves the audit-gate on a double-reconcile of a failed run."""
    audit = InMemoryAuditLogStore()
    trigger_runs = InMemoryTriggerRunStore()
    run_store = InMemoryRunStore()
    run_id, trigger_id = uuid4(), uuid4()
    await run_store.create(_run_info(run_id, status=RunStatus.ERROR, error="boom"))
    fired = await trigger_runs.create(_fired_run(trigger_id=trigger_id, run_id=run_id, attempt=5))
    scheduler, _ = await _build_scheduler(
        trigger_store=InMemoryTriggerStore(),
        trigger_run_store=trigger_runs,
        run_store=run_store,
        audit_store=audit,
    )

    now = datetime.now(UTC)
    await scheduler._reconcile_one(fired, now=now)
    await scheduler._reconcile_one(fired, now=now)  # duplicate sweep / endpoint race

    page = await audit.query(AuditQuery(tenant_id=_TENANT, action=AuditAction.TRIGGER_FAILED))
    assert len(page.entries) == 1
    row = await trigger_runs.get(trigger_run_id=fired.id, tenant_id=_TENANT)
    assert row is not None
    assert row.status is TriggerRunStatus.DEAD_LETTER


# --- PR1 地基(scheduled-tasks-conversational) — RRULE dual-path -----------
#
# ``_next_occurrence`` is the new source of truth (rrule-first, cron-
# fallback); ``_next_fire`` / ``_is_cron_due`` above stay in place
# unmodified — ``tools/eval/trigger.py`` (the J.10 eval harness) and the
# cron-math tests above import them directly, so deleting them would break
# a consumer outside this task's file list. A config-keyed trigger factory
# (distinct from ``_trigger`` above, which only builds the legacy
# ``{"expr": ...}`` shape) covers both the rrule and legacy-cron branches.


def _config_trigger(
    config: dict[str, object], *, created: datetime, last_fired: datetime | None = None
) -> TriggerRecord:
    return TriggerRecord(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_name="a",
        agent_version="1.0.0",
        name="t",
        kind="cron",
        config=config,
        created_at=created,
        updated_at=created,
        last_fired_at=last_fired,
    )


def test_rrule_daily_next_occurrence() -> None:
    created = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
    trig = _config_trigger(
        {"rrule": "FREQ=DAILY;BYHOUR=3;BYMINUTE=0", "timezone": "UTC"}, created=created
    )
    nxt = _next_occurrence(trig, after=created)
    assert nxt == datetime(2026, 5, 2, 3, 0, tzinfo=UTC)


def test_rrule_timezone_shifts_utc_instant() -> None:
    created = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    # Shanghai 03:00 local = UTC 19:00 the previous day.
    trig = _config_trigger(
        {"rrule": "FREQ=DAILY;BYHOUR=3;BYMINUTE=0", "timezone": "Asia/Shanghai"},
        created=created,
    )
    nxt = _next_occurrence(trig, after=created)
    assert nxt is not None
    assert nxt.astimezone(UTC).hour == 19


def test_rrule_bounded_count_exhausts_to_none() -> None:
    created = datetime(2026, 5, 1, 2, 0, tzinfo=UTC)
    trig = _config_trigger(
        {"rrule": "FREQ=DAILY;BYHOUR=3;BYMINUTE=0;COUNT=1", "timezone": "UTC"},
        created=created,
        last_fired=datetime(2026, 5, 1, 3, 0, tzinfo=UTC),  # the one occurrence already fired
    )
    assert _next_occurrence(trig, after=datetime(2026, 5, 1, 3, 0, tzinfo=UTC)) is None


def test_legacy_cron_still_works() -> None:
    created = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
    trig = _config_trigger({"expr": "0 3 * * *"}, created=created)
    nxt = _next_occurrence(trig, after=created)
    assert nxt == datetime(2026, 5, 2, 3, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_fire_due_cron_disables_exhausted_rrule() -> None:
    """A bounded RRULE (``COUNT=1``) whose one occurrence already fired is
    exhausted — the next sweep disables the trigger instead of scanning it
    forever. Reuses this file's existing ``_build_scheduler`` harness
    (in-memory stores + stub runtime) per the brief's fallback: no
    ``scheduler_harness`` fixture exists in this file to reuse instead, and
    the exhausted trigger never reaches ``_fire_cron`` so no agent needs
    seeding."""
    triggers = InMemoryTriggerStore()
    trigger_runs = InMemoryTriggerRunStore()
    trig = await triggers.create(
        _config_trigger(
            {"rrule": "FREQ=DAILY;BYHOUR=3;BYMINUTE=0;COUNT=1", "timezone": "UTC"},
            created=datetime(2026, 5, 1, 2, 0, tzinfo=UTC),
            last_fired=datetime(2026, 5, 1, 3, 0, tzinfo=UTC),
        )
    )
    scheduler, _ = await _build_scheduler(trigger_store=triggers, trigger_run_store=trigger_runs)

    fired = await scheduler._fire_due_cron(datetime(2026, 5, 2, 3, 0, tzinfo=UTC))

    assert fired == 0
    got = await triggers.get(trigger_id=trig.id, tenant_id=trig.tenant_id)
    assert got is not None
    assert got.enabled is False
