"""B-175 P3-PR2 —— 重新入库没能套回去的分段修订,满期限删掉。

``knowledge_chunk_revision.orphaned_at``:一次重新入库后,这条修订所属的整块找不到了(人工
修改没能套回去,或停用没能保住 —— ``switch_off_lost``;控制台「未套用的修改」里列着)。
满 ``days`` 天删整段历史 —— 修订正文可能含健康数据,不留到天荒地老;整块还在的修订
(``orphaned_at`` 为空)不按时间删,随文档删除(外键级联,迁移 0176)。与 ``job.py``
其它 pass 同款 ``ctid`` 限批;授权见迁移 0176。
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_DELETE_ORPHANED_SQL = text(
    """
    DELETE FROM knowledge_chunk_revision
    WHERE ctid IN (
        SELECT r.ctid
        FROM knowledge_chunk_revision r
        WHERE r.orphaned_at IS NOT NULL
          AND r.orphaned_at < now() - make_interval(days => :days)
        LIMIT :batch
    )
    """
)


async def delete_orphaned_revisions(
    session_factory: async_sessionmaker[AsyncSession], *, days: int, batch: int
) -> int:
    """Delete up to ``batch`` revision rows orphaned more than ``days`` ago;
    returns how many went."""
    async with session_factory() as session:
        result = await session.execute(_DELETE_ORPHANED_SQL, {"days": days, "batch": batch})
        await session.commit()
    # ``rowcount`` is only typed on ``CursorResult`` (see ``job.py``).
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
