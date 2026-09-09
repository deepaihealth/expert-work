"""留存链 PR2 —— 三条工作区规则 + **不变式**(波 3 线 R)。

不变式:留存 job 对**活着的**工作区,只删
(a) 登记过的产物文件(``artifact_version.path_in_workspace``,满 90 天的版本)
(b) ``uploads/`` 下登记过的文件(``user_upload.ref``,满 90 天)
(c) 孤儿 ``threads/<thread_id>/``(thread 行不存在)
—— 根目录任何其它文件/目录(``style/``、MEMORY.md、未登记文件、非 UUID 名的
threads 子目录、已软删用户的整棵树)永不触碰。

``test_run_once_touches_only_the_three_registered_shapes`` 用一个临时目录造出测试
环境实测到的全部形态,跑一次 ``run_once``,把「跑前 - 跑后」的路径差集与预期
逐字比对 —— 多删一个、少删一个都红。变异自证见 PR 正文(把 unlink 改成删父目录
→ ``style/`` 没了 → 红;去掉「thread 行存在」判据 → 活会话目录没了 → 红;去掉
软删用户跳过 → 归档前的树被动了 → 红)。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import (
    InMemoryArtifactStore,
    InMemoryAuditLogStore,
    InMemoryThreadMetaStore,
    InMemoryUserUploadStore,
    InMemoryUserWorkspaceStore,
)
from expert_work.protocol import AuditAction, AuditQuery
from expert_work.runtime.audit import (
    AuditLogger,
    DefaultSecretRedactor,
    InMemoryAuditFallbackQueue,
)
from retention_cleanup_job.job import RetentionCleanupJob
from retention_cleanup_job.workspace_files import deleted_marker

_OLD = datetime.now(UTC) - timedelta(days=100)
_FRESH = datetime.now(UTC) - timedelta(days=1)


# ---------------------------------------------------------------- fixtures


def _stub_sql_passes(job: RetentionCleanupJob) -> None:
    """The raw-SQL passes need Postgres; this file is about the workspace rules."""

    async def _audit_log() -> tuple[int, dict[str, int]]:
        return 0, {}

    async def _zero() -> int:
        return 0

    job._delete_audit_log = _audit_log  # type: ignore[method-assign]
    job._count_unacked_past_retention = _zero  # type: ignore[method-assign]
    job._delete_event_log = _zero  # type: ignore[method-assign]
    job._delete_sandbox_egress_audit = _zero  # type: ignore[method-assign]
    job._delete_expired_jwt_blacklist = _zero  # type: ignore[method-assign]


def _write(root: Path, tenant: UUID, user: UUID, rel: str) -> Path:
    target = root / str(tenant) / str(user) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x")
    return target


def _tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


async def _register_version(
    artifacts: InMemoryArtifactStore,
    *,
    tenant: UUID,
    user: UUID,
    name: str,
    path: str,
    created_at: datetime,
) -> UUID:
    version = await artifacts.save_version(
        tenant_id=tenant,
        user_id=user,
        name=name,
        kind="document",
        path_in_workspace=path,
        created_in_thread="t",
    )
    artifacts._versions = [  # backdate: the in-memory store stamps now()
        v.model_copy(update={"created_at": created_at}) if v.id == version.id else v
        for v in artifacts._versions
    ]
    return version.artifact_id


async def _register_upload(
    uploads: InMemoryUserUploadStore,
    *,
    tenant: UUID,
    user: UUID,
    ref: str,
    created_at: datetime,
    kind: str = "document",
) -> UUID:
    upload_id = uuid4()
    await uploads.insert(
        upload_id=upload_id,
        tenant_id=tenant,
        user_id=user,
        thread_id=uuid4(),
        kind=kind,  # type: ignore[arg-type]
        ref=ref,
        mime_type="application/octet-stream",
        size_bytes=1,
        filename=ref.rsplit("/", 1)[-1],
    )
    row = uploads._rows[(tenant, upload_id)]
    uploads._rows[(tenant, upload_id)] = row.model_copy(update={"created_at": created_at})
    return upload_id


class _World:
    """Everything one scenario needs, built by :func:`world`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.artifacts = InMemoryArtifactStore()
        self.uploads = InMemoryUserUploadStore()
        self.threads = InMemoryThreadMetaStore()
        self.workspaces = InMemoryUserWorkspaceStore()
        self.audit_store = InMemoryAuditLogStore()
        self.tenant = uuid4()
        self.user = uuid4()
        self.deleted_user = uuid4()
        self.live_thread = uuid4()
        self.orphan_thread = uuid4()
        self.deleted_user_orphan_thread = uuid4()
        self.ids: dict[str, UUID] = {}

    def job(self, **overrides: Any) -> RetentionCleanupJob:
        kwargs: dict[str, Any] = {
            "db_session_factory": lambda: None,
            "artifact_store": self.artifacts,
            "user_upload_store": self.uploads,
            "thread_store": self.threads,
            "workspace_store": self.workspaces,
            "workspace_root": str(self.root),
            "artifact_retention_days": 90,
            "upload_retention_days": 90,
            "audit_logger": AuditLogger(
                store=self.audit_store,
                redactor=DefaultSecretRedactor(),
                fallback=InMemoryAuditFallbackQueue(),
            ),
        }
        kwargs.update(overrides)
        job = RetentionCleanupJob(**kwargs)
        _stub_sql_passes(job)
        return job


