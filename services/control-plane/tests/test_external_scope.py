"""External-plane ownership gate — the shared 404 semantics behind every
``/v1/agents/{agent_code}/...`` endpoint a third-party API key can reach."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from control_plane.api._external import (
    EXTERNAL_SUBJECT_PREFIX,
    ExternalScopeError,
    external_error,
    external_subject_id,
    load_owned_run,
    load_owned_session,
    resolve_external_user_id,
)
from expert_work.persistence.tenant_user import InMemoryTenantUserStore
from expert_work.persistence.thread_meta import InMemoryThreadMetaStore
from expert_work.runtime.runs import DisconnectMode, InMemoryRunStore, RunInfo, RunStatus

_NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=UTC)


def _run(*, run_id: UUID, tenant_id: UUID, thread_id: UUID, user_id: UUID | None) -> RunInfo:
    return RunInfo(
        run_id=run_id,
        tenant_id=tenant_id,
        thread_id=thread_id,
        user_id=user_id,
        status=RunStatus.SUCCESS,
        on_disconnect=DisconnectMode.CANCEL,
        is_resume=False,
        error=None,
        created_at=_NOW,
        updated_at=_NOW,
        finished_at=_NOW,
        trace_id=None,
    )


def test_external_subject_id_namespaces_the_app_supplied_id() -> None:
    assert external_subject_id("cust-77") == "ext:cust-77"
    assert EXTERNAL_SUBJECT_PREFIX == "ext:"


def test_external_subject_id_cannot_collide_with_a_keycloak_uuid() -> None:
    # An employee's subject_id is a bare Keycloak sub (a UUID). A third party
    # passing that exact UUID must NOT resolve to the employee's row.
    employee_sub = str(uuid4())
    assert external_subject_id(employee_sub) != employee_sub


@pytest.mark.asyncio
async def test_resolve_external_user_id_is_stable_and_prefixed() -> None:
    users = InMemoryTenantUserStore()
    tenant_id = uuid4()
    first = await resolve_external_user_id(tenant_id=tenant_id, user_id="cust-77", users=users)
    again = await resolve_external_user_id(tenant_id=tenant_id, user_id="cust-77", users=users)
    assert first == again  # mint-on-use is idempotent

    stored = await users.get(first, tenant_id=tenant_id)
    assert stored is not None
    assert stored.subject_id == "ext:cust-77"
    assert stored.subject_type == "user"  # ops page + purge pipeline key on this


@pytest.mark.asyncio
async def test_external_user_never_resolves_to_an_employee_row() -> None:
    users = InMemoryTenantUserStore()
    tenant_id = uuid4()
    employee_sub = str(uuid4())
    employee = await users.resolve(
        tenant_id=tenant_id, subject_type="user", subject_id=employee_sub
    )
    impostor = await resolve_external_user_id(
        tenant_id=tenant_id, user_id=employee_sub, users=users
    )
    assert impostor != employee.id


@pytest.mark.asyncio
async def test_load_owned_session_returns_the_session_for_its_owner() -> None:
    users = InMemoryTenantUserStore()
    threads = InMemoryThreadMetaStore()
    tenant_id = uuid4()
    owner = await resolve_external_user_id(tenant_id=tenant_id, user_id="cust-77", users=users)
    session_id = uuid4()
    await threads.create(
        thread_id=session_id,
        tenant_id=tenant_id,
        created_by="sa",
        user_id=owner,
        agent_name="support-bot",
        agent_version="1.0.0",
    )
    meta = await load_owned_session(
        tenant_id=tenant_id,
        agent_code="support-bot",
        user_id="cust-77",
        session_id=session_id,
        threads=threads,
        users=users,
    )
    assert meta.thread_id == session_id


@pytest.mark.asyncio
async def test_load_owned_session_404s_for_another_user() -> None:
    users = InMemoryTenantUserStore()
    threads = InMemoryThreadMetaStore()
    tenant_id = uuid4()
    owner = await resolve_external_user_id(tenant_id=tenant_id, user_id="cust-77", users=users)
    session_id = uuid4()
    await threads.create(
        thread_id=session_id,
        tenant_id=tenant_id,
        created_by="sa",
        user_id=owner,
        agent_name="support-bot",
        agent_version="1.0.0",
    )
    with pytest.raises(ExternalScopeError) as caught:
        await load_owned_session(
            tenant_id=tenant_id,
            agent_code="support-bot",
            user_id="someone-else",
            session_id=session_id,
            threads=threads,
            users=users,
        )
    assert caught.value.status_code == 404
    assert caught.value.code == "SESSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_load_owned_session_404s_for_another_agent() -> None:
    users = InMemoryTenantUserStore()
    threads = InMemoryThreadMetaStore()
    tenant_id = uuid4()
    owner = await resolve_external_user_id(tenant_id=tenant_id, user_id="cust-77", users=users)
    session_id = uuid4()
    await threads.create(
        thread_id=session_id,
        tenant_id=tenant_id,
        created_by="sa",
        user_id=owner,
        agent_name="support-bot",
        agent_version="1.0.0",
    )
    with pytest.raises(ExternalScopeError) as caught:
        await load_owned_session(
            tenant_id=tenant_id,
            agent_code="other-bot",
            user_id="cust-77",
            session_id=session_id,
            threads=threads,
            users=users,
        )
    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_load_owned_run_returns_the_run_and_session_for_its_owner() -> None:
    users = InMemoryTenantUserStore()
    threads = InMemoryThreadMetaStore()
    runs = InMemoryRunStore()
    tenant_id = uuid4()
    owner = await resolve_external_user_id(tenant_id=tenant_id, user_id="cust-77", users=users)
    session_id = uuid4()
    await threads.create(
        thread_id=session_id,
        tenant_id=tenant_id,
        created_by="sa",
        user_id=owner,
        agent_name="support-bot",
        agent_version="1.0.0",
    )
    run_id = uuid4()
    await runs.create(_run(run_id=run_id, tenant_id=tenant_id, thread_id=session_id, user_id=owner))

    run, meta = await load_owned_run(
        tenant_id=tenant_id,
        agent_code="support-bot",
        user_id="cust-77",
        run_id=run_id,
        runs=runs,
        threads=threads,
        users=users,
    )
    assert run.run_id == run_id
    assert meta.thread_id == session_id


@pytest.mark.asyncio
async def test_load_owned_run_404s_as_run_not_found_for_another_user() -> None:
    # The session-ownership failure inside load_owned_run must be rewritten to
    # RUN_NOT_FOUND, never left as the session's own SESSION_NOT_FOUND code —
    # a third party asking about someone else's run must not learn "this run
    # exists but belongs to another user" from the error code.
    users = InMemoryTenantUserStore()
    threads = InMemoryThreadMetaStore()
    runs = InMemoryRunStore()
    tenant_id = uuid4()
    owner = await resolve_external_user_id(tenant_id=tenant_id, user_id="cust-77", users=users)
    session_id = uuid4()
    await threads.create(
        thread_id=session_id,
        tenant_id=tenant_id,
        created_by="sa",
        user_id=owner,
        agent_name="support-bot",
        agent_version="1.0.0",
    )
    run_id = uuid4()
    await runs.create(_run(run_id=run_id, tenant_id=tenant_id, thread_id=session_id, user_id=owner))

    with pytest.raises(ExternalScopeError) as caught:
        await load_owned_run(
            tenant_id=tenant_id,
            agent_code="support-bot",
            user_id="someone-else",
            run_id=run_id,
            runs=runs,
            threads=threads,
            users=users,
        )
    assert caught.value.code == "RUN_NOT_FOUND"
    assert caught.value.code != "SESSION_NOT_FOUND"
    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_load_owned_run_404s_for_unknown_run_id() -> None:
    users = InMemoryTenantUserStore()
    threads = InMemoryThreadMetaStore()
    runs = InMemoryRunStore()
    tenant_id = uuid4()

    with pytest.raises(ExternalScopeError) as caught:
        await load_owned_run(
            tenant_id=tenant_id,
            agent_code="support-bot",
            user_id="cust-77",
            run_id=uuid4(),
            runs=runs,
            threads=threads,
            users=users,
        )
    assert caught.value.code == "RUN_NOT_FOUND"
    assert caught.value.status_code == 404


def test_external_error_renders_the_standard_envelope() -> None:
    exc = ExternalScopeError("X", "msg", 404)
    resp = external_error(exc)
    assert resp.status_code == 404
    assert json.loads(resp.body) == {
        "success": False,
        "data": None,
        "error": {"code": "X", "message": "msg"},
    }
