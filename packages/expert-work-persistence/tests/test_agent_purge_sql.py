"""B-278 —— the per-store count / delete methods the agent purge runs, on real Postgres.

Every method is keyed by the agent (name / ``agent_key``) or by its
conversations, and isolated by ``WHERE tenant_id`` alone (the runtime role
bypasses RLS). Each test seeds the target, a sibling that must survive (another
agent / a shared row), and the same name in another tenant, then checks the
count equals what the delete removes and only the target goes.
"""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlAgentDisableStore,
    SqlArtifactStore,
    SqlCurationCandidateStore,
    SqlEvalDatasetStore,
    SqlMemoryStore,
    SqlQualityDriftAlertStore,
    SqlQualityScoreStore,
    SqlTenantConfigStore,
    SqlUserUploadStore,
    SqlWebhookDeliveryStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.agent_instance.sql import SqlAgentInstanceStore
from expert_work.persistence.agent_spec import (
    AgentSpecPurgeBlockedError,
    DuplicateAgentSpecError,
    SqlAgentSpecStore,
)
from expert_work.persistence.approval.sql import SqlApprovalStore
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.persistence.feedback_store import DbFeedbackStore, FeedbackRecord
from expert_work.persistence.image_upload.sql import SqlImageUploadStore
from expert_work.persistence.knowledge import RetrievalLogEntry
from expert_work.persistence.knowledge.retrieval_log import SqlKnowledgeRetrievalLogStore
from expert_work.persistence.memory.dlq import SqlMemoryWritebackDLQ
from expert_work.persistence.memory.writeback_job import SqlMemoryWritebackJobStore
from expert_work.persistence.models import SecretAllowlistRow
from expert_work.persistence.secret_allowlist import SqlAgentSecretAllowlistStore
from expert_work.protocol import (
    AgentSpec,
    AgentSpecStatus,
    ApprovalRecord,
    CandidateStatus,
    CurationCandidateRecord,
    EvalDatasetRecord,
    MemoryItem,
    QualityDriftAlertRecord,
    QualityScoreRecord,
    TenantConfigPatch,
    WebhookDeliveryRecord,
)

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)
_X = "doomed-agent"
_Y = "other-agent"

_BASE_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": _X, "version": "1.0.0", "tenant": "t"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
        "system_prompt": {"template": "you are a reviewer"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}

SF = async_sessionmaker[AsyncSession]


def _spec(name: str = _X, version: str = "1.0.0", prompt: str = "you are a reviewer") -> AgentSpec:
    doc = deepcopy(_BASE_SPEC)
    doc["metadata"]["name"] = name
    doc["metadata"]["version"] = version
    doc["spec"]["system_prompt"]["template"] = prompt
    return AgentSpec.model_validate(doc)


def _dsn(container: PostgresContainer, driver: str) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", f"+{driver}").replace(
        "postgresql://", f"postgresql+{driver}://", 1
    )


@pytest.fixture
def sf(postgres_container: PostgresContainer) -> Iterator[SF]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _dsn(postgres_container, "psycopg"))
    command.upgrade(cfg, "head")
    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=_dsn(postgres_container, "asyncpg"))
    )
    yield create_async_session_factory(engine)


# --------------------------------------------------------------------------- agent_spec


