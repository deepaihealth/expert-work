"""B-280 —— 删用户(``purge_user``)补上的两件事。

* 用户各条对话上的质量评分(评审的理由会引用那一轮对话)与评测候选样本(带用户自己的 👎 评论,
  指向这条对话的轨迹)随对话删掉;别的用户对话上的不动(候选样本里 ``user_id`` 是这个用户的,
  照旧只抹掉关联)。
* 图片字节删不掉:那一行留着、这一步记失败,重跑删完;一页删完还有就接着删。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.purge import PurgeUserDeps, purge_user, user_purge
from control_plane.settings import Settings
from expert_work.persistence import (
    InMemoryCurationCandidateStore,
    InMemoryQualityScoreStore,
    InMemoryTenantUserStore,
    InMemoryThreadMetaStore,
)
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.image_upload import InMemoryImageUploadStore
from expert_work.persistence.knowledge.retrieval_log import InMemoryKnowledgeRetrievalLogStore
from expert_work.protocol import CurationCandidateRecord, QualityScoreRecord
from expert_work.runtime.runs import InMemoryRunStore
from expert_work.runtime.storage import InMemoryObjectStore
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier
from tests.test_user_purge import _deps_with


def _score(tenant: UUID, thread: UUID) -> QualityScoreRecord:
    return QualityScoreRecord(
        tenant_id=tenant,
        agent_name="alpha",
        agent_version="1.0.0",
        run_id=uuid4(),
        thread_id=thread,
        overall=2,
        dimensions={},
        rationale="the reply ignored the user's allergy",
        judge_model="m",
    )


def _candidate(tenant: UUID, thread: UUID, user: UUID) -> CurationCandidateRecord:
    return CurationCandidateRecord(
        id=uuid4(),
        tenant_id=tenant,
        agent_name="alpha",
        thread_id=thread,
        user_id=user,
        trajectory_key=f"k-{uuid4().hex}",
        outcome="failed",
        signal="negative_feedback",
        feedback_comment="这个建议不对,我对花生过敏",
        detected_at=datetime.now(UTC),
    )


class _World:
    """User A with one conversation; user B with one. Quality scores and
    curation candidates on both; one candidate of A's on B's conversation."""

    def __init__(self) -> None:
        self.tenant = uuid4()
        self.threads = InMemoryThreadMetaStore()
        self.users = InMemoryTenantUserStore()
        self.scores = InMemoryQualityScoreStore()
        self.candidates = InMemoryCurationCandidateStore()

    async def seed(self) -> None:
        self.a = (
            await self.users.resolve(tenant_id=self.tenant, subject_type="user", subject_id="a")
        ).id
        self.b = (
            await self.users.resolve(tenant_id=self.tenant, subject_type="user", subject_id="b")
        ).id
        self.a_thread, self.b_thread = uuid4(), uuid4()
        for thread, user in ((self.a_thread, self.a), (self.b_thread, self.b)):
            await self.threads.create(
                thread_id=thread,
                tenant_id=self.tenant,
                created_by="seed",
                user_id=user,
                agent_name="alpha",
                agent_version="1.0.0",
            )
        self.a_score, self.b_score = (
            _score(self.tenant, self.a_thread),
            _score(self.tenant, self.b_thread),
        )
        for score in (self.a_score, self.b_score):
            await self.scores.insert(score)
        self.a_cand = _candidate(self.tenant, self.a_thread, self.a)
        self.b_cand = _candidate(self.tenant, self.b_thread, self.b)
        # A's run on B's conversation (an approver's continuation, say).
        self.a_on_b = _candidate(self.tenant, self.b_thread, self.a)
        for cand in (self.a_cand, self.b_cand, self.a_on_b):
            await self.candidates.upsert(cand)

    def deps(self) -> PurgeUserDeps:
        base = _deps_with(
            threads=self.threads,
            runs=InMemoryRunStore(),
            retrieval_logs=InMemoryKnowledgeRetrievalLogStore(),
            users=self.users,
        )
        return replace(base, quality_scores=self.scores, curation_candidates=self.candidates)


async def _purge_a(world: _World, deps: PurgeUserDeps) -> user_purge.PurgeSummary:
    return await purge_user(
        tenant_id=world.tenant, user_id=world.a, subject_id="a", deps=deps, actor_id="admin"
    )


@pytest.mark.asyncio
async def test_the_users_quality_scores_and_candidates_go_with_their_conversations() -> None:
    world = _World()
    await world.seed()

    summary = await _purge_a(world, world.deps())

    assert summary.failures == {}
    assert summary.deleted["quality_score"] == 1
    assert summary.deleted["curation_candidate"] == 1
    assert not await world.scores.exists(tenant_id=world.tenant, run_id=world.a_score.run_id)
    assert await world.scores.exists(tenant_id=world.tenant, run_id=world.b_score.run_id)
    assert await world.candidates.get(candidate_id=world.a_cand.id, tenant_id=world.tenant) is None
    # B's candidate untouched; A's candidate on B's conversation stays, without A.
    b_cand = await world.candidates.get(candidate_id=world.b_cand.id, tenant_id=world.tenant)
    assert b_cand is not None and b_cand.user_id == world.b
    a_on_b = await world.candidates.get(candidate_id=world.a_on_b.id, tenant_id=world.tenant)
    assert a_on_b is not None and a_on_b.user_id is None
    assert summary.anonymized["curation_candidate"] == 1


class _Broken:
    def __init__(self, message: str) -> None:
        self.message = message

    async def __call__(self, **_kw: object) -> int:
        raise RuntimeError(self.message)


@pytest.mark.asyncio
async def test_a_failed_child_keeps_the_conversation_and_a_rerun_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quality scores have no user column and candidates get anonymised later:
    once the conversation row is gone nothing finds them again. So a failure
    keeps the conversation (``skipped`` has ``threads``) and a re-run finishes."""
    world = _World()
    await world.seed()
    secret = "postgres://u:hunter2@db/x"
    monkeypatch.setattr(world.scores, "delete_for_threads", _Broken(secret))
    monkeypatch.setattr(world.candidates, "delete_for_threads", _Broken(secret))

    summary = await _purge_a(world, world.deps())

    assert summary.failures["quality_score"] == "RuntimeError"
    assert summary.failures["curation_candidate_delete"] == "RuntimeError"
    assert secret not in str(summary.as_dict())
    assert not summary.ok
    assert "threads" in summary.skipped
    assert summary.threads_purged == 0
    assert await world.threads.get(world.a_thread, tenant_id=world.tenant) is not None

    monkeypatch.undo()
    rerun = await _purge_a(world, world.deps())
    assert rerun.ok, rerun.as_dict()
    assert rerun.threads_purged == 1
    assert await world.threads.get(world.a_thread, tenant_id=world.tenant) is None
    assert not await world.scores.exists(tenant_id=world.tenant, run_id=world.a_score.run_id)
    assert await world.candidates.get(candidate_id=world.a_cand.id, tenant_id=world.tenant) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["quality_score", "curation_candidate_delete"])
