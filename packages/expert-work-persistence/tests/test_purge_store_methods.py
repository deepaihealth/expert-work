"""Phase 3a (purge_user) — per-store bulk purge methods.

Each store gained a ``delete_all_for_user`` (hard-delete set) or
``anonymize_all_for_user`` (billing / tenant-asset set) + ``TenantUserStore``
gained ``deactivate``. These verify the two invariants that make the cascade
purge safe:

* **only the target** ``(tenant_id, user_id)`` rows are touched — another
  user's and another tenant's rows are always left intact;
* **anonymize keeps the row** (nulls the user link) while **delete removes it**.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import (
    InMemoryApprovalStore,
    InMemoryArtifactStore,
    InMemoryCurationCandidateStore,
    InMemoryEvalDatasetStore,
    InMemoryMcpOAuthConnectionStore,
    InMemoryMemoryStore,
    InMemoryTenantUserStore,
    InMemoryTriggerRunStore,
    InMemoryTriggerStore,
)
from expert_work.persistence.agent_instance.memory import InMemoryAgentInstanceStore
from expert_work.persistence.image_upload import InMemoryImageUploadStore
from expert_work.persistence.skill import InMemorySkillStore
from expert_work.persistence.token_usage_store import (
    InMemoryTokenUsageStore,
    TokenUsageRecord,
)
from expert_work.protocol import (
    ApprovalRecord,
    ApprovalStatus,
    CurationCandidateRecord,
    EvalDatasetRecord,
    MemoryItem,
    TriggerRecord,
    TriggerRunRecord,
)
from expert_work.runtime.runs import DisconnectMode, InMemoryRunStore, RunInfo, RunStatus

_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# tenant_user.deactivate + list_by_tenant exclusion
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tenant_user_deactivate_excludes_from_list_and_is_idempotent() -> None:
    store = InMemoryTenantUserStore()
    tenant, other_tenant = uuid4(), uuid4()
    a = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="a")
    b = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="b")
    c = await store.resolve(tenant_id=other_tenant, subject_type="user", subject_id="c")

    assert await store.deactivate(a.id, tenant_id=tenant, now=_NOW) is True
    # Idempotent re-deactivate still True (row exists), cross-tenant is False.
    assert await store.deactivate(a.id, tenant_id=tenant, now=_NOW) is True
    assert await store.deactivate(a.id, tenant_id=other_tenant, now=_NOW) is False
    assert await store.deactivate(uuid4(), tenant_id=tenant, now=_NOW) is False

    listed = {u.id for u in await store.list_by_tenant(tenant, subject_type="user")}
    assert a.id not in listed  # purged user gone from the roster
    assert b.id in listed  # sibling untouched
    # get still returns the (soft-deactivated) row with deleted_at set —
    # so a re-purge stays idempotent.
    got = await store.get(a.id, tenant_id=tenant)
    assert got is not None and got.deleted_at is not None
    # Other tenant untouched.
    assert {u.id for u in await store.list_by_tenant(other_tenant, subject_type="user")} == {c.id}


@pytest.mark.asyncio
async def test_tenant_user_resolve_reactivates_a_purged_identity() -> None:
    store = InMemoryTenantUserStore()
    tenant = uuid4()
    u = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="x")
    assert await store.deactivate(u.id, tenant_id=tenant, now=_NOW) is True
    assert await store.list_by_tenant(tenant, subject_type="user") == []  # hidden

    # A returning identity re-resolves to the SAME row and reactivates cleanly:
    # deleted_at cleared, back in the roster — never invisible-but-producing-data.
    again = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="x")
    assert again.id == u.id
    assert again.deleted_at is None
    assert {r.id for r in await store.list_by_tenant(tenant, subject_type="user")} == {u.id}


# --------------------------------------------------------------------------- #
# ANONYMIZE — keep the row, null the user link
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_token_usage_anonymize_keeps_row_nulls_user_only_for_target() -> None:
    store = InMemoryTokenUsageStore()
    tenant, other_tenant = uuid4(), uuid4()
    a, b = uuid4(), uuid4()

    def _rec(*, tid: object, uid: object) -> TokenUsageRecord:
        return TokenUsageRecord(
            tenant_id=tid,  # type: ignore[arg-type]
            agent_name="alpha",
            agent_version="1.0.0",
            model="m1",
            user_id=uid,  # type: ignore[arg-type]
            input_tokens=10,
            output_tokens=5,
        )

    await store.insert(_rec(tid=tenant, uid=a))
    await store.insert(_rec(tid=tenant, uid=a))
    await store.insert(_rec(tid=tenant, uid=b))
    await store.insert(_rec(tid=other_tenant, uid=a))  # same user id, other tenant

    n = await store.anonymize_all_for_user(tenant_id=tenant, user_id=a)
    assert n == 2

    rows = list(await store.list_for_tenant(tenant_id=tenant, limit=100))
    assert len(rows) == 3  # rows KEPT, not deleted
    assert sum(1 for r in rows if r.user_id is None) == 2  # a's rows anonymized
    assert sum(1 for r in rows if r.user_id == b) == 1  # b untouched
    # Other tenant's same-user-id row untouched.
    other = list(await store.list_for_tenant(tenant_id=other_tenant, limit=100))
    assert [r.user_id for r in other] == [a]
    # Idempotent re-run.
    assert await store.anonymize_all_for_user(tenant_id=tenant, user_id=a) == 0


@pytest.mark.asyncio
async def test_agent_run_anonymize_keeps_row_nulls_user() -> None:
    store = InMemoryRunStore()
    tenant = uuid4()
    a, b = uuid4(), uuid4()
    t1, t2 = uuid4(), uuid4()

    def _run(*, uid: object, thread: object) -> RunInfo:
        return RunInfo(
            run_id=uuid4(),
            tenant_id=tenant,
            thread_id=thread,  # type: ignore[arg-type]
            user_id=uid,  # type: ignore[arg-type]
            status=RunStatus.SUCCESS,
            on_disconnect=DisconnectMode.CANCEL,
            is_resume=False,
            error=None,
            created_at=_NOW,
            updated_at=_NOW,
            finished_at=_NOW,
            trace_id=None,
        )

    await store.create(_run(uid=a, thread=t1))
    await store.create(_run(uid=b, thread=t2))

    assert await store.anonymize_all_for_user(tenant_id=tenant, user_id=a) == 1
    rows = await store.list_for_tenant(tenant_id=tenant, limit=100)
    # Both rows KEPT — anonymize only nulls the user link. Row removal is
    # ``delete_by_thread``'s job (which now also empties the ``run_event``
    # children — deletion-hygiene PR3 §A), never this method's.
    assert len(rows) == 2
    assert sorted((r.user_id is None) for r in rows) == [False, True]
    assert await store.anonymize_all_for_user(tenant_id=tenant, user_id=a) == 0


@pytest.mark.asyncio
async def test_skill_anonymize_nulls_all_five_actor_columns() -> None:
    store = InMemorySkillStore()
    tenant, other_tenant = uuid4(), uuid4()
    a, b = uuid4(), uuid4()

    s_a = uuid4()
    await store.create_skill(skill_id=s_a, tenant_id=tenant, name="s-a", created_by_user_id=a)
    s_b = uuid4()
    await store.create_skill(skill_id=s_b, tenant_id=tenant, name="s-b", created_by_user_id=b)
    # A kill-switch engaged by A (tenant scope).
    await store.set_kill_switch(
        switch_id=uuid4(), scope="tenant", tenant_id=tenant, engaged=True, actor_user_id=a
    )

    touched = await store.anonymize_all_for_user(tenant_id=tenant, user_id=a)
    assert touched >= 2  # skill s-a + the kill-switch

    kept_a = await store.get_skill(skill_id=s_a, tenant_id=tenant)
    assert kept_a is not None and kept_a.created_by_user_id is None  # KEPT, actor nulled
    kept_b = await store.get_skill(skill_id=s_b, tenant_id=tenant)
    assert kept_b is not None and kept_b.created_by_user_id == b  # sibling untouched
    sw = await store.get_kill_switch(scope="tenant", tenant_id=tenant)
    assert sw is not None and sw.engaged_by_user_id is None
    assert await store.anonymize_all_for_user(tenant_id=tenant, user_id=a) == 0

    # A skill in another tenant created by the SAME user id is NOT touched.
    s_other = uuid4()
    await store.create_skill(
        skill_id=s_other, tenant_id=other_tenant, name="s-o", created_by_user_id=a
    )
    assert await store.anonymize_all_for_user(tenant_id=tenant, user_id=a) == 0
    kept_other = await store.get_skill(skill_id=s_other, tenant_id=other_tenant)
    assert kept_other is not None and kept_other.created_by_user_id == a


@pytest.mark.asyncio
async def test_eval_and_curation_anonymize_keep_rows() -> None:
    evals = InMemoryEvalDatasetStore()
    cands = InMemoryCurationCandidateStore()
    tenant = uuid4()
    a, b = uuid4(), uuid4()

    ds_a = EvalDatasetRecord(
        id=uuid4(),
        tenant_id=tenant,
        agent_name="r",
        name="n",
        source="trajectory",
        source_user_id=a,
        created_at=_NOW,
        updated_at=_NOW,
    )
    ds_b = EvalDatasetRecord(
        id=uuid4(),
        tenant_id=tenant,
        agent_name="r",
        name="n",
        source="trajectory",
        source_user_id=b,
        created_at=_NOW,
        updated_at=_NOW,
    )
    await evals.create(ds_a)
    await evals.create(ds_b)
    assert await evals.anonymize_all_for_user(tenant_id=tenant, user_id=a) == 1
    got_a = await evals.get(dataset_id=ds_a.id, tenant_id=tenant)
    assert got_a is not None and got_a.source_user_id is None  # KEPT, nulled
    got_b = await evals.get(dataset_id=ds_b.id, tenant_id=tenant)
    assert got_b is not None and got_b.source_user_id == b

    cand = CurationCandidateRecord(
        id=uuid4(),
        tenant_id=tenant,
        agent_name="r",
        thread_id=uuid4(),
        user_id=a,
        trajectory_key="k",
        outcome="success",
        signal="implicit_success",
        detected_at=_NOW,
    )
    await cands.upsert(cand)
    assert await cands.anonymize_all_for_user(tenant_id=tenant, user_id=a) == 1
    got = await cands.get(candidate_id=cand.id, tenant_id=tenant)
    assert got is not None and got.user_id is None  # KEPT, nulled


# --------------------------------------------------------------------------- #
# HARD-DELETE — remove only the target user's rows
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_memory_delete_all_for_user_soft_deletes_only_target() -> None:
    store = InMemoryMemoryStore()
    tenant, other_tenant = uuid4(), uuid4()
    a, b = uuid4(), uuid4()

    def _item(*, tid: UUID, uid: UUID, content: str) -> MemoryItem:
        return MemoryItem(
            id=uuid4(),
            tenant_id=tid,
            user_id=uid,
            kind="fact",
            content=content,
            embedding=(1.0, 0.0),
        )

    await store.write(
        [_item(tid=tenant, uid=a, content="a1"), _item(tid=tenant, uid=a, content="a2")]
    )
    await store.write([_item(tid=tenant, uid=b, content="b1")])
    await store.write([_item(tid=other_tenant, uid=a, content="ot")])  # same user, other tenant

    assert await store.delete_all_for_user(tenant_id=tenant, user_id=a) == 2
    assert await store.list_for_user(tenant_id=tenant, user_id=a) == []  # a's gone
    assert len(await store.list_for_user(tenant_id=tenant, user_id=b)) == 1  # b untouched
    assert len(await store.list_for_user(tenant_id=other_tenant, user_id=a)) == 1  # other tenant
    assert await store.delete_all_for_user(tenant_id=tenant, user_id=a) == 0  # idempotent


@pytest.mark.asyncio
async def test_artifact_delete_all_for_user_removes_only_target() -> None:
    store = InMemoryArtifactStore()
    tenant = uuid4()
    a, b = uuid4(), uuid4()
    await store.save_version(
        tenant_id=tenant,
        user_id=a,
        name="doc",
        kind="document",
        path_in_workspace="/w/doc",
        created_in_thread="t",
    )
    await store.save_version(
        tenant_id=tenant,
        user_id=b,
        name="doc",
        kind="document",
        path_in_workspace="/w/doc",
        created_in_thread="t",
    )
    assert await store.delete_all_for_user(tenant_id=tenant, user_id=a) == 1
    assert await store.list_for_user(tenant_id=tenant, user_id=a, include_deleted=True) == []
    assert len(await store.list_for_user(tenant_id=tenant, user_id=b)) == 1
    assert await store.delete_all_for_user(tenant_id=tenant, user_id=a) == 0


@pytest.mark.asyncio
async def test_agent_instance_delete_all_for_user_removes_only_target() -> None:
    store = InMemoryAgentInstanceStore()
    tenant, other_tenant = uuid4(), uuid4()
    a, b = uuid4(), uuid4()
    await store.touch(tenant_id=tenant, agent_code="x", user_id=a)
    await store.touch(tenant_id=tenant, agent_code="y", user_id=a)
    await store.touch(tenant_id=tenant, agent_code="x", user_id=b)
    await store.touch(tenant_id=other_tenant, agent_code="x", user_id=a)

    assert await store.delete_all_for_user(tenant_id=tenant, user_id=a) == 2
    assert await store.list_by_user(tenant_id=tenant, user_id=a) == []
    assert len(await store.list_by_user(tenant_id=tenant, user_id=b)) == 1
    assert len(await store.list_by_user(tenant_id=other_tenant, user_id=a)) == 1
    assert await store.delete_all_for_user(tenant_id=tenant, user_id=a) == 0


@pytest.mark.asyncio
async def test_image_upload_delete_all_for_user_removes_only_target() -> None:
    store = InMemoryImageUploadStore()
    tenant = uuid4()
    a, b = uuid4(), uuid4()
    thread = uuid4()
    ia = await store.insert(
        image_id=uuid4(),
        tenant_id=tenant,
        thread_id=thread,
        user_id=a,
        object_key="k/a",
        size_bytes=1,
        mime_type="image/png",
        sha256="h",
    )
    await store.insert(
        image_id=uuid4(),
        tenant_id=tenant,
        thread_id=thread,
        user_id=b,
        object_key="k/b",
        size_bytes=1,
        mime_type="image/png",
        sha256="h",
    )
    assert await store.delete_all_for_user(tenant_id=tenant, user_id=a) == 1
    assert await store.get(image_id=ia.id, tenant_id=tenant) is None
    assert await store.delete_all_for_user(tenant_id=tenant, user_id=a) == 0


@pytest.mark.asyncio
async def test_approval_delete_all_for_user_removes_only_target() -> None:
    store = InMemoryApprovalStore()
    tenant = uuid4()
    a, b = uuid4(), uuid4()

    def _appr(*, uid: UUID) -> ApprovalRecord:
        return ApprovalRecord(
            id=uuid4(),
            tenant_id=tenant,
            user_id=uid,
            run_id=uuid4(),
            thread_id=uuid4(),
            request_id="r",
            node="n",
            reason_kind="risk_confirmation",
            action_summary="s",
            proposed_args={},
            requested_at=_NOW,
            timeout_at=_NOW,
        )

    await store.create(_appr(uid=a))
    await store.create(_appr(uid=b))
    assert await store.delete_all_for_user(tenant_id=tenant, user_id=a) == 1
    _, total_pending = await store.list_for_tenant(
        tenant_id=tenant, status=ApprovalStatus.PENDING, limit=100
    )
    assert total_pending == 1  # only b's remains
    assert await store.delete_all_for_user(tenant_id=tenant, user_id=a) == 0


@pytest.mark.asyncio
async def test_mcp_oauth_delete_all_for_user_keyed_by_string_subject() -> None:
    store = InMemoryMcpOAuthConnectionStore()
    tenant = uuid4()
    catalog = uuid4()
    # user_id here is the STRING subject_id (not the surrogate UUID).
    await store.create(
        tenant_id=tenant,
        user_id="subj-a",
        catalog_id=catalog,
        name="c1",
        resolved_url="https://x",
    )
    await store.create(
        tenant_id=tenant,
        user_id="subj-b",
        catalog_id=catalog,
        name="c2",
        resolved_url="https://x",
    )
    assert await store.delete_all_for_user(tenant_id=tenant, user_id="subj-a") == 1
    assert await store.list_for_user(tenant_id=tenant, user_id="subj-a") == []
    assert len(await store.list_for_user(tenant_id=tenant, user_id="subj-b")) == 1
    assert await store.delete_all_for_user(tenant_id=tenant, user_id="subj-a") == 0


@pytest.mark.asyncio
async def test_trigger_delete_all_for_user_and_child_runs() -> None:
    triggers = InMemoryTriggerStore()
    runs = InMemoryTriggerRunStore()
    tenant = uuid4()
    a, b = uuid4(), uuid4()

    tr_a = TriggerRecord(
        id=uuid4(),
        tenant_id=tenant,
        user_id=a,
        agent_name="ag",
        agent_version="1",
        name="ta",
        kind="cron",
        created_at=_NOW,
        updated_at=_NOW,
    )
    tr_b = TriggerRecord(
        id=uuid4(),
        tenant_id=tenant,
        user_id=b,
        agent_name="ag",
        agent_version="1",
        name="tb",
        kind="cron",
        created_at=_NOW,
        updated_at=_NOW,
    )
    await triggers.create(tr_a)
    await triggers.create(tr_b)
    await runs.create(
        TriggerRunRecord(id=uuid4(), tenant_id=tenant, trigger_id=tr_a.id, triggered_at=_NOW)
    )
    await runs.create(
        TriggerRunRecord(id=uuid4(), tenant_id=tenant, trigger_id=tr_b.id, triggered_at=_NOW)
    )

    deleted_ids = await triggers.delete_all_for_user(tenant_id=tenant, user_id=a)
    assert deleted_ids == [tr_a.id]
    assert await runs.delete_for_triggers(trigger_ids=deleted_ids, tenant_id=tenant) == 1
    # b's trigger + its run survive.
    assert len(await runs.list_by_trigger(trigger_id=tr_b.id, tenant_id=tenant)) == 1
    # Call kept out of the assert — an assert with a side effect vanishes under
    # `python -O` (CodeQL py/side-effect-in-assert).
    b_trigger_deleted = await triggers.delete(trigger_id=tr_b.id, tenant_id=tenant)
    assert b_trigger_deleted is True
    # Idempotent — nothing left for a.
    assert await triggers.delete_all_for_user(tenant_id=tenant, user_id=a) == []