@pytest.fixture
async def world(tmp_path: Path) -> Iterator[_World]:
    w = _World(tmp_path)
    t, u = w.tenant, w.user
    root = tmp_path

    # ---- registered artifacts -------------------------------------------
    # report.pptx: single version, 100d → file + version go, artifact expires.
    w.ids["report"] = await _register_version(
        w.artifacts, tenant=t, user=u, name="report.pptx", path="report.pptx", created_at=_OLD
    )
    # summary.md: v1 (100d, own file) + v2 (1d) → only v1's file goes, artifact stays active.
    w.ids["summary"] = await _register_version(
        w.artifacts, tenant=t, user=u, name="summary.md", path="summary_v1.md", created_at=_OLD
    )
    await _register_version(
        w.artifacts, tenant=t, user=u, name="summary.md", path="summary.md", created_at=_FRESH
    )
    # plan.json: fresh → untouched.
    w.ids["plan"] = await _register_version(
        w.artifacts, tenant=t, user=u, name="plan.json", path="plan.json", created_at=_FRESH
    )
    # nested deliverable in a Chinese-named folder: file goes, folder + sibling stay.
    w.ids["followup"] = await _register_version(
        w.artifacts,
        tenant=t,
        user=u,
        name="高血压21天随访",
        path="高血压21天随访/day1.md",
        created_at=_OLD,
    )
    # registered but already gone from disk → row still expires, no error.
    w.ids["ghost"] = await _register_version(
        w.artifacts, tenant=t, user=u, name="ghost.md", path="ghost.md", created_at=_OLD
    )
    for rel in (
        "report.pptx",
        "summary_v1.md",
        "summary.md",
        "plan.json",
        "高血压21天随访/day1.md",
        "高血压21天随访/notes.md",
    ):
        _write(root, t, u, rel)

    # ---- registered uploads ---------------------------------------------
    w.ids["upload_old"] = await _register_upload(
        w.uploads, tenant=t, user=u, ref="uploads/old.pdf", created_at=_OLD
    )
    w.ids["upload_new"] = await _register_upload(
        w.uploads, tenant=t, user=u, ref="uploads/new.pdf", created_at=_FRESH
    )
    # image-kind upload: the ref is an object-store URI, no workspace file.
    w.ids["upload_image"] = await _register_upload(
        w.uploads,
        tenant=t,
        user=u,
        ref="expert_work://image/abc",
        created_at=_OLD,
        kind="image",
    )
    for rel in ("uploads/old.pdf", "uploads/new.pdf", "uploads/stray.pdf"):
        _write(root, t, u, rel)

    # ---- threads ----------------------------------------------------------
    await w.threads.create(thread_id=w.live_thread, tenant_id=t, created_by="u", user_id=u)
    for tid in (w.live_thread, w.orphan_thread):
        _write(root, t, u, f"threads/{tid}/MEMORY.md")
        _write(root, t, u, f"threads/{tid}/PLAN.md")
    _write(root, t, u, "threads/not-a-uuid/x.md")

    # ---- everything else at the root: must survive untouched -------------
    for rel in (
        "unregistered.md",
        "style/rules.md",
        "style/tone.md",
        "MEMORY.md",
        "PLAN.md",
        "TODO.md",
        "assets/a.png",
        "design/d.txt",
        "payload/p.json",
        "qa/q.md",
        "scripts/s.py",
        "tools/t.py",
        ".tool_results/r.json",
    ):
        _write(root, t, u, rel)

    # ---- a soft-deleted user (marker present): whole tree is the janitor's -
    _write(root, t, w.deleted_user, f"threads/{w.deleted_user_orphan_thread}/MEMORY.md")
    _write(root, t, w.deleted_user, "style/x.md")
    marker = deleted_marker(str(root), t, w.deleted_user)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()

    # ---- root-level scratch (janitor's, 24h) -----------------------------
    (root / "_scratch" / "sbx-1").mkdir(parents=True)
    (root / "_scratch" / "sbx-1" / "tmp.txt").write_bytes(b"x")

    yield w