async def test_one_failed_child_alone_keeps_the_conversation(
    monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    world = _World()
    await world.seed()
    store = world.scores if failing == "quality_score" else world.candidates
    monkeypatch.setattr(store, "delete_for_threads", _Broken("x"))

    summary = await _purge_a(world, world.deps())

    assert set(summary.failures) == {failing}
    assert "threads" in summary.skipped
    assert await world.threads.get(world.a_thread, tenant_id=world.tenant) is not None


def _image_world() -> tuple[UUID, UUID, InMemoryImageUploadStore]:
    return uuid4(), uuid4(), InMemoryImageUploadStore()


async def _images(store: InMemoryImageUploadStore, tenant: UUID, user: UUID, n: int) -> list[str]:
    keys = []
    for _ in range(n):
        image_id = uuid4()
        key = f"{tenant}/uploads/{uuid4()}/{image_id}.png"
        await store.insert(
            image_id=image_id,
            tenant_id=tenant,
            thread_id=uuid4(),
            user_id=user,
            object_key=key,
            size_bytes=1,
            mime_type="image/png",
            sha256="x",
        )
        keys.append(key)
    return keys


class _OneKeyFails(InMemoryObjectStore):
    def __init__(self, bad: str) -> None:
        super().__init__()
        self.bad = bad

    async def delete(self, key: str) -> None:
        if key == self.bad:
            raise TimeoutError("oss")
        await super().delete(key)


@pytest.mark.asyncio
async def test_only_the_image_whose_bytes_stayed_keeps_its_row() -> None:
    world = _World()
    await world.seed()
    images = InMemoryImageUploadStore()
    keys = await _images(images, world.tenant, world.a, 3)
    objects = _OneKeyFails(keys[1])
    for key in keys:
        await objects.put(key, b"png")
    deps = replace(world.deps(), image_uploads=images, object_store=objects)

    summary = await _purge_a(world, deps)

    assert summary.failures["image_upload"] == "ImageBytesNotDeletedError"
    assert (summary.image_blobs_removed, summary.image_blobs_failed) == (2, 1)
    assert summary.deleted["image_upload"] == 2
    left = await images.list_for_user(tenant_id=world.tenant, user_id=world.a)
    assert [r.object_key for r in left] == [keys[1]]
    assert await objects.list_prefix(keys[1]) == [keys[1]]
    assert await objects.list_prefix(keys[0]) == []


@pytest.mark.asyncio
async def test_images_beyond_one_page_are_all_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    await world.seed()
    monkeypatch.setattr(user_purge, "_IMAGE_PAGE", 2)
    images = InMemoryImageUploadStore()
    keys = await _images(images, world.tenant, world.a, 5)
    objects = InMemoryObjectStore()
    for key in keys:
        await objects.put(key, b"png")
    deps = replace(world.deps(), image_uploads=images, object_store=objects)

    summary = await _purge_a(world, deps)

    assert "image_upload" not in summary.failures
    assert summary.deleted["image_upload"] == 5
    assert summary.image_blobs_removed == 5
    assert await images.list_for_user(tenant_id=world.tenant, user_id=world.a) == []


def test_the_purge_endpoint_wires_the_quality_score_store() -> None:
    """生产唯一的装配点要把质量评分接进清除流程 —— 漏接就是静默不删。"""
    from control_plane.api.agent_users import _build_purge_deps

    app = create_app(
        settings=Settings(
            env="dev", auth_mode="dev", oidc_issuer=TEST_ISSUER, oidc_audience=[TEST_AUDIENCE]
        ),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    deps = _build_purge_deps(SimpleNamespace(app=app))  # type: ignore[arg-type]
    assert deps.quality_scores is app.state.quality_score_store
