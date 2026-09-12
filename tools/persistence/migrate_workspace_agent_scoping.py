"""B-50 Task 11 —— 把存量工作区搬成按 agent 分层的布局。

搬迁之前,一个用户的 ``/workspace`` 是扁平一棵树:同一个用户名下的多个 agent
在里头互相读错写错(spec §1)。搬迁之后是::

    {tenant}/{user}/
      agents/<agent_key>/…     ← 反推得出归属的,各归其位
      shared/…                 ← 反推不出的 legacy,可读不可写,冻结
      skills/…                 ← 保留段,原地不动

**归属判定照 spec §7.1,推不出来就进 ``shared/``,不猜。** 猜错的代价不对称:
猜对了省一次人工认领,猜错了把 A 的客户资料喂进 B 的上下文 —— 而且事后查不出来
(本来就是因为查不出归属才要猜的)。

库 + CLI 双形态,范本是同目录的 ``restore_volume.py``:**默认 dry-run**,
``--apply`` 才真搬,结果交回运维。三个函数的分工刻意切开 ——
:func:`collect_attributions` 是唯一碰库的,:func:`plan_migration` 只读文件系统
且不碰库(于是归属判定的每一档都能单独喂进去单测),:func:`apply_migration`
只按计划动盘。

运行顺序见 ``docs/runbooks/workspace-agent-scoping-migration.md``。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence import (
    WORKSPACE_AGENTS_DIR,
    WORKSPACE_RESERVED_PREFIXES,
    WORKSPACE_SHARED_DIR,
    WORKSPACE_UPLOADS_DIR,
)
from expert_work.protocol.agent_key import sanitize_agent_key

logger = logging.getLogger(__name__)

#: 会话产物目录 —— ``threads/<thread_id>/…``。归属沿 thread_id 直接查。
_THREADS_DIR = "threads"

#: 工具结果溢出缓存 —— ``.tool_results/<run_id>/…``。归属多一跳:
#: run_id → ``agent_run.thread_id`` → ``thread_meta.agent_name``。
_TOOL_RESULTS_DIR = ".tool_results"

#: 搬迁一个字节都不碰的顶层段。
#:
#: ``skills`` / ``uploads`` 来自 :data:`WORKSPACE_RESERVED_PREFIXES`,但两者
#: **处置完全不同**:``skills/`` 是运行时播种的机器文件,留在用户根;
#: ``uploads/`` 是用户输入,spec §7.1 明确要求跟着会话的 agent 走。所以这里
#: 只取 ``skills``,不是整个保留段集合 —— 直接复用那个集合会让 ``uploads/``
#: 悄悄留在原地,而它恰恰是本次要分的三类文件之一。
#:
#: ``agents`` / ``shared`` 是搬迁的**终态目录**:重跑时它们已经在正确位置,
#: 再当成待搬的源会搬成 ``agents/<key>/agents/<key>/x``。
_NEVER_MOVED: frozenset[str] = frozenset(
    {WORKSPACE_AGENTS_DIR, WORKSPACE_SHARED_DIR}
    | (WORKSPACE_RESERVED_PREFIXES - {WORKSPACE_UPLOADS_DIR})
)


@dataclass(frozen=True)
class Attributions:
    """从库里查出来的归属事实 —— 纯数据,让 :func:`plan_migration` 保持可单测。

    每张映射的键都是**工作区相对路径或其中的 id 段**,值是 ``agent_key``
    (已经过 ``sanitize_agent_key``,与沙箱里的目录名同一个算法)。
    """

    #: 该用户只用过一个 agent 时的 agent_key;多 agent 用户为 ``None``。
    #: 实测 64 个有会话的用户里 56 个属于这一档,整棵树归它,不用逐文件判。
    sole_agent_key: str | None
    #: ``uploads/<name>`` → agent_key(来自 ``user_upload.ref`` + ``thread_id``)。
    uploads: Mapping[str, str]
    #: ``artifact_version.path_in_workspace`` → agent_key(来自 ``artifact.agent_key``,
    #: 迁移 ``0154`` 已回填)。
    artifacts: Mapping[str, str]
    #: thread_id(str)→ agent_key(来自 ``thread_meta.agent_name``)。
    threads: Mapping[str, str]
    #: run_id(str)→ agent_key(来自 ``agent_run`` → ``thread_meta``)。
    runs: Mapping[str, str]


@dataclass(frozen=True)
class MigrationPlan:
    """一个用户的搬迁计划 —— 算好但还没动盘。"""

    tenant_id: UUID
    user_id: UUID
    #: 旧相对路径 → 新相对路径。**包含**进 ``shared/`` 的那些,
    #: :func:`apply_migration` 照这一张表执行就够了。
    moves: Mapping[str, str]
    #: ``moves`` 里目的地落在 ``shared/`` 的源路径(诊断用)。
    to_shared: tuple[str, ...]
    #: ``to_shared`` 的子集 —— 进 ``shared/`` 的原因是**目的地已有文件**
    #: 而不是「反推不出归属」。单独报出来:它意味着 agent 目录里有一份更新的
    #: 同名文件,运维可能想人工看一眼两者差异。
    conflicts: tuple[str, ...]
    #: 保留段等一律不动的相对路径(断言守恒时要算进去)。
    untouched: tuple[str, ...]
    #: ``artifact_version.path_in_workspace`` 的旧值 → 新值。
    artifact_path_updates: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MigrationReport:
    """一次 :func:`apply_migration` 的结果 —— 运维读的那份数。

    三个计数**互不重叠**,``moved + to_shared + untouched`` 就是这个用户的
    文件总数。计划里的 ``moves`` 是执行表(含进 ``shared/`` 的那批),
    报告里的 ``moved`` 只数「搬进了某个 agent 目录」的 —— 两者刻意不同名同义:
    一份给机器执行,一份给人读,合并成一个数会让守恒断言变成重言式。
    """

    #: 搬进 ``agents/<key>/`` 的文件数。
    moved: int
    #: 搬进 ``shared/`` 的文件数(反推不出归属 + 目的地已被占)。
    to_shared: int
    #: 原地不动的文件数(保留段、已在终态、目的地冲突到无处可去)。
    untouched: int
    #: 同步改掉的 ``artifact_version.path_in_workspace`` 行数。
    artifact_rows_updated: int


def _user_root(root: str, tenant_id: UUID, user_id: UUID) -> Path:
    return Path(root) / str(tenant_id) / str(user_id)


def _iter_relpaths(user_root: Path) -> list[str]:
    """用户根下的全部文件,POSIX 相对路径,排序后返回。

    ``followlinks=False`` 与 ``NasWorkspaceStore.list_files`` 同口径 ——
    跟进符号链接会把链接目标(可能在工作区之外)当成用户的文件搬进来,
    而且枚举到的东西会和 agent 自己看得见的东西对不上。
    """
    if not user_root.is_dir():
        return []
    out: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(user_root, followlinks=False):
        base = Path(dirpath)
        for name in filenames:
            out.append((base / name).relative_to(user_root).as_posix())
    return sorted(out)


def _owner_of(rel: str, attributions: Attributions) -> str | None:
    """这个相对路径归哪个 agent;反推不出返回 ``None``(→ ``shared/``)。

    判定顺序照 spec §7.1 的表。**形状可判不等于归属可判** —— 第一段是
    ``threads`` 但查不到登记行(会话行被清理过),仍然返回 ``None``:
    按「形状对就归第一个 agent」处理会静默串号。
    """
    if attributions.sole_agent_key is not None:
        return attributions.sole_agent_key

    parts = PurePosixPath(rel).parts
    head = parts[0] if parts else ""

    if head == WORKSPACE_UPLOADS_DIR:
        return attributions.uploads.get(rel)
    if head == _THREADS_DIR and len(parts) >= 2:
        return attributions.threads.get(parts[1])
    if head == _TOOL_RESULTS_DIR and len(parts) >= 2:
        return attributions.runs.get(parts[1])
    return attributions.artifacts.get(rel)


def plan_migration(
    root: str,
    tenant_id: UUID,
    user_id: UUID,
    *,
    attributions: Attributions,
) -> MigrationPlan:
    """算出一个用户的搬迁计划。只读文件系统,不碰库,不动盘。

    ``root`` 是 NAS 根(用户目录 = ``{root}/{tenant}/{user}``)。
    """
    user_root = _user_root(root, tenant_id, user_id)
    moves: dict[str, str] = {}
    to_shared: list[str] = []
    conflicts: list[str] = []
    untouched: list[str] = []
    artifact_updates: dict[str, str] = {}

    existing = set(_iter_relpaths(user_root))
    #: 本轮计划要占用的目的地。两个源不许搬到同一个目的地 —— 后搬的会静默
    #: 盖掉先搬的,而两边都是用户的真实文件。
    claimed: set[str] = set()

    for rel in sorted(existing):
        head = PurePosixPath(rel).parts[0] if PurePosixPath(rel).parts else ""
        if head in _NEVER_MOVED:
            untouched.append(rel)
            continue

        owner = _owner_of(rel, attributions)
        if owner:
            dest = f"{WORKSPACE_AGENTS_DIR}/{owner}/{rel}"
            # 目的地已有文件 → 不覆盖,源进 shared/。
            #
            # 这是单 agent 用户的常态,不是边角:PR3 上线后 agent 的**写**一律
            # 落 agents/<key>/,而**读**不到就回落用户根 —— 于是「读老的
            # MEMORY.md、写新的 agents/<key>/MEMORY.md」每天都在发生。把老的
            # 盖上去抹掉的正是这段时间里的全部更新,且事后无从恢复。
            if dest in existing or dest in claimed:
                conflicts.append(rel)
                dest = f"{WORKSPACE_SHARED_DIR}/{rel}"
                to_shared.append(rel)
        else:
            dest = f"{WORKSPACE_SHARED_DIR}/{rel}"
            to_shared.append(rel)

        if dest in existing or dest in claimed:
            # shared/ 侧也可能撞上(重跑、或两个源同名)。撞了就原地不动 ——
            # 宁可留一个没搬走的文件让运维看见,也不能悄悄盖掉一份真实内容。
            logger.warning("workspace_migration.destination_taken rel=%s dest=%s", rel, dest)
            untouched.append(rel)
            if to_shared and to_shared[-1] == rel:
                to_shared.pop()
            if conflicts and conflicts[-1] == rel:
                conflicts.pop()
            continue

        claimed.add(dest)
        moves[rel] = dest
        if rel in attributions.artifacts:
            artifact_updates[rel] = dest

    return MigrationPlan(
        tenant_id=tenant_id,
        user_id=user_id,
        moves=moves,
        to_shared=tuple(to_shared),
        conflicts=tuple(conflicts),
        untouched=tuple(untouched),
        artifact_path_updates=artifact_updates,
    )


def apply_migration(
    plan: MigrationPlan,
    *,
    root: str,
    dry_run: bool,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> MigrationReport:
    """按计划动盘。``dry_run=True``(默认形态)只算数,一个字节都不写。

    ``session_factory`` 给出时,顺带把 ``artifact_version.path_in_workspace``
    更新成新路径 —— 那一列存的是**含前缀的完整相对路径**,七个下游消费点都是
    拿它去 join 用户根,所以前缀在值里就自然对了(spec §7.1 简化)。
    """
    user_root = _user_root(root, plan.tenant_id, plan.user_id)
    if not dry_run:
        for rel, dest in plan.moves.items():
            src = user_root / rel
            dst = user_root / dest
            dst.parent.mkdir(parents=True, exist_ok=True)
            # os.replace 是同一文件系统内的原子改名,不读内容不改权限位 ——
            # 沙箱迁移 W2-BUG-1 的教训:任何「读出来再写回去」的搬法都会把
            # 文件的属主/权限位换成当前进程的,跨 uid 之后读不了。
            os.replace(src, dst)
        _prune_empty_dirs(user_root, plan)

    rows_updated = 0
    if plan.artifact_path_updates and session_factory is not None and not dry_run:
        rows_updated = asyncio.run(
            _update_artifact_paths(
                session_factory,
                tenant_id=plan.tenant_id,
                user_id=plan.user_id,
                updates=plan.artifact_path_updates,
            )
        )

    return MigrationReport(
        moved=len(plan.moves) - len(plan.to_shared),
        to_shared=len(plan.to_shared),
        untouched=len(plan.untouched),
        artifact_rows_updated=rows_updated,
    )


def _prune_empty_dirs(user_root: Path, plan: MigrationPlan) -> None:
    """搬空的老目录删掉 —— 只删**本次搬空的**,且只删空目录。

    留着空的 ``uploads/`` / ``threads/`` 会让浏览面和 ``list_dir`` 里多出一排
    什么都没有的目录,模型会当成「这里本来有东西但被删了」。只按本次 moves
    的来源目录自底向上试删,``rmdir`` 非空就抛 OSError,天然不会误删。
    """
    candidates = {
        str(PurePosixPath(rel).parent) for rel in plan.moves if PurePosixPath(rel).parent.name
    }
    for rel_dir in sorted(candidates, key=lambda p: p.count("/"), reverse=True):
        path = user_root / rel_dir
        while path != user_root and path.is_dir():
            try:
                path.rmdir()
            except OSError:
                break
            path = path.parent


async def _update_artifact_paths(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    user_id: UUID,
    updates: Mapping[str, str],
) -> int:
    """把 ``artifact_version.path_in_workspace`` 的旧值改成新值。

    按 ``(tenant_id, user_id)`` 收口 —— ``path_in_workspace`` 是相对路径,
    不同用户之间必然重名(人人都有 ``报告.docx``),不收口会把别人的行改掉。
    """
    updated = 0
    async with session_factory() as session:
        for old, new in updates.items():
            result = await session.execute(
                sa.text(
                    """
                    UPDATE artifact_version AS av
                       SET path_in_workspace = :new
                      FROM artifact AS a
                     WHERE av.artifact_id = a.id
                       AND a.tenant_id = :tenant_id
                       AND a.user_id = :user_id
                       AND av.path_in_workspace = :old
                    """
                ),
                {"new": new, "old": old, "tenant_id": tenant_id, "user_id": user_id},
            )
            # ``CursorResult.rowcount`` —— ``session.execute`` 的静态返回类型是
            # ``Result``,它上面没有这个属性;走 text() 的 UPDATE 实际拿到的
            # 一定是 ``CursorResult``。报的是「改了几行」,给运维对账用。
            updated += cast("CursorResult[Any]", result).rowcount or 0
        await session.commit()
    return updated


async def collect_attributions(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    user_id: UUID,
) -> Attributions:
    """查出一个用户的全部归属事实。**唯一碰库的读函数。**

    先看这个用户用过几个 agent:只有一个就走捷径(整棵树归它),剩下的
    四张映射都不用查 —— 实测 64 个有会话的用户里 56 个属于这一档。
    """
    async with session_factory() as session:
        names = (
            (
                await session.execute(
                    sa.text(
                        """
                        SELECT DISTINCT agent_name FROM thread_meta
                         WHERE tenant_id = :t AND user_id = :u
                           AND agent_name IS NOT NULL AND agent_name <> ''
                        """
                    ),
                    {"t": tenant_id, "u": user_id},
                )
            )
            .scalars()
            .all()
        )
        if len(names) == 1:
            return Attributions(
                sole_agent_key=sanitize_agent_key(names[0]),
                uploads={},
                artifacts={},
                threads={},
                runs={},
            )

        threads = {
            str(row.thread_id): sanitize_agent_key(row.agent_name)
            for row in await session.execute(
                sa.text(
                    """
                    SELECT thread_id, agent_name FROM thread_meta
                     WHERE tenant_id = :t AND user_id = :u
                       AND agent_name IS NOT NULL AND agent_name <> ''
                    """
                ),
                {"t": tenant_id, "u": user_id},
            )
        }
        # 只取 kind='document' —— image 的 ref 是 ``expert_work://image/…``
        # 对象存储 URI,根本不是工作区路径,混进来会造出一条永远匹配不上的键。
        uploads = {
            row.ref: threads[str(row.thread_id)]
            for row in await session.execute(
                sa.text(
                    """
                    SELECT thread_id, ref FROM user_upload
                     WHERE tenant_id = :t AND user_id = :u AND kind = 'document'
                    """
                ),
                {"t": tenant_id, "u": user_id},
            )
            if str(row.thread_id) in threads
        }
        artifacts = {
            row.path_in_workspace: row.agent_key
            for row in await session.execute(
                sa.text(
                    """
                    SELECT DISTINCT av.path_in_workspace, a.agent_key
                      FROM artifact AS a
                      JOIN artifact_version AS av ON av.artifact_id = a.id
                     WHERE a.tenant_id = :t AND a.user_id = :u
                       AND a.agent_key <> ''
                       AND av.path_in_workspace IS NOT NULL
                       AND av.path_in_workspace <> ''
                    """
                ),
                {"t": tenant_id, "u": user_id},
            )
        }
        runs = {
            str(row.id): threads[str(row.thread_id)]
            for row in await session.execute(
                sa.text(
                    """
                    SELECT r.id, r.thread_id FROM agent_run AS r
                      JOIN thread_meta AS tm ON tm.thread_id = r.thread_id
                     WHERE tm.tenant_id = :t AND tm.user_id = :u
                    """
                ),
                {"t": tenant_id, "u": user_id},
            )
            if str(row.thread_id) in threads
        }

    return Attributions(
        sole_agent_key=None,
        uploads=uploads,
        artifacts=artifacts,
        threads=threads,
        runs=runs,
    )


async def assert_backfill_ran(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """迁移 ``0154`` 必须已经跑过且回填出过东西,否则拒绝开工。

    ``0154`` 回填 ``artifact.agent_key``;它没跑(或跑了但回填链断了)时
    整张表都是空串,``collect_attributions`` 的 artifacts 映射会是空的 ——
    于是**本可归属的产物会被静默扫进 ``shared/``**。没有任何报错,搬完也数得
    平(守恒照样成立),只有事后人工比对才看得出来。所以要在开工前挡住。
    """
    async with session_factory() as session:
        total = await session.scalar(sa.text("SELECT count(*) FROM artifact"))
        attributed = await session.scalar(
            sa.text("SELECT count(*) FROM artifact WHERE agent_key <> ''")
        )
    if total and not attributed:
        msg = (
            f"artifact.agent_key is empty on all {total} rows — migration 0154 has not run "
            "(or its backfill resolved nothing). Run `alembic upgrade head` first: "
            "migrating files now would sweep every attributable artifact into shared/."
        )
        raise RuntimeError(msg)


def _parse_argv(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate one user's workspace to the per-agent layout (B-50). "
            "Dry-run by default; pass --apply to actually move files."
        )
    )
    parser.add_argument("--root", required=True, help="NAS root holding {tenant}/{user} dirs")
    parser.add_argument("--tenant", required=True, help="tenant UUID")
    parser.add_argument("--user", required=True, help="user UUID")
    parser.add_argument("--dsn", default=None, help="database DSN; default = Settings().db_dsn")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually move the files (default: dry-run, prints the plan only)",
    )
    return parser.parse_args(argv)


def _render(plan: MigrationPlan, report: MigrationReport, *, dry_run: bool) -> str:
    lines = [
        f"{'DRY-RUN' if dry_run else 'APPLIED'} tenant={plan.tenant_id} user={plan.user_id}",
        f"  moved     {report.moved}",
        f"  to shared {report.to_shared}",
        f"  untouched {report.untouched}",
        f"  artifact rows updated {report.artifact_rows_updated}",
    ]
    if plan.conflicts:
        lines.append(
            f"  ⚠️ {len(plan.conflicts)} file(s) went to shared/ because the agent dir "
            "already holds a newer copy — review before deleting anything:"
        )
        lines.extend(f"      {p}" for p in plan.conflicts)
    if plan.to_shared:
        lines.append("  files with no inferable owner (→ shared/):")
        lines.extend(f"      {p}" for p in plan.to_shared)
    return "\n".join(lines) + "\n"


async def _main(argv: list[str]) -> int:
    args = _parse_argv(argv)
    from expert_work.persistence import (
        DatabaseConfig,
        create_async_engine_from_config,
        create_async_session_factory,
    )

    dsn = args.dsn
    if dsn is None:
        from control_plane.settings import Settings

        dsn = Settings().db_dsn

    engine = create_async_engine_from_config(DatabaseConfig(dsn=dsn))
    try:
        session_factory = create_async_session_factory(engine)
        await assert_backfill_ran(session_factory)
        attributions = await collect_attributions(
            session_factory, tenant_id=UUID(args.tenant), user_id=UUID(args.user)
        )
        plan = plan_migration(
            args.root, UUID(args.tenant), UUID(args.user), attributions=attributions
        )
        report = apply_migration(
            plan,
            root=args.root,
            dry_run=not args.apply,
            session_factory=session_factory if args.apply else None,
        )
    finally:
        await engine.dispose()

    sys.stdout.write(_render(plan, report, dry_run=not args.apply))
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI entrypoint
    sys.exit(asyncio.run(_main(sys.argv[1:])))