# ---------------------------------------------------------------- the invariant


@pytest.mark.asyncio
async def test_run_once_touches_only_the_three_registered_shapes(world: _World) -> None:
    w = world
    t, u = w.tenant, w.user
    before = _tree(w.root)

    report = await w.job().run_once()

    after = _tree(w.root)
    prefix = f"{t}/{u}/"
    expected_removed = {
        prefix + "report.pptx",
        prefix + "summary_v1.md",
        prefix + "高血压21天随访/day1.md",
        prefix + "uploads/old.pdf",
        prefix + f"threads/{w.orphan_thread}",
        prefix + f"threads/{w.orphan_thread}/MEMORY.md",
        prefix + f"threads/{w.orphan_thread}/PLAN.md",
    }
    assert before - after == expected_removed
    assert after - before == set()  # nothing created either

    # Counters line up with the filesystem diff.
    assert report.artifact_versions_deleted == 4  # report, summary v1, followup, ghost
    assert report.artifact_files_removed == 3  # ghost was already gone
    assert report.artifacts_expired == 3  # report, followup, ghost — summary keeps v2
    assert report.uploads_expired == 2  # old.pdf + the image-kind row
    assert report.upload_files_removed == 1
    assert report.thread_dirs_removed == 1

    # Registry side.
    by_name = {
        a.name: a
        for a in await w.artifacts.list_for_user(tenant_id=t, user_id=u, include_deleted=True)
    }
    assert by_name["report.pptx"].deleted_at is not None
    assert by_name["高血压21天随访"].deleted_at is not None
    assert by_name["ghost.md"].deleted_at is not None
    assert by_name["summary.md"].deleted_at is None
    assert by_name["plan.json"].deleted_at is None
    summary_versions = await w.artifacts.list_versions(tenant_id=t, user_id=u, name="summary.md")
    assert summary_versions is not None
    assert [v.path_in_workspace for v in summary_versions] == ["summary.md"]
    old_upload = await w.uploads.get(upload_id=w.ids["upload_old"], tenant_id=t)
    new_upload = await w.uploads.get(upload_id=w.ids["upload_new"], tenant_id=t)
    image_upload = await w.uploads.get(upload_id=w.ids["upload_image"], tenant_id=t)
    assert old_upload is not None and old_upload.deleted_at is not None
    assert new_upload is not None and new_upload.deleted_at is None
    assert image_upload is not None and image_upload.deleted_at is not None

    # One ARTIFACT_EXPIRED audit row per logical artifact that lost versions.
    page = await w.audit_store.query(AuditQuery(tenant_id=t))
    expired_rows = [r for r in page.entries if r.action is AuditAction.ARTIFACT_EXPIRED]
    assert {r.resource_id for r in expired_rows} == {
        str(w.ids["report"]),
        str(w.ids["summary"]),
        str(w.ids["followup"]),
        str(w.ids["ghost"]),
    }
    summary_row = next(r for r in expired_rows if r.resource_id == str(w.ids["summary"]))
    assert summary_row.details["versions_deleted"] == [1]
    assert summary_row.details["artifact_expired"] is False
    assert all(r.actor_id == "retention_cleanup_job" for r in expired_rows)


