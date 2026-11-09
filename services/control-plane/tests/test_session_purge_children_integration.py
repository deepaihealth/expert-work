"""B-280 —— 删单个对话时的子数据删除,在生产那一套 SQL store 上(真 Postgres + Alembic schema)。

种三份:要删的对话、同一个智能体同一个用户的另一条对话、另一个租户里同一个 ``thread_id``
(运行时的数据库角色绕过 RLS,隔离只靠 ``WHERE tenant_id``)。只有第一份没了。某一类删不掉
时别的类照删,重跑删完剩下的。API 层的语义(503、对话留着)由
``test_session_purge_children.py`` 钉住。

跑法::

    export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
    uv run --no-sync pytest \
        services/control-plane/tests/test_session_purge_children_integration.py -m integration -q
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from testcontainers.postgres import PostgresContainer

from control_plane.purge.session_purge import SessionPurgeStores, purge_thread_children
from expert_work.persistence import (
    DatabaseConfig,
    SqlCurationCandidateStore,
    SqlQualityScoreStore,
    SqlUserUploadStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.feedback_store import DbFeedbackStore, FeedbackRecord
from expert_work.persistence.image_upload.sql import SqlImageUploadStore
from expert_work.persistence.memory.dlq import SqlMemoryWritebackDLQ
from expert_work.persistence.models import UserUploadRow
from expert_work.protocol import (
    CurationCandidateRecord,
    QualityScoreRecord,
    is_session_deleted_tombstone,
)
from expert_work.runtime.storage import InMemoryObjectStore

pytestmark = pytest.mark.integration

_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "packages/expert-work-persistence/alembic.ini"
_NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
_AGENT = "b280-agent"

SF = async_sessionmaker[AsyncSession]


def _dsn(container: PostgresContainer, driver: str) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", f"+{driver}").replace(
        "postgresql://", f"postgresql+{driver}://", 1
    )


@pytest.fixture
def sf(postgres_container: PostgresContainer) -> Iterator[SF]:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _dsn(postgres_container, "psycopg"))
    command.upgrade(cfg, "head")
    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=_dsn(postgres_container, "asyncpg"))
    )
    yield create_async_session_factory(engine)


def _stores(sf: SF, objects: InMemoryObjectStore) -> SessionPurgeStores:
    return SessionPurgeStores(
        feedback=DbFeedbackStore(sf),
        quality_scores=SqlQualityScoreStore(sf),
        curation_candidates=SqlCurationCandidateStore(sf),
        memory_dlq=SqlMemoryWritebackDLQ(sf),
        user_uploads=SqlUserUploadStore(sf),
        image_uploads=SqlImageUploadStore(sf),
        object_store=objects,
    )


@dataclass(frozen=True)
class _Seeded:
    tenant_id: UUID
    thread_id: UUID
    run_id: UUID
    candidate_id: UUID
    object_key: str
    upload_id: UUID


async def _seed(s: SessionPurgeStores, tenant_id: UUID, thread_id: UUID, user: UUID) -> _Seeded:
    assert isinstance(s.object_store, InMemoryObjectStore)
    run_id, candidate_id, image_id, upload_id = uuid4(), uuid4(), uuid4(), uuid4()
    object_key = f"{tenant_id}/uploads/{thread_id}/{image_id}.png"
    await s.feedback.insert(
        FeedbackRecord(tenant_id=tenant_id, thread_id=thread_id, rating="down", actor_id="a")
    )
    await s.quality_scores.insert(
        QualityScoreRecord(
            tenant_id=tenant_id,
            agent_name=_AGENT,
            agent_version="1.0.0",
            run_id=run_id,
            thread_id=thread_id,
            overall=2,
            dimensions={},
            rationale="r",
            judge_model="m",
        )
    )
    await s.curation_candidates.upsert(
        CurationCandidateRecord(
            id=candidate_id,
            tenant_id=tenant_id,
            agent_name=_AGENT,
            thread_id=thread_id,
            user_id=user,
            trajectory_key=f"k-{uuid4().hex}",
            outcome="failed",
            signal="failed_outcome",
            detected_at=_NOW,
        )
    )
    await s.memory_dlq.enqueue(
        tenant_id=tenant_id,
        user_id=user,
        source_thread_id=str(thread_id),
        extracted=[("fact", "c")],
        error="e",
    )
    await s.user_uploads.insert(
        upload_id=upload_id,
        tenant_id=tenant_id,
        user_id=user,
        thread_id=thread_id,
        kind="document",
        ref="uploads/report.pdf",
        mime_type="application/pdf",
        size_bytes=1,
        filename="report.pdf",
    )
    await s.object_store.put(object_key, b"png", content_type="image/png")
    await s.image_uploads.insert(
        image_id=image_id,
        tenant_id=tenant_id,
        thread_id=thread_id,
        user_id=user,
        object_key=object_key,
        size_bytes=3,
        mime_type="image/png",
        sha256="x",
    )
    return _Seeded(tenant_id, thread_id, run_id, candidate_id, object_key, upload_id)


async def _remaining(s: SessionPurgeStores, seeded: _Seeded) -> dict[str, int]:
    assert s.object_store is not None
    t, ids = seeded.tenant_id, [seeded.thread_id]
    upload = await s.user_uploads.get(upload_id=seeded.upload_id, tenant_id=t)
    return {
        "feedback": await s.feedback.count_for_threads(tenant_id=t, thread_ids=ids),
        "quality_scores": int(await s.quality_scores.exists(tenant_id=t, run_id=seeded.run_id)),
        "curation_candidates": int(
            await s.curation_candidates.get(candidate_id=seeded.candidate_id, tenant_id=t)
            is not None
        ),
        "memory_writeback_dlq": await s.memory_dlq.count_for_threads(tenant_id=t, thread_ids=ids),
        # A tombstone (B-280) is not a live upload: it keeps no name, ref or bytes.
        "user_uploads": int(upload is not None and not is_session_deleted_tombstone(upload)),
        "image_uploads": len(await s.image_uploads.list_for_threads(tenant_id=t, thread_ids=ids)),
        "image_blob": len(await s.object_store.list_prefix(seeded.object_key)),
    }


_ONE = {
    "feedback": 1,
    "quality_scores": 1,
    "curation_candidates": 1,
    "memory_writeback_dlq": 1,
    "user_uploads": 1,
    "image_uploads": 1,
    "image_blob": 1,
}
_ZERO = dict.fromkeys(_ONE, 0)


@pytest.mark.asyncio
async def test_only_the_purged_conversations_rows_go(sf: SF) -> None:
    s = _stores(sf, InMemoryObjectStore())
    tenant, other_tenant, user = uuid4(), uuid4(), uuid4()
    doomed, sibling = uuid4(), uuid4()
    mine = await _seed(s, tenant, doomed, user)
    theirs = await _seed(s, tenant, sibling, user)
    elsewhere = await _seed(s, other_tenant, doomed, user)

    result = await purge_thread_children(s, tenant_id=tenant, thread_id=doomed)

    assert result.failures == {}
    assert result.deleted == {
        "feedback": 1,
        "quality_scores": 1,
        "curation_candidates": 1,
        "memory_writeback_dlq": 1,
        "user_uploads": 1,
        "image_uploads": 1,
        # B-285 —— no trajectory store wired here (``test_purge_trajectories.py``).
        "trajectories": 0,
    }
    assert result.image_blobs_removed == 1
    assert await _remaining(s, mine) == _ZERO
    assert await _remaining(s, theirs) == _ONE
    assert await _remaining(s, elsewhere) == _ONE

    again = await purge_thread_children(s, tenant_id=tenant, thread_id=doomed)
    assert again.failures == {}
    assert set(again.deleted.values()) == {0}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "attr"),
    [
        ("feedback", "feedback"),
        ("quality_scores", "quality_scores"),
        ("curation_candidates", "curation_candidates"),
        ("memory_writeback_dlq", "memory_dlq"),
        ("user_uploads", "user_uploads"),
        ("image_uploads", "image_uploads"),
    ],
)
async def test_a_failed_kind_leaves_only_itself_and_a_rerun_finishes(
    sf: SF, monkeypatch: pytest.MonkeyPatch, kind: str, attr: str
) -> None:
    s = _stores(sf, InMemoryObjectStore())
    tenant, user, doomed = uuid4(), uuid4(), uuid4()
    mine = await _seed(s, tenant, doomed, user)
    method = {"image_uploads": "list_for_threads", "user_uploads": "tombstone_for_threads"}.get(
        kind, "delete_for_threads"
    )

    async def _boom(**_kw: object) -> int:
        raise ConnectionError("db blip")

    monkeypatch.setattr(getattr(s, attr), method, _boom)
    first = await purge_thread_children(s, tenant_id=tenant, thread_id=doomed)
    assert first.failures == {kind: "ConnectionError"}
    monkeypatch.undo()  # the store works again (``_remaining`` reads through it too)
    left = await _remaining(s, mine)
    expected = dict(_ZERO)
    expected[kind] = 1
    if kind == "image_uploads":
        expected["image_blob"] = 1
    assert left == expected

    rerun = await purge_thread_children(s, tenant_id=tenant, thread_id=doomed)
    assert rerun.failures == {}
    assert rerun.deleted[kind] == 1
    assert await _remaining(s, mine) == _ZERO


@pytest.mark.asyncio
async def test_a_row_whose_bytes_did_not_go_is_kept(
    sf: SF, monkeypatch: pytest.MonkeyPatch
) -> None:
    objects = InMemoryObjectStore()
    s = _stores(sf, objects)
    tenant, user, doomed = uuid4(), uuid4(), uuid4()
    first_image = await _seed(s, tenant, doomed, user)
    second_image = await _seed(s, tenant, doomed, user)
    real_delete = objects.delete

    async def _second_fails(key: str) -> None:
        if key == second_image.object_key:
            raise TimeoutError("oss")
        await real_delete(key)

    monkeypatch.setattr(objects, "delete", _second_fails)
    result = await purge_thread_children(s, tenant_id=tenant, thread_id=doomed)
    assert result.failures == {"image_uploads": "ImageBytesNotDeletedError"}
    assert (result.image_blobs_removed, result.image_blobs_failed) == (1, 1)
    assert result.deleted["image_uploads"] == 1
    rows = await s.image_uploads.list_for_threads(tenant_id=tenant, thread_ids=[doomed])
    assert [r.object_key for r in rows] == [second_image.object_key]
    assert await objects.list_prefix(first_image.object_key) == []

    monkeypatch.undo()
    rerun = await purge_thread_children(s, tenant_id=tenant, thread_id=doomed)
    assert rerun.failures == {}
    assert await s.image_uploads.list_for_threads(tenant_id=tenant, thread_ids=[doomed]) == []
    assert await objects.list_prefix(second_image.object_key) == []


#: Columns a tombstone keeps: what the external lookup needs to answer
#: ``UPLOAD_SESSION_DELETED`` to the same end user (id, tenant, user, thread,
#: kind) plus ``created_at`` / ``deleted_at``. Every other column is scrubbed to
#: the value below — the four NOT NULL text / size columns to empty / 0.
_TOMBSTONE_KEEPS = {"id", "tenant_id", "user_id", "thread_id", "kind", "created_at"}
_TOMBSTONE_SCRUBS: dict[str, object] = {
    "ref": "",
    "filename": "",
    "mime_type": "",
    "size_bytes": 0,
    "sha256": None,
    "last_used_at": None,
}


async def _raw(sf: SF, upload_id: UUID) -> dict[str, object] | None:
    async with sf() as session:
        row = await session.get(UserUploadRow, upload_id)
        if row is None:
            return None
        return {c.key: getattr(row, c.key) for c in UserUploadRow.__mapper__.column_attrs}


@pytest.mark.asyncio
async def test_a_tombstone_keeps_only_what_the_lookup_needs(sf: SF) -> None:
    uploads = SqlUserUploadStore(sf)
    tenant, user, doomed, sibling = uuid4(), uuid4(), uuid4(), uuid4()
    doc, image, expired, kept = uuid4(), uuid4(), uuid4(), uuid4()
    for upload_id, thread, kind in (
        (doc, doomed, "document"),
        (image, doomed, "image"),
        (expired, doomed, "document"),
        (kept, sibling, "document"),
    ):
        await uploads.insert(
            upload_id=upload_id,
            tenant_id=tenant,
            user_id=user,
            thread_id=thread,
            kind=kind,  # type: ignore[arg-type]
            ref="uploads/客户A-体检.pdf"
            if kind == "document"
            else f"expert_work://image/{uuid4()}",
            mime_type="application/pdf",
            size_bytes=1234,
            filename="客户A-体检.pdf",
            sha256="ab" * 32,
        )
    await uploads.touch(upload_id=doc, tenant_id=tenant, now=_NOW)
    await uploads.soft_delete(upload_id=expired, tenant_id=tenant, now=_NOW)
    before = await _raw(sf, doc)
    assert before is not None
    assert set(before) == _TOMBSTONE_KEEPS | set(_TOMBSTONE_SCRUBS) | {"deleted_at"}, (
        "user_upload grew a column: decide whether a tombstone keeps or scrubs it"
    )

    handled = await uploads.tombstone_for_threads(tenant_id=tenant, thread_ids=[doomed], now=_NOW)

    assert handled == 3
    after = await _raw(sf, doc)
    assert after is not None
    for column in _TOMBSTONE_KEEPS:
        assert after[column] == before[column], column
    for column, scrubbed in _TOMBSTONE_SCRUBS.items():
        assert after[column] == scrubbed, column
    assert after["deleted_at"] == _NOW
    tombstone = await uploads.get(upload_id=doc, tenant_id=tenant)
    assert tombstone is not None and is_session_deleted_tombstone(tombstone)
    # Image rows and already-expired rows answered UPLOAD_NOT_FOUND before; gone now.
    assert await _raw(sf, image) is None
    assert await _raw(sf, expired) is None
    # Another conversation's upload is untouched.
    sibling_row = await _raw(sf, kept)
    assert sibling_row is not None and sibling_row["filename"] == "客户A-体检.pdf"

    # B-63 retention never sees a tombstone: not expired, not a reference holder.
    far = _NOW + timedelta(days=3650)
    expired = await uploads.list_expired(now=far, default_days=1)
    assert doc not in {e.upload.id for e in expired}
    by_ref = await uploads.list_unexpired_by_ref(
        tenant_id=tenant, user_id=user, ref="", now=_NOW, default_days=3650
    )
    assert by_ref == []

    # Re-run: the tombstone stays as it is.
    assert await uploads.tombstone_for_threads(tenant_id=tenant, thread_ids=[doomed], now=_NOW) == 0
    assert await _raw(sf, doc) == after
