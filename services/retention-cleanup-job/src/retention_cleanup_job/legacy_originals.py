"""B-281 —— 一次性清理:老对话迁移之后留在 agent 根的上传原件。

``python -m retention_cleanup_job.legacy_originals``(默认只数不删)/ ``… --apply``(删)。

老对话(``workspace_layout = 'agent_root'``)的上传落在 agent 共用的 ``agents/<key>/uploads/``。
B-262 P3 起,老对话下一次 run 之前把本对话的上传**复制**进 ``threads/<会话>/work/uploads/`` 并把
会话翻成对话视图;登记行从此按新布局解析到那份副本,agent 根下的原件没有任何登记行再指着它,
留存作业永远删不到它(:mod:`retention_cleanup_job.uploads` 模块头)。

**不扫目录**:候选只来自登记表。「引用」= 没删的文档类登记行 + 成功的 ``DOCUMENT_UPLOAD`` 审计行
(B-281 之前的控制台上传与跨会话复制只有审计),按「老视图」会落到同一个原件
(``agents/<key>/uploads/<名字>``)上的那些。一份原件只有同时满足下面每一条才删:

1. 至少有一条没删的登记行指着它(只处理登记过的;只有审计的原件不碰);
2. 没有任何引用所在的会话还在老视图(登记行:同一用户;审计行:同一 agent 的任何用户 —— 审计里
   看不出文件落进了谁的工作区);也没有引用所在的会话没有用户(同理);
3. 没有任何同租户、同用户、同名字的没删登记行,所在会话已经不在(算不出它原来在哪,宁可留;
   会话已不在的审计行不算引用 —— 那段对话已经清除,它的上传谁也拿不到了);
4. 每一条引用(都在对话视图的会话里)的副本都在、是普通文件,内容与原件逐字节相同(sha256);
   引用自己记了大小 / sha256 的,也必须与原件相同 —— 包括还没迁移的对话视图会话(B-262 P1 之前开的,
   上传还只在 agent 根):它没有副本,原件留着;
5. 原件是普通文件(路径上任何一段是软链都不碰)。

也就是说删掉的只是一份**逐字节重复**的文件,每个还可能用到它的会话手里都有一份相同的。文件操作
全部用 :func:`~retention_cleanup_job.workspace_files.open_upload_file`(从 NAS 根逐段 ``openat`` +
``O_NOFOLLOW``,核对与删除在同一组 fd 上做,删之前再比一次 inode);``--apply`` 时每删一个之前
再查一次登记表(期间新登记的同名上传 → 不删)。

输出只有计数与字节数,不打文件名、租户、用户。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Row, Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence import (
    AuditLogRow,
    DatabaseConfig,
    SqlThreadMetaStore,
    UserUploadRow,
    create_async_engine_from_config,
)
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.persistence.workspace.layout import WORKSPACE_UPLOADS_DIR
from expert_work.protocol import AuditAction, AuditResult, ThreadMeta
from retention_cleanup_job.job import _bypass_rls
from retention_cleanup_job.main import (
    EXIT_PASS_FAILED,
    build_session_factory,
    configure_logging,
    resolve_workspace_root,
)
from retention_cleanup_job.settings import RetentionCleanupSettings
from retention_cleanup_job.workspace_files import (
    OpenedUpload,
    UnsafeWorkspacePathError,
    WorkspaceOwnerUnavailableError,
    WorkspaceRootUnavailableError,
    open_upload_file,
    upload_relpath,
)

logger = logging.getLogger(__name__)

#: 一次 ``IN (...)`` 绑多少个 id。
_CHUNK = 5000

_ThreadKey = tuple[UUID, UUID]
#: (租户, 用户, 原件的用户根相对路径)
_OriginalKey = tuple[UUID, UUID, str]
_Metas = Mapping[_ThreadKey, ThreadMeta | None]

#: 一份原件的结论。
Outcome = Literal[
    "no_original",
    "original_differs",
    "copy_missing",
    "copy_changed",
    "unsafe",
    "deletable",
    "deleted",
]


@dataclass(frozen=True)
class LiveUpload:
    """一条没删的文档类登记行(只取这里要用的列)。"""

    id: UUID
    tenant_id: UUID
    user_id: UUID
    thread_id: UUID
    ref: str
    size_bytes: int
    sha256: str | None


@dataclass(frozen=True)
class AuditedUpload:
    """一条成功的 ``DOCUMENT_UPLOAD`` 审计行里这里要用的东西(大小 / sha256 老行可能没有)。"""

    tenant_id: UUID
    thread_id: UUID
    ref: str
    size_bytes: int | None
    sha256: str | None


@dataclass(frozen=True, order=True)
class Copy:
    """一条引用在它自己(对话视图)会话里应有的那份副本。"""

    user_id: UUID
    relpath: str
    size_bytes: int | None
    sha256: str | None


@dataclass(frozen=True)
class Original:
    """一份可能可以删的原件、指着它的登记行,以及每条引用应有的副本。"""

    tenant_id: UUID
    user_id: UUID
    ref: str
    relpath: str
    size_bytes: int
    row_ids: frozenset[UUID]
    copies: tuple[Copy, ...]


@dataclass
class LegacyOriginalsTally:
    """计数。``checked`` = 有登记行、登记行全在对话视图会话里的 (用户, agent, 名字) 组数 ——
    其中大多数从来没有原件(B-262 P1 之后开的对话直接落自己的目录),计在 ``no_original``。"""

    checked: int = 0
    deletable: int = 0
    deleted: int = 0
    bytes: int = 0
    no_original: int = 0
    kept_old_view_row: int = 0
    kept_unregistered_upload: int = 0
    kept_session_gone: int = 0
    kept_copy_missing: int = 0
    kept_copy_changed: int = 0
    kept_original_differs: int = 0
    kept_unsafe: int = 0
    kept_changed_during_run: int = 0
    failed: int = 0

    def line(self, *, apply: bool) -> str:
        mode = "apply" if apply else "dry-run"
        parts = " ".join(f"{f.name}={getattr(self, f.name)}" for f in fields(self))
        return f"legacy_originals mode={mode} {parts}"


def _chunks(ids: Iterable[UUID]) -> Iterator[list[UUID]]:
    ordered = sorted(set(ids), key=str)
    for start in range(0, len(ordered), _CHUNK):
        yield ordered[start : start + _CHUNK]


def _old_view(meta: ThreadMeta) -> ThreadMeta:
    return meta.model_copy(update={"workspace_layout": "agent_root"})


@dataclass
class _Refs:
    """:func:`plan` 的中间结果:按原件归好的引用。"""

    gone: set[tuple[UUID, UUID, str]] = field(default_factory=set)
    old_view_rows: set[_OriginalKey] = field(default_factory=set)
    #: (租户, 原件路径):老视图 / 没有用户的会话的审计行指着它 —— 不论哪个用户的原件都留。
    unregistered: set[tuple[UUID, str]] = field(default_factory=set)
    rows: dict[_OriginalKey, list[LiveUpload]] = field(default_factory=dict)
    copies: dict[_OriginalKey, set[Copy]] = field(default_factory=dict)


def _collect_rows(refs: _Refs, rows: Sequence[LiveUpload], metas: _Metas) -> None:
    for row in rows:
        meta = metas.get((row.tenant_id, row.thread_id))
        if meta is None:
            refs.gone.add((row.tenant_id, row.user_id, row.ref))
            continue
        if not meta.agent_name:
            continue  # 没绑 agent:文件在用户根的 ``uploads/``,没有「agent 根原件」
        key = (row.tenant_id, row.user_id, upload_relpath(row.ref, _old_view(meta)))
        if meta.workspace_layout == "agent_root":
            refs.old_view_rows.add(key)
            continue
        refs.rows.setdefault(key, []).append(row)
        copy = Copy(row.user_id, upload_relpath(row.ref, meta), row.size_bytes, row.sha256)
        refs.copies.setdefault(key, set()).add(copy)


def _collect_audits(refs: _Refs, audits: Sequence[AuditedUpload], metas: _Metas) -> None:
    for audit in audits:
        meta = metas.get((audit.tenant_id, audit.thread_id))
        if meta is None or not meta.agent_name:
            continue  # 会话已清除(谁也拿不到它的上传了)/ 没绑 agent(不在 agent 根)
        relpath = upload_relpath(audit.ref, _old_view(meta))
        if meta.workspace_layout == "agent_root" or meta.user_id is None:
            refs.unregistered.add((audit.tenant_id, relpath))
            continue
        copy = Copy(meta.user_id, upload_relpath(audit.ref, meta), audit.size_bytes, audit.sha256)
        refs.copies.setdefault((audit.tenant_id, meta.user_id, relpath), set()).add(copy)


def plan(
    rows: Sequence[LiveUpload],
    audits: Sequence[AuditedUpload],
    metas: _Metas,
    tally: LegacyOriginalsTally,
) -> list[Original]:
    """按引用把候选原件分组,挡掉第 1 至 3 条(见模块头)不满足的。不碰文件。"""
    refs = _Refs()
    _collect_rows(refs, rows, metas)
    _collect_audits(refs, audits, metas)
    originals: list[Original] = []
    for key, registered in refs.rows.items():
        tenant_id, user_id, relpath = key
        ref = registered[0].ref
        tally.checked += 1
        if (tenant_id, user_id, ref) in refs.gone:
            tally.kept_session_gone += 1
        elif key in refs.old_view_rows:
            tally.kept_old_view_row += 1
        elif (tenant_id, relpath) in refs.unregistered:
            tally.kept_unregistered_upload += 1
        else:
            originals.append(
                Original(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    ref=ref,
                    relpath=relpath,
                    size_bytes=registered[0].size_bytes,
                    row_ids=frozenset(r.id for r in registered),
                    copies=tuple(sorted(refs.copies[key])),
                )
            )
    return originals


def _digest(opened: OpenedUpload) -> str:
    with os.fdopen(os.dup(opened.leaf_fd), "rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def _copies_hold(root: str, original: Original, digest: str) -> Outcome | None:
    """第 4 条:每条引用记的大小 / sha256(有的话)等于原件的,它的副本在、与原件同内容。
    ``None`` = 都满足。"""
    size = original.size_bytes
    for copy in original.copies:
        if (copy.size_bytes is not None and copy.size_bytes != size) or (
            copy.sha256 is not None and copy.sha256 != digest
        ):
            return "original_differs"
        opened = open_upload_file(
            root, original.tenant_id, copy.user_id, copy.relpath, size_bytes=size, sha256=digest
        )
        opened.close()
        if opened.outcome == "missing":
            return "copy_missing"
        if opened.outcome == "changed":
            return "copy_changed"
    return None


def release(root: str, original: Original, *, unlink: bool) -> tuple[Outcome, int]:
    """第 4、5 条,满足且 ``unlink`` 时删原件。回 (结论, 原件字节数)。路径不可用 → ``"unsafe"``;
    暂时性 I/O 错误与工作区根 / 属主目录打不开原样抛。"""
    size = original.size_bytes
    try:
        opened = open_upload_file(
            root,
            original.tenant_id,
            original.user_id,
            original.relpath,
            size_bytes=size,
            sha256=None,
        )
    except UnsafeWorkspacePathError:
        return "unsafe", 0
    if opened.outcome == "missing":
        return "no_original", 0
    if opened.outcome == "changed":
        return "original_differs", 0
    try:
        blocked = _copies_hold(root, original, _digest(opened))
        if blocked is not None:
            return blocked, 0
        if not unlink:
            return "deletable", size
        outcome = opened.unlink()
    except UnsafeWorkspacePathError:
        return "unsafe", 0
    finally:
        opened.close()
    if outcome == "removed":
        return "deleted", size
    # 核对之后名字被换成了别的文件(``changed``)或已经不在了:都不算删了。
    return ("no_original" if outcome == "missing" else "original_differs"), 0


_KEPT: dict[Outcome, str] = {
    "no_original": "no_original",
    "original_differs": "kept_original_differs",
    "copy_missing": "kept_copy_missing",
    "copy_changed": "kept_copy_changed",
    "unsafe": "kept_unsafe",
}


@dataclass
class LegacyOriginalsCleanup:
    """查库 → :func:`plan` → 逐个 :func:`release`。``apply=False`` 只数。"""

    session_factory: async_sessionmaker[AsyncSession]
    threads: ThreadMetaStore
    root: str
    apply: bool = False
    tally: LegacyOriginalsTally = field(default_factory=LegacyOriginalsTally)

    async def run(self) -> LegacyOriginalsTally:
        rows = await self._live_uploads()
        audits = await self._audited_uploads({r.tenant_id for r in rows})
        metas = await self._metas(
            [(r.tenant_id, r.thread_id) for r in rows]
            + [(a.tenant_id, a.thread_id) for a in audits]
        )
        for original in plan(rows, audits, metas, self.tally):
            await self._one(original)
        return self.tally

    async def _one(self, original: Original) -> None:
        tally = self.tally
        try:
            if self.apply and not await self._still_a_candidate(original):
                tally.kept_changed_during_run += 1
                return
            outcome, size = await asyncio.to_thread(release, self.root, original, unlink=self.apply)
        except WorkspaceRootUnavailableError:
            raise
        except (WorkspaceOwnerUnavailableError, OSError) as exc:
            tally.failed += 1
            logger.warning("legacy_originals.failed error=%s", type(exc).__name__)
            return
        if outcome in ("deletable", "deleted"):
            setattr(tally, outcome, getattr(tally, outcome) + 1)
            tally.bytes += size
        else:
            setattr(tally, _KEPT[outcome], getattr(tally, _KEPT[outcome]) + 1)

    async def _still_a_candidate(self, original: Original) -> bool:
        """删之前再查一次登记表:同租户同用户同 ``ref`` 的没删行重新分组,这份原件还得是候选,而且
        登记行正好是核对过副本的那些(期间新传 / 新复制进来的同名文件 → 这一轮不删)。审计不再查:
        B-281 起新的控制台上传与复制都建登记行,审计那一侧只剩历史。"""
        async with self.session_factory() as session:
            result = await session.execute(
                _live_uploads_stmt().where(
                    UserUploadRow.tenant_id == original.tenant_id,
                    UserUploadRow.user_id == original.user_id,
                    UserUploadRow.ref == original.ref,
                )
            )
            rows = [_to_live(r) for r in result.all()]
        metas = await self._metas((r.tenant_id, r.thread_id) for r in rows)
        candidates = plan(rows, [], metas, LegacyOriginalsTally())
        again = [o for o in candidates if o.relpath == original.relpath]
        return len(again) == 1 and again[0].row_ids == original.row_ids

    async def _live_uploads(self) -> list[LiveUpload]:
        async with self.session_factory() as session:
            result = await session.execute(_live_uploads_stmt())
            return [_to_live(r) for r in result.all()]

    async def _audited_uploads(self, tenants: set[UUID]) -> list[AuditedUpload]:
        """这些租户全部成功的 ``DOCUMENT_UPLOAD`` 审计行(不按时间收窄:老对话的上传多早都算)。"""
        if not tenants:
            return []
        stmt = select(AuditLogRow.tenant_id, AuditLogRow.details).where(
            AuditLogRow.tenant_id.in_(sorted(tenants, key=str)),
            AuditLogRow.action == AuditAction.DOCUMENT_UPLOAD.value,
            AuditLogRow.result == AuditResult.SUCCESS.value,
        )
        async with self.session_factory() as session:
            result = await session.execute(stmt)
            return [a for t, d in result.all() if (a := _audited(t, d)) is not None]

    async def _metas(self, keys: Iterable[_ThreadKey]) -> dict[_ThreadKey, ThreadMeta | None]:
        by_tenant: dict[UUID, set[UUID]] = {}
        for tenant_id, thread_id in keys:
            by_tenant.setdefault(tenant_id, set()).add(thread_id)
        out: dict[_ThreadKey, ThreadMeta | None] = {}
        for tenant_id, thread_ids in by_tenant.items():
            for chunk in _chunks(thread_ids):
                found = await self.threads.get_many(chunk, tenant_id=tenant_id)
                for thread_id in chunk:
                    out[(tenant_id, thread_id)] = found.get(thread_id)
        return out


def _audited(tenant_id: UUID, details: Mapping[str, Any] | None) -> AuditedUpload | None:
    """审计行 ``details`` → :class:`AuditedUpload`;认不出会话或名字的不算(那样的行指不到文件)。"""
    details = details or {}
    path, thread = details.get("workspace_path"), details.get("thread_id")
    if not isinstance(path, str) or not path.startswith(f"{WORKSPACE_UPLOADS_DIR}/"):
        return None
    try:
        thread_id = UUID(str(thread))
    except ValueError:
        return None
    size, sha = details.get("file_size_bytes"), details.get("sha256")
    return AuditedUpload(
        tenant_id=tenant_id,
        thread_id=thread_id,
        ref=path,
        size_bytes=size if isinstance(size, int) else None,
        sha256=sha if isinstance(sha, str) else None,
    )


_LiveColumns = tuple[UUID, UUID, UUID, UUID, str, int, str | None]


def _live_uploads_stmt() -> Select[_LiveColumns]:
    return select(
        UserUploadRow.id,
        UserUploadRow.tenant_id,
        UserUploadRow.user_id,
        UserUploadRow.thread_id,
        UserUploadRow.ref,
        UserUploadRow.size_bytes,
        UserUploadRow.sha256,
    ).where(
        UserUploadRow.deleted_at.is_(None),
        UserUploadRow.kind == "document",
        UserUploadRow.ref.startswith(f"{WORKSPACE_UPLOADS_DIR}/", autoescape=True),
    )


def _to_live(row: Row[_LiveColumns]) -> LiveUpload:
    upload_id, tenant_id, user_id, thread_id, ref, size_bytes, sha256 = row
    return LiveUpload(upload_id, tenant_id, user_id, thread_id, ref, size_bytes, sha256)


async def _amain(argv: Sequence[str] | None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m retention_cleanup_job.legacy_originals",
        description="B-281: delete agent-root upload originals whose conversations were migrated "
        "(dry-run unless --apply). Prints counts and bytes only.",
    )
    parser.add_argument("--apply", action="store_true", help="really delete (default: count only)")
    args = parser.parse_args(argv)

    settings = RetentionCleanupSettings()
    configure_logging(settings)
    root = resolve_workspace_root(settings, environ=os.environ)
    if root is None:
        logger.error("legacy_originals.workspace_root_unset — refusing to run")
        return 2
    engine = create_async_engine_from_config(DatabaseConfig(dsn=settings.db_dsn))
    session_factory = build_session_factory(engine)
    try:
        with _bypass_rls():
            tally = await LegacyOriginalsCleanup(
                session_factory=session_factory,
                threads=SqlThreadMetaStore(session_factory),
                root=root,
                apply=args.apply,
            ).run()
    finally:
        await engine.dispose()
    sys.stdout.write(tally.line(apply=args.apply) + "\n")
    return EXIT_PASS_FAILED if tally.failed else 0


def main(argv: Sequence[str] | None = None) -> None:
    code = asyncio.run(_amain(argv))
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