@pytest.mark.asyncio
async def test_purge_name_drops_versions_and_revisions_so_the_name_can_be_recreated(
    sf: SF,
) -> None:
    store = SqlAgentSpecStore(sf)
    t1, t2 = uuid4(), uuid4()
    for version in ("1.0.0", "2.0.0"):
        await store.create(
            tenant_id=t1, spec=_spec(version=version), spec_sha256="a" * 64, created_by="u"
        )
    await store.update_spec(
        tenant_id=t1,
        name=_X,
        version="1.0.0",
        spec=_spec(prompt="edited"),
        spec_sha256="b" * 64,
        updated_by="u",
    )
    await store.create(tenant_id=t1, spec=_spec(name=_Y), spec_sha256="a" * 64, created_by="u")
    await store.create(tenant_id=t2, spec=_spec(), spec_sha256="a" * 64, created_by="u")
    # The same name, also deleted, in another tenant: only the WHERE tenant_id keeps it.
    await store.update_status(
        tenant_id=t2, name=_X, version="1.0.0", status=AgentSpecStatus.DELETED
    )
    assert await store.count_revisions_for_name(tenant_id=t1, name=_X) == 3

    await store.update_status(
        tenant_id=t1, name=_X, version="1.0.0", status=AgentSpecStatus.DELETED
    )
    with pytest.raises(AgentSpecPurgeBlockedError) as blocked:
        await store.purge_name(tenant_id=t1, name=_X)
    assert blocked.value.active_versions == ["2.0.0"]
    assert await store.get(tenant_id=t1, name=_X, version="1.0.0", include_deleted=True) is not None

    await store.update_status(
        tenant_id=t1, name=_X, version="2.0.0", status=AgentSpecStatus.DELETED
    )
    assert await store.purge_name(tenant_id=t1, name=_X) == (2, 3)
    assert await store.purge_name(tenant_id=t1, name=_X) == (0, 0)  # re-run
    assert await store.count_revisions_for_name(tenant_id=t1, name=_X) == 0

    # The point of B-278: the same name and version can be created again.
    again = await store.create(tenant_id=t1, spec=_spec(), spec_sha256="c" * 64, created_by="u")
    assert again.status is AgentSpecStatus.ACTIVE
    with pytest.raises(DuplicateAgentSpecError):
        await store.create(tenant_id=t1, spec=_spec(), spec_sha256="c" * 64, created_by="u")
    # Another agent and the same name in another tenant are untouched.
    assert await store.get(tenant_id=t1, name=_Y, version="1.0.0") is not None
    assert await store.get(tenant_id=t2, name=_X, version="1.0.0", include_deleted=True) is not None
    assert await store.count_revisions_for_name(tenant_id=t2, name=_X) == 1


@pytest.mark.asyncio
async def test_restore_only_flips_a_deleted_row(sf: SF) -> None:
    store = SqlAgentSpecStore(sf)
    t1 = uuid4()
    await store.create(tenant_id=t1, spec=_spec(), spec_sha256="a" * 64, created_by="u")
    assert await store.restore(tenant_id=t1, name=_X, version="1.0.0") is None  # not deleted
    await store.update_status(
        tenant_id=t1, name=_X, version="1.0.0", status=AgentSpecStatus.DELETED
    )
    assert await store.restore(tenant_id=uuid4(), name=_X, version="1.0.0") is None  # cross-tenant
    restored = await store.restore(tenant_id=t1, name=_X, version="1.0.0")
    assert restored is not None and restored.status is AgentSpecStatus.ACTIVE


# --------------------------------------------------------------------------- name-keyed rows


@pytest.mark.asyncio
async def test_agent_disable_delete_is_scoped(sf: SF) -> None:
    store = SqlAgentDisableStore(sf)
    t1, t2 = uuid4(), uuid4()
    for tenant, name in ((t1, _X), (t1, _Y), (t2, _X)):
        await store.set_disabled(
            tenant_id=tenant, agent_name=name, disabled=True, reason="r", disabled_by="u"
        )
    assert await store.delete(tenant_id=t1, agent_name=_X) is True
    assert await store.delete(tenant_id=t1, agent_name=_X) is False
    assert await store.get(tenant_id=t1, agent_name=_X) is None
    assert await store.get(tenant_id=t1, agent_name=_Y) is not None
    assert await store.get(tenant_id=t2, agent_name=_X) is not None


def _vec() -> tuple[float, ...]:
    return (1.0,) + (0.0,) * (EMBEDDING_DIM - 1)