@pytest.mark.asyncio
async def test_run_once_is_idempotent(world: _World) -> None:
    w = world
    await w.job().run_once()
    snapshot = _tree(w.root)
    audit_before = len((await w.audit_store.query(AuditQuery(tenant_id=w.tenant))).entries)

    second = await w.job().run_once()

    assert _tree(w.root) == snapshot
    assert second.artifact_versions_deleted == 0
    assert second.artifact_files_removed == 0
    assert second.artifacts_expired == 0
    assert second.uploads_expired == 0
    assert second.upload_files_removed == 0
    assert second.thread_dirs_removed == 0
    assert len((await w.audit_store.query(AuditQuery(tenant_id=w.tenant))).entries) == audit_before


@pytest.mark.asyncio
async def test_file_rules_are_skipped_without_workspace_root(world: _World) -> None:
    """No NAS root → rows are NOT deleted either (a row is the only index to its
    file; deleting one without the other is a permanent orphan)."""
    w = world
    before = _tree(w.root)

    report = await w.job(workspace_root=None).run_once()

    assert _tree(w.root) == before
    assert (
        report.artifact_versions_deleted,
        report.uploads_expired,
        report.thread_dirs_removed,
    ) == (0, 0, 0)
    by_name = {
        a.name: a
        for a in await w.artifacts.list_for_user(
            tenant_id=w.tenant, user_id=w.user, include_deleted=True
        )
    }
    # Stage 1 of the pre-existing metadata sweep still soft-deletes by
    # ``updated_at`` — that is the old behaviour, unchanged; only the file-
    # touching rules are gated on the root.
    assert by_name["plan.json"].deleted_at is None
    old_upload = await w.uploads.get(upload_id=w.ids["upload_old"], tenant_id=w.tenant)
    assert old_upload is not None and old_upload.deleted_at is None


# ---------------------------------------------------------- per-rule edge cases


@pytest.mark.asyncio
async def test_unremovable_artifact_file_keeps_its_version_row(world: _World) -> None:
    """A file that cannot be unlinked (here: the registered path is a directory
    → unsafe shape) keeps its version row so tomorrow's sweep retries; nothing
    else about that artifact changes."""
    w = world
    t, u = w.tenant, w.user
    (w.root / str(t) / str(u) / "report.pptx").unlink()
    (w.root / str(t) / str(u) / "report.pptx").mkdir()

    report = await w.job().run_once()

    versions = await w.artifacts.list_versions(tenant_id=t, user_id=u, name="report.pptx")
    assert versions is not None and len(versions) == 1
    assert (w.root / str(t) / str(u) / "report.pptx").is_dir()
    assert report.artifact_versions_deleted == 3  # the other three still went


@pytest.mark.asyncio
async def test_orphan_scan_never_deletes_a_live_thread_dir_by_age(world: _World) -> None:
    """The live thread's directory is 'old' in every sense the filesystem can
    express, and still survives: the only criterion is the thread row."""
    w = world
    import os
    import time

    live_dir = w.root / str(w.tenant) / str(w.user) / "threads" / str(w.live_thread)
    ancient = time.time() - 365 * 86400
    for p in (live_dir, *live_dir.iterdir()):
        os.utime(p, (ancient, ancient))

    await w.job().run_once()

    assert (live_dir / "MEMORY.md").exists()


@pytest.mark.asyncio
async def test_soft_deleted_artifact_files_are_unlinked_before_hard_delete(world: _World) -> None:
    """Stage 2 of the metadata sweep (soft-deleted past the grace window) now
    unlinks the remaining version files before dropping the rows — a user-
    initiated soft delete at day 10 must not leave day-70 orphans on NAS."""
    w = world
    t, u = w.tenant, w.user
    await w.artifacts.soft_delete(
        tenant_id=t, user_id=u, name="plan.json", now=datetime.now(UTC) - timedelta(days=70)
    )
    target = w.root / str(t) / str(u) / "plan.json"
    assert target.exists()

    report = await w.job(artifact_hard_delete_grace_days=60).run_once()

    assert report.artifacts_hard_deleted == 1
    assert not target.exists()
    assert await w.artifacts.list_versions_by_artifact(artifact_id=w.ids["plan"]) == []