@pytest.mark.asyncio
async def test_memory_for_agent_keeps_shared_and_other_tenant(sf: SF) -> None:
    store = SqlMemoryStore(sf)
    t1, t2, user = uuid4(), uuid4(), uuid4()

    def _item(tenant: UUID, agent: str | None, content: str) -> MemoryItem:
        return MemoryItem(
            id=uuid4(),
            tenant_id=tenant,
            user_id=user,
            kind="fact",
            content=content,
            embedding=_vec(),
            agent_name=agent,
        )

    await store.write(
        [
            _item(t1, _X, "x one"),
            _item(t1, _X, "x two"),
            _item(t1, None, "shared fact"),
            _item(t1, _Y, "y"),
            _item(t2, _X, "x elsewhere"),
        ]
    )
    assert await store.count_for_agent(tenant_id=t1, agent_name=_X) == 2
    assert await store.delete_for_agent(tenant_id=t1, agent_name=_X) == 2
    assert await store.count_for_agent(tenant_id=t1, agent_name=_X) == 0
    left = await store.list_for_user(tenant_id=t1, user_id=user)
    assert sorted(m.content for m in left) == ["shared fact", "y"]
    assert await store.count_for_agent(tenant_id=t2, agent_name=_X) == 1


@pytest.mark.asyncio
async def test_writeback_jobs_quality_curation_instances_secret_for_agent(sf: SF) -> None:
    t1, t2, user = uuid4(), uuid4(), uuid4()
    jobs = SqlMemoryWritebackJobStore(sf, claim_lock_classid=4242)
    scores = SqlQualityScoreStore(sf)
    alerts = SqlQualityDriftAlertStore(sf)
    candidates = SqlCurationCandidateStore(sf)
    datasets = SqlEvalDatasetStore(sf)
    instances = SqlAgentInstanceStore(sf)
    allowlist = SqlAgentSecretAllowlistStore(sf)

    for tenant, agent in ((t1, _X), (t1, _X), (t1, _Y), (t2, _X)):
        await jobs.enqueue(
            tenant_id=tenant,
            user_id=user,
            agent_name=agent,
            agent_version="1.0.0",
            thread_id=uuid4(),
            run_id=uuid4(),
            trace_id=None,
            checkpoint_id="ckpt",
            now=_NOW,
        )
        await scores.insert(
            QualityScoreRecord(
                tenant_id=tenant,
                agent_name=agent,
                agent_version="1.0.0",
                run_id=uuid4(),
                thread_id=uuid4(),
                overall=4,
                dimensions={},
                rationale="r",
                judge_model="m",
            )
        )
        await alerts.insert(
            QualityDriftAlertRecord(
                tenant_id=tenant,
                agent_name=agent,
                recent_mean=3.0,
                baseline_mean=4.0,
                drift_pct=0.25,
                recent_count=10,
                baseline_count=10,
            )
        )
        dataset = await datasets.create(
            EvalDatasetRecord(
                id=uuid4(),
                tenant_id=tenant,
                agent_name=agent,
                name=f"d-{uuid4().hex[:8]}",
                source="trajectory",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        await candidates.upsert(
            CurationCandidateRecord(
                id=uuid4(),
                tenant_id=tenant,
                agent_name=agent,
                thread_id=uuid4(),
                trajectory_key=f"k-{uuid4().hex}",
                outcome="failed",
                signal="failed_outcome",
                status=CandidateStatus.PROMOTED,
                eval_dataset_id=dataset.id,
                detected_at=_NOW,
                reviewed_at=_NOW,
            )
        )
        await instances.touch(tenant_id=tenant, agent_code=agent, user_id=uuid4())
        async with sf() as session:
            await session.execute(
                insert(SecretAllowlistRow).values(
                    tenant_id=tenant,
                    agent_name=agent,
                    agent_version="1.0.0",
                    secret_ref=uuid4().hex,
                )
            )
            await session.commit()

    pairs = (
        (jobs.count_for_agent, jobs.delete_for_agent, 2),
        (scores.count_for_agent, scores.delete_for_agent, 2),
        (alerts.count_for_agent, alerts.delete_for_agent, 2),
        (candidates.count_for_agent, candidates.delete_for_agent, 2),
        (allowlist.count_for_agent, allowlist.delete_for_agent, 2),
    )
    for count, delete, expected in pairs:
        assert await count(tenant_id=t1, agent_name=_X) == expected
        assert await delete(tenant_id=t1, agent_name=_X) == expected
        assert await count(tenant_id=t1, agent_name=_X) == 0
        assert await count(tenant_id=t1, agent_name=_Y) == 1
        assert await count(tenant_id=t2, agent_name=_X) == 1
    assert await instances.count_for_agent(tenant_id=t1, agent_code=_X) == 2
    assert await instances.delete_for_agent(tenant_id=t1, agent_code=_X) == 2
    assert await instances.count_for_agent(tenant_id=t1, agent_code=_Y) == 1
    assert await instances.count_for_agent(tenant_id=t2, agent_code=_X) == 1
    # Candidates went first, so the agent's datasets (RESTRICT FK) can go now.
    for d in await datasets.list_by_agent(tenant_id=t1, agent_name=_X):
        assert await datasets.delete(dataset_id=d.id, tenant_id=t1) is True


@pytest.mark.asyncio
async def test_tenant_default_agent_cleared_only_when_it_is_the_agent(sf: SF) -> None:
    store = SqlTenantConfigStore(sf)
    t1 = uuid4()
    await store.create(tenant_id=t1, display_name="t", actor_id="u")
    await store.upsert(tenant_id=t1, patch=TenantConfigPatch(default_agent_name=_X), actor_id="u")
    assert await store.clear_default_agent_name(tenant_id=t1, agent_name=_Y) is False
    assert await store.clear_default_agent_name(tenant_id=uuid4(), agent_name=_X) is False
    assert await store.clear_default_agent_name(tenant_id=t1, agent_name=_X) is True
    record = await store.get(tenant_id=t1)
    assert record is not None and record.default_agent_name is None


# --------------------------------------------------------------------------- artifacts


@pytest.mark.asyncio
async def test_artifacts_for_agent_key_every_user_only_this_key(sf: SF) -> None:
    store = SqlArtifactStore(sf)
    t1, t2, u1, u2 = uuid4(), uuid4(), uuid4(), uuid4()
    key, other = "doomed-agent-0badc0de", "other-agent-12345678"

    async def _save(tenant: UUID, user: UUID, agent_key: str, name: str) -> None:
        await store.save_version(
            tenant_id=tenant,
            user_id=user,
            agent_key=agent_key,
            name=name,
            kind="document",
            path_in_workspace=name,
            created_in_thread="t",
            snapshot_key=str(uuid4()),
            size_bytes=1,
            sha256="0" * 64,
        )

    await _save(t1, u1, key, "a.md")
    await _save(t1, u1, key, "a.md")  # second version of the same artifact
    await _save(t1, u2, key, "b.md")
    await _save(t1, u1, other, "a.md")
    await _save(t2, u1, key, "a.md")

    versions = await store.list_versions_for_agent(tenant_id=t1, agent_key=key)
    assert len(versions) == 3
    assert {v.user_id for v in versions} == {u1, u2}
    assert all(v.snapshot_key for v in versions)
    assert await store.count_for_agent(tenant_id=t1, agent_key=key) == 2
    assert await store.delete_all_for_agent(tenant_id=t1, agent_key=key) == 2
    assert await store.list_versions_for_agent(tenant_id=t1, agent_key=key) == []
    assert await store.count_for_agent(tenant_id=t1, agent_key=other) == 1
    assert len(await store.list_versions_for_agent(tenant_id=t1, agent_key=other)) == 1
    assert await store.count_for_agent(tenant_id=t2, agent_key=key) == 1
    with pytest.raises(ValueError, match="non-empty"):
        await store.count_for_agent(tenant_id=t1, agent_key="")


# ---------------------------------------------------------- conversation-keyed rows


@pytest.mark.asyncio
async def test_thread_scoped_counts_match_deletes(sf: SF) -> None:
    t1, t2, user = uuid4(), uuid4(), uuid4()
    mine = [uuid4(), uuid4()]
    other = uuid4()
    dlq = SqlMemoryWritebackDLQ(sf)
    uploads = SqlUserUploadStore(sf)
    images = SqlImageUploadStore(sf)
    approvals = SqlApprovalStore(sf)
    feedback = DbFeedbackStore(sf)
    logs = SqlKnowledgeRetrievalLogStore(sf)

    for tenant, thread in ((t1, mine[0]), (t1, mine[1]), (t1, other), (t2, mine[0])):
        await dlq.enqueue(
            tenant_id=tenant,
            user_id=user,
            source_thread_id=str(thread),
            extracted=[("fact", "c")],
            error="e",
        )
        await uploads.insert(
            upload_id=uuid4(),
            tenant_id=tenant,
            user_id=user,
            thread_id=thread,
            kind="document",
            ref=f"uploads/{uuid4().hex}.pdf",
            mime_type="application/pdf",
            size_bytes=1,
            filename="f.pdf",
        )
        image_id = uuid4()
        await images.insert(
            image_id=image_id,
            tenant_id=tenant,
            thread_id=thread,
            user_id=user,
            object_key=f"tenants/{tenant}/uploads/{image_id}.png",
            size_bytes=1,
            mime_type="image/png",
            sha256="x",
        )
        await approvals.create(
            ApprovalRecord(
                id=uuid4(),
                tenant_id=tenant,
                user_id=None,
                run_id=uuid4(),
                thread_id=thread,
                request_id="approval:seed",
                node="tools",
                reason_kind="policy_gate",
                action_summary="s",
                requested_at=_NOW,
                timeout_at=_NOW,
            )
        )
        await feedback.insert(
            FeedbackRecord(tenant_id=tenant, thread_id=thread, rating="up", actor_id="a")
        )
        await logs.add(
            RetrievalLogEntry(
                tenant_id=tenant,
                kb_ids=(uuid4(),),
                source="agent",
                query="q",
                status="ok",
                latency_ms=1,
                run_id=uuid4(),
                thread_id=thread,
            )
        )

    assert len(await images.list_for_threads(tenant_id=t1, thread_ids=mine)) == 2
    assert await images.delete_for_threads(tenant_id=t1, thread_ids=mine) == 2
    assert len(await images.list_for_threads(tenant_id=t1, thread_ids=[other])) == 1
    assert len(await images.list_for_threads(tenant_id=t2, thread_ids=mine)) == 1

    for store in (dlq, uploads):
        assert await store.count_for_threads(tenant_id=t1, thread_ids=mine) == 2
        assert await store.delete_for_threads(tenant_id=t1, thread_ids=mine) == 2
        assert await store.count_for_threads(tenant_id=t1, thread_ids=mine) == 0
        assert await store.count_for_threads(tenant_id=t1, thread_ids=[other]) == 1
        assert await store.count_for_threads(tenant_id=t2, thread_ids=mine) == 1

    assert await approvals.count_for_threads(tenant_id=t1, thread_ids=mine) == 2
    assert await approvals.delete_for_threads(thread_ids=mine, tenant_id=t1) == 2
    assert await feedback.count_for_threads(tenant_id=t1, thread_ids=mine) == 2
    assert await feedback.delete_for_threads(tenant_id=t1, thread_ids=mine) == 2
    assert await logs.count_for_threads(tenant_id=t1, thread_ids=mine) == 2
    assert await logs.delete_for_threads(tenant_id=t1, thread_ids=mine) == 2
    for counter in (approvals, feedback, logs):
        assert await counter.count_for_threads(tenant_id=t1, thread_ids=[other]) == 1
        assert await counter.count_for_threads(tenant_id=t2, thread_ids=mine) == 1
    # Empty id list: nothing counted, nothing deleted.
    assert await uploads.count_for_threads(tenant_id=t1, thread_ids=[]) == 0
    assert await uploads.delete_for_threads(tenant_id=t1, thread_ids=[]) == 0


@pytest.mark.asyncio
async def test_webhook_delivery_count_for_endpoints(sf: SF) -> None:
    store = SqlWebhookDeliveryStore(sf)
    t1, t2 = uuid4(), uuid4()
    mine, other = uuid4(), uuid4()
    for tenant, endpoint in ((t1, mine), (t1, mine), (t1, other), (t2, mine)):
        await store.create(
            WebhookDeliveryRecord(
                id=uuid4(),
                tenant_id=tenant,
                endpoint_id=endpoint,
                event_id=uuid4().hex,
                event_type="run.completed",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    assert await store.count_for_endpoints(endpoint_ids=[mine], tenant_id=t1) == 2
    assert await store.delete_for_endpoints(endpoint_ids=[mine], tenant_id=t1) == 2
    assert await store.count_for_endpoints(endpoint_ids=[mine, other], tenant_id=t1) == 1
    assert await store.count_for_endpoints(endpoint_ids=[mine], tenant_id=t2) == 1
